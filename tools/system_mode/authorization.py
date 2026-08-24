"""Create the explicit host-to-installer recovery authorization contract."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import stat
import tempfile
import threading
import time
import uuid
from typing import Any, Callable, Mapping

from tools.system_mode.doctor import AdbClient, ProbeError, validate_report


AUTHORIZATION_SCHEMA_VERSION = 1
AUTHORIZATION_PATH = "/data/local/tmp/kitsune-system-mode-recovery-v1.env"
BACKUP_DIGEST_DOMAIN = b"kitsune-system-mode-backup-directory-v1\0"
AUTHORIZATION_TTL_SECONDS = 5 * 60
REPORT_MAX_AGE_SECONDS = 60 * 60
REPORT_MAX_BYTES = 4 * 1024 * 1024


class HostLease:
    """One-shot host proof reached through this exact live ADB transport.

    The guest fetches a random nonce through an ephemeral ``adb reverse`` TCP
    mapping immediately before consuming its authorization. A copied guest
    authorization has neither the transport mapping nor a second nonce response.
    The verifier runs at handoff time so the external backup and host instance
    identity cannot silently change while the user is reading the warning UI.
    """

    def __init__(
        self,
        authorization_id: str,
        verifier: Callable[[], None],
        *,
        timeout: int = AUTHORIZATION_TTL_SECONDS,
    ) -> None:
        self.authorization_id = str(uuid.UUID(authorization_id))
        if self.authorization_id != authorization_id.lower():
            raise ValueError("host lease authorization ID must be canonical")
        if timeout < 1 or timeout > AUTHORIZATION_TTL_SECONDS:
            raise ValueError("host lease timeout is outside the authorization lifetime")
        self._verifier = verifier
        self._timeout = timeout
        self._nonce = secrets.token_hex(32)
        self.nonce_sha256 = hashlib.sha256(self._nonce.encode("ascii")).hexdigest()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self._listener.settimeout(0.25)
        self.host_port = int(self._listener.getsockname()[1])
        self._stop = threading.Event()
        self._finished = threading.Event()
        self._error: BaseException | None = None
        self._served = False
        self._thread = threading.Thread(
            target=self._serve,
            name="kitsune-system-mode-host-lease",
            daemon=True,
        )

    @property
    def served(self) -> bool:
        return self._served

    def __enter__(self) -> HostLease:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _reply(connection: socket.socket, status: str, body: bytes = b"") -> None:
        response = (
            f"HTTP/1.0 {status}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Content-Type: text/plain\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii") + body
        connection.sendall(response)

    def _serve(self) -> None:
        deadline = time.monotonic() + self._timeout
        accepted = {
            f"GET /{self.authorization_id} HTTP/1.0",
            f"GET /{self.authorization_id} HTTP/1.1",
        }
        try:
            while not self._stop.is_set() and time.monotonic() < deadline:
                try:
                    connection, _ = self._listener.accept()
                except socket.timeout:
                    continue
                with connection:
                    try:
                        connection.settimeout(5)
                        request = bytearray()
                        while b"\r\n\r\n" not in request and len(request) <= 8192:
                            block = connection.recv(2048)
                            if not block:
                                break
                            request.extend(block)
                    except (OSError, socket.timeout):
                        # One incomplete local/device connection is availability
                        # noise, not a reason to destroy the still-unconsumed lease.
                        continue
                    first_line = bytes(request).split(b"\r\n", 1)[0]
                    try:
                        decoded = first_line.decode("ascii")
                    except UnicodeDecodeError:
                        decoded = ""
                    if decoded not in accepted:
                        try:
                            self._reply(connection, "404 Not Found")
                        except OSError:
                            pass
                        continue
                    self._verifier()
                    if self._stop.is_set() or time.monotonic() >= deadline:
                        self._error = TimeoutError(
                            "host lease expired while recovery evidence was revalidated"
                        )
                        return
                    # A matching request consumes the one-shot lease even if its
                    # peer resets while the response is in flight. Retrying after
                    # a possibly partial nonce response would violate one-shot
                    # semantics; the outer authorization flow will fail closed.
                    self._served = True
                    self._reply(connection, "200 OK", self._nonce.encode("ascii"))
                    return
            if not self._stop.is_set():
                self._error = TimeoutError("host lease expired before the installer requested it")
        except BaseException as exc:
            self._error = exc
        finally:
            try:
                self._listener.close()
            except OSError:
                pass
            self._finished.set()

    def wait(self, timeout: int | float | None = None) -> None:
        if not self._finished.wait(timeout):
            raise TimeoutError("timed out waiting for the live installer host lease")
        if self._error is not None:
            raise self._error
        if not self._served:
            raise RuntimeError("host lease ended without serving its one-shot nonce")

    def close(self) -> None:
        self._stop.set()
        try:
            self._listener.close()
        except OSError:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=2)


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value is not permitted: {value}")


def _safe_adapter_component(value: object) -> str:
    component = re.sub(r"[^a-z0-9._-]+", "-", str(value or "unknown").lower())
    return component.strip("-") or "unknown"


def _stat_identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Return metadata that changes for replacement or in-place mutation."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _open_canonical_nofollow(path: Path, *, directory: bool) -> int:
    """Open one canonical absolute path without following any more links."""

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not nofollow or not directory_flag:
        raise RuntimeError("host does not provide the no-follow directory APIs required for recovery")
    if path.anchor != os.sep:
        raise ValueError("host evidence path is not a canonical POSIX path")

    descriptor = os.open(os.sep, os.O_RDONLY | directory_flag | cloexec)
    try:
        components = path.parts[1:]
        if not components:
            if directory:
                return descriptor
            raise ValueError("a regular host file cannot be the filesystem root")
        for index, component in enumerate(components):
            final = index == len(components) - 1
            flags = os.O_RDONLY | nofollow | cloexec
            if not final or directory:
                flags |= directory_flag
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_stable_path(path: Path, *, directory: bool) -> tuple[Path, Path, int, os.stat_result]:
    """Resolve once, open by canonical components, and bind the opened inode."""

    requested = path.expanduser()
    if not requested.is_absolute():
        raise ValueError("host evidence location must be an absolute path")
    before = requested.stat(follow_symlinks=False)
    if stat.S_ISLNK(before.st_mode):
        raise ValueError(f"host evidence location is a symbolic link: {requested}")
    canonical = requested.resolve(strict=True)
    descriptor = _open_canonical_nofollow(canonical, directory=directory)
    try:
        opened = os.fstat(descriptor)
        expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected_kind(opened.st_mode):
            kind = "directory" if directory else "regular file"
            raise ValueError(f"host evidence location is not a {kind}: {requested}")
        if _stat_identity(before) != _stat_identity(opened):
            raise ValueError(f"host evidence path changed while it was being opened: {requested}")
        return requested, canonical, descriptor, opened
    except BaseException:
        os.close(descriptor)
        raise


def _verify_stable_path(
    requested: Path,
    canonical: Path,
    expected: os.stat_result,
    *,
    purpose: str,
) -> None:
    after = requested.stat(follow_symlinks=False)
    if (
        stat.S_ISLNK(after.st_mode)
        or _stat_identity(after) != _stat_identity(expected)
        or requested.resolve(strict=True) != canonical
    ):
        raise ValueError(f"{purpose} path changed while it was being read: {requested}")


def _sha256_descriptor(
    descriptor: int,
    expected: os.stat_result,
    *,
    purpose: str,
    capture: bool = False,
    max_bytes: int | None = None,
) -> tuple[str, bytes | None, os.stat_result]:
    if max_bytes is not None and expected.st_size > max_bytes:
        raise ValueError(f"{purpose} exceeds the {max_bytes}-byte safety limit")
    digest = hashlib.sha256()
    content = bytearray() if capture else None
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        block = os.read(descriptor, 1024 * 1024)
        if not block:
            break
        digest.update(block)
        if content is not None:
            content.extend(block)
            if max_bytes is not None and len(content) > max_bytes:
                raise ValueError(f"{purpose} exceeds the {max_bytes}-byte safety limit")
    after = os.fstat(descriptor)
    if _stat_identity(expected) != _stat_identity(after):
        raise ValueError(f"{purpose} changed while it was being read")
    return digest.hexdigest(), bytes(content) if content is not None else None, after


def stable_regular_file(
    path: Path,
    *,
    purpose: str,
    capture: bool = False,
    max_bytes: int | None = None,
) -> tuple[dict[str, Any], bytes | None]:
    """Read/hash a regular host file through one no-follow stable descriptor."""

    requested, canonical, descriptor, before = _open_stable_path(path, directory=False)
    try:
        digest, content, after = _sha256_descriptor(
            descriptor,
            before,
            purpose=purpose,
            capture=capture,
            max_bytes=max_bytes,
        )
    finally:
        os.close(descriptor)
    _verify_stable_path(requested, canonical, after, purpose=purpose)
    return (
        {
            "path": str(canonical),
            "sha256": digest,
            "device": after.st_dev,
            "inode": after.st_ino,
            "size": after.st_size,
            "mode": f"{stat.S_IMODE(after.st_mode):04o}",
            "uid": after.st_uid,
            "gid": after.st_gid,
            "mtime_ns": after.st_mtime_ns,
            "ctime_ns": after.st_ctime_ns,
        },
        content,
    )


def _sha256_file(path: Path) -> tuple[str, os.stat_result]:
    """Hash one stable regular file and reject pathname or inode mutation."""

    requested, canonical, descriptor, before = _open_stable_path(path, directory=False)
    try:
        digest, _, after = _sha256_descriptor(
            descriptor,
            before,
            purpose=f"backup file {canonical}",
        )
    finally:
        os.close(descriptor)
    _verify_stable_path(requested, canonical, after, purpose="backup file")
    return digest, after


def _digest_field(digest: Any, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, "big"))
    digest.update(value)


def digest_backup_path(path: Path) -> str:
    """Return the v1 digest for an immutable external recovery file or tree.

    A file uses its ordinary SHA-256. A directory uses a domain-separated,
    sorted inventory of relative names, modes, ownership, sizes, and file
    digests. Symbolic links and special nodes are rejected because their
    restore semantics are host-dependent and cannot prove an exact recovery
    artifact.
    """

    requested = path.expanduser()
    if not requested.is_absolute():
        raise ValueError("backup location must be an absolute host path")
    requested_metadata = requested.stat(follow_symlinks=False)
    if stat.S_ISLNK(requested_metadata.st_mode):
        raise ValueError(f"backup location is a symbolic link: {requested}")
    if stat.S_ISREG(requested_metadata.st_mode):
        return _sha256_file(requested)[0]
    if not stat.S_ISDIR(requested_metadata.st_mode):
        raise ValueError("backup location must be a regular file or directory")

    requested, candidate, root_descriptor, root_metadata = _open_stable_path(
        requested,
        directory=True,
    )

    digest = hashlib.sha256(BACKUP_DIGEST_DOMAIN)

    def inventory(directory: int, relative: Path) -> None:
        before = os.fstat(directory)
        if not stat.S_ISDIR(before.st_mode):
            raise ValueError(f"backup directory changed during hashing: {relative}")
        relative_bytes = os.fsencode(str(relative))
        _digest_field(digest, b"directory")
        _digest_field(digest, relative_bytes)
        _digest_field(digest, f"{stat.S_IMODE(before.st_mode):04o}".encode("ascii"))
        _digest_field(digest, str(before.st_uid).encode("ascii"))
        _digest_field(digest, str(before.st_gid).encode("ascii"))

        names = sorted(os.listdir(directory), key=os.fsencode)
        names_before = [os.fsencode(name) for name in names]
        for name in names:
            child_relative = relative / name
            child_metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISLNK(child_metadata.st_mode):
                raise ValueError(f"backup contains a symbolic link: {child_relative}")
            if stat.S_ISDIR(child_metadata.st_mode):
                child_descriptor = os.open(
                    name,
                    os.O_RDONLY
                    | os.O_DIRECTORY
                    | os.O_NOFOLLOW
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=directory,
                )
                try:
                    opened = os.fstat(child_descriptor)
                    if _stat_identity(opened) != _stat_identity(child_metadata):
                        raise ValueError(
                            f"backup directory changed while it was opened: {child_relative}"
                        )
                    inventory(child_descriptor, child_relative)
                finally:
                    os.close(child_descriptor)
                final_child = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if _stat_identity(final_child) != _stat_identity(child_metadata):
                    raise ValueError(
                        f"backup directory changed during hashing: {child_relative}"
                    )
                continue
            if not stat.S_ISREG(child_metadata.st_mode):
                raise ValueError(f"backup contains a special node: {child_relative}")
            child_descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                dir_fd=directory,
            )
            try:
                opened = os.fstat(child_descriptor)
                if _stat_identity(opened) != _stat_identity(child_metadata):
                    raise ValueError(
                        f"backup file changed while it was opened: {child_relative}"
                    )
                file_digest, _, stable_metadata = _sha256_descriptor(
                    child_descriptor,
                    opened,
                    purpose=f"backup file {child_relative}",
                )
            finally:
                os.close(child_descriptor)
            final_child = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if _stat_identity(final_child) != _stat_identity(stable_metadata):
                raise ValueError(f"backup file changed during hashing: {child_relative}")
            _digest_field(digest, b"file")
            _digest_field(digest, os.fsencode(str(child_relative)))
            _digest_field(
                digest,
                f"{stat.S_IMODE(stable_metadata.st_mode):04o}".encode("ascii"),
            )
            _digest_field(digest, str(stable_metadata.st_uid).encode("ascii"))
            _digest_field(digest, str(stable_metadata.st_gid).encode("ascii"))
            _digest_field(digest, str(stable_metadata.st_size).encode("ascii"))
            _digest_field(digest, file_digest.encode("ascii"))

        names_after = sorted((os.fsencode(name) for name in os.listdir(directory)))
        after = os.fstat(directory)
        if names_before != names_after or _stat_identity(before) != _stat_identity(after):
            raise ValueError(f"backup directory changed during hashing: {relative}")

    try:
        inventory(root_descriptor, Path("."))
        final_root = os.fstat(root_descriptor)
        if _stat_identity(root_metadata) != _stat_identity(final_root):
            raise ValueError("backup root directory changed during hashing")
    finally:
        os.close(root_descriptor)
    _verify_stable_path(requested, candidate, final_root, purpose="backup directory")
    return digest.hexdigest()


def verify_report_backup(report: Mapping[str, Any]) -> str:
    recovery = report["recovery"]
    location = Path(str(recovery["backup_location"]))
    expected = str(recovery["backup_digest"]).lower()
    actual = digest_backup_path(location)
    if actual != expected:
        raise ValueError(
            "doctor report backup digest does not match the current external recovery artifact"
        )
    return actual


def authorization_target_contract(report: Mapping[str, Any]) -> dict[str, Any]:
    """Return the mutation-sensitive target state that one authorization binds."""

    staging = report["staging"]
    return {
        "source": {
            "serial_sha256": report["source"]["serial_sha256"],
            "probe_sha256": report["source"]["probe_sha256"],
        },
        "device": report["device"],
        "bootstrap": report["bootstrap"],
        "mounts": report["mounts"],
        "layout": report["layout"],
        "init": report["init"],
        "selinux": report["selinux"],
        "staging": {
            "path": staging["path"],
            "required_bytes": staging["required_bytes"],
            "sufficient": staging["sufficient"],
        },
        "existing_install": report["existing_install"],
    }


def authorization_target_digest(report: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        authorization_target_contract(report),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _generated_epoch(report: Mapping[str, Any]) -> int:
    generated = str(report.get("generated_at", ""))
    try:
        parsed = dt.datetime.fromisoformat(generated.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("doctor report has an invalid generation timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("doctor report generation timestamp must include a timezone")
    return int(parsed.timestamp())


def _validate_restore_command(value: object) -> str:
    command = str(value or "").strip()
    try:
        words = shlex.split(command)
    except ValueError as exc:
        raise ValueError("recovery restore command is not valid shell text") from exc
    if not words:
        raise ValueError("recovery restore command is empty")
    normalized = [Path(word).name.lower() for word in words]
    trivial = {
        ("true",),
        ("false",),
        (":",),
        ("exit", "0"),
        ("sh", "-c", "true"),
        ("bash", "-c", "true"),
        ("zsh", "-c", "true"),
        ("env", "true"),
    }
    if tuple(normalized) in trivial:
        raise ValueError("recovery restore command is a no-op, not recovery evidence")
    if not Path(words[0]).is_absolute():
        raise ValueError("recovery restore command must use an absolute executable path")
    return command


def validate_fresh_target(stored: Mapping[str, Any], fresh: Mapping[str, Any]) -> str:
    """Require an immediate read-only re-probe to match every critical field."""

    validate_report(stored)
    validate_report(fresh)
    if fresh["assessment"].get("verdict") != "supported":
        raise ValueError("live target is no longer supported")
    expected = authorization_target_digest(stored)
    actual = authorization_target_digest(fresh)
    if actual != expected:
        raise ValueError("doctor report is stale: live boot-critical target state changed")
    if fresh["source"].get("repository_dirty") is not False:
        raise ValueError("live authorization source tree is dirty")
    if fresh["source"].get("repository_commit") != stored["source"].get("repository_commit"):
        raise ValueError("doctor report source commit is stale")
    return actual


def build_authorization(
    report: Mapping[str, Any],
    report_bytes: bytes,
    artifact_sha256: str,
    *,
    lease_port: int,
    lease_nonce_sha256: str,
    issued_at: int | None = None,
    authorization_id: str | None = None,
) -> bytes:
    """Return a non-executable, line-oriented authorization for one exact target."""

    validate_report(report)
    if type(report.get("schema_version")) is not int or report["schema_version"] != 1:
        raise ValueError("doctor report schema version must be integer 1")
    try:
        serialized_report = json.loads(
            report_bytes.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("authorization report bytes are not valid UTF-8 JSON") from exc
    try:
        serialized_identity = json.dumps(
            serialized_report,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        report_identity = json.dumps(
            report,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("authorization report contains unsupported JSON values") from exc
    if serialized_identity != report_identity:
        raise ValueError("authorization report bytes do not match the validated report object")
    source = report["source"]
    source_commit = str(source.get("repository_commit", ""))
    if source.get("repository_dirty") is not False:
        raise ValueError("doctor report was collected from a dirty source tree")
    if not re.fullmatch(r"[a-f0-9]{40}", source_commit):
        raise ValueError("doctor report does not contain an exact source commit")
    if not re.fullmatch(r"[a-f0-9]{64}", artifact_sha256):
        raise ValueError("authorization requires an exact artifact SHA-256")
    if not 1 <= lease_port <= 65535:
        raise ValueError("authorization requires a valid host lease port")
    if not re.fullmatch(r"[a-f0-9]{64}", lease_nonce_sha256):
        raise ValueError("authorization requires an exact host lease nonce digest")
    recovery = report["recovery"]
    persistence = report["persistence"]
    init = report["init"]
    if recovery.get("verified") is not True:
        raise ValueError("doctor report does not contain verified external recovery")
    if persistence.get("proven") is not True:
        raise ValueError("doctor report does not contain persistent-write evidence")
    if init.get("import_proof") != "proven" or not init.get("selected_directory"):
        raise ValueError("doctor report does not prove an init import directory")
    if init["selected_directory"] not in {"/system/etc/init", "/system/etc/init/hw"}:
        raise ValueError("doctor report selected an init subcontext not qualified by PR7")
    if report["assessment"].get("verdict") != "supported":
        raise ValueError("doctor report is not supported")
    now = int(time.time()) if issued_at is None else issued_at
    generated = _generated_epoch(report)
    if generated > now + 30 or now - generated > REPORT_MAX_AGE_SECONDS:
        raise ValueError("doctor report is stale or has a future timestamp")
    auth_id = authorization_id or str(uuid.uuid4())
    try:
        parsed_auth_id = uuid.UUID(auth_id)
    except ValueError as exc:
        raise ValueError("authorization ID must be a UUID") from exc
    if str(parsed_auth_id) != auth_id.lower():
        raise ValueError("authorization ID must use canonical UUID text")
    restore_command = _validate_restore_command(recovery.get("restore_command"))

    device = report["device"]
    selinux = report["selinux"]
    adapter_id = _safe_adapter_component(recovery.get("adapter_id"))
    qualification_sha256 = str(recovery.get("qualification_sha256") or "")
    instance_identity_sha256 = str(recovery.get("instance_identity_sha256") or "")
    if not re.fullmatch(r"[a-f0-9]{64}", qualification_sha256) or not re.fullmatch(
        r"[a-f0-9]{64}", instance_identity_sha256
    ):
        raise ValueError("authorization requires machine qualification and instance identity digests")
    fields = (
        ("SCHEMA_VERSION", str(AUTHORIZATION_SCHEMA_VERSION)),
        ("REPORT_SHA256", hashlib.sha256(report_bytes).hexdigest()),
        ("AUTHORIZATION_ID", auth_id),
        ("ISSUED_AT_EPOCH", str(now)),
        ("EXPIRES_AT_EPOCH", str(now + AUTHORIZATION_TTL_SECONDS)),
        ("TARGET_CONTRACT_SHA256", authorization_target_digest(report)),
        ("BOOT_ID_SHA256", str(device["boot_id_sha256"])),
        ("PROBE_SHA256", str(source["probe_sha256"])),
        ("SOURCE_COMMIT", source_commit),
        ("ARTIFACT_SHA256", artifact_sha256),
        ("QUALIFICATION_SHA256", qualification_sha256),
        ("INSTANCE_IDENTITY_SHA256", instance_identity_sha256),
        ("LEASE_PORT", str(lease_port)),
        ("LEASE_NONCE_SHA256", lease_nonce_sha256),
        ("SERIAL_SHA256", str(report["source"]["serial_sha256"])),
        ("FINGERPRINT_SHA256", str(device["fingerprint_sha256"])),
        ("TARGET_API", str(device["api"])),
        ("TARGET_ABIS_B64", _b64(",".join(device["abis"]))),
        ("ADAPTER_ID_B64", _b64(adapter_id)),
        ("INIT_DIRECTORY_B64", _b64(str(init["selected_directory"]))),
        ("SELINUX_STRATEGY_B64", _b64(str(selinux["strategy"]))),
        ("SNAPSHOT_ID_B64", _b64(str(recovery["snapshot_id"]))),
        ("BACKUP_LOCATION_B64", _b64(str(recovery["backup_location"]))),
        ("BACKUP_SHA256", str(recovery["backup_digest"])),
        ("RESTORE_COMMAND_B64", _b64(restore_command)),
    )
    return ("\n".join(f"{key}={value}" for key, value in fields) + "\n").encode("ascii")


def load_authorization_report(path: Path) -> tuple[dict[str, Any], bytes]:
    _, captured = stable_regular_file(
        path,
        purpose="doctor authorization report",
        capture=True,
        max_bytes=REPORT_MAX_BYTES,
    )
    if captured is None:
        raise ValueError("doctor authorization report could not be read")
    raw = captured
    try:
        report = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("doctor authorization report is not valid UTF-8 JSON") from exc
    if not isinstance(report, dict):
        raise ValueError("doctor report root must be an object")
    return report, raw


def stage_authorization(client: AdbClient, authorization: bytes) -> str:
    """Stage and byte-verify an authorization in shell-owned temporary storage."""

    expected = hashlib.sha256(authorization).hexdigest()
    staged_path = f"{AUTHORIZATION_PATH}.new-{uuid.uuid4()}"
    quoted_staged = shlex.quote(staged_path)
    quoted_destination = shlex.quote(AUTHORIZATION_PATH)
    with tempfile.NamedTemporaryFile(prefix="kitsune-system-mode-auth-", suffix=".env") as temp:
        temp.write(authorization)
        temp.flush()
        try:
            client.push(temp.name, staged_path)
            result = client.shell(f"chmod 0600 {quoted_staged} && sha256sum {quoted_staged}")
            if result.returncode != 0:
                raise ProbeError(result.stderr or "could not verify staged recovery authorization")
            actual = result.stdout.split()[0].lower() if result.stdout.split() else ""
            if actual != expected:
                raise ProbeError("staged recovery authorization digest mismatch")
            result = client.shell(
                f"rm -f {quoted_destination} && mv {quoted_staged} {quoted_destination} && "
                f"chmod 0600 {quoted_destination} && sha256sum {quoted_destination}"
            )
            if result.returncode != 0:
                raise ProbeError(result.stderr or "could not publish recovery authorization")
            published = result.stdout.split()[0].lower() if result.stdout.split() else ""
            if published != expected:
                raise ProbeError("published recovery authorization digest mismatch")
        finally:
            client.shell(f"rm -f {quoted_staged}")
    return expected


def remove_authorization(client: AdbClient) -> None:
    quoted = shlex.quote(AUTHORIZATION_PATH)
    result = client.shell(f"rm -f {quoted} && test ! -e {quoted} && test ! -L {quoted}")
    if result.returncode != 0:
        raise ProbeError(result.stderr or "could not remove the one-shot recovery authorization")


def wait_authorization_consumed(client: AdbClient, *, timeout: int = 30) -> None:
    deadline = time.monotonic() + timeout
    quoted = shlex.quote(AUTHORIZATION_PATH)
    while time.monotonic() < deadline:
        result = client.shell(f"test ! -e {quoted} && test ! -L {quoted}")
        if result.returncode == 0:
            return
        time.sleep(0.25)
    raise ProbeError("installer received its host lease but did not consume the authorization")
