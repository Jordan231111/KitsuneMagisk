#!/usr/bin/env python3
"""Capture exact guest boot identities and render a bounded compatibility table."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "kitsune.device-qualification.v1"


def capture(adb: str, serial: str, apk: Path, phase: str, output: Path) -> None:
    def read(*command: str) -> str:
        return subprocess.check_output([adb, "-s", serial, "exec-out", *command],
                                       timeout=20, text=True).strip()

    boot_id = str(uuid.UUID(read("cat", "/proc/sys/kernel/random/boot_id")))
    if read("getprop", "sys.boot_completed") != "1":
        raise ValueError("Android has not completed boot")
    with zipfile.ZipFile(apk) as archive:
        identity = json.loads(archive.read("assets/build-info.json"))
    artifact = {"sha256": hashlib.sha256(apk.read_bytes()).hexdigest(), "build": identity}
    try:
        page_size = int(read("getconf", "PAGESIZE"))
    except (subprocess.CalledProcessError, ValueError):
        # Legacy toolbox has no getconf, and old adbd can return its error text
        # with exit status zero. Read the running process's kernel page size.
        match = re.search(r"(?m)^KernelPageSize:\s+(\d+)\s+kB\s*$",
                          read("cat", "/proc/self/smaps"))
        if match is None:
            raise ValueError("cannot determine the guest kernel page size")
        page_size = int(match[1]) * 1024
    if page_size <= 0 or page_size & (page_size - 1):
        raise ValueError("invalid guest kernel page size")
    try:
        enforce = read("cat", "/sys/fs/selinux/enforce")
    except subprocess.CalledProcessError:
        enforce = ""
    # Some vendor getenforce binaries report Disabled on an active permissive
    # kernel. Absence alone does not prove a disabled kernel/container either.
    selinux = {"0": "Permissive", "1": "Enforcing"}.get(enforce, "Unknown")
    guest = {"fingerprint": read("getprop", "ro.build.fingerprint"),
             "api": int(read("getprop", "ro.build.version.sdk")),
             "abi": read("getprop", "ro.product.cpu.abi"),
             "page_size": page_size,
             "kernel": read("uname", "-r"), "selinux": selinux}
    record = json.loads(output.read_text()) if output.exists() else {
        "schema": SCHEMA, "artifact": artifact, "guest": guest, "boots": []}
    if record.get("schema") != SCHEMA or record["artifact"] != artifact or record["guest"] != guest:
        raise ValueError("artifact or target changed within a qualification record")
    if any(boot["id"] == boot_id for boot in record["boots"]):
        raise ValueError("a cold boot must have a new boot ID")
    record["boots"].append({"id": boot_id, "phase": phase,
                            "at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    with temporary.open("x") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
    os.replace(temporary, output)


def render(manifest: dict, root: Path = ROOT) -> tuple[dict, str]:
    if manifest.get("schema") != 1 or not isinstance(manifest.get("targets"), list):
        raise ValueError("invalid compatibility manifest")
    records = []
    names = set()
    for target in manifest["targets"]:
        name = target["id"]
        if name in names or not re.fullmatch(r"[a-z0-9-]+", name):
            raise ValueError("duplicate or invalid target ID")
        names.add(name)
        status = target["status"]
        if status not in {"experimental", "unsupported", "unqualified"}:
            raise ValueError("PR15 cannot promote a production support claim")
        evidence = target["evidence"]
        if not evidence:
            raise ValueError("every result needs linked evidence")
        for link in evidence:
            file = (root / link).resolve()
            if not file.is_relative_to(root.resolve()) or not file.is_file():
                raise ValueError(f"missing or escaping evidence: {link}")
        if status == "experimental":
            runs = target.get("runs", [])
            if not runs:
                raise ValueError("experimental targets need machine boot records")
            for link in runs:
                if link not in evidence:
                    raise ValueError("runtime record must be listed as evidence")
                run = json.loads((root / link).read_text())
                if run.get("schema") != SCHEMA:
                    raise ValueError("runtime evidence schema mismatch")
                if not re.fullmatch(r"[0-9a-f]{64}", run["artifact"]["sha256"]):
                    raise ValueError("runtime evidence lacks exact artifact identity")
                build = run["artifact"]["build"]
                if build.get("source_dirty") is not False or not re.fullmatch(r"[0-9a-f]{40}", build.get("source_commit", "")):
                    raise ValueError("runtime evidence must identify clean source")
                boots = run["boots"]
                if len(boots) < 2 or len({b["id"] for b in boots}) != len(boots):
                    raise ValueError("runtime evidence requires distinct cold boots")
                if boots[-1]["phase"] != "accepted":
                    raise ValueError("runtime evidence has no final acceptance")
                for boot in boots:
                    uuid.UUID(boot["id"])
        records.append(target)
    result = {"schema": 1, "production_qualified": False, "targets": records}
    table = ["# Tested targets", "", "Generated by `tools/device_qualification.py`; do not edit by hand.", "",
             "These exact results are experimental. A phone skin or model property does not qualify physical hardware.", "",
             "| Target | Route | Result | Limits and evidence |", "|---|---|---|---|"]
    for target in records:
        def cell(value: str) -> str:
            return value.replace("|", "\\|").replace("\n", " ")
        links = ", ".join(f"[record {n + 1}](../{path})" for n, path in enumerate(target["evidence"]))
        table.append(f"| {cell(target['name'])} | {cell(target['route'])} | {target['status']} | "
                     f"{cell(target['limits'])} {links} |")
    return result, "\n".join(table) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser("capture")
    record.add_argument("--adb", default="adb")
    record.add_argument("--serial", required=True)
    record.add_argument("--apk", required=True, type=Path)
    record.add_argument("--phase", choices=("setup", "cold-boot", "accepted"), required=True)
    record.add_argument("--output", required=True, type=Path)
    generate = commands.add_parser("render")
    generate.add_argument("--manifest", type=Path, default=ROOT / "docs/device-pr15.json")
    generate.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.command == "capture":
        capture(args.adb, args.serial, args.apk, args.phase, args.output)
    else:
        data, table = render(json.loads(args.manifest.read_text()))
        outputs = {ROOT / "compatibility/emulators.json": json.dumps(data, indent=2) + "\n",
                   ROOT / "compatibility/TESTED.md": table}
        for path, content in outputs.items():
            if args.check:
                if path.read_text() != content:
                    raise ValueError(f"generated compatibility output is stale: {path}")
            else:
                path.write_text(content)


if __name__ == "__main__":
    main()
