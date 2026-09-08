#!/usr/bin/env python3
"""Read-only System Mode capability probe and deterministic classifier.

The collector intentionally does not remount filesystems, create marker files, or
change SELinux policy.  Evidence that requires mutation or a lifecycle operation
is accepted only as an explicit, externally supplied qualification input.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from typing import Any, Iterable, Mapping, Sequence


CONTRACT_VERSION = 1
PROBE_VERSION = "1.0.0"
STAGING_REQUIRED_BYTES = 32 * 1024 * 1024
TARGET_PATHS = ("/", "/system", "/vendor", "/odm", "/product", "/system_ext")
INIT_DIRECTORIES = (
    "/system/etc/init",
    "/system/etc/init/hw",
)
POLICY_CANDIDATES = (
    "/sepolicy",
    "/sepolicy_debug",
    "/vendor/etc/selinux/precompiled_sepolicy",
    "/odm/etc/selinux/precompiled_sepolicy",
    "/system/etc/selinux/plat_sepolicy.cil",
    "/system/etc/selinux/mapping",
    "/vendor/etc/selinux/vendor_sepolicy.cil",
    "/odm/etc/selinux/odm_sepolicy.cil",
    "/product/etc/selinux/product_sepolicy.cil",
    "/system_ext/etc/selinux/system_ext_sepolicy.cil",
    "/vendor/etc/selinux/precompiled_sepolicy.plat_sepolicy_and_mapping.sha256",
    "/vendor/etc/selinux/precompiled_sepolicy.system_ext_sepolicy_and_mapping.sha256",
    "/vendor/etc/selinux/precompiled_sepolicy.product_sepolicy_and_mapping.sha256",
    "/odm/etc/selinux/precompiled_sepolicy.plat_sepolicy_and_mapping.sha256",
    "/odm/etc/selinux/precompiled_sepolicy.system_ext_sepolicy_and_mapping.sha256",
    "/odm/etc/selinux/precompiled_sepolicy.product_sepolicy_and_mapping.sha256",
)
EMULATOR_VERSION_PROPERTIES = (
    "nemud.player_version",
    "ro.ldplayer.version",
    "ro.nox.version",
    "ro.bluestacks.version",
)
EMULATOR_PRODUCT_PROPERTIES = (
    "nemud.player_engine",
    "nemud.player_package",
    "ro.ldplayer.channel",
    "ro.nox.product",
    "ro.bluestacks.product",
)
INSTALL_CONFIG = "/system/etc/init/magisk/config"
MANIFEST_CANDIDATES = (
    "/system/etc/init/magisk/install-manifest.json",
    "/system/etc/init/magisk/manifest.json",
)

_ROOT = Path(__file__).resolve().parents[2]
_REASON_CODES_PATH = Path(__file__).with_name("contracts") / "reason-codes.json"


class ProbeError(RuntimeError):
    """Raised when required read-only probe data cannot be collected."""


@dataclasses.dataclass(frozen=True)
class QualificationEvidence:
    """Evidence produced outside the read-only doctor invocation."""

    init_import_proven: bool = False
    snapshot_id: str | None = None
    backup_location: str | None = None
    backup_digest: str | None = None
    restore_command: str | None = None
    recovery_verified: bool = False
    backing_write_probe: str = "not_run"
    cold_boots: int = 0
    host_restarts: int = 0
    qualification_record: str | None = None
    qualification_sha256: str | None = None
    adapter_id: str | None = None
    instance_identity_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.backing_write_probe not in {"not_run", "passed", "failed"}:
            raise ValueError("backing_write_probe must be not_run, passed, or failed")
        if self.cold_boots < 0 or self.host_restarts < 0:
            raise ValueError("boot and host restart counts cannot be negative")
        if self.backup_digest is not None and not re.fullmatch(r"[a-fA-F0-9]{64}", self.backup_digest):
            raise ValueError("backup_digest must be a SHA-256 hex digest")
        for field_name, digest in (
            ("qualification_sha256", self.qualification_sha256),
            ("instance_identity_sha256", self.instance_identity_sha256),
        ):
            if digest is not None and not re.fullmatch(r"[a-fA-F0-9]{64}", digest):
                raise ValueError(f"{field_name} must be a SHA-256 hex digest")
        if self.qualification_record is not None and not Path(self.qualification_record).is_absolute():
            raise ValueError("qualification_record must be an absolute host path")

    @property
    def recovery_is_proven(self) -> bool:
        return bool(
            self.recovery_verified
            and self.snapshot_id
            and self.backup_location
            and self.backup_digest
            and self.restore_command
            and self.qualification_record
            and self.qualification_sha256
            and self.adapter_id
            and self.instance_identity_sha256
        )

    @property
    def persistence_is_proven(self) -> bool:
        return self.backing_write_probe == "passed" and self.cold_boots >= 3


@dataclasses.dataclass(frozen=True)
class CommandResult:
    stdout: str
    stderr: str
    returncode: int


class AdbClient:
    """Small ADB transport with explicit serial selection and timeouts."""

    def __init__(self, adb: str = "adb", serial: str | None = None, timeout: int = 15):
        self.adb = adb
        self.serial = serial
        self.timeout = timeout

    def _command(self, *args: str, timeout: int | None = None) -> CommandResult:
        command = [self.adb]
        if self.serial:
            command += ["-s", self.serial]
        command += list(args)
        try:
            proc = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProbeError(f"ADB command failed: {exc}") from exc
        return CommandResult(
            stdout=proc.stdout.replace("\r\n", "\n").rstrip("\n"),
            stderr=proc.stderr.replace("\r\n", "\n").rstrip("\n"),
            returncode=proc.returncode,
        )

    def connect(self, endpoint: str) -> None:
        result = self._command("connect", endpoint, timeout=30)
        combined = f"{result.stdout}\n{result.stderr}".lower()
        if result.returncode != 0 or "unable" in combined or "failed" in combined:
            raise ProbeError(f"cannot connect to {endpoint}: {combined.strip()}")
        self.serial = endpoint

    def resolve_serial(self) -> str:
        """Pin an implicit single-device transport to its concrete ADB serial."""

        if self.serial:
            return self.serial
        result = self._command("get-serialno")
        serial = result.stdout.strip()
        if result.returncode != 0 or not serial or serial in {"unknown", "offline"}:
            raise ProbeError(result.stderr or "could not resolve the exact ADB target serial")
        self.serial = serial
        return serial

    def wait_for_device(self) -> None:
        result = self._command("wait-for-device", timeout=30)
        if result.returncode != 0:
            raise ProbeError(result.stderr or "ADB target did not become ready")

    def push(self, source: str, destination: str) -> None:
        result = self._command("push", source, destination, timeout=30)
        if result.returncode != 0:
            raise ProbeError(result.stderr or f"could not push {source} to {destination}")

    def install_replace(self, apk: Path) -> None:
        """Install one exact APK through the pinned transport using ``adb install -r``."""

        result = self._command("install", "-r", str(apk), timeout=180)
        if result.returncode != 0 or "success" not in result.stdout.lower():
            raise ProbeError(result.stderr or result.stdout or "adb install -r failed")

    def start_activity(self, component: str, *, string_extras: Mapping[str, str] | None = None) -> None:
        command = ["shell", "am", "start", "-W", "-n", component]
        for key, value in (string_extras or {}).items():
            command += ["--es", key, value]
        result = self._command(*command, timeout=30)
        combined = f"{result.stdout}\n{result.stderr}".lower()
        if result.returncode != 0 or "error:" in combined or "exception" in combined:
            raise ProbeError(result.stderr or result.stdout or "could not launch manager activity")

    def reverse_tcp(self, host_port: int) -> int:
        """Create an ADB-transport-scoped reverse mapping with a dynamic device port."""

        if not 1 <= host_port <= 65535:
            raise ValueError("host reverse port is outside the TCP port range")
        result = self._command("reverse", "tcp:0", f"tcp:{host_port}", timeout=30)
        if result.returncode != 0:
            raise ProbeError(result.stderr or "could not create ADB reverse lease")
        output = result.stdout.strip()
        match = re.search(r"(?:tcp:)?(\d+)$", output)
        if not match:
            raise ProbeError("ADB did not return the allocated reverse port")
        device_port = int(match.group(1))
        if not 1 <= device_port <= 65535:
            raise ProbeError("ADB returned an invalid reverse port")
        return device_port

    def remove_reverse_tcp(self, device_port: int) -> None:
        result = self._command("reverse", "--remove", f"tcp:{device_port}", timeout=30)
        if result.returncode != 0:
            raise ProbeError(result.stderr or "could not remove ADB reverse lease")

    def shell(self, command: str, *, root: bool = False, timeout: int | None = None) -> CommandResult:
        if root:
            command = f"su -c {shlex.quote(command)}"
        return self._command("shell", command, timeout=timeout)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()


def _parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_id(output: str) -> tuple[int | None, str | None]:
    uid_match = re.search(r"(?:^|\s)uid=(\d+)", output)
    context_match = re.search(r"(?:^|\s)context=([^\s]+)", output)
    uid = int(uid_match.group(1)) if uid_match else None
    context = context_match.group(1) if context_match else None
    return uid, context


def _parse_getprop(output: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in output.splitlines():
        match = re.fullmatch(r"\[([^]]+)\]: \[(.*)\]", line)
        if match:
            props[match.group(1)] = match.group(2)
    return props


def _unescape_mountinfo(value: str) -> str:
    # proc_pid_mountinfo(5) octal-escapes whitespace and backslashes in path
    # fields. Interpret only the four escapes defined by the kernel interface.
    return (
        value.replace(r"\040", " ")
        .replace(r"\011", "\t")
        .replace(r"\012", "\n")
        .replace(r"\134", "\\")
    )


def parse_mountinfo(output: str) -> list[dict[str, Any]]:
    """Parse Linux mountinfo without collapsing bind mounts or superblock flags."""

    mounts: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        if " - " not in raw_line:
            continue
        before, after = raw_line.split(" - ", 1)
        left = before.split()
        right = after.split()
        if len(left) < 6 or len(right) < 3:
            continue
        mounts.append(
            {
                "mount_id": left[0],
                "parent_id": left[1],
                "major_minor": left[2],
                "root": _unescape_mountinfo(left[3]),
                "mount_point": _unescape_mountinfo(left[4]),
                "mount_options": left[5].split(","),
                "optional_fields": left[6:],
                "fs_type": right[0],
                "source": _unescape_mountinfo(right[1]),
                "super_options": right[2].split(","),
            }
        )
    return mounts


def _path_is_below(path: str, mount_point: str) -> bool:
    if mount_point == "/":
        return path.startswith("/")
    normalized = mount_point.rstrip("/")
    return path == normalized or path.startswith(normalized + "/")


def _effective_mount(path: str, mounts: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    selected: Mapping[str, Any] | None = None
    selected_length = -1
    for mount in mounts:
        mount_point = str(mount["mount_point"])
        if not _path_is_below(path, mount_point):
            continue
        length = len(mount_point)
        # Later mountinfo entries win ties so a stacked mount at the same path
        # cannot expose the hidden lower filesystem as the effective view.
        if length >= selected_length:
            selected = mount
            selected_length = length
    return selected


def _probe_paths(client: AdbClient, paths: Iterable[str], *, root: bool) -> dict[str, dict[str, Any]]:
    quoted = " ".join(shlex.quote(path) for path in paths)
    command = f"""
