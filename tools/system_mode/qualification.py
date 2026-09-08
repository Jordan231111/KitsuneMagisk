"""Machine-observed qualification for one exact writable System Mode target."""

from __future__ import annotations

import datetime as dt
import base64
from contextlib import closing
import hashlib
import hmac
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping
import uuid

from tools.system_mode.adapters import built_in_descriptors
from tools.system_mode.doctor import (
    AdbClient,
    ProbeError,
    QualificationEvidence,
    classify_report,
    collect_report,
)
from tools.system_mode.schema_validation import validate_schema_instance


QUALIFICATION_SCHEMA_VERSION = 1
QUALIFICATION_PROPERTY = "kitsune.system_mode.qualify"
MAX_IDENTITY_BYTES = 16 * 1024 * 1024
MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_DATABASE_BYTES = 16 * 1024 * 1024
QUALIFICATION_KEY_BYTES = 32
QUALIFICATION_KEY_ENV = "KITSUNE_SYSTEM_MODE_QUALIFICATION_KEY"
SEAL_ALGORITHM = "HMAC-SHA256"
PROCESS_GROUP_KILL_WAIT_SECONDS = 5
_SCHEMA = Path(__file__).with_name("schemas") / "qualification-v1.schema.json"
_EVIDENCE_ONLY_REASONS = {
    "INIT_IMPORT_UNPROVEN",
    "NO_RECOVERY_PATH",
    "PERSISTENT_WRITABILITY_UNPROVEN",
}

# Exact persistent boot paths that the PR7 transaction is permitted to create,
# replace, or remove. Directories are inventoried recursively. Runtime tmpfs
# paths and volatile logs are intentionally excluded: an external restore must
# reproduce durable boot state, not a later boot's incidental log timestamps.
_BOOT_CRITICAL_STATIC_PATHS = (
    "/system/etc/init/magisk",
    "/system/etc/init/magisk.rc",
    "/system/etc/init/bootanim.rc",
    "/system/etc/init/bootanim.rc.gz",
    "/system/addon.d/99-magisk.sh",
    "/system/addon.d/magisk",
    "/data/adb/kitsune/system-mode",
    "/data/adb/.kitsune-system-mode-setup-v1.env",
    "/data/adb/.kitsune-system-mode-rollback-v1.env",
    "/data/adb/.kitsune-system-mode-prior-v1.env",
    "/data/adb/magisk",
    "/data/adb/magisk.db",
    "/data/adb/magisk.db-wal",
    "/data/adb/magisk.db-shm",
    "/data/adb/modules",
    "/data/adb/modules_update",
    "/data/adb/post-fs-data.d",
    "/data/adb/service.d",
    "/data/adb/sepolicy.rule",
    "/sepolicy",
    "/sepolicy.gz",
    "/sepolicy_debug",
    "/sepolicy_debug.gz",
    "/vendor/etc/selinux/precompiled_sepolicy",
    "/vendor/etc/selinux/precompiled_sepolicy.gz",
    "/odm/etc/selinux/precompiled_sepolicy",
    "/odm/etc/selinux/precompiled_sepolicy.gz",
    "/system/etc/selinux/precompiled_sepolicy",
    "/system/etc/selinux/precompiled_sepolicy.gz",
    "/system_root/sepolicy",
    "/system_root/sepolicy.gz",
    "/system_root/sepolicy_debug",
    "/system_root/sepolicy_debug.gz",
    "/system_root/sepolicy.unlocked",
    "/system_root/sepolicy.unlocked.gz",
)


class IndeterminateLifecycleError(ProbeError):
    """A host lifecycle process group could not be proven stopped."""


def _wait_process_group_gone(process_group: int) -> bool:
    deadline = time.monotonic() + PROCESS_GROUP_KILL_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


def _sha256(value: bytes | str) -> str:
    data = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    rendered = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return f"{rendered}\n".encode("ascii")


def _default_seal_key_path() -> Path:
    configured = os.environ.get(QUALIFICATION_KEY_ENV)
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "share" / "kitsune-magisk" / "qualification-v1.key"


def _reject_symlink_components(path: Path) -> None:
    """Reject a key path whose existing absolute prefix contains a symlink."""

    if not path.is_absolute():
        raise ValueError("qualification seal key path must be absolute")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"qualification seal key path contains a symbolic link: {current}")


def _validate_seal_key_ancestors(path: Path) -> None:
    parent = path.parent
    metadata = parent.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise ValueError("qualification seal key parent must be owner-only")
    current = parent
    while current != Path(current.anchor):
        ancestor = current.stat(follow_symlinks=False)
        if stat.S_IMODE(ancestor.st_mode) & 0o022:
            raise ValueError(
                f"qualification seal key ancestor is group/world writable: {current}"
            )
        current = current.parent


def _load_seal_key(path: Path | None, *, create: bool) -> tuple[Path, bytes]:
    """Load the local trust anchor, creating it once with mode 0600 if requested."""

    from tools.system_mode.authorization import stable_regular_file

    requested = (path or _default_seal_key_path()).expanduser()
    if not requested.is_absolute():
        raise ValueError("qualification seal key path must be absolute")
    requested = Path(os.path.normpath(str(requested)))
    _reject_symlink_components(requested)
    if create and not requested.exists():
        parent = requested.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _reject_symlink_components(parent)
        _validate_seal_key_ancestors(requested)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(requested, flags, 0o600)
        try:
            key = os.urandom(QUALIFICATION_KEY_BYTES)
            written = os.write(descriptor, key)
            if written != len(key):
                raise OSError("short qualification seal key write")
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            requested.unlink(missing_ok=True)
            raise
        else:
            os.close(descriptor)
            parent_descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(parent_descriptor)
            finally:
                os.close(parent_descriptor)
    _reject_symlink_components(requested)
    _validate_seal_key_ancestors(requested)
    identity, captured = stable_regular_file(
        requested,
        purpose="qualification seal key",
        capture=True,
        max_bytes=QUALIFICATION_KEY_BYTES,
    )
    if captured is None or len(captured) != QUALIFICATION_KEY_BYTES:
        raise ValueError("qualification seal key must contain exactly 32 bytes")
    mode = int(str(identity["mode"]), 8)
    if mode & 0o077:
        raise ValueError("qualification seal key must not be group/world accessible")
    return Path(str(identity["path"])), captured


def _unsigned_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "seal"}


def _seal_record(record: dict[str, Any], seal_key_path: Path | None) -> None:
    _, key = _load_seal_key(seal_key_path, create=True)
    record["seal"] = {
        "algorithm": SEAL_ALGORITHM,
        "key_id": hashlib.sha256(key).hexdigest(),
        "value": hmac.new(key, _canonical_bytes(_unsigned_record(record)), hashlib.sha256).hexdigest(),
    }


