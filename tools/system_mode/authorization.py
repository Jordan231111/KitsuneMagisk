"""Create the explicit host-to-installer recovery authorization contract."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re
import shlex
import tempfile
from typing import Any, Mapping

from tools.system_mode.doctor import AdbClient, ProbeError, validate_report


AUTHORIZATION_SCHEMA_VERSION = 1
AUTHORIZATION_PATH = "/data/local/tmp/kitsune-system-mode-recovery-v1.env"


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _safe_adapter_component(value: object) -> str:
    component = re.sub(r"[^a-z0-9._-]+", "-", str(value or "unknown").lower())
    return component.strip("-") or "unknown"


def build_authorization(report: Mapping[str, Any], report_bytes: bytes) -> bytes:
    """Return a non-executable, line-oriented authorization for one exact target."""

    validate_report(report)
    recovery = report["recovery"]
    persistence = report["persistence"]
    init = report["init"]
    if not recovery.get("verified"):
        raise ValueError("doctor report does not contain verified external recovery")
    if not persistence.get("proven"):
        raise ValueError("doctor report does not contain persistent-write evidence")
    if init.get("import_proof") != "proven" or not init.get("selected_directory"):
        raise ValueError("doctor report does not prove an init import directory")
    if report["assessment"].get("verdict") != "supported":
        raise ValueError("doctor report is not supported")

    device = report["device"]
    selinux = report["selinux"]
    adapter_id = "-".join(
        (
            _safe_adapter_component(device.get("vendor")),
            _safe_adapter_component(device.get("emulator_version") or device.get("model")),
        )
    )
    fields = (
        ("SCHEMA_VERSION", str(AUTHORIZATION_SCHEMA_VERSION)),
        ("REPORT_SHA256", hashlib.sha256(report_bytes).hexdigest()),
        ("FINGERPRINT_SHA256", str(device["fingerprint_sha256"])),
        ("TARGET_API", str(device["api"])),
        ("TARGET_ABIS_B64", _b64(",".join(device["abis"]))),
        ("ADAPTER_ID_B64", _b64(adapter_id)),
        ("INIT_DIRECTORY_B64", _b64(str(init["selected_directory"]))),
        ("SELINUX_STRATEGY_B64", _b64(str(selinux["strategy"]))),
        ("SNAPSHOT_ID_B64", _b64(str(recovery["snapshot_id"]))),
        ("BACKUP_LOCATION_B64", _b64(str(recovery["backup_location"]))),
        ("BACKUP_SHA256", str(recovery["backup_digest"])),
        ("RESTORE_COMMAND_B64", _b64(str(recovery["restore_command"]))),
    )
    return ("\n".join(f"{key}={value}" for key, value in fields) + "\n").encode("ascii")


def load_authorization_report(path: Path) -> tuple[dict[str, Any], bytes]:
    raw = path.read_bytes()
    report = json.loads(raw.decode("utf-8"))
    if not isinstance(report, dict):
        raise ValueError("doctor report root must be an object")
    return report, raw


def stage_authorization(client: AdbClient, authorization: bytes) -> str:
    """Stage and byte-verify an authorization in shell-owned temporary storage."""

    expected = hashlib.sha256(authorization).hexdigest()
    with tempfile.NamedTemporaryFile(prefix="kitsune-system-mode-auth-", suffix=".env") as temp:
        temp.write(authorization)
        temp.flush()
        client.push(temp.name, AUTHORIZATION_PATH)
    quoted = shlex.quote(AUTHORIZATION_PATH)
    result = client.shell(f"chmod 0600 {quoted} && sha256sum {quoted}")
    if result.returncode != 0:
        raise ProbeError(result.stderr or "could not verify staged recovery authorization")
    actual = result.stdout.split()[0].lower() if result.stdout.split() else ""
    if actual != expected:
        raise ProbeError("staged recovery authorization digest mismatch")
    return expected