for path in {quoted}; do
  kind=missing
  [ -d "$path" ] && kind=directory
  [ -f "$path" ] && kind=file
  readable=false
  writable=false
  [ -r "$path" ] && readable=true
  [ -w "$path" ] && writable=true
  resolved=$(readlink -f "$path" 2>/dev/null || printf '%s' "$path")
  uid=$(stat -c %u "$path" 2>/dev/null || printf '?')
  mode=$(stat -c %a "$path" 2>/dev/null || printf '?')
  printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' "$path" "$kind" "$readable" "$writable" "$resolved" "$uid" "$mode"
done
""".strip()
    result = client.shell(command, root=root)
    if result.returncode != 0:
        raise ProbeError(result.stderr or "path capability probe failed")
    records: dict[str, dict[str, Any]] = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 7:
            continue
        path, kind, readable, writable, resolved_path, uid, mode = fields
        records[path] = {
            "path": path,
            "resolved_path": resolved_path or path,
            "exists": kind != "missing",
            "kind": kind,
            "readable": readable == "true",
            "permission_writable": writable == "true",
            "uid": int(uid) if uid.isdigit() else None,
            "mode": mode.zfill(4) if re.fullmatch(r"[0-7]{3,4}", mode) else None,
        }
    return records


def _read_optional(client: AdbClient, path: str, *, root: bool) -> str | None:
    result = client.shell(f"test -r {shlex.quote(path)} && cat {shlex.quote(path)}", root=root)
    return result.stdout if result.returncode == 0 and result.stdout else None


def _df_available_bytes(client: AdbClient, path: str) -> int:
    result = client.shell(f"df -Pk {shlex.quote(path)}")
    if result.returncode != 0:
        return 0
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        return 0
    fields = lines[-1].split()
    if len(fields) < 4:
        return 0
    return _parse_int(fields[3]) * 1024


def _ext4_features(
    client: AdbClient,
    source: str | None,
    *,
    root: bool,
) -> tuple[list[str], str, str | None, int | None]:
    if not source or not source.startswith("/"):
        return [], "unavailable", None, None

    tune_command = (
        f"tune2fs -l {shlex.quote(source)} 2>/dev/null "
        "| grep '^Filesystem features:'"
    )
    tune_result = client.shell(tune_command, root=root)
    tune_observed = tune_result.returncode == 0 and ":" in tune_result.stdout
    features = (
        tune_result.stdout.split(":", 1)[1].strip().split()
        if tune_observed
        else []
    )

    # Android guests often omit tune2fs. Read only the ext4 magic and
    # s_feature_ro_compat word from the documented superblock offsets so
    # shared-block images can still be rejected without running fsck/remount.
    quoted = shlex.quote(source)
    raw_command = f"""