def _verify_record_seal(record: Mapping[str, Any], seal_key_path: Path | None) -> None:
    _, key = _load_seal_key(seal_key_path, create=False)
    seal = record["seal"]
    key_id = hashlib.sha256(key).hexdigest()
    if not hmac.compare_digest(str(seal["key_id"]), key_id):
        raise ValueError("qualification record was not issued by the trusted host key")
    expected = hmac.new(
        key,
        _canonical_bytes(_unsigned_record(record)),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(str(seal["value"]), expected):
        raise ValueError("qualification record seal is invalid")


def stable_target_contract(report: Mapping[str, Any]) -> dict[str, Any]:
    """Return target state that must survive the external restore unchanged."""

    device = dict(report["device"])
    device.pop("boot_id_sha256", None)
    staging = report["staging"]
    return {
        "source": {
            "serial_sha256": report["source"]["serial_sha256"],
            "probe_sha256": report["source"]["probe_sha256"],
        },
        "device": device,
        "bootstrap": report["bootstrap"],
        "mounts": report["mounts"],
        "layout": report["layout"],
        "init_candidates": report["init"]["candidate_directories"],
        "selinux": report["selinux"],
        # Free bytes fluctuate as Android rotates logs and app caches. Bind the
        # supported-space verdict and contract, not an incidental byte count.
        "staging": {
            "path": staging["path"],
            "required_bytes": staging["required_bytes"],
            "sufficient": staging["sufficient"],
        },
        "existing_install": report["existing_install"],
    }


def stable_target_digest(report: Mapping[str, Any]) -> str:
    return _sha256(_canonical_bytes(stable_target_contract(report)))


def instance_identity(path: Path) -> dict[str, Any]:
    """Bind qualification to one stable host-owned instance metadata file."""

    from tools.system_mode.authorization import stable_regular_file

    identity, _ = stable_regular_file(
        path,
        purpose="host instance identity",
        max_bytes=MAX_IDENTITY_BYTES,
    )
    if identity["size"] < 1:
        raise ValueError("instance identity file must not be empty")
    return identity


def verify_instance_identity(expected: Mapping[str, Any]) -> None:
    actual = instance_identity(Path(str(expected["path"])))
    if actual != dict(expected):
        raise ValueError("host instance identity changed after qualification")


def _checked_command(
    value: str,
    purpose: str,
    *,
    backup_placeholder: bool = False,
) -> dict[str, Any]:
    try:
        command = shlex.split(value)
    except ValueError as exc:
        raise ValueError(f"{purpose} command is invalid: {exc}") from exc
    if not command:
        raise ValueError(f"{purpose} command is empty")
    normalized = tuple(Path(item).name.lower() for item in command)
    if normalized in {("true",), ("false",), (":",), ("exit", "0")}:
        raise ValueError(f"{purpose} command is a no-op")
    executable = Path(command[0]).expanduser()
    if not executable.is_absolute():
        raise ValueError(f"{purpose} command must use an absolute executable path")
    from tools.system_mode.authorization import stable_regular_file

    executable_identity, executable_bytes = stable_regular_file(
        executable,
        purpose=f"{purpose} executable",
        capture=True,
        max_bytes=MAX_IDENTITY_BYTES,
    )
    if int(str(executable_identity["mode"]), 8) & 0o111 == 0:
        raise ValueError(f"{purpose} executable has no execute permission")
    if executable_bytes is None or not executable_bytes.startswith(b"#!/bin/sh\n"):
        raise ValueError(f"{purpose} wrapper must be an exact #!/bin/sh script")
    interpreter_identity, _ = stable_regular_file(
        Path("/bin/sh").resolve(strict=True),
        purpose=f"{purpose} interpreter",
        max_bytes=MAX_IDENTITY_BYTES,
    )
    argv = [str(executable_identity["path"]), *command[1:]]
    if any("\x00" in argument or "\n" in argument or "\r" in argument for argument in argv):
        raise ValueError(f"{purpose} command arguments must be single-line text")
    placeholder_count = argv[1:].count("{backup}")
    embedded_placeholder = any(
        "{backup}" in argument and argument != "{backup}" for argument in argv[1:]
    )
    if backup_placeholder and (placeholder_count != 1 or embedded_placeholder):
        raise ValueError(
            f"{purpose} command must contain one standalone {{backup}} argument"
        )
    if not backup_placeholder and (placeholder_count or embedded_placeholder):
        raise ValueError(f"{purpose} command must not contain a backup placeholder")
    return {
        "argv": argv,
        "executable": executable_identity,
        "interpreter": interpreter_identity,
    }


def _verify_command_identity(expected: Mapping[str, Any], purpose: str) -> None:
    argv = expected.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
        raise ValueError(f"{purpose} command identity has invalid argv")
    actual = _checked_command(
        shlex.join(argv),
        purpose,
        backup_placeholder=purpose in {"backup", "restore"},
    )
    if actual != dict(expected):
        raise ValueError(f"{purpose} executable changed after qualification")


def _materialize_command(
    command_identity: Mapping[str, Any],
    backup_location: Path | None = None,
) -> list[str]:
    argv = list(command_identity["argv"])
    if backup_location is None:
        if any("{backup}" in argument for argument in argv):
            raise ValueError("lifecycle command requires an exact backup location")
        return argv
    if argv[1:].count("{backup}") != 1:
        raise ValueError("backup lifecycle command is not an exact one-path template")
    return [str(backup_location) if argument == "{backup}" else argument for argument in argv]


def _run_host(
    command_identity: Mapping[str, Any],
    purpose: str,
    timeout: int,
    *,
    backup_location: Path | None = None,
) -> None:
    """Execute the exact opened wrapper inode, not a pathname reopened later."""

    from tools.system_mode.authorization import (
        _open_stable_path,
        _sha256_descriptor,
        _verify_stable_path,
    )

    identity = dict(command_identity["executable"])
    interpreter_identity = dict(command_identity["interpreter"])
    from tools.system_mode.authorization import stable_regular_file

    current_interpreter, _ = stable_regular_file(
        Path(str(interpreter_identity["path"])),
        purpose=f"{purpose} interpreter",
        max_bytes=MAX_IDENTITY_BYTES,
    )
    if current_interpreter != interpreter_identity:
        raise ValueError(f"{purpose} interpreter changed after qualification")
    requested, canonical, descriptor, before = _open_stable_path(
        Path(str(identity["path"])),
        directory=False,
    )
    try:
        digest, _, opened = _sha256_descriptor(
            descriptor,
            before,
            purpose=f"{purpose} executable",
        )
        actual = {
            "path": str(canonical),
            "sha256": digest,
            "device": opened.st_dev,
            "inode": opened.st_ino,
            "size": opened.st_size,
            "mode": f"{stat.S_IMODE(opened.st_mode):04o}",
            "uid": opened.st_uid,
            "gid": opened.st_gid,
            "mtime_ns": opened.st_mtime_ns,
            "ctime_ns": opened.st_ctime_ns,
        }
        if actual != identity:
            raise ValueError(f"{purpose} executable changed after qualification")
        command = _materialize_command(command_identity, backup_location)
        os.lseek(descriptor, 0, os.SEEK_SET)
        fd_path = f"/dev/fd/{descriptor}"
        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                [str(interpreter_identity["path"]), fd_path, *command[1:]],
                executable=str(interpreter_identity["path"]),
                pass_fds=(descriptor,),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                env={
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                },
            )
            stdout, stderr = process.communicate(timeout=timeout)
            result = subprocess.CompletedProcess(
                command,
                process.returncode,
                stdout,
                stderr,
            )
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                pass
            else:
                # A lifecycle wrapper is a synchronous transaction boundary.
                # Same-session descendants after the wrapper exits make the
                # point-in-time backup/restore result unknowable.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                if not _wait_process_group_gone(process.pid):
                    raise IndeterminateLifecycleError(
                        f"{purpose} command left a process group that could not be proven stopped"
                    )
                raise ProbeError(
                    f"{purpose} command exited with live process-group descendants"
                )
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                # The direct shell can exit and close its pipes while a child
                # ignores SIGTERM. Always kill the session once the grace
                # interval ends; a successful communicate is not group proof.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.communicate(timeout=PROCESS_GROUP_KILL_WAIT_SECONDS)
                except subprocess.TimeoutExpired:
                    pass
                if not _wait_process_group_gone(process.pid):
                    raise IndeterminateLifecycleError(
                        f"{purpose} timed out and its process group could not be proven stopped"
                    ) from exc
            raise ProbeError(
                f"{purpose} command timed out after its entire process group was stopped"
            ) from exc
        except OSError as exc:
            raise ProbeError(f"{purpose} command failed: {exc}") from exc
        _, _, after = _sha256_descriptor(
            descriptor,
            opened,
            purpose=f"{purpose} executable",
        )
        _verify_stable_path(
            requested,
            canonical,
            after,
            purpose=f"{purpose} executable",
        )
        final_interpreter, _ = stable_regular_file(
            Path(str(interpreter_identity["path"])),
            purpose=f"{purpose} interpreter",
            max_bytes=MAX_IDENTITY_BYTES,
        )
        if final_interpreter != interpreter_identity:
            raise ValueError(f"{purpose} interpreter changed after qualification")
    finally:
        os.close(descriptor)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        suffix = f": {detail[:500]}" if detail else ""
        raise ProbeError(f"{purpose} command exited with {result.returncode}{suffix}")


def _root_checked(client: AdbClient, command: str, purpose: str, timeout: int = 30) -> str:
    result = client.shell(command, root=True, timeout=timeout)
    if result.returncode != 0:
        raise ProbeError(result.stderr or f"{purpose} failed")
    return result.stdout.strip()


def _remote_file_evidence(client: AdbClient, path: str) -> dict[str, Any]:
    quoted = shlex.quote(path)
    output = _root_checked(
        client,
        f"test -f {quoted} && test ! -L {quoted} && "
        f"sha256sum {quoted} && stat -c '%s\t%a\t%u\t%g' {quoted} && "
        f"(ls -Zd {quoted} 2>/dev/null || true)",
        f"remote file evidence for {path}",
    )
    lines = output.splitlines()
    if len(lines) not in {2, 3}:
        raise ProbeError("remote file evidence is incomplete")
    digest = lines[0].split()[0].lower() if lines[0].split() else ""
    fields = lines[1].split("\t")
    if (
        len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or len(fields) != 4
    ):
        raise ProbeError("remote file evidence is invalid")
    try:
        size, mode, uid, gid = (
            int(field, 8 if index == 1 else 10)
            for index, field in enumerate(fields)
        )
    except ValueError as exc:
        raise ProbeError("remote file metadata is invalid") from exc
    if min(size, uid, gid) < 0 or mode < 0 or mode > 0o7777:
        raise ProbeError("remote file metadata is outside its valid range")
    context = lines[2].split()[0] if len(lines) == 3 and lines[2].split() else None
    if context is not None and ":" not in context:
        context = None
    return {
        "path": path,
        "sha256": digest,
        "size": size,
        "mode": f"{mode:04o}",
        "uid": uid,
        "gid": gid,
        "selinux_context": context,
    }


def _remote_sha256(client: AdbClient, path: str) -> str:
    return str(_remote_file_evidence(client, path)["sha256"])


def _boot_critical_roots(selected_init: str) -> tuple[str, ...]:
    dynamic = (
        f"{selected_init}/magisk.rc",
        f"{selected_init}/00-kitsune-magisk-rescue.rc",
        f"{selected_init}/.kitsune-system-mode-rescue",
    )
    return tuple(sorted(set((*_BOOT_CRITICAL_STATIC_PATHS, *dynamic))))


