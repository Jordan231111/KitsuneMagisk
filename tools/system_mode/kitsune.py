#!/usr/bin/env python3
"""Host entry point for the portable Kitsune System Mode contract."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

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
from tools.system_mode.authorization import (  # noqa: E402
    AUTHORIZATION_TTL_SECONDS,
    HostLease,
    build_authorization,
    digest_backup_path,
    load_authorization_report,
    remove_authorization,
    stable_regular_file,
    stage_authorization,
    validate_fresh_target,
    wait_authorization_consumed,
)
from tools.system_mode.adapters import built_in_descriptors  # noqa: E402
from tools.system_mode.qualification import (  # noqa: E402
    evidence_from_record,
    qualify_target,
    verify_report_qualification,
)


MANAGER_APPLICATION_ID = "io.github.huskydg.magisk.next"
MANAGER_COMPONENT = f"{MANAGER_APPLICATION_ID}/com.topjohnwu.magisk.ui.MainActivity"


def _endpoint(value: str) -> str:
    return f"127.0.0.1:{value}" if value.isdigit() else value


def _doctor(args: argparse.Namespace) -> int:
    try:
        client = AdbClient(adb=args.adb, serial=args.serial, timeout=args.timeout)
        if args.connect:
            client.connect(_endpoint(args.connect))
        seal_key = Path(args.seal_key).expanduser() if args.seal_key else None
        evidence = (
            evidence_from_record(
                Path(args.qualification_record).expanduser().resolve(),
                seal_key_path=seal_key,
            )
            if args.qualification_record
            else QualificationEvidence()
        )
        report = collect_report(client, evidence)
        if args.qualification_record:
            verify_report_qualification(report, seal_key_path=seal_key)
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
        if report["recovery"].get("verified"):
            verify_report_qualification(
                report,
                seal_key_path=Path(args.seal_key).expanduser() if args.seal_key else None,
            )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f"invalid doctor report: {exc}", file=sys.stderr)
        return 1
    print(f"valid doctor report: {args.path}")
    return 0


def _authorize(args: argparse.Namespace) -> int:
    client: AdbClient | None = None
    reverse_port: int | None = None
    consumed = False
    try:
        report_path = Path(args.report).expanduser()
        seal_key = Path(args.seal_key).expanduser() if args.seal_key else None
        report, raw = load_authorization_report(report_path)
        verify_report_qualification(report, seal_key_path=seal_key)
        initial_report_sha256 = hashlib.sha256(raw).hexdigest()
        artifact_path = Path(args.artifact).expanduser()
        if artifact_path.is_symlink() or not artifact_path.is_file():
            raise ValueError("authorized artifact must be one regular APK file")
        artifact_path = artifact_path.resolve(strict=True)
        artifact_identity, artifact_bytes = stable_regular_file(
            artifact_path,
            purpose="authorized manager APK",
            capture=True,
        )
        if artifact_bytes is None:
            raise ValueError("authorized manager APK could not be captured")
        artifact_digest = str(artifact_identity["sha256"])
        client = AdbClient(adb=args.adb, serial=args.serial, timeout=args.timeout)
        if args.connect:
            client.connect(_endpoint(args.connect))
        exact_serial = client.resolve_serial()
        client.wait_for_device()
        live_serial_digest = hashlib.sha256(
            exact_serial.encode("utf-8", errors="replace")
        ).hexdigest()
        if live_serial_digest != report["source"]["serial_sha256"]:
            raise ValueError("doctor report ADB instance does not match the live target")

        # Install the exact bytes first, then re-probe. The authorization is
        # staged only after ``adb install -r`` succeeds on the pinned target.
        with tempfile.NamedTemporaryFile(
            prefix="kitsune-authorized-",
            suffix=".apk",
        ) as pinned_artifact:
            pinned_artifact.write(artifact_bytes)
            pinned_artifact.flush()
            os.fsync(pinned_artifact.fileno())
            if hashlib.sha256(artifact_bytes).hexdigest() != artifact_digest:
                raise ValueError("captured manager APK differs from its pinned identity")
            client.install_replace(Path(pinned_artifact.name))
        record_path = Path(str(report["recovery"]["qualification_record"]))
        fresh = collect_report(
            client,
            evidence_from_record(record_path, seal_key_path=seal_key),
        )
        validate_fresh_target(report, fresh)
        verify_report_qualification(fresh, seal_key_path=seal_key)

        authorization_id = str(uuid.uuid4())

        def verify_host_handoff() -> None:
            current_report, current_raw = load_authorization_report(report_path)
            if (
                hashlib.sha256(current_raw).hexdigest() != initial_report_sha256
                or current_report != report
            ):
                raise ValueError("doctor report changed before installer handoff")
            verify_report_qualification(current_report, seal_key_path=seal_key)
            current_artifact, _ = stable_regular_file(
                artifact_path,
                purpose="authorized manager APK",
            )
            if current_artifact != artifact_identity:
                raise ValueError("authorized manager APK changed before installer handoff")
            if client.resolve_serial() != exact_serial:
                raise ValueError("ADB transport changed before installer handoff")
            handoff = collect_report(
                client,
                evidence_from_record(record_path, seal_key_path=seal_key),
            )
            validate_fresh_target(report, handoff)
            verify_report_qualification(handoff, seal_key_path=seal_key)

        with HostLease(authorization_id, verify_host_handoff) as lease:
            reverse_port = client.reverse_tcp(lease.host_port)
            authorization = build_authorization(
                report,
                raw,
                artifact_digest,
                authorization_id=authorization_id,
                lease_port=reverse_port,
                lease_nonce_sha256=lease.nonce_sha256,
            )
            digest = stage_authorization(client, authorization)
            client.start_activity(
                MANAGER_COMPONENT,
                string_extras={"section": "install"},
            )
            print(
                "installed exact debug APK and opened Install; choose System Mode, "
                "confirm the warning, and start within five minutes",
                file=sys.stderr,
            )
            lease.wait(AUTHORIZATION_TTL_SECONDS + 5)
            wait_authorization_consumed(client)
            consumed = True
    except (OSError, ProbeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f"authorization failed: {exc}", file=sys.stderr)
        return 2
    except (TimeoutError, RuntimeError) as exc:
        print(f"authorization failed: {exc}", file=sys.stderr)
        return 2
    finally:
        if client is not None and not consumed:
            try:
                remove_authorization(client)
            except (ProbeError, ValueError) as exc:
                print(
                    f"warning: could not remove staged guest authorization: {exc}",
                    file=sys.stderr,
                )
        if client is not None and reverse_port is not None:
            try:
                client.remove_reverse_tcp(reverse_port)
            except (ProbeError, ValueError) as exc:
                print(
                    f"warning: could not remove ADB reverse mapping tcp:{reverse_port}: {exc}",
                    file=sys.stderr,
                )
    print(f"System Mode installer consumed one-shot authorization: sha256={digest}")
    return 0


def _qualify(args: argparse.Namespace) -> int:
    try:
        client = AdbClient(adb=args.adb, serial=args.serial, timeout=args.timeout)
        endpoint = _endpoint(args.connect) if args.connect else None
        if endpoint:
            client.connect(endpoint)
        record = qualify_target(
            client,
            output=Path(args.output).expanduser().resolve(),
            backup_location=Path(args.backup_location).expanduser(),
            snapshot_id=args.snapshot_id,
            backup_command=args.backup_command,
            restore_command=args.restore_command,
            cold_boot_command=args.cold_boot_command,
            instance_identity_path=Path(args.instance_identity).expanduser(),
            seal_key_path=Path(args.seal_key).expanduser() if args.seal_key else None,
            endpoint=endpoint,
            lifecycle_timeout=args.lifecycle_timeout,
        )
    except (OSError, ProbeError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f"qualification failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


def _digest_backup(args: argparse.Namespace) -> int:
    try:
        digest = digest_backup_path(Path(args.path))
    except (OSError, ValueError) as exc:
        print(f"backup digest failed: {exc}", file=sys.stderr)
        return 2
    print(digest)
    return 0


def _list_adapters(args: argparse.Namespace) -> int:
    descriptors = [item.as_dict() for item in built_in_descriptors()]
    if args.json:
        print(json.dumps(descriptors, indent=2, sort_keys=True))
    else:
        for item in descriptors:
            print(
                f"{item['adapter_id']}: tier {item['tier']}, "
                f"execution={item['execution']}"
            )
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
    doctor.add_argument(
        "--qualification-record",
        help="machine-generated qualification JSON; evidence cannot be supplied as flags",
    )
    doctor.add_argument(
        "--seal-key",
        help="owner-only host key that authenticates the qualification record",
    )
    doctor.set_defaults(handler=_doctor)

    qualify = system_commands.add_parser(
        "qualify",
        help="prove init import, three cold boots, and an exact external restore",
    )
    qualify.add_argument("--output", required=True, help="new qualification record path")
    qualify.add_argument("--backup-location", required=True, help="absolute external backup file/tree")
    qualify.add_argument("--snapshot-id", required=True)
    qualify.add_argument(
        "--backup-command",
        required=True,
        help="absolute reviewed argv template with one standalone {backup} argument",
    )
    qualify.add_argument("--restore-command", required=True, help="host argv text that restores the backup")
    qualify.add_argument("--cold-boot-command", required=True, help="host argv text for one true cold boot")
    qualify.add_argument(
        "--instance-identity",
        required=True,
        help="absolute, stable host metadata file unique to this emulator instance",
    )
    qualify.add_argument(
        "--seal-key",
        help="owner-only host key to create/use (default: per-user Kitsune key)",
    )
    qualify.add_argument("--adb", default="adb", help="ADB executable")
    qualify.add_argument("--serial", help="existing ADB serial")
    qualify.add_argument("--connect", help="ADB endpoint or port to reconnect after each lifecycle action")
    qualify.add_argument("--timeout", type=int, default=15)
    qualify.add_argument("--lifecycle-timeout", type=int, default=300)
    qualify.set_defaults(handler=_qualify)

    fixtures = system_commands.add_parser("validate-fixtures", help="validate classifier fixtures")
    fixtures.add_argument(
        "--directory",
        default=str(Path(__file__).with_name("fixtures")),
        help="fixture directory",
    )
    fixtures.set_defaults(handler=_validate_fixtures)

    report = system_commands.add_parser("validate-report", help="validate a stored doctor report")
    report.add_argument("path")
    report.add_argument(
        "--seal-key",
        help="owner-only host key that authenticates qualification evidence",
    )
    report.set_defaults(handler=_validate_report)

    backup = system_commands.add_parser(
        "digest-backup",
        help="calculate the exact v1 digest of an external recovery artifact",
    )
    backup.add_argument("path", help="absolute path to a regular backup file or directory")
    backup.set_defaults(handler=_digest_backup)

    authorize = system_commands.add_parser(
        "authorize",
        help="install the exact APK, open Install, and lease one explicit System Mode mutation",
    )
    authorize.add_argument("report", help="supported doctor JSON with verified external recovery")
    authorize.add_argument(
        "--artifact",
        required=True,
        help="exact regular Magisk APK that will perform the authorized installation",
    )
    authorize.add_argument("--adb", default="adb", help="ADB executable")
    authorize.add_argument("--serial", help="existing ADB serial")
    authorize.add_argument("--connect", help="ADB endpoint or port to connect before staging")
    authorize.add_argument("--timeout", type=int, default=15)
    authorize.add_argument(
        "--seal-key",
        help="owner-only host key that authenticates qualification evidence",
    )
    authorize.set_defaults(handler=_authorize)

    adapters = system_commands.add_parser(
        "adapters",
        help="list the built-in capability adapter interfaces",
    )
    adapters.add_argument("--json", action="store_true")
    adapters.set_defaults(handler=_list_adapters)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