device={quoted}
magic=$(dd if="$device" bs=1 skip=1080 count=2 2>/dev/null | od -An -tx2 | tr -d '[:space:]')
flags=$(dd if="$device" bs=1 skip=1124 count=4 2>/dev/null | od -An -tu4 | tr -d '[:space:]')
printf '%s %s' "$magic" "$flags"
""".strip()
    raw_result = client.shell(raw_command, root=root)
    raw_flags: int | None = None
    raw_fields = raw_result.stdout.split()
    if raw_result.returncode == 0 and len(raw_fields) == 2:
        magic, flags = raw_fields
        if magic.lower() == "ef53" and flags.isdigit():
            raw_flags = int(flags)
            if raw_flags & 0x4000 and "shared_blocks" not in features:
                features.append("shared_blocks")

    if tune_observed and raw_flags is not None:
        method = "tune2fs+superblock"
    elif tune_observed:
        method = "tune2fs"
    elif raw_flags is not None:
        method = "superblock"
    else:
        method = None
    status = "observed" if method else "unavailable"
    return features, status, method, raw_flags


def _device_mapper_report(
    client: AdbClient,
    sources: Iterable[str],
    *,
    root: bool,
) -> dict[str, Any]:
    candidates = sorted(
        {
            source
            for source in sources
            if source.startswith("/dev/block/dm-") or "/dev/block/mapper/" in source
        }
    )
    if not candidates:
        return {"probe": "not_applicable", "layers": []}

    layers: list[dict[str, Any]] = []
    for source in candidates:
        quoted = shlex.quote(source)
        command = f"""
source={quoted}
real=$(readlink -f "$source" 2>/dev/null) || exit 1
base=${{real##*/}}
case "$base" in dm-*) ;; *) exit 1 ;; esac
name=$(cat "/sys/class/block/$base/dm/name" 2>/dev/null)
uuid=$(cat "/sys/class/block/$base/dm/uuid" 2>/dev/null)
slaves=$(ls "/sys/class/block/$base/slaves" 2>/dev/null | tr '\n' ',' | sed 's/,$//')
target_probe=unavailable
targets=
if command -v dmsetup >/dev/null 2>&1; then
  table=$(dmsetup table "$real" 2>/dev/null)
  if [ $? -eq 0 ]; then
    target_probe=observed
    targets=$(printf '%s\n' "$table" | awk '{{print $3}}' | sort -u | tr '\n' ',' | sed 's/,$//')
  fi
fi
printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$source" "$real" "$name" "$uuid" "$slaves" "$target_probe" "$targets"
""".strip()
        result = client.shell(command, root=root)
        if result.returncode != 0:
            continue
        fields = result.stdout.splitlines()[-1].split("\t") if result.stdout else []
        if len(fields) != 7:
            continue
        original, resolved, name, uuid, slaves, target_probe, target_types = fields
        layers.append(
            {
                "source": original,
                "resolved_source": resolved,
                "name": name or None,
                "uuid": uuid or None,
                "slaves": [value for value in slaves.split(",") if value],
                "target_probe": target_probe,
                "target_types": [value for value in target_types.split(",") if value],
            }
        )

    if not layers:
        probe = "unavailable"
    elif len(layers) == len(candidates):
        probe = "observed"
    else:
        probe = "partial"
    return {"probe": probe, "layers": layers}


def _policy_file_metadata(
    client: AdbClient,
    records: Sequence[Mapping[str, Any]],
    *,
    root: bool,
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for record in records:
        item = dict(record)
        item["sha256"] = None
        item["size"] = None
        if record.get("kind") == "file" and record.get("readable"):
            path = str(record["path"])
            result = client.shell(
                f"sha256sum {shlex.quote(path)} 2>/dev/null; stat -c %s {shlex.quote(path)} 2>/dev/null",
                root=root,
            )
            lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if lines:
                digest = lines[0].split()[0]
                if re.fullmatch(r"[a-fA-F0-9]{64}", digest):
                    item["sha256"] = digest.lower()
            if len(lines) > 1:
                size = _parse_int(lines[-1], -1)
                if size >= 0:
                    item["size"] = size
        enriched.append(item)
    return enriched


def _repository_state(repository_root: Path) -> tuple[str, bool]:
    try:
        commit_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        status_proc = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=repository_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return commit_proc.stdout.strip(), bool(status_proc.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return "0" * 40, True


def _probe_digest() -> str:
    root = Path(__file__).parent
    paths = [root / "doctor.py", root / "kitsune.py"]
    paths.extend(sorted((root / "contracts").glob("*.json")))
    paths.extend(sorted((root / "schemas").glob("*.json")))
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _infer_vendor(props: Mapping[str, str], mountinfo: str) -> tuple[str, list[str]]:
    haystack = "\n".join(
        [
            props.get("ro.product.manufacturer", ""),
            props.get("ro.product.brand", ""),
            props.get("ro.product.model", ""),
            props.get("ro.hardware", ""),
            props.get("ro.build.fingerprint", ""),
            mountinfo,
        ]
    ).lower()
    rules = (
        ("mumu", ("mumu12shared", "mumuplayer", "netease mumu", "mumu")),
        ("ldplayer", ("ldplayer", "dnplayer", "leidian")),
        ("nox", ("nox", "bignox")),
        ("bluestacks", ("bluestacks", "bstsharedfolder", "bstk")),
        ("android-studio-avd", ("ranchu", "goldfish")),
        ("waydroid", ("waydroid",)),
    )
    for vendor, markers in rules:
        hits = [marker for marker in markers if marker in haystack]
        if hits:
            return vendor, hits
    manufacturer = props.get("ro.product.manufacturer", "").strip().lower()
    return (manufacturer or "unknown"), ([f"manufacturer:{manufacturer}"] if manufacturer else [])


def _mount_record(
    target: str,
    resolved_path: str,
    exists: bool,
    mount: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if mount is None:
        return {
            "target": target,
            "resolved_path": resolved_path,
            "exists": exists,
            "mount_point": None,
            "root": None,
            "source": None,
            "fs_type": None,
            "mount_options": [],
            "super_options": [],
            "major_minor": None,
            "writable_view": False,
            "writable_backing": False,
        }
    mount_options = list(mount["mount_options"])
    super_options = list(mount["super_options"])
    return {
        "target": target,
        "resolved_path": resolved_path,
        "exists": exists,
        "mount_point": mount["mount_point"],
        "root": mount["root"],
        "source": mount["source"],
        "fs_type": mount["fs_type"],
        "mount_options": mount_options,
        "super_options": super_options,
        "major_minor": mount["major_minor"],
        "writable_view": "rw" in mount_options,
        "writable_backing": "rw" in super_options,
    }


def _selinux_strategy(path_records: Mapping[str, Mapping[str, Any]], enabled: bool) -> str:
    if not enabled:
        return "disabled"
    if any(
        path_records.get(path, {}).get("kind") == "file"
        and path_records.get(path, {}).get("readable")
        for path in ("/sepolicy", "/sepolicy_debug")
    ):
        return "monolithic"
    if any(
        Path(path).name == "precompiled_sepolicy"
        and record.get("kind") == "file"
        and record.get("readable")
        for path, record in path_records.items()
    ):
        return "precompiled"
    if any(
        path.endswith(".cil")
        and record.get("kind") == "file"
        and record.get("readable")
        for path, record in path_records.items()
    ):
        return "split"
    return "unknown"


def _trusted_init_directory(record: Mapping[str, Any]) -> bool:
    mode = record.get("mode")
    return (
        record.get("kind") == "directory"
        and record.get("readable") is True
        and record.get("permission_writable") is True
        and record.get("resolved_path") == record.get("path")
        and type(record.get("uid")) is int
        and record["uid"] == 0
        and isinstance(mode, str)
        and re.fullmatch(r"[0-7]{4}", mode) is not None
        and int(mode, 8) & 0o022 == 0
    )


def _select_init_directory(candidates: Iterable[Mapping[str, Any]]) -> str | None:
    """Select a writable init directory with the installer's ownership rules."""

    return next(
        (str(record["path"]) for record in candidates if _trusted_init_directory(record)),
        None,
    )