def _remote_node_evidence(client: AdbClient, path: str) -> dict[str, Any]:
    quoted = shlex.quote(path)
    output = _root_checked(
        client,
        "set -o pipefail && "
        f"if [ -L {quoted} ]; then echo symlink; "
        f"elif [ -f {quoted} ]; then echo file; sha256sum {quoted}; "
        f"elif [ -d {quoted} ]; then echo directory; "
        f"elif [ -e {quoted} ]; then echo special; "
        "else echo absent; exit 0; fi; "
        f"stat -c '%s\t%a\t%u\t%g\t%Y' {quoted}; "
        f"(ls -Zd {quoted} 2>/dev/null || true)",
        "boot-critical inventory",
    )
    lines = output.splitlines()
    if not lines:
        raise ProbeError("boot-critical inventory returned no node type")
    kind = lines[0]
    if kind == "absent":
        if len(lines) != 1:
            raise ProbeError("absent boot-critical node returned metadata")
        return {
            "path": path,
            "kind": kind,
            "sha256": None,
            "size": None,
            "mode": None,
            "uid": None,
            "gid": None,
            "mtime_epoch": None,
            "selinux_context": None,
        }
    if kind in {"symlink", "special"}:
        raise ProbeError(f"boot-critical path is an unsupported {kind}: {path}")
    if kind not in {"file", "directory"}:
        raise ProbeError("boot-critical inventory returned an invalid node type")
    digest_index = 1 if kind == "file" else None
    metadata_index = 2 if kind == "file" else 1
    context_index = metadata_index + 1
    if len(lines) not in {context_index, context_index + 1}:
        raise ProbeError("boot-critical inventory metadata is incomplete")
    digest: str | None = None
    if digest_index is not None:
        words = lines[digest_index].split()
        digest = words[0].lower() if words else ""
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ProbeError("boot-critical file digest is invalid")
    fields = lines[metadata_index].split("\t")
    if len(fields) != 5:
        raise ProbeError("boot-critical node metadata is invalid")
    try:
        size, mode, uid, gid, mtime = (
            int(field, 8 if index == 1 else 10)
            for index, field in enumerate(fields)
        )
    except ValueError as exc:
        raise ProbeError("boot-critical node metadata is invalid") from exc
    if min(size, uid, gid, mtime) < 0 or mode < 0 or mode > 0o7777:
        raise ProbeError("boot-critical node metadata is outside its valid range")
    context = None
    if len(lines) == context_index + 1 and lines[context_index].split():
        candidate = lines[context_index].split()[0]
        if ":" in candidate:
            context = candidate
    return {
        "path": path,
        "kind": kind,
        "sha256": digest,
        "size": size,
        "mode": f"{mode:04o}",
        "uid": uid,
        "gid": gid,
        "mtime_epoch": mtime,
        "selinux_context": context,
    }


def _remote_tree_paths(client: AdbClient, root: str) -> list[str]:
    encoded = _root_checked(
        client,
        f"set -o pipefail && find {shlex.quote(root)} -xdev -print0 | "
        "base64 | tr -d '\\n\\r'",
        "boot-critical tree enumeration",
        timeout=120,
    )
    try:
        decoded = base64.b64decode(encoded, validate=True)
        paths = [item.decode("utf-8") for item in decoded.split(b"\0") if item]
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProbeError("boot-critical tree enumeration is not canonical UTF-8") from exc
    if not paths or paths[0] != root or len(paths) != len(set(paths)):
        raise ProbeError("boot-critical tree enumeration is incomplete or duplicated")
    prefix = f"{root}/"
    if any(path != root and not path.startswith(prefix) for path in paths):
        raise ProbeError("boot-critical tree escaped its canonical root")
    return sorted(paths)


def _sqlite_content_digest(data: bytes) -> str:
    """Hash database meaning, excluding SQLite's boot-dependent storage layout."""

    if not data.startswith(b"SQLite format 3\0") or len(data) > MAX_DATABASE_BYTES:
        raise ProbeError("Magisk database is not a bounded SQLite database")

    def value(item: Any) -> list[Any]:
        if item is None:
            return ["null"]
        if isinstance(item, int):
            return ["integer", str(item)]
        if isinstance(item, float):
            return ["real", item.hex()]
        if isinstance(item, bytes):
            return ["blob", base64.b64encode(item).decode("ascii")]
        return ["text", item]

    try:
        with tempfile.TemporaryDirectory(prefix="kitsune-database-inventory-") as temporary:
            path = Path(temporary) / "magisk.db"
            path.write_bytes(data)
            path.chmod(0o600)
            with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)) as database:
                deadline = time.monotonic() + 20
                database.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
                database.execute("PRAGMA query_only=ON")
                database.execute("PRAGMA trusted_schema=OFF")
                if database.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ProbeError("Magisk database failed SQLite integrity checking")
                schema = database.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
                ).fetchall()
                tables = []
                for kind, name, _, sql in schema:
                    if kind != "table":
                        continue
                    if sql and "CREATE VIRTUAL TABLE" in sql.upper():
                        raise ProbeError("Magisk database contains an unsupported virtual table")
                    quoted = '"' + name.replace('"', '""') + '"'
                    columns = database.execute(f"PRAGMA table_info({quoted})").fetchall()
                    # A declared primary key is the row identity for Magisk's
                    # tables. REPLACE may change their implicit rowids at boot.
                    # Preserve rowids for any extension table without a key.
                    selection = "*" if any(column[5] for column in columns) else "rowid,*"
                    rows = [
                        [value(item) for item in row]
                        for row in database.execute(f"SELECT {selection} FROM {quoted}")
                    ]
                    rows.sort(key=lambda row: json.dumps(row, ensure_ascii=True, separators=(",", ":")))
                    tables.append({"name": name, "columns": columns, "rows": rows})
                content = {
                    "schema": schema,
                    "tables": tables,
                    "user_version": database.execute("PRAGMA user_version").fetchone()[0],
                    "application_id": database.execute("PRAGMA application_id").fetchone()[0],
                    "encoding": database.execute("PRAGMA encoding").fetchone()[0],
                }
                return _sha256(_canonical_bytes(content))
    except sqlite3.DatabaseError as exc:
        raise ProbeError("Magisk database is corrupt or cannot be inventoried") from exc


def _remote_database_digest(client: AdbClient, node: Mapping[str, Any]) -> str:
    path = "/data/adb/magisk.db"
    # The generic qualifier accepts only a stable, checkpointed database.
    # Never ignore committed rows that may still reside in a WAL or journal.
    checks = " && ".join(f"[ ! -e {path}{suffix} ] && [ ! -L {path}{suffix} ]" for suffix in (
        "-wal", "-shm", "-journal",
    ))
    encoded = _root_checked(
        client,
        f"set -o pipefail && [ -f {path} ] && [ ! -L {path} ] && {checks} && "
        f"[ \"$(stat -c %s {path})\" -le {MAX_DATABASE_BYTES} ] && "
        f"head -c {MAX_DATABASE_BYTES + 1} {path} | base64 | tr -d '\\n\\r'",
        "stable checkpointed Magisk database capture",
    )
    try:
        data = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ProbeError("Magisk database capture is not canonical base64") from exc
    if len(data) != node["size"] or _sha256(data) != node["sha256"]:
        raise ProbeError("Magisk database changed during inventory capture")
    after = _remote_file_evidence(client, path)
    if any(after[key] != node[key] for key in after):
        raise ProbeError("Magisk database metadata changed during inventory capture")
    _root_checked(client, checks, "checkpointed Magisk database recheck")
    return _sqlite_content_digest(data)


