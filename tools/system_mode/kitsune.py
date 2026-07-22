#!/usr/bin/env python3
"""Host entry point for the portable Kitsune System Mode contract."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.system_mode.doctor import (  # noqa: E402
    AdbClient,
    ProbeError,
    QualificationEvidence,
    classify_report,
    collect_report,
    human_summary,
    load_fixture,
    validate_report,
)


def _endpoint(value: str) -> str:
    return f"127.0.0.1:{value}" if value.isdigit() else value


def _doctor(args: argparse.Namespace) -> int:
    try:
        client = AdbClient(adb=args.adb, serial=args.serial, timeout=args.timeout)
        if args.connect:
            client.connect(_endpoint(args.connect))
        evidence = QualificationEvidence(
            init_import_proven=args.init_import_proven,
            snapshot_id=args.snapshot_id,
            backup_digest=args.backup_digest,
            restore_command=args.restore_command,
            recovery_verified=args.recovery_verified,
            backing_write_probe=args.backing_write_probe,
            cold_boots=args.cold_boots,
            host_restarts=args.host_restarts,
        )
        report = collect_report(client, evidence)
    except (ProbeError, ValueError) as exc:
        print(f"doctor failed: {exc}", file=sys.stderr)
        return 2

    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        try:
            output = Path(args.output)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered, encoding="utf-8")
        except OSError as exc:
            print(f"doctor failed to write {args.output}: {exc}", file=sys.stderr)
            return 2
    if args.json or not args.output:
        print(rendered if args.json else human_summary(report))
    return 0 if report["assessment"]["verdict"] == "supported" else 3


def _validate_fixtures(args: argparse.Namespace) -> int:
    root = Path(args.directory)
    failures: list[str] = []
    paths = sorted(root.glob("*.json"))
    if not paths:
        print(f"no fixtures found in {root}", file=sys.stderr)
        return 2
    for path in paths:
        try:
            fixture = load_fixture(path)
            actual = classify_report(fixture["report"])
            expected = fixture["expected"]
            for field in ("verdict", "primary_reason", "reason_codes"):
                if actual[field] != expected[field]:
                    failures.append(f"{path.name}: {field}: expected {expected[field]!r}, got {actual[field]!r}")
        except (OSError, ValueError, TypeError, AttributeError, KeyError, json.JSONDecodeError) as exc:
            failures.append(f"{path.name}: {exc}")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"validated {len(paths)} System Mode fixtures")
    return 0


def _validate_report(args: argparse.Namespace) -> int:
    try:
        report = json.loads(Path(args.path).read_text(encoding="utf-8"))
        validate_report(report)
        expected = classify_report(report)
        if expected != report["assessment"]:
            raise ValueError("stored assessment does not match the current classifier")
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f"invalid doctor report: {exc}", file=sys.stderr)
        return 1
    print(f"valid doctor report: {args.path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kitsune", description="KitsuneMagisk host tooling")
    subcommands = parser.add_subparsers(dest="command", required=True)
    system_mode = subcommands.add_parser("system-mode", help="System Mode contract and characterization")
    system_commands = system_mode.add_subparsers(dest="system_command", required=True)

    doctor = system_commands.add_parser("doctor", help="collect a read-only capability report over ADB")
    doctor.add_argument("--adb", default="adb", help="ADB executable")
    doctor.add_argument("--serial", help="existing ADB serial")
    doctor.add_argument("--connect", help="ADB endpoint or port to connect before probing")
    doctor.add_argument("--timeout", type=int, default=15)
    doctor.add_argument("--json", action="store_true", help="print canonical JSON instead of a human summary")
    doctor.add_argument("--output", help="write canonical JSON to this path")
    doctor.add_argument("--init-import-proven", action="store_true")
    doctor.add_argument("--snapshot-id")
    doctor.add_argument("--backup-digest")
    doctor.add_argument("--restore-command")
    doctor.add_argument("--recovery-verified", action="store_true")
    doctor.add_argument("--backing-write-probe", choices=("not_run", "passed", "failed"), default="not_run")
    doctor.add_argument("--cold-boots", type=int, default=0)
    doctor.add_argument("--host-restarts", type=int, default=0)
    doctor.set_defaults(handler=_doctor)

    fixtures = system_commands.add_parser("validate-fixtures", help="validate classifier fixtures")
    fixtures.add_argument(
        "--directory",
        default=str(Path(__file__).with_name("fixtures")),
        help="fixture directory",
    )
    fixtures.set_defaults(handler=_validate_fixtures)

    report = system_commands.add_parser("validate-report", help="validate a stored doctor report")
    report.add_argument("path")
    report.set_defaults(handler=_validate_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