def _avb_state(props: Mapping[str, str]) -> str:
    verified = props.get("ro.boot.verifiedbootstate", "").lower()
    verity = props.get("ro.boot.veritymode", "").lower()
    locked = props.get("ro.boot.flash.locked", "")
    device_state = props.get("ro.boot.vbmeta.device_state", "").lower()
    if verity in {"enforcing", "restart"} or (verified == "green" and locked == "1"):
        return "enforcing"
    if verified == "orange" or locked == "0" or device_state == "unlocked":
        return "unlocked"
    if verity in {"disabled", "logging"} or verified in {"red", "yellow"}:
        return "disabled"
    return "unknown"


def collect_report(
    client: AdbClient,
    evidence: QualificationEvidence | None = None,
    *,
    repository_root: Path = _ROOT,
) -> dict[str, Any]:
    """Collect and classify one read-only report from an ADB target."""

    evidence = evidence or QualificationEvidence()
    client.resolve_serial()
    client.wait_for_device()

    prop_result = client.shell("getprop")
    mount_result = client.shell("cat /proc/self/mountinfo")
    id_result = client.shell("id")
    if prop_result.returncode or mount_result.returncode or id_result.returncode:
        raise ProbeError("required getprop, mountinfo, or identity probe failed")

    props = _parse_getprop(prop_result.stdout)
    mounts_raw = parse_mountinfo(mount_result.stdout)
    if not props or not mounts_raw:
        raise ProbeError("required property or mountinfo output was empty")

    adb_uid, adb_context = _parse_id(id_result.stdout)
    if adb_uid is None:
        raise ProbeError("could not parse adb shell identity")

    root_uid: int | None = None
    root_context: str | None = None
    root_available = adb_uid == 0
    if root_available:
        root_uid, root_context = adb_uid, adb_context
    else:
        try:
            root_result = client.shell("id", root=True, timeout=10)
        except ProbeError:
            root_result = CommandResult("", "root probe timed out", 1)
        if root_result.returncode == 0:
            root_uid, root_context = _parse_id(root_result.stdout)
            root_available = root_uid == 0

    use_root = root_available and adb_uid != 0
    magisk_version: str | None = None
    magisk_version_code: int | None = None
    if root_available:
        version_result = client.shell("magisk -v 2>/dev/null; magisk -V 2>/dev/null", root=use_root)
        version_lines = [line.strip() for line in version_result.stdout.splitlines() if line.strip()]
        if version_lines:
            magisk_version = version_lines[0]
        if len(version_lines) > 1:
            parsed_code = _parse_int(version_lines[1], -1)
            magisk_version_code = parsed_code if parsed_code >= 0 else None

    if adb_uid == 0:
        transport = "root_adb"
    elif root_available and (magisk_version or (root_context and "magisk" in root_context)):
        transport = "magisk_su"
    elif root_available:
        transport = "vendor_su"
    else:
        transport = "none"

    all_paths = tuple(
        dict.fromkeys(
            TARGET_PATHS
            + INIT_DIRECTORIES
            + POLICY_CANDIDATES
            + ("/sys/fs/selinux", INSTALL_CONFIG)
            + MANIFEST_CANDIDATES
        )
    )
    path_records = _probe_paths(client, all_paths, root=use_root)

    mount_records = [
        _mount_record(
            target,
            path_records.get(target, {}).get("resolved_path", target),
            path_records.get(target, {}).get("exists", target == "/"),
            _effective_mount(path_records.get(target, {}).get("resolved_path", target), mounts_raw),
        )
        for target in TARGET_PATHS
    ]
    system_mount = next((record for record in mount_records if record["target"] == "/system" and record["exists"]), mount_records[0])

    fs_types = {str(record["fs_type"]) for record in mount_records if record["exists"]}
    sources = {str(record["source"]) for record in mount_records if record["exists"]}
    avb_state = _avb_state(props)
    dynamic_partitions = props.get("ro.boot.dynamic_partitions", "").lower() == "true" or any(
        source.startswith("/dev/block/dm-") or "/mapper/" in source for source in sources
    )
    (
        ext4_features,
        ext4_features_probe,
        ext4_features_method,
        ext4_ro_compat_flags,
    ) = _ext4_features(
        client,
        system_mount["source"] if system_mount["fs_type"] == "ext4" else None,
        root=use_root,
    )
    device_mapper = _device_mapper_report(client, sources, root=use_root)
    dm_verity_observed = any(
        "verity" in layer["target_types"] or "verity" in str(layer.get("uuid") or "").lower()
        for layer in device_mapper["layers"]
    )

    config_present = path_records.get(INSTALL_CONFIG, {}).get("kind") == "file"
    config = _read_optional(client, INSTALL_CONFIG, root=use_root) if config_present else None
    system_mode = bool(config and re.search(r"(?m)^SYSTEMMODE=true$", config))
    manifest_path = next(
        (path for path in MANIFEST_CANDIDATES if path_records.get(path, {}).get("exists")),
        None,
    )
    active_magisk = bool(magisk_version or magisk_version_code is not None)
    ordinary_magisk = active_magisk and not system_mode
    existing_detected = bool(config_present or manifest_path or active_magisk)

    init_candidates = [
        path_records.get(
            path,
            {
                "path": path,
                "resolved_path": path,
                "exists": False,
                "kind": "missing",
                "readable": False,
                "permission_writable": False,
            },
        )
        for path in INIT_DIRECTORIES
    ]
    selected_init = _select_init_directory(init_candidates)
    init_proof = "proven" if evidence.init_import_proven else (
        "observed_existing_install" if system_mode else "unproven"
    )

    enforce_result = client.shell("getenforce 2>/dev/null", root=use_root)
    enforce_value = enforce_result.stdout.strip().lower()
    if enforce_value == "enforcing":
        selinux_state = "enforcing"
    elif enforce_value == "permissive":
        selinux_state = "permissive"
    elif not path_records.get("/sys/fs/selinux", {}).get("exists", False) and enforce_value in {"", "disabled"}:
        selinux_state = "disabled"
    else:
        selinux_state = "unknown"
    selinux_enabled = selinux_state not in {"disabled"}
    policy_records = _policy_file_metadata(
        client,
        [path_records[path] for path in POLICY_CANDIDATES if path_records.get(path, {}).get("exists")],
        root=use_root,
    )

    boot_id = _read_optional(client, "/proc/sys/kernel/random/boot_id", root=False) or "unknown"
    fingerprint = props.get("ro.build.fingerprint", "unknown")
    vendor, vendor_evidence = _infer_vendor(props, mount_result.stdout)
    emulator_version_property = next(
        (key for key in EMULATOR_VERSION_PROPERTIES if props.get(key)),
        None,
    )
    emulator_product_property = next(
        (key for key in EMULATOR_PRODUCT_PROPERTIES if props.get(key)),
        None,
    )
    serial = client.serial
    if not serial:
        raise ProbeError("exact ADB target serial was not pinned")
    staging_available = _df_available_bytes(client, "/data/local/tmp")
    repository_commit, repository_dirty = _repository_state(repository_root)
    abis = [abi for abi in props.get("ro.product.cpu.abilist", "").split(",") if abi]
    if not abis and props.get("ro.product.cpu.abi"):
        abis = [props["ro.product.cpu.abi"]]
    page_size = _parse_int(client.shell("getconf PAGESIZE").stdout.strip())
    if page_size < 1:
        page_size = _parse_int(client.shell("getconf PAGE_SIZE").stdout.strip())

    report: dict[str, Any] = {
        "schema_version": CONTRACT_VERSION,
        "probe_version": PROBE_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": {
            "kind": "adb",
            "read_only": True,
            "serial_sha256": _sha256(serial),
            "repository_commit": repository_commit,
            "repository_dirty": repository_dirty,
            "probe_sha256": _probe_digest(),
        },
        "device": {
            "vendor": vendor,
            "vendor_evidence": vendor_evidence,
            "manufacturer": props.get("ro.product.manufacturer", ""),
            "brand": props.get("ro.product.brand", ""),
            "model": props.get("ro.product.model", ""),
            "product": props.get("ro.product.name", ""),
            "device": props.get("ro.product.device", ""),
            "build_id": props.get("ro.build.id", ""),
            "build_incremental": props.get("ro.build.version.incremental", ""),
            "emulator_product": props.get(emulator_product_property, "") or None,
            "emulator_product_property": emulator_product_property,
            "emulator_version": props.get(emulator_version_property, "") or None,
            "emulator_version_property": emulator_version_property,
            "android_release": props.get("ro.build.version.release", ""),
            "api": _parse_int(props.get("ro.build.version.sdk", "")),
            "abis": abis,
            "page_size": page_size,
            "fingerprint_sha256": _sha256(fingerprint),
            "kernel": client.shell("uname -a").stdout.strip(),
            "boot_id_sha256": _sha256(boot_id.strip()),
        },
        "bootstrap": {
            "adb_uid": adb_uid,
            "root_available": root_available,
            "transport": transport,
            "root_uid": root_uid,
            "root_context": root_context,
            "magisk_version": magisk_version,
            "magisk_version_code": magisk_version_code,
        },
        "mounts": mount_records,
        "layout": {
            "system_target": system_mount["target"],
            "system_fs_type": system_mount["fs_type"],
            "system_source": system_mount["source"],
            "system_writable_view": system_mount["writable_view"],
            "system_writable_backing": system_mount["writable_backing"],
            "overlayfs": "overlay" in fs_types,
            "dynamic_partitions": dynamic_partitions,
            "device_mapper": device_mapper,
            "dm_verity": avb_state == "enforcing" or dm_verity_observed,
            "avb_state": avb_state,
            "slot_suffix": props.get("ro.boot.slot_suffix", ""),
            "ext4_features": ext4_features,
            "ext4_features_probe": ext4_features_probe,
            "ext4_features_method": ext4_features_method,
            "ext4_ro_compat_flags": ext4_ro_compat_flags,
            "shared_blocks": (
                "shared_blocks" in ext4_features
                if ext4_features_probe == "observed"
                else None
            ),
        },
        "init": {
            "candidate_directories": init_candidates,
            "selected_directory": selected_init,
            "import_proof": init_proof,
        },
        "selinux": {
            "enabled": selinux_enabled,
            "state": selinux_state,
            "policy_candidates": policy_records,
            "strategy": _selinux_strategy(path_records, selinux_enabled),
        },
        "staging": {
            "path": "/data/local/tmp",
            "available_bytes": staging_available,
            "required_bytes": STAGING_REQUIRED_BYTES,
            "sufficient": staging_available >= STAGING_REQUIRED_BYTES,
        },
        "recovery": {
            "snapshot_id": evidence.snapshot_id,
            "backup_location": evidence.backup_location,
            "backup_digest": evidence.backup_digest.lower() if evidence.backup_digest else None,
            "restore_command": evidence.restore_command,
            "qualification_record": evidence.qualification_record,
            "qualification_sha256": (
                evidence.qualification_sha256.lower()
                if evidence.qualification_sha256
                else None
            ),
            "adapter_id": evidence.adapter_id,
            "instance_identity_sha256": (
                evidence.instance_identity_sha256.lower()
                if evidence.instance_identity_sha256
                else None
            ),
            "verified": evidence.recovery_is_proven,
        },
        "persistence": {
            "backing_write_probe": evidence.backing_write_probe,
            "cold_boots": evidence.cold_boots,
            "host_restarts": evidence.host_restarts,
            "proven": evidence.persistence_is_proven,
        },
        "existing_install": {
            "detected": existing_detected,
            "active_magisk": active_magisk,
            "ordinary_magisk": ordinary_magisk,
            "system_mode": system_mode,
            "config_path": INSTALL_CONFIG if config_present else None,
            "manifest_path": manifest_path,
            "manifest_present": manifest_path is not None,
        },
        "assessment": {},
    }
    report["assessment"] = classify_report(report)
    validate_report(report)
    return report