def _boot_critical_inventory(client: AdbClient, selected_init: str) -> list[dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    for root in _boot_critical_roots(selected_init):
        root_evidence = _remote_node_evidence(client, root)
        if root == "/data/adb/magisk.db" and root_evidence["kind"] == "file":
            root_evidence["sqlite_content_sha256"] = _remote_database_digest(client, root_evidence)
        nodes[root] = root_evidence
        if root_evidence["kind"] != "directory":
            continue
        for path in _remote_tree_paths(client, root):
            if path == root:
                continue
            if path in nodes:
                raise ProbeError("boot-critical inventory contains overlapping duplicate paths")
            nodes[path] = _remote_node_evidence(client, path)
    return [nodes[path] for path in sorted(nodes)]


def _inventory_digest(inventory: list[dict[str, Any]]) -> str:
    paths = []
    for node in inventory:
        node = dict(node)
        if "sqlite_content_sha256" in node:
            if node["path"] != "/data/adb/magisk.db" or node["kind"] != "file":
                raise ValueError("SQLite content evidence is only valid for the Magisk database")
            # Keep raw bytes/metadata in the sealed record for audit. Compare
            # logical contents across boots, retaining mode, owner and label.
            for key in ("sha256", "size", "mtime_epoch"):
                node.pop(key)
        paths.append(node)
    return _sha256(_canonical_bytes({"paths": paths}))


def _exec_result_payload(nonce: str, role: str, boot_id: str, domain: str) -> bytes:
    return (
        "kitsune-system-mode-init-exec-v1\n"
        f"nonce={nonce}\n"
        f"role={role}\n"
        f"boot_id={boot_id}\n"
        f"domain={domain}\n"
    ).encode("ascii")


def _verify_init_exec_probe(
    client: AdbClient,
    *,
    probe: Mapping[str, Any],
    helper: Mapping[str, Any],
    result_paths: Mapping[str, str],
    nonce: str,
    boot_id: str,
) -> dict[str, Any]:
    expected_property = f"{nonce}:{boot_id}"
    checks = " && ".join(
        f"[ -f {shlex.quote(path)} ] && [ ! -L {shlex.quote(path)} ]"
        for path in result_paths.values()
    )
    deadline = time.monotonic() + 30
    while True:
        try:
            result = client.shell(
                f"{checks} && getprop {QUALIFICATION_PROPERTY}", root=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip() == expected_property:
                break
        except ProbeError:
            pass
        if time.monotonic() >= deadline:
            raise ProbeError("timed out waiting for both init domain probe results")
        time.sleep(0.25)
    if _remote_file_evidence(client, str(probe["path"])) != dict(probe):
        raise ProbeError("init qualification RC did not persist")
    if _remote_file_evidence(client, str(helper["path"])) != dict(helper):
        raise ProbeError("init qualification helper did not persist")
    results: dict[str, Any] = {}
    for role, domain in (("init", "u:r:init:s0"), ("magisk", "u:r:magisk:s0")):
        evidence = _remote_file_evidence(client, result_paths[role])
        expected = _exec_result_payload(nonce, role, boot_id, domain)
        if evidence["sha256"] != hashlib.sha256(expected).hexdigest() or evidence["size"] != len(expected):
            raise ProbeError(f"selected init directory did not execute the {role} domain probe")
        if evidence["mode"] != "0600" or evidence["uid"] != 0 or evidence["gid"] != 0:
            raise ProbeError(f"{role} domain probe result metadata is invalid")
        results[role] = evidence
    result = client.shell(f"getprop {QUALIFICATION_PROPERTY}")
    if result.returncode != 0 or result.stdout.strip() != expected_property:
        raise ProbeError("selected init directory did not freshly execute both domain probes")
    return {
        "boot_id_sha256": _sha256(boot_id),
        "init_result": results["init"],
        "magisk_result": results["magisk"],
    }


def _write_probe(
    client: AdbClient,
    path: str,
    content: bytes,
    mode: str = "0644",
    selinux_context: str | None = "u:object_r:system_file:s0",
) -> dict[str, Any]:
    try:
        payload = content.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("qualification probes must be ASCII") from exc
    quoted = shlex.quote(path)
    parent = shlex.quote(str(Path(path).parent))
    label = (
        f"chcon {shlex.quote(selinux_context)} {quoted} 2>/dev/null || true"
        if selinux_context
        else f"restorecon {quoted} 2>/dev/null || true"
    )
    command = (
        f"test -d {parent} && test ! -e {quoted} && test ! -L {quoted} && "
        f"umask 077 && printf %s {shlex.quote(payload)} > {quoted} && "
        f"chmod {mode} {quoted} && chown 0:0 {quoted} && "
        f"({label}) && sync"
    )
    _root_checked(client, command, "qualification probe publication")
    evidence = _remote_file_evidence(client, path)
    expected = hashlib.sha256(content).hexdigest()
    expected = {
        "path": path,
        "sha256": expected,
        "size": len(content),
        "mode": mode,
        "uid": 0,
        "gid": 0,
    }
    if {key: evidence[key] for key in expected} != expected:
        raise ProbeError("qualification probe digest mismatch")
    return evidence


def _replace_probe(
    client: AdbClient,
    path: str,
    content: bytes,
    mode: str = "0600",
    selinux_context: str | None = "u:object_r:system_file:s0",
) -> dict[str, Any]:
    try:
        payload = content.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ValueError("qualification probes must be ASCII") from exc
    quoted = shlex.quote(path)
    label = (
        f"chcon {shlex.quote(selinux_context)} {quoted} 2>/dev/null || true"
        if selinux_context
        else f"restorecon {quoted} 2>/dev/null || true"
    )
    command = (
        f"test -f {quoted} && test ! -L {quoted} && umask 077 && "
        f"printf %s {shlex.quote(payload)} > {quoted} && chmod {mode} {quoted} && "
        f"chown 0:0 {quoted} && "
        f"({label}) && sync"
    )
    _root_checked(client, command, "qualification recovery anchor mutation")
    evidence = _remote_file_evidence(client, path)
    expected = {
        "path": path,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "mode": mode,
        "uid": 0,
        "gid": 0,
    }
    if {key: evidence[key] for key in expected} != expected:
        raise ProbeError("qualification recovery anchor mutation did not persist exactly")
    return evidence


def _remove_probe(client: AdbClient, path: str, purpose: str) -> None:
    quoted = shlex.quote(path)
    output = _root_checked(
        client,
        f"rm -f {quoted} && test ! -e {quoted} && test ! -L {quoted} && sync && echo absent",
        purpose,
    )
    if output != "absent":
        raise ProbeError(f"{purpose} did not remove its temporary path")


def _clear_qualification_property(client: AdbClient) -> None:
    output = _root_checked(
        client,
        f"setprop {QUALIFICATION_PROPERTY} '' && "
        f"test -z \"$(getprop {QUALIFICATION_PROPERTY})\" && echo absent",
        "qualification property cleanup",
    )
    if output != "absent":
        raise ProbeError("qualification property could not be cleared")


def _assert_qualification_absent(client: AdbClient, paths: tuple[str, ...]) -> None:
    checks = " && ".join(
        f"test ! -e {shlex.quote(path)} && test ! -L {shlex.quote(path)}"
        for path in paths
    )
    output = _root_checked(
        client,
        f"{checks} && test -z \"$(getprop {QUALIFICATION_PROPERTY})\" && echo absent",
        "qualification residue check",
    )
    if output != "absent":
        raise ProbeError("qualification left boot-critical guest residue")


def _remove_host_backup(path: Path) -> None:
    """Remove only a qualification-created exact backup path."""

    metadata = path.stat(follow_symlinks=False)
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("qualification temporary backup became a symbolic link")
    if stat.S_ISREG(metadata.st_mode):
        path.unlink()
    elif stat.S_ISDIR(metadata.st_mode):
        shutil.rmtree(path)
    else:
        raise ValueError("qualification temporary backup became a special node")
    if path.exists() or path.is_symlink():
        raise ValueError("qualification temporary backup cleanup failed")
    descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_verified_qualification_backups(
    challenge_backup: Path,
    canonical_backup: Path,
    *,
    final_ready: bool,
    qualification_succeeded: bool,
    recovery_verified: bool,
    lifecycle_indeterminate: bool,
    cleanup_error: BaseException | None,
) -> BaseException | None:
    """Delete temporary recovery artifacts only after a proven clean guest state."""

    if (
        not qualification_succeeded
        or lifecycle_indeterminate
        or cleanup_error is not None
        or not recovery_verified
    ):
        return cleanup_error
    try:
        if challenge_backup.exists() or challenge_backup.is_symlink():
            _remove_host_backup(challenge_backup)
        if not final_ready and (canonical_backup.exists() or canonical_backup.is_symlink()):
            _remove_host_backup(canonical_backup)
    except (OSError, ValueError) as exc:
        return exc
    return None


def _restore_unchanged_backup(
    backup: Path,
    expected_digest: str,
    purpose: str,
    restore: Callable[[], None],
) -> None:
    """Run one restore only while its complete recovery artifact stays exact."""

    from tools.system_mode.authorization import digest_backup_path

    if digest_backup_path(backup) != expected_digest:
        raise ValueError(f"{purpose} backup changed before restore")
    restore()
    if digest_backup_path(backup) != expected_digest:
        raise ValueError(f"{purpose} backup changed during restore")


def _failure_recovery_candidates(
    *,
    final_verified: bool,
    challenge_verified: bool,
    canonical_backup: Path,
    challenge_backup: Path,
    final_digest: str,
    challenge_digest: str,
) -> list[tuple[str, Path, str]]:
    """Return only proven recovery artifacts, strongest first."""

    candidates: list[tuple[str, Path, str]] = []
    if final_verified:
        candidates.append(("clean", canonical_backup, final_digest))
    if challenge_verified:
        candidates.append(("challenge", challenge_backup, challenge_digest))
    return candidates


def _recover_from_candidates(
    candidates: list[tuple[str, Path, str]],
    recover: Callable[[tuple[str, Path, str]], None],
    direct_cleanup: Callable[[], None],
) -> str | None:
    """Try proven artifacts in order, then an exactly verified direct cleanup."""

    errors: list[BaseException] = []
    for candidate in candidates:
        try:
            recover(candidate)
            return candidate[0]
        except IndeterminateLifecycleError:
            raise
        except (OSError, ProbeError, ValueError) as exc:
            errors.append(exc)
    try:
        direct_cleanup()
        return None
    except IndeterminateLifecycleError:
        raise
    except (OSError, ProbeError, ValueError) as exc:
        errors.append(exc)
    raise ProbeError("every qualification failure-recovery attempt failed") from errors[-1]


def _boot_id(client: AdbClient) -> str:
    result = client.shell("cat /proc/sys/kernel/random/boot_id")
    value = result.stdout.strip().lower()
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ProbeError("target boot ID is unavailable") from exc
    if str(parsed) != value:
        raise ProbeError("target boot ID is not canonical")
    return value


def _wait_for_new_boot(
    client: AdbClient,
    prior_boot: str,
    *,
    endpoint: str | None,
    timeout: int,
) -> str:
    deadline = time.monotonic() + timeout
    last_error = "target did not reconnect"
    while time.monotonic() < deadline:
        try:
            if endpoint:
                client.connect(endpoint)
            client.wait_for_device()
            completed = client.shell("getprop sys.boot_completed", timeout=10)
            if completed.stdout.strip() != "1":
                time.sleep(2)
                continue
            current = _boot_id(client)
            if current != prior_boot:
                return current
            last_error = "lifecycle command did not produce a new boot ID"
        except ProbeError as exc:
            last_error = str(exc)
        time.sleep(2)
    raise ProbeError(last_error)


def _validate_inventory(
    inventory: list[dict[str, Any]],
    selected_init: str,
    expected_digest: str,
) -> None:
    paths = [str(item["path"]) for item in inventory]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ValueError("qualification boot-critical inventory is not sorted and unique")
    roots = _boot_critical_roots(selected_init)
    for root in roots:
        if root not in paths:
            raise ValueError("qualification boot-critical inventory is missing a required root")
    for item in inventory:
        path = str(item["path"])
        if not path.startswith("/") or os.path.normpath(path) != path:
            raise ValueError("qualification boot-critical inventory path is not canonical")
        matching_roots = [
            root for root in roots if path == root or path.startswith(f"{root}/")
        ]
        if not matching_roots:
            raise ValueError("qualification boot-critical inventory escaped its allowlist")
        kind = item["kind"]
        if kind == "absent" and path not in roots:
            raise ValueError("only a boot-critical root may be recorded as absent")
        if kind == "file" and item["sha256"] is None:
            raise ValueError("boot-critical file is missing its byte digest")
        if kind != "file" and item["sha256"] is not None:
            raise ValueError("non-file boot-critical node unexpectedly has a byte digest")
        metadata = (
            item["size"],
            item["mode"],
            item["uid"],
            item["gid"],
            item["mtime_epoch"],
        )
        if kind == "absent" and any(value is not None for value in metadata):
            raise ValueError("absent boot-critical node unexpectedly has metadata")
        if kind != "absent" and any(value is None for value in metadata):
            raise ValueError("present boot-critical node is missing metadata")
    if _inventory_digest(inventory) != expected_digest:
        raise ValueError("qualification boot-critical inventory digest is invalid")


def validate_qualification_record(record: Mapping[str, Any]) -> None:
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    validate_schema_instance(record, schema)
    if record["backup"]["sha256_before"] != record["backup"]["sha256_after"]:
        raise ValueError("qualification backup changed during restore exercise")
    cold_boots = record["persistence"]["cold_boot_ids"]
    if len(cold_boots) < 3 or len(cold_boots) != len(set(cold_boots)):
        raise ValueError("qualification requires three distinct cold boots")
    lifecycle_boots = [
        record["challenge"]["created_boot_id_sha256"],
        *cold_boots,
        record["challenge"]["restored_boot_id_sha256"],
        record["backup"]["created_boot_id_sha256"],
        record["recovery"]["restored_boot_id_sha256"],
    ]
    if len(lifecycle_boots) != len(set(lifecycle_boots)):
        raise ValueError("every qualification lifecycle action must yield a distinct boot ID")
    if (
        record["target"]["baseline_contract_sha256"]
        != record["recovery"]["target_contract_sha256_after_restore"]
    ):
        raise ValueError("restored target contract differs from the qualified baseline")
    if record["challenge"]["sha256_before"] != record["challenge"]["sha256_after"]:
        raise ValueError("qualification challenge backup changed during restore exercise")
    if record["challenge"]["sha256_before"] == record["backup"]["sha256_before"]:
        raise ValueError("final clean backup is indistinguishable from challenged backup")
    if (
        record["challenge"]["anchor_before"]["sha256"]
        == record["challenge"]["anchor_mutated"]["sha256"]
    ):
        raise ValueError("qualification recovery anchor was not changed after backup creation")
    if (
        record["challenge"]["data_anchor_before"]["sha256"]
        == record["challenge"]["data_anchor_mutated"]["sha256"]
    ):
        raise ValueError("qualification /data recovery anchor was not changed after backup creation")
    if (
        record["challenge"]["anchor_before"]
        != record["challenge"]["anchor_after_restore"]
    ):
        raise ValueError("external restore did not recover the exact pre-backup anchor")
    if (
        record["challenge"]["data_anchor_before"]
        != record["challenge"]["data_anchor_after_restore"]
    ):
        raise ValueError("external restore did not recover the exact pre-backup /data anchor")
    commands = record["commands"]
    backup_location = record["backup"]["location"]
    final_backup_command = shlex.join(
        _materialize_command(commands["backup"], Path(backup_location))
    )
    final_restore_command = shlex.join(
        _materialize_command(commands["restore"], Path(backup_location))
    )
    if record["backup"]["backup_command"] != final_backup_command:
        raise ValueError("backup command text differs from its immutable identity")
    if record["backup"]["restore_command"] != final_restore_command:
        raise ValueError("restore command text differs from its immutable identity")
    if record["persistence"]["cold_boot_command"] != shlex.join(commands["cold_boot"]["argv"]):
        raise ValueError("cold boot command text differs from its immutable identity")
    if not Path(backup_location).is_absolute() or os.path.normpath(backup_location) != backup_location:
        raise ValueError("qualification backup location is not a normalized absolute path")
    for name in ("backup", "restore"):
        if commands[name]["argv"][1:].count("{backup}") != 1:
            raise ValueError(
                f"{name} command is not a unique backup-location template"
            )
    for name in ("backup", "cold_boot", "restore"):
        argv = commands[name]["argv"]
        if argv[0] != commands[name]["executable"]["path"]:
            raise ValueError(f"{name} command executable path differs from argv")
        if any(
            "\x00" in argument or "\n" in argument or "\r" in argument
            for argument in argv
        ):
            raise ValueError(f"{name} command arguments must be single-line text")
        if commands[name]["executable"]["path"] == record["instance_identity"]["path"]:
            raise ValueError("instance identity must be distinct from lifecycle executables")
    selected_init = record["init"]["directory"]
    record_id = record["record_id"]
    anchor_path = f"{selected_init}/.kitsune-system-mode-backup-anchor-{record_id}"
    data_anchor_path = f"/data/adb/.kitsune-system-mode-backup-anchor-{record_id}"
    helper_path = f"{selected_init}/.kitsune-system-mode-qualification-{record_id}.sh"
    init_result_path = f"/dev/.kitsune-system-mode-qualification-{record_id}-init.result"
    magisk_result_path = f"/dev/.kitsune-system-mode-qualification-{record_id}-magisk.result"
    expected_paths = (
        (record["challenge"]["anchor_before"], anchor_path),
        (record["challenge"]["anchor_mutated"], anchor_path),
        (record["challenge"]["anchor_after_restore"], anchor_path),
        (record["challenge"]["data_anchor_before"], data_anchor_path),
        (record["challenge"]["data_anchor_mutated"], data_anchor_path),
        (record["challenge"]["data_anchor_after_restore"], data_anchor_path),
        (
            record["init"]["probe"],
            f"{selected_init}/kitsune-system-mode-qualification-{record_id}.rc",
        ),
        (record["init"]["helper"], helper_path),
        (
            record["challenge"]["marker"],
            f"{selected_init}/.kitsune-system-mode-recovery-{record_id}",
        ),
        (
            record["challenge"]["data_marker"],
            f"/data/adb/.kitsune-system-mode-recovery-{record_id}",
        ),
        (
            record["recovery"]["marker"],
            f"{selected_init}/.kitsune-system-mode-final-recovery-{record_id}",
        ),
        (
            record["recovery"]["data_marker"],
            f"/data/adb/.kitsune-system-mode-final-recovery-{record_id}",
        ),
    )
    for field, expected_path in expected_paths:
        if field["path"] != expected_path:
            raise ValueError("qualification probe path does not match its record identity")
    proofs = record["persistence"]["exec_proofs"]
    if len(proofs) != len(cold_boots):
        raise ValueError("qualification init exec proof count differs from cold boot count")
    for expected_boot, proof in zip(cold_boots, proofs):
        if proof["boot_id_sha256"] != expected_boot:
            raise ValueError("qualification init exec proof belongs to a different boot")
        if proof["init_result"]["path"] != init_result_path:
            raise ValueError("qualification init-domain result path is invalid")
        if proof["magisk_result"]["path"] != magisk_result_path:
            raise ValueError("qualification magisk-domain result path is invalid")
    if len({proof["init_result"]["sha256"] for proof in proofs}) != len(proofs):
        raise ValueError("qualification init-domain proof was not fresh on every boot")
    if len({proof["magisk_result"]["sha256"] for proof in proofs}) != len(proofs):
        raise ValueError("qualification magisk-domain proof was not fresh on every boot")
    _validate_inventory(
        record["target"]["baseline_inventory"],
        selected_init,
        record["target"]["baseline_inventory_sha256"],
    )
    _validate_inventory(
        record["recovery"]["inventory_after_restore"],
        selected_init,
        record["recovery"]["inventory_sha256_after_restore"],
    )
    if record["target"]["baseline_inventory_sha256"] != record["recovery"]["inventory_sha256_after_restore"]:
        raise ValueError("external restore did not recover the exact boot-critical inventory")


def load_qualification_record(
    path: Path,
    *,
    seal_key_path: Path | None = None,
) -> tuple[dict[str, Any], bytes, str, Path]:
    from tools.system_mode.authorization import stable_regular_file

    identity, captured = stable_regular_file(
        path,
        purpose="qualification record",
        capture=True,
        max_bytes=MAX_RECORD_BYTES,
    )
    if captured is None:
        raise ValueError("qualification record could not be read")
    raw = captured
    record = json.loads(raw.decode("utf-8"))
    if not isinstance(record, dict):
        raise ValueError("qualification record root must be an object")
    validate_qualification_record(record)
    _verify_record_seal(record, seal_key_path)
    return record, raw, str(identity["sha256"]), Path(str(identity["path"]))


def _evidence_from_loaded_record(
    record: Mapping[str, Any],
    path: Path,
    digest: str,
) -> QualificationEvidence:
    verify_instance_identity(record["instance_identity"])
    for name in ("backup", "cold_boot", "restore"):
        _verify_command_identity(record["commands"][name], name.replace("_", " "))
    from tools.system_mode.authorization import digest_backup_path

    backup = record["backup"]
    if digest_backup_path(Path(backup["location"])) != backup["sha256_after"]:
        raise ValueError("qualified external backup is missing or changed")
    return QualificationEvidence(
        init_import_proven=True,
        snapshot_id=str(backup["snapshot_id"]),
        backup_location=str(backup["location"]),
        backup_digest=str(backup["sha256_after"]),
        restore_command=str(backup["restore_command"]),
        recovery_verified=True,
        backing_write_probe="passed",
        cold_boots=len(record["persistence"]["cold_boot_ids"]),
        host_restarts=len(record["persistence"]["host_restart_ids"]),
        qualification_record=str(path.resolve()),
        qualification_sha256=digest,
        adapter_id=str(record["adapter"]["id"]),
        instance_identity_sha256=_sha256(_canonical_bytes(record["instance_identity"])),
    )


def load_qualification_evidence(
    path: Path,
    *,
    seal_key_path: Path | None = None,
) -> tuple[dict[str, Any], QualificationEvidence]:
    """Load one sealed record and hash its bound recovery artifact exactly once."""

    record, _, digest, canonical = load_qualification_record(
        path,
        seal_key_path=seal_key_path,
    )
    return record, _evidence_from_loaded_record(record, canonical, digest)


def evidence_from_record(
    path: Path,
    *,
    seal_key_path: Path | None = None,
) -> QualificationEvidence:
    _, evidence = load_qualification_evidence(
        path,
        seal_key_path=seal_key_path,
    )
    return evidence


def verify_report_qualification_evidence(
    report: Mapping[str, Any],
    record: Mapping[str, Any],
    evidence: QualificationEvidence,
) -> dict[str, Any]:
    """Bind a live report to already verified record and backup evidence."""

    recovery = report["recovery"]
    if evidence.qualification_sha256 != recovery.get("qualification_sha256"):
        raise ValueError("doctor report qualification digest mismatch")
    expected_recovery = {
        "snapshot_id": evidence.snapshot_id,
        "backup_location": evidence.backup_location,
        "backup_digest": evidence.backup_digest,
        "restore_command": evidence.restore_command,
        "qualification_record": evidence.qualification_record,
        "qualification_sha256": evidence.qualification_sha256,
        "adapter_id": evidence.adapter_id,
        "instance_identity_sha256": evidence.instance_identity_sha256,
        "verified": True,
    }
    if dict(recovery) != expected_recovery:
        raise ValueError("doctor report recovery evidence differs from qualification")
    expected_persistence = {
        "backing_write_probe": evidence.backing_write_probe,
        "cold_boots": evidence.cold_boots,
        "host_restarts": evidence.host_restarts,
        "proven": True,
    }
    if dict(report["persistence"]) != expected_persistence:
        raise ValueError("doctor report persistence evidence differs from qualification")
    if report["source"]["serial_sha256"] != record["target"]["serial_sha256"]:
        raise ValueError("qualification belongs to a different ADB instance")
    if report["source"]["repository_commit"] != record["source"]["repository_commit"]:
        raise ValueError("qualification belongs to a different source commit")
    if report["source"]["probe_sha256"] != record["source"]["probe_sha256"]:
        raise ValueError("qualification belongs to a different probe implementation")
    for field in ("fingerprint_sha256", "api", "abis"):
        if report["device"][field] != record["target"][field]:
            raise ValueError(f"qualification target {field} differs from the live report")
    if report["init"]["selected_directory"] != record["init"]["directory"]:
        raise ValueError("qualification init directory differs from the live report")
    if stable_target_digest(report) != record["target"]["baseline_contract_sha256"]:
        raise ValueError("qualified target changed after its restore exercise")
    return dict(record)


def verify_report_qualification(
    report: Mapping[str, Any],
    *,
    seal_key_path: Path | None = None,
) -> dict[str, Any]:
    recovery = report["recovery"]
    record_path = Path(str(recovery.get("qualification_record") or ""))
    record, evidence = load_qualification_evidence(
        record_path,
        seal_key_path=seal_key_path,
    )
    return verify_report_qualification_evidence(report, record, evidence)



def qualify_target(
    client: AdbClient,
    *,
    output: Path,
    backup_location: Path,
    snapshot_id: str,
    backup_command: str,
    restore_command: str,
    cold_boot_command: str,
    instance_identity_path: Path,
    seal_key_path: Path | None = None,
    endpoint: str | None = None,
    lifecycle_timeout: int = 300,
) -> dict[str, Any]:
    """Prove a challenged restore, then retain only a clean proven backup."""

    output = output.expanduser()
    backup_location = backup_location.expanduser()
    if lifecycle_timeout < 1:
        raise ValueError("qualification lifecycle timeout must be positive")
    if not snapshot_id or any(character in snapshot_id for character in "\x00\n\r"):
        raise ValueError("qualification snapshot ID must be non-empty single-line text")
    if not output.is_absolute() or not backup_location.is_absolute():
        raise ValueError("qualification output and backup location must be absolute paths")
    if output.exists() or output.is_symlink():
        raise ValueError("qualification output already exists")
    if not output.parent.is_dir():
        raise ValueError("qualification output parent must already exist")
    if backup_location.exists() or backup_location.is_symlink():
        raise ValueError(
            "qualification backup destination must not exist; the reviewed backup command must create it"
        )
    backup_parent = backup_location.parent.resolve(strict=True)
    canonical_backup = backup_parent / backup_location.name
    canonical_output = output.parent.resolve(strict=True) / output.name
    canonical_identity = instance_identity_path.expanduser().resolve(strict=True)
    if canonical_backup in {canonical_output, canonical_identity}:
        raise ValueError("backup, qualification record, and instance identity must be distinct paths")
    canonical_seal_key, _ = _load_seal_key(seal_key_path, create=True)
    if canonical_seal_key in {canonical_backup, canonical_output, canonical_identity}:
        raise ValueError(
            "qualification seal key, backup, record, and instance identity must be distinct"
        )

    descriptors = {descriptor.adapter_id: descriptor for descriptor in built_in_descriptors()}
    adapter = descriptors["generic-in-guest"]
    commands = {
        "backup": _checked_command(backup_command, "backup", backup_placeholder=True),
        "cold_boot": _checked_command(cold_boot_command, "cold boot"),
        "restore": _checked_command(restore_command, "restore", backup_placeholder=True),
    }
    if canonical_identity in {
        Path(command["executable"]["path"])
        for command in commands.values()
    }:
        raise ValueError("instance identity must be distinct from lifecycle executables")

    from tools.system_mode.authorization import digest_backup_path

    identity = instance_identity(instance_identity_path)
    baseline = collect_report(client, QualificationEvidence())
    assessment = classify_report(baseline)
    blockers = (
        set(assessment["reason_codes"])
        - set(assessment["warnings"])
        - _EVIDENCE_ONLY_REASONS
    )
    if blockers:
        raise ValueError(f"target has non-evidence System Mode blockers: {sorted(blockers)}")
    if baseline["source"].get("repository_dirty") is not False:
        raise ValueError("qualification requires a clean, exact source commit")
    baseline_digest = stable_target_digest(baseline)
    selected_init = baseline["init"].get("selected_directory")
    if selected_init not in {"/system/etc/init", "/system/etc/init/hw"}:
        raise ValueError("target has no supported writable init directory")
    baseline_inventory = _boot_critical_inventory(client, selected_init)
    baseline_inventory_sha256 = _inventory_digest(baseline_inventory)

    record_id = str(uuid.uuid4())
    nonce = uuid.uuid4().hex
    challenge_backup = canonical_backup.parent / (
        f".{canonical_backup.name}.kitsune-challenge-{record_id}"
    )
    if challenge_backup.exists() or challenge_backup.is_symlink():
        raise ValueError("qualification challenge backup path already exists")
    anchor_path = f"{selected_init}/.kitsune-system-mode-backup-anchor-{record_id}"
    data_anchor_path = f"/data/adb/.kitsune-system-mode-backup-anchor-{record_id}"
    probe_path = f"{selected_init}/kitsune-system-mode-qualification-{record_id}.rc"
    helper_path = f"{selected_init}/.kitsune-system-mode-qualification-{record_id}.sh"
    marker_path = f"{selected_init}/.kitsune-system-mode-recovery-{record_id}"
    data_marker_path = f"/data/adb/.kitsune-system-mode-recovery-{record_id}"
    final_marker_path = f"{selected_init}/.kitsune-system-mode-final-recovery-{record_id}"
    data_final_marker_path = f"/data/adb/.kitsune-system-mode-final-recovery-{record_id}"
    init_result_path = f"/dev/.kitsune-system-mode-qualification-{record_id}-init.result"
    magisk_result_path = f"/dev/.kitsune-system-mode-qualification-{record_id}-magisk.result"
    result_paths = {"init": init_result_path, "magisk": magisk_result_path}
    residue_paths = (
        anchor_path,
        data_anchor_path,
        probe_path,
        helper_path,
        marker_path,
        data_marker_path,
        final_marker_path,
        data_final_marker_path,
        init_result_path,
        f"{init_result_path}.new",
        magisk_result_path,
        f"{magisk_result_path}.new",
    )

    anchor_before_payload = (
        f"kitsune-backup-anchor-v1:{uuid.uuid4().hex}:{record_id}\n"
    ).encode("ascii")
    anchor_mutated_payload = (
        f"kitsune-backup-mutated-v1:{uuid.uuid4().hex}:{record_id}\n"
    ).encode("ascii")
    data_anchor_before_payload = (
        f"kitsune-data-backup-anchor-v1:{uuid.uuid4().hex}:{record_id}\n"
    ).encode("ascii")
    data_anchor_mutated_payload = (
        f"kitsune-data-backup-mutated-v1:{uuid.uuid4().hex}:{record_id}\n"
    ).encode("ascii")
    helper = (
        "#!/system/bin/sh\n"
        "set -eu\n"
        "role=\"$1\"\n"
        "case \"$role\" in\n"
        f"  init) expected='u:r:init:s0'; output='{init_result_path}' ;;\n"
        f"  magisk) expected='u:r:magisk:s0'; output='{magisk_result_path}' ;;\n"
        "  *) exit 64 ;;\n"
        "esac\n"
        "domain=\"$(cat /proc/$$/attr/current | tr -d '\\000')\"\n"
        "[ \"$domain\" = \"$expected\" ] || exit 65\n"
        "boot_id=\"$(cat /proc/sys/kernel/random/boot_id)\"\n"
        "tmp=\"$output.new\"\n"
        "umask 077\n"
        "printf 'kitsune-system-mode-init-exec-v1\\nnonce=%s\\nrole=%s\\nboot_id=%s\\ndomain=%s\\n' "
        f"'{nonce}' \"$role\" \"$boot_id\" \"$domain\" >\"$tmp\"\n"
        "chmod 0600 \"$tmp\"\n"
        "chown 0:0 \"$tmp\"\n"
        f"setprop {QUALIFICATION_PROPERTY} '{nonce}:'\"$boot_id\"\n"
        "mv -f \"$tmp\" \"$output\"\n"
        "sync\n"
    ).encode("ascii")
    probe = (
        "# Kitsune System Mode qualification schema 1; temporary.\n"
        # The existing root provider may create the Magisk domain during its
        # own post-fs-data bootstrap. Test execution after that policy exists,
        # just as the production launcher prepares policy before entering it.
        "on property:sys.boot_completed=1\n"
        f"    exec u:r:init:s0 0 0 -- /system/bin/sh {helper_path} init\n"
        f"    exec u:r:magisk:s0 0 0 -- /system/bin/sh {helper_path} magisk\n"
    ).encode("ascii")
    marker = f"kitsune-system-mode-challenge-recovery:{record_id}\n".encode("ascii")
    data_marker = f"kitsune-data-challenge-recovery:{record_id}\n".encode("ascii")
    final_marker = (
        f"kitsune-system-mode-final-recovery:{uuid.uuid4().hex}:{record_id}\n"
    ).encode("ascii")
    data_final_marker = (
        f"kitsune-data-final-recovery:{uuid.uuid4().hex}:{record_id}\n"
    ).encode("ascii")

    live_dirty = False
    challenge_ready = False
    challenge_verified = False
    final_ready = False
    final_verified = False
    restored = False
    qualification_succeeded = False
    lifecycle_indeterminate = False
    prior_boot = _boot_id(client)
    cold_boot_ids: list[str] = []
    exec_proofs: list[dict[str, Any]] = []
    challenge_created_boot_id_sha256 = ""
    challenge_restored_boot_id_sha256 = ""
    final_created_boot_id_sha256 = ""
    challenge_before = ""
    final_before = ""

    def run_lifecycle(
        command_name: str,
        purpose: str,
        backup_path: Path | None = None,
    ) -> str:
        nonlocal prior_boot, lifecycle_indeterminate
        verify_instance_identity(identity)
        try:
            _run_host(
                commands[command_name],
                purpose,
                lifecycle_timeout,
                backup_location=backup_path,
            )
        except IndeterminateLifecycleError:
            lifecycle_indeterminate = True
            raise
        prior_boot = _wait_for_new_boot(
            client,
            prior_boot,
            endpoint=endpoint,
            timeout=lifecycle_timeout,
        )
        verify_instance_identity(identity)
        return prior_boot

    def verify_backup_created(path: Path, purpose: str) -> str:
        if not path.exists() or path.is_symlink():
            raise ProbeError(f"{purpose} did not create the exact non-symlink destination")
        if path.resolve(strict=True) != path:
            raise ProbeError(f"{purpose} destination parent changed")
        return digest_backup_path(path)

    def cleanup_live_without_restore() -> None:
        for path in residue_paths:
            _remove_probe(client, path, "qualification guest cleanup")
        _clear_qualification_property(client)
        _assert_qualification_absent(client, residue_paths)

    try:
        cleanup_live_without_restore()
        live_dirty = True
        anchor_before = _write_probe(
            client,
            anchor_path,
            anchor_before_payload,
            mode="0600",
        )
        data_anchor_before = _write_probe(
            client,
            data_anchor_path,
            data_anchor_before_payload,
            mode="0600",
            selinux_context=None,
        )
        run_lifecycle("backup", "challenge backup creation", challenge_backup)
        challenge_before = verify_backup_created(
            challenge_backup,
            "challenge backup command",
        )
        challenge_ready = True
        challenge_created_boot_id_sha256 = _sha256(prior_boot)
        if _remote_file_evidence(client, anchor_path) != anchor_before:
            raise ProbeError("backup lifecycle changed the pre-backup recovery anchor")
        if _remote_file_evidence(client, data_anchor_path) != data_anchor_before:
            raise ProbeError("backup lifecycle changed the pre-backup /data recovery anchor")
        after_backup = collect_report(client, QualificationEvidence())
        if stable_target_digest(after_backup) != baseline_digest:
            raise ProbeError("backup lifecycle changed the qualified target contract")

        anchor_mutated = _replace_probe(
            client,
            anchor_path,
            anchor_mutated_payload,
            mode="0600",
        )
        data_anchor_mutated = _replace_probe(
            client,
            data_anchor_path,
            data_anchor_mutated_payload,
            mode="0600",
            selinux_context=None,
        )
        probe_evidence = _write_probe(client, probe_path, probe)
        helper_evidence = _write_probe(client, helper_path, helper, mode="0755")
        marker_evidence = _write_probe(client, marker_path, marker, mode="0600")
        data_marker_evidence = _write_probe(
            client,
            data_marker_path,
            data_marker,
            mode="0600",
            selinux_context=None,
        )
        for _ in range(3):
            run_lifecycle("cold_boot", "cold boot")
            if _remote_file_evidence(client, anchor_path) != anchor_mutated:
                raise ProbeError("mutated recovery anchor did not persist through cold boot")
            if _remote_file_evidence(client, data_anchor_path) != data_anchor_mutated:
                raise ProbeError("mutated /data recovery anchor did not persist through cold boot")
            if _remote_file_evidence(client, marker_path) != marker_evidence:
                raise ProbeError("recovery marker did not persist through cold boot")
            if _remote_file_evidence(client, data_marker_path) != data_marker_evidence:
                raise ProbeError("/data recovery marker did not persist through cold boot")
            proof = _verify_init_exec_probe(
                client,
                probe=probe_evidence,
                helper=helper_evidence,
                result_paths=result_paths,
                nonce=nonce,
                boot_id=prior_boot,
            )
            cold_boot_ids.append(_sha256(prior_boot))
            exec_proofs.append(proof)

        _restore_unchanged_backup(
            challenge_backup,
            challenge_before,
            "challenge",
            lambda: run_lifecycle("restore", "challenge restore", challenge_backup),
        )
        anchor_after_restore = _remote_file_evidence(client, anchor_path)
        if anchor_after_restore != anchor_before:
            raise ProbeError("challenge restore did not recover the exact random anchor")
        data_anchor_after_restore = _remote_file_evidence(client, data_anchor_path)
        if data_anchor_after_restore != data_anchor_before:
            raise ProbeError("challenge restore did not recover the exact random /data anchor")
        challenge_restored_boot_id_sha256 = _sha256(prior_boot)

        # The challenge snapshot intentionally contains the anchor. It is never
        # authorization evidence. Clean the live target, prove its original
        # state, and create a separate final backup with zero qualification
        # files or property residue.
        cleanup_live_without_restore()
        if _inventory_digest(_boot_critical_inventory(client, selected_init)) != baseline_inventory_sha256:
            raise ProbeError("challenge cleanup did not return boot-critical baseline state")
        clean_report = collect_report(client, QualificationEvidence())
        if stable_target_digest(clean_report) != baseline_digest:
            raise ProbeError("challenge cleanup did not return the baseline target contract")
        challenge_verified = True
        live_dirty = False

        live_dirty = True
        run_lifecycle("backup", "clean backup creation", canonical_backup)
        final_before = verify_backup_created(canonical_backup, "clean backup command")
        final_ready = True
        final_created_boot_id_sha256 = _sha256(prior_boot)
        if final_before == challenge_before:
            raise ProbeError("clean backup did not differ from the challenged backup")
        _assert_qualification_absent(client, residue_paths)
        if _inventory_digest(_boot_critical_inventory(client, selected_init)) != baseline_inventory_sha256:
            raise ProbeError("clean backup lifecycle changed boot-critical state")
        after_clean_backup = collect_report(client, QualificationEvidence())
        if stable_target_digest(after_clean_backup) != baseline_digest:
            raise ProbeError("clean backup lifecycle changed the baseline target contract")
        final_verified = True
        live_dirty = False

        live_dirty = True
        final_marker_evidence = _write_probe(
            client,
            final_marker_path,
            final_marker,
            mode="0600",
        )
        data_final_marker_evidence = _write_probe(
            client,
            data_final_marker_path,
            data_final_marker,
            mode="0600",
            selinux_context=None,
        )
        _restore_unchanged_backup(
            canonical_backup,
            final_before,
            "clean external",
            lambda: run_lifecycle("restore", "clean backup restore", canonical_backup),
        )
        _assert_qualification_absent(client, residue_paths)
        final_after = final_before
        restored_report = collect_report(client, QualificationEvidence())
        restored_digest = stable_target_digest(restored_report)
        if restored_digest != baseline_digest:
            raise ValueError("clean external restore did not return the baseline target contract")
        restored_inventory = _boot_critical_inventory(client, selected_init)
        if _inventory_digest(restored_inventory) != baseline_inventory_sha256:
            raise ValueError("clean external restore did not return boot-critical bytes and metadata")
        restored_boot_id_sha256 = _sha256(prior_boot)
        live_dirty = False
        restored = True
        qualification_succeeded = True
    finally:
        original_error = sys.exc_info()[1]
        cleanup_error: BaseException | None = None
        cleanup_verified = not live_dirty or restored
        if live_dirty and not restored and not lifecycle_indeterminate:
            recovery_errors: list[BaseException] = []
            candidates = _failure_recovery_candidates(
                final_verified=final_verified,
                challenge_verified=challenge_verified,
                canonical_backup=canonical_backup,
                challenge_backup=challenge_backup,
                final_digest=final_before,
                challenge_digest=challenge_before,
            )

            def verify_failure_baseline(label: str) -> None:
                if _inventory_digest(_boot_critical_inventory(client, selected_init)) != baseline_inventory_sha256:
                    raise ProbeError(f"failure recovery {label} inventory mismatch")
                recovery_report = collect_report(client, QualificationEvidence())
                if stable_target_digest(recovery_report) != baseline_digest:
                    raise ProbeError(f"failure recovery {label} contract mismatch")

            def recover_candidate(recovery: tuple[str, Path, str]) -> None:
                recovery_name, recovery_path, recovery_digest = recovery
                purpose = f"failure recovery {recovery_name} restore"
                _restore_unchanged_backup(
                    recovery_path,
                    recovery_digest,
                    f"failure recovery {recovery_name}",
                    lambda: run_lifecycle("restore", purpose, recovery_path),
                )
                if recovery_name == "clean":
                    _assert_qualification_absent(client, residue_paths)
                else:
                    cleanup_live_without_restore()
                verify_failure_baseline(recovery_name)

            def direct_cleanup() -> None:
                cleanup_live_without_restore()
                verify_failure_baseline("direct cleanup")

            try:
                _recover_from_candidates(candidates, recover_candidate, direct_cleanup)
                live_dirty = False
                cleanup_verified = True
            except (OSError, ProbeError, ValueError) as exc:
                recovery_errors.append(exc)
                cleanup_error = recovery_errors[-1]
        cleanup_error = _remove_verified_qualification_backups(
            challenge_backup,
            canonical_backup,
            final_ready=final_ready,
            qualification_succeeded=qualification_succeeded,
            recovery_verified=cleanup_verified,
            lifecycle_indeterminate=lifecycle_indeterminate,
            cleanup_error=cleanup_error,
        )
        if lifecycle_indeterminate:
            raise IndeterminateLifecycleError(
                "host lifecycle state is indeterminate; automatic guest restore and host backup cleanup were suppressed"
            )
        if cleanup_error is not None:
            raise ProbeError(
                "qualification failed and automatic cleanup could not be verified: "
                f"{original_error}; cleanup: {cleanup_error}"
            ) from cleanup_error

    record: dict[str, Any] = {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "record_id": record_id,
        "generated_at": (
            dt.datetime.now(dt.timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "adapter": {
            "id": adapter.adapter_id,
            "execution": adapter.execution,
            "clone_authorized": False,
        },
        "source": {
            "repository_commit": baseline["source"]["repository_commit"],
            "probe_sha256": baseline["source"]["probe_sha256"],
        },
        "target": {
            "serial_sha256": baseline["source"]["serial_sha256"],
            "fingerprint_sha256": baseline["device"]["fingerprint_sha256"],
            "api": baseline["device"]["api"],
            "abis": baseline["device"]["abis"],
            "baseline_contract_sha256": baseline_digest,
            "baseline_inventory": baseline_inventory,
            "baseline_inventory_sha256": baseline_inventory_sha256,
        },
        "instance_identity": identity,
        "commands": commands,
        "backup": {
            "snapshot_id": snapshot_id,
            "location": str(canonical_backup),
            "sha256_before": final_before,
            "sha256_after": final_after,
            "backup_command": shlex.join(
                _materialize_command(commands["backup"], canonical_backup)
            ),
            "restore_command": shlex.join(
                _materialize_command(commands["restore"], canonical_backup)
            ),
            "created_boot_id_sha256": final_created_boot_id_sha256,
            "qualification_residue": "absent",
        },
        "challenge": {
            "sha256_before": challenge_before,
            "sha256_after": challenge_before,
            "created_boot_id_sha256": challenge_created_boot_id_sha256,
            "restored_boot_id_sha256": challenge_restored_boot_id_sha256,
            "anchor_before": anchor_before,
            "anchor_mutated": anchor_mutated,
            "anchor_after_restore": anchor_after_restore,
            "marker": marker_evidence,
            "data_anchor_before": data_anchor_before,
            "data_anchor_mutated": data_anchor_mutated,
            "data_anchor_after_restore": data_anchor_after_restore,
            "data_marker": data_marker_evidence,
            "temporary_backup_removed": True,
        },
        "init": {
            "directory": selected_init,
            "probe": probe_evidence,
            "helper": helper_evidence,
            "property": QUALIFICATION_PROPERTY,
            "nonce_sha256": _sha256(nonce),
            "domains": {
                "init": "u:r:init:s0",
                "magisk": "u:r:magisk:s0",
            },
        },
        "persistence": {
            "backing_write_probe": "passed",
            "boot_evidence": "host-command-new-boot-id-and-init-and-magisk-exec",
            "cold_boot_command": shlex.join(commands["cold_boot"]["argv"]),
            "cold_boot_ids": cold_boot_ids,
            "host_restart_ids": [],
            "exec_proofs": exec_proofs,
        },
        "recovery": {
            "marker": final_marker_evidence,
            "data_marker": data_final_marker_evidence,
            "restore_executed": True,
            "qualification_paths_absent_after_restore": True,
            "restored_boot_id_sha256": restored_boot_id_sha256,
            "target_contract_sha256_after_restore": restored_digest,
            "inventory_after_restore": restored_inventory,
            "inventory_sha256_after_restore": _inventory_digest(restored_inventory),
        },
    }
    _seal_record(record, canonical_seal_key)
    validate_qualification_record(record)
    _verify_record_seal(record, canonical_seal_key)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(canonical_output, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical_bytes(record))
            stream.flush()
            os.fsync(stream.fileno())
        parent_descriptor = os.open(canonical_output.parent, os.O_RDONLY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except BaseException:
        canonical_output.unlink(missing_ok=True)
        raise
    return record