def _reason_code_metadata() -> dict[str, Mapping[str, Any]]:
    data = json.loads(_REASON_CODES_PATH.read_text(encoding="utf-8"))
    return {item["code"]: item for item in data["codes"]}


def classify_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Classify a normalized report using stable, ordered reason codes."""

    reasons: list[str] = []
    warnings: list[str] = []
    bootstrap = report.get("bootstrap", {})
    layout = report.get("layout", {})
    staging = report.get("staging", {})
    selinux = report.get("selinux", {})
    init = report.get("init", {})
    recovery = report.get("recovery", {})
    persistence = report.get("persistence", {})
    existing = report.get("existing_install", {})

    if not bootstrap.get("root_available"):
        reasons.append("NO_BOOTSTRAP_ROOT")
    if _parse_int(report.get("device", {}).get("api", 0)) < 25:
        reasons.append("ANDROID_API_UNSUPPORTED")

    fs_type = str(layout.get("system_fs_type") or "").lower()
    if not fs_type:
        reasons.append("PROBE_INCOMPLETE")
    elif fs_type == "erofs":
        reasons.append("EROFS")
    elif fs_type == "squashfs":
        reasons.append("SQUASHFS")
    elif not layout.get("system_writable_view"):
        reasons.append("READ_ONLY_FS")
    elif not layout.get("system_writable_backing"):
        reasons.append("READ_ONLY_BACKING_FS")

    if fs_type == "ext4" and layout.get("ext4_features_probe") != "observed":
        reasons.append("EXT4_FEATURES_UNPROVEN")
    if layout.get("shared_blocks") is True:
        reasons.append("EXT4_SHARED_BLOCKS")

    device_mapper = layout.get("device_mapper", {})
    if (
        layout.get("dynamic_partitions") and device_mapper.get("probe") != "observed"
    ) or device_mapper.get("probe") in {"partial", "unavailable"} or any(
        layer.get("target_probe") != "observed"
        for layer in device_mapper.get("layers", [])
    ):
        reasons.append("DEVICE_MAPPER_UNPROVEN")

    if layout.get("overlayfs"):
        reasons.append("OVERLAY_WRITABLE_ONLY")
    if layout.get("dm_verity") or layout.get("avb_state") == "enforcing":
        reasons.append("VERITY_ACTIVE")
    elif layout.get("avb_state") == "unknown":
        warnings.append("AVB_STATE_UNKNOWN")

    if not staging.get("sufficient"):
        reasons.append("INSUFFICIENT_STAGING_SPACE")
    if selinux.get("enabled") and selinux.get("strategy") == "unknown":
        reasons.append("SEPOLICY_UNSUPPORTED")
    if selinux.get("state") == "unknown":
        reasons.append("SELINUX_STATE_UNKNOWN")
    if selinux.get("state") in {"permissive", "disabled"}:
        warnings.append("SELINUX_NOT_ENFORCING")

    selected_init = init.get("selected_directory")
    selected_record = next(
        (
            record
            for record in init.get("candidate_directories", [])
            if isinstance(record, Mapping) and record.get("path") == selected_init
        ),
        None,
    )
    if not selected_init or not selected_record or not _trusted_init_directory(selected_record):
        reasons.append("INIT_PATH_UNAVAILABLE")
    elif init.get("import_proof") != "proven":
        reasons.append("INIT_IMPORT_UNPROVEN")
    if not recovery.get("verified"):
        reasons.append("NO_RECOVERY_PATH")
    if not persistence.get("proven"):
        reasons.append("PERSISTENT_WRITABILITY_UNPROVEN")

    if existing.get("system_mode"):
        warnings.append("SYSTEM_MODE_ALREADY_INSTALLED")
        if not existing.get("manifest_present"):
            warnings.append("LEGACY_INSTALL_NO_MANIFEST")
    elif existing.get("ordinary_magisk"):
        reasons.append("ORDINARY_MAGISK_ALREADY_INSTALLED")
    elif existing.get("detected"):
        reasons.append("EXISTING_INSTALL_UNRECOGNIZED")

    reasons = list(dict.fromkeys(reasons))
    warnings = list(dict.fromkeys(warnings))
    technical_blockers = {
        "NO_BOOTSTRAP_ROOT",
        "ANDROID_API_UNSUPPORTED",
        "READ_ONLY_FS",
        "READ_ONLY_BACKING_FS",
        "EROFS",
        "SQUASHFS",
        "EXT4_SHARED_BLOCKS",
        "VERITY_ACTIVE",
        "INSUFFICIENT_STAGING_SPACE",
        "SEPOLICY_UNSUPPORTED",
        "INIT_PATH_UNAVAILABLE",
        "OVERLAY_WRITABLE_ONLY",
        "ORDINARY_MAGISK_ALREADY_INSTALLED",
        "EXISTING_INSTALL_UNRECOGNIZED",
    }
    if "PROBE_INCOMPLETE" in reasons:
        verdict = "error"
    elif technical_blockers.intersection(reasons):
        verdict = "blocked"
    elif reasons:
        verdict = "needs_evidence"
    else:
        verdict = "supported"
        reasons.append("SUPPORTED")

    return {
        "verdict": verdict,
        "primary_reason": reasons[0],
        "reason_codes": reasons + warnings,
        "warnings": warnings,
    }


def validate_report(report: Mapping[str, Any]) -> None:
    """Fast dependency-free validation for CI and field collection.

    The JSON Schema remains authoritative for external validators; this function
    covers the invariants needed by the in-repository driver without adding a
    third-party runtime dependency to recovery tooling.
    """

    if not isinstance(report, Mapping):
        raise ValueError("doctor report root must be an object")

    required = {
        "schema_version",
        "probe_version",
        "generated_at",
        "source",
        "device",
        "bootstrap",
        "mounts",
        "layout",
        "init",
        "selinux",
        "staging",
        "recovery",
        "persistence",
        "existing_install",
        "assessment",
    }
    missing = sorted(required.difference(report))
    if missing:
        raise ValueError(f"doctor report is missing fields: {', '.join(missing)}")
    if report["schema_version"] != CONTRACT_VERSION:
        raise ValueError(f"unsupported doctor schema version: {report['schema_version']}")
    for field in (
        "source",
        "device",
        "bootstrap",
        "layout",
        "init",
        "selinux",
        "staging",
        "recovery",
        "persistence",
        "existing_install",
        "assessment",
    ):
        if not isinstance(report[field], Mapping):
            raise ValueError(f"doctor report field {field} must be an object")
    if not isinstance(report["mounts"], list) or not report["mounts"]:
        raise ValueError("doctor report must contain at least one mount record")
    if not report["device"].get("abis"):
        raise ValueError("doctor report must contain at least one ABI")
    if _parse_int(report["device"].get("api", 0)) < 1:
        raise ValueError("doctor report must contain a valid Android API level")
    page_size = _parse_int(report["device"].get("page_size", 0))
    if page_size < 1 or page_size & (page_size - 1):
        raise ValueError("doctor report must contain a positive power-of-two page size")
    if report["staging"].get("required_bytes") != STAGING_REQUIRED_BYTES:
        raise ValueError("doctor report has an unexpected staging-space contract")
    shared_blocks = report["layout"].get("shared_blocks")
    if shared_blocks is not None and not isinstance(shared_blocks, bool):
        raise ValueError("layout.shared_blocks must be true, false, or null")
    device_mapper = report["layout"].get("device_mapper")
    if not isinstance(device_mapper, Mapping) or not isinstance(device_mapper.get("layers"), list):
        raise ValueError("layout.device_mapper must contain a layer list")
    if not all(isinstance(layer, Mapping) for layer in device_mapper["layers"]):
        raise ValueError("layout.device_mapper layers must be objects")
    init_candidates = report["init"].get("candidate_directories")
    if not isinstance(init_candidates, list) or not all(
        isinstance(candidate, Mapping) for candidate in init_candidates
    ):
        raise ValueError("init.candidate_directories must be an object list")
    recovery = report["recovery"]
    if recovery.get("verified") and not all(
        recovery.get(field)
        for field in ("snapshot_id", "backup_location", "backup_digest", "restore_command")
    ):
        raise ValueError("verified recovery requires a complete recovery tuple")
    persistence = report["persistence"]
    if persistence.get("proven") and not (
        persistence.get("backing_write_probe") == "passed"
        and _parse_int(persistence.get("cold_boots", 0)) >= 3
    ):
        raise ValueError("proven persistence requires a passed probe and three cold boots")
    existing = report["existing_install"]
    for field in ("detected", "system_mode", "active_magisk", "ordinary_magisk"):
        if field in existing and not isinstance(existing[field], bool):
            raise ValueError(f"existing_install.{field} must be a boolean")
    if "ordinary_magisk" in existing:
        if "active_magisk" not in existing:
            raise ValueError("ordinary_magisk requires active_magisk evidence")
        expected_ordinary = bool(
            existing.get("active_magisk") and not existing.get("system_mode")
        )
        if existing["ordinary_magisk"] != expected_ordinary:
            raise ValueError("ordinary_magisk contradicts active Magisk/System Mode evidence")
    if any(
        existing.get(field)
        for field in ("system_mode", "manifest_present", "active_magisk")
    ) and not existing.get("detected"):
        raise ValueError("existing install evidence requires detected=true")

    metadata = _reason_code_metadata()
    codes = report["assessment"].get("reason_codes", [])
    if not isinstance(codes, list) or not all(isinstance(code, str) for code in codes):
        raise ValueError("assessment.reason_codes must be a string list")
    unknown = [code for code in codes if code not in metadata]
    if unknown:
        raise ValueError(f"doctor report contains unknown reason codes: {unknown}")
    if not codes or report["assessment"].get("primary_reason") not in codes:
        raise ValueError("assessment primary_reason must be present in reason_codes")
    if dict(report["assessment"]) != classify_report(report):
        raise ValueError("stored assessment does not match the current classifier")


def human_summary(report: Mapping[str, Any]) -> str:
    assessment = report["assessment"]
    device = report["device"]
    layout = report["layout"]
    bootstrap = report["bootstrap"]
    lines = [
        f"System Mode doctor: {assessment['verdict'].upper()} ({assessment['primary_reason']})",
        f"Target: {device['vendor']} / {device['model']} / Android {device['android_release']} (API {device['api']})",
        f"Runtime: {', '.join(device['abis'])} / {device['page_size']}-byte pages",
        f"Bootstrap: {bootstrap['transport']} (root_available={str(bootstrap['root_available']).lower()})",
        f"System: {layout['system_fs_type']} {layout['system_source']} "
        f"view={'rw' if layout['system_writable_view'] else 'ro'} "
        f"backing={'rw' if layout['system_writable_backing'] else 'ro'}",
        f"SELinux: {report['selinux']['state']} / {report['selinux']['strategy']}",
        f"Init proof: {report['init']['import_proof']}",
        f"Recovery verified: {str(report['recovery']['verified']).lower()}",
        f"Persistence proven: {str(report['persistence']['proven']).lower()}",
        "Reasons: " + ", ".join(assessment["reason_codes"]),
    ]
    return "\n".join(lines)


def load_fixture(path: os.PathLike[str] | str) -> dict[str, Any]:
    fixture = json.loads(Path(path).read_text(encoding="utf-8"))
    if fixture.get("schema_version") != CONTRACT_VERSION:
        raise ValueError(f"unsupported fixture schema in {path}")
    fixture["report"] = fixture_report(fixture["input"])
    validate_report(fixture["report"])
    return fixture


def fixture_report(values: Mapping[str, Any]) -> dict[str, Any]:
    """Expand the intentionally small classifier-fixture input into a report."""

    fixture_values = dict(values)
    bootstrap = dict(fixture_values.get("bootstrap", {}))
    root_available = bool(bootstrap.get("root_available"))
    bootstrap.setdefault("adb_uid", 0 if root_available else 2000)
    bootstrap.setdefault("transport", "root_adb" if root_available else "none")
    bootstrap.setdefault("root_uid", 0 if root_available else None)
    bootstrap.setdefault("root_context", "u:r:su:s0" if root_available else None)
    bootstrap.setdefault("magisk_version", None)
    bootstrap.setdefault("magisk_version_code", None)
    fixture_values["bootstrap"] = bootstrap

    layout = dict(fixture_values.get("layout", {}))
    layout.setdefault("system_target", "/system")
    layout.setdefault("slot_suffix", "")
    layout.setdefault("ext4_features", [])
    layout.setdefault("ext4_features_probe", "unavailable")
    layout.setdefault(
        "ext4_features_method",
        "synthetic" if layout["ext4_features_probe"] == "observed" else None,
    )
    layout.setdefault("ext4_ro_compat_flags", None)
    layout.setdefault("shared_blocks", None)
    layout.setdefault("device_mapper", {"probe": "not_applicable", "layers": []})
    fixture_values["layout"] = layout

    recovery = dict(fixture_values.get("recovery", {}))
    if recovery.get("verified"):
        recovery.setdefault("snapshot_id", "synthetic-snapshot")
        recovery.setdefault("backup_location", "synthetic-backup")
        recovery.setdefault("backup_digest", "0" * 64)
        recovery.setdefault("restore_command", "synthetic-restore")
        recovery.setdefault("qualification_record", "/synthetic/qualification.json")
        recovery.setdefault("qualification_sha256", "1" * 64)
        recovery.setdefault("adapter_id", "synthetic-fixture")
        recovery.setdefault("instance_identity_sha256", "2" * 64)
    else:
        recovery.setdefault("snapshot_id", None)
        recovery.setdefault("backup_location", None)
        recovery.setdefault("backup_digest", None)
        recovery.setdefault("restore_command", None)
        recovery.setdefault("qualification_record", None)
        recovery.setdefault("qualification_sha256", None)
        recovery.setdefault("adapter_id", None)
        recovery.setdefault("instance_identity_sha256", None)
    fixture_values["recovery"] = recovery

    persistence = dict(fixture_values.get("persistence", {}))
    if persistence.get("proven"):
        persistence.setdefault("backing_write_probe", "passed")
        persistence.setdefault("cold_boots", 3)
    else:
        persistence.setdefault("backing_write_probe", "not_run")
        persistence.setdefault("cold_boots", 0)
    persistence.setdefault("host_restarts", 0)
    fixture_values["persistence"] = persistence

    init = dict(fixture_values.get("init", {}))
    init.setdefault("selected_directory", "/system/etc/init")
    init.setdefault(
        "candidate_directories",
        [
            {
                "path": "/system/etc/init",
                "resolved_path": "/system/etc/init",
                "exists": True,
                "kind": "directory",
                "readable": True,
                "permission_writable": True,
                "uid": 0,
                "mode": "0755",
            }
        ],
    )
    fixture_values["init"] = init

    selinux = dict(fixture_values.get("selinux", {}))
    selinux.setdefault("policy_candidates", [])
    fixture_values["selinux"] = selinux

    staging = dict(fixture_values.get("staging", {}))
    staging.setdefault("path", "/data/local/tmp")
    staging.setdefault(
        "available_bytes",
        STAGING_REQUIRED_BYTES if staging.get("sufficient") else 0,
    )
    fixture_values["staging"] = staging

    existing = dict(fixture_values.get("existing_install", {}))
    existing.setdefault("active_magisk", False)
    existing.setdefault(
        "ordinary_magisk",
        bool(existing.get("active_magisk") and not existing.get("system_mode")),
    )
    existing.setdefault(
        "detected",
        bool(
            existing.get("system_mode")
            or existing.get("manifest_present")
            or existing.get("active_magisk")
        ),
    )
    existing.setdefault("config_path", INSTALL_CONFIG if existing.get("system_mode") else None)
    existing.setdefault(
        "manifest_path",
        MANIFEST_CANDIDATES[0] if existing.get("manifest_present") else None,
    )
    fixture_values["existing_install"] = existing

    report: dict[str, Any] = {
        "schema_version": CONTRACT_VERSION,
        "probe_version": PROBE_VERSION,
        "generated_at": "2000-01-01T00:00:00Z",
        "source": {
            "kind": "fixture",
            "read_only": True,
            "serial_sha256": "0" * 64,
            "repository_commit": "0" * 40,
            "repository_dirty": False,
            "probe_sha256": "0" * 64,
        },
        "device": {
            "vendor": "fixture",
            "vendor_evidence": ["synthetic"],
            "manufacturer": "fixture",
            "brand": "fixture",
            "model": "fixture",
            "product": "fixture",
            "device": "fixture",
            "build_id": "fixture",
            "build_incremental": "fixture",
            "emulator_product": None,
            "emulator_product_property": None,
            "emulator_version": None,
            "emulator_version_property": None,
            "android_release": "12",
            "api": 32,
            "abis": ["x86_64"],
            "page_size": 4096,
            "fingerprint_sha256": "0" * 64,
            "kernel": "fixture",
            "boot_id_sha256": "0" * 64,
        },
        "mounts": [
            {
                "target": "/system",
                "resolved_path": "/system",
                "exists": True,
                "mount_point": "/system",
                "root": "/",
                "source": values.get("layout", {}).get("system_source"),
                "fs_type": values.get("layout", {}).get("system_fs_type"),
                "mount_options": ["rw"] if values.get("layout", {}).get("system_writable_view") else ["ro"],
                "super_options": ["rw"] if values.get("layout", {}).get("system_writable_backing") else ["ro"],
                "major_minor": "8:1",
                "writable_view": bool(values.get("layout", {}).get("system_writable_view")),
                "writable_backing": bool(values.get("layout", {}).get("system_writable_backing")),
            }
        ],
    }
    report.update(fixture_values)
    report["assessment"] = classify_report(report)
    return report
