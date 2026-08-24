from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
from pathlib import Path
import os
import socket
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.request

from tools.system_mode.authorization import (
    AUTHORIZATION_PATH,
    HostLease,
    authorization_target_digest,
    build_authorization,
    digest_backup_path,
    load_authorization_report,
    stage_authorization,
    validate_fresh_target,
    verify_report_backup,
)
from tools.system_mode.doctor import CommandResult, classify_report, fixture_report


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tools" / "system_mode" / "fixtures" / "mumu-writable.json"


def parse_authorization(raw: bytes) -> dict[str, str]:
    return dict(line.split("=", 1) for line in raw.decode("ascii").splitlines())


class SystemModeAuthorizationTest(unittest.TestCase):
    @staticmethod
    def build(
        report: dict[str, object],
        raw_report: bytes | None = None,
        artifact_digest: str = "a" * 64,
    ) -> bytes:
        if raw_report is None:
            raw_report = json.dumps(report, sort_keys=True).encode("utf-8")
        return build_authorization(
            report,
            raw_report,
            artifact_digest,
            lease_port=37173,
            lease_nonce_sha256="f" * 64,
        )

    def supported_report(self) -> dict[str, object]:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        report = fixture_report(fixture["input"])
        report["recovery"]["restore_command"] = "/opt/kitsune/restore exact-backup"
        report["generated_at"] = dt.datetime.now(dt.timezone.utc).replace(
            microsecond=0
        ).isoformat().replace("+00:00", "Z")
        return report

    def test_authorization_binds_recovery_to_the_exact_target(self) -> None:
        report = self.supported_report()
        raw_report = json.dumps(report, sort_keys=True).encode("utf-8")
        artifact_digest = "a" * 64
        authorization = parse_authorization(
            self.build(report, raw_report, artifact_digest)
        )

        self.assertEqual("1", authorization["SCHEMA_VERSION"])
        self.assertEqual(
            report["device"]["fingerprint_sha256"],
            authorization["FINGERPRINT_SHA256"],
        )
        self.assertEqual(report["source"]["serial_sha256"], authorization["SERIAL_SHA256"])
        self.assertEqual(report["source"]["repository_commit"], authorization["SOURCE_COMMIT"])
        self.assertEqual(artifact_digest, authorization["ARTIFACT_SHA256"])
        self.assertEqual(
            authorization_target_digest(report), authorization["TARGET_CONTRACT_SHA256"]
        )
        self.assertEqual(report["device"]["boot_id_sha256"], authorization["BOOT_ID_SHA256"])
        self.assertEqual(report["source"]["probe_sha256"], authorization["PROBE_SHA256"])
        self.assertEqual(
            report["recovery"]["qualification_sha256"],
            authorization["QUALIFICATION_SHA256"],
        )
        self.assertEqual(
            report["recovery"]["instance_identity_sha256"],
            authorization["INSTANCE_IDENTITY_SHA256"],
        )
        self.assertEqual("37173", authorization["LEASE_PORT"])
        self.assertEqual("f" * 64, authorization["LEASE_NONCE_SHA256"])
        self.assertEqual("0" * 64, authorization["BACKUP_SHA256"])
        self.assertEqual(
            report["recovery"]["backup_location"],
            base64.b64decode(authorization["BACKUP_LOCATION_B64"]).decode("utf-8"),
        )
        self.assertEqual(
            report["recovery"]["restore_command"],
            base64.b64decode(authorization["RESTORE_COMMAND_B64"]).decode("utf-8"),
        )
        self.assertEqual(
            report["init"]["selected_directory"],
            base64.b64decode(authorization["INIT_DIRECTORY_B64"]).decode("utf-8"),
        )

    def test_backup_file_uses_ordinary_sha256(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-backup-digest-") as temp:
            backup = Path(temp) / "system.img"
            backup.write_bytes(b"exact recovery bytes")
            self.assertEqual(
                hashlib.sha256(backup.read_bytes()).hexdigest(),
                digest_backup_path(backup),
            )

    def test_backup_directory_digest_binds_names_modes_and_contents(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-backup-tree-") as temp:
            backup = Path(temp) / "vm"
            (backup / "nested").mkdir(parents=True)
            image = backup / "nested" / "system.img"
            image.write_bytes(b"system-v1")
            first = digest_backup_path(backup)
            self.assertEqual(first, digest_backup_path(backup))
            image.write_bytes(b"system-v2")
            second = digest_backup_path(backup)
            self.assertNotEqual(first, second)
            image.write_bytes(b"system-v1")
            image.chmod(0o600)
            self.assertNotEqual(first, digest_backup_path(backup))

    def test_backup_digest_rejects_links_special_nodes_and_missing_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-backup-unsafe-") as temp:
            root = Path(temp)
            target = root / "target"
            target.write_bytes(b"target")
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                digest_backup_path(link)

            tree = root / "tree"
            tree.mkdir()
            fifo = tree / "fifo"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(ValueError, "special node"):
                digest_backup_path(tree)
            with self.assertRaises(FileNotFoundError):
                digest_backup_path(root / "missing")

    def test_authorization_rehashes_the_current_external_backup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-backup-verify-") as temp:
            backup = Path(temp) / "system.img"
            backup.write_bytes(b"verified")
            report = self.supported_report()
            report["recovery"]["backup_location"] = str(backup)
            report["recovery"]["backup_digest"] = digest_backup_path(backup)
            self.assertEqual(report["recovery"]["backup_digest"], verify_report_backup(report))
            backup.write_bytes(b"stale")
            with self.assertRaisesRegex(ValueError, "does not match"):
                verify_report_backup(report)

    def test_authorization_rejects_unproven_recovery(self) -> None:
        report = self.supported_report()
        report["recovery"]["verified"] = False
        report["assessment"] = classify_report(report)
        with self.assertRaisesRegex(ValueError, "verified external recovery"):
            self.build(report)

    def test_authorization_rejects_a_non_supported_report(self) -> None:
        report = self.supported_report()
        report["layout"]["system_fs_type"] = "erofs"
        report["assessment"] = classify_report(report)
        with self.assertRaisesRegex(ValueError, "not supported"):
            self.build(report)

    def test_authorization_rejects_unqualified_init_subcontexts(self) -> None:
        report = self.supported_report()
        report["init"]["candidate_directories"] = [
            {
                "path": "/vendor/etc/init",
                "resolved_path": "/vendor/etc/init",
                "exists": True,
                "kind": "directory",
                "readable": True,
                "permission_writable": True,
            }
        ]
        report["init"]["selected_directory"] = "/vendor/etc/init"
        report["assessment"] = classify_report(report)
        with self.assertRaisesRegex(ValueError, "init subcontext"):
            self.build(report)

    def test_authorization_rejects_dirty_source_and_invalid_artifact_identity(self) -> None:
        report = self.supported_report()
        report["source"]["repository_dirty"] = True
        with self.assertRaisesRegex(ValueError, "dirty source"):
            self.build(report)

        report["source"]["repository_dirty"] = False
        with self.assertRaisesRegex(ValueError, "artifact SHA-256"):
            self.build(report, artifact_digest="not-a-digest")

    def test_authorization_rejects_stale_target_state_and_noop_recovery(self) -> None:
        report = self.supported_report()
        stale = json.loads(json.dumps(report))
        stale["device"]["boot_id_sha256"] = "1" * 64
        with self.assertRaisesRegex(ValueError, "boot-critical target state changed"):
            validate_fresh_target(report, stale)

        report["recovery"]["restore_command"] = "true"
        with self.assertRaisesRegex(ValueError, "no-op"):
            self.build(report)

        report["recovery"]["restore_command"] = "relative-restore snapshot"
        with self.assertRaisesRegex(ValueError, "absolute executable"):
            self.build(report)

    def test_authorization_rejects_report_bytes_from_another_object(self) -> None:
        report = self.supported_report()
        with self.assertRaisesRegex(ValueError, "do not match"):
            self.build(report, raw_report=b"{}")

        type_changed = json.loads(json.dumps(report))
        type_changed["schema_version"] = True
        self.assertEqual(type_changed, report)
        with self.assertRaisesRegex(ValueError, "do not match"):
            self.build(report, raw_report=json.dumps(type_changed).encode("utf-8"))

    def test_authorization_report_read_rejects_a_symlink(self) -> None:
        report = self.supported_report()
        with tempfile.TemporaryDirectory(prefix="kitsune-report-link-") as temp:
            root = Path(temp)
            target = root / "doctor.json"
            target.write_text(json.dumps(report), encoding="utf-8")
            link = root / "report.json"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                load_authorization_report(link)

    def test_backup_hash_detects_path_replacement_after_fd_read(self) -> None:
        from tools.system_mode import authorization as module

        with tempfile.TemporaryDirectory(prefix="kitsune-backup-race-") as temp:
            root = Path(temp)
            backup = root / "system.img"
            backup.write_bytes(b"original")
            displaced = root / "original.img"
            original_reader = module._sha256_descriptor

            def replace_after_read(*args: object, **kwargs: object):
                result = original_reader(*args, **kwargs)
                backup.rename(displaced)
                backup.write_bytes(b"replacement")
                return result

            with mock.patch(
                "tools.system_mode.authorization._sha256_descriptor",
                side_effect=replace_after_read,
            ):
                with self.assertRaisesRegex(ValueError, "path changed"):
                    digest_backup_path(backup)

    def test_staging_uses_a_verified_atomic_remote_publication(self) -> None:
        authorization = b"SCHEMA_VERSION=1\n"
        expected = hashlib.sha256(authorization).hexdigest()

        class Client:
            def __init__(self) -> None:
                self.pushes: list[tuple[str, str]] = []
                self.commands: list[str] = []

            def push(self, source: str, destination: str) -> None:
                self.pushes.append((source, destination))

            def shell(self, command: str) -> CommandResult:
                self.commands.append(command)
                if "sha256sum" in command:
                    return CommandResult(f"{expected}  remote", "", 0)
                return CommandResult("", "", 0)

        client = Client()
        with mock.patch(
            "tools.system_mode.authorization.uuid.uuid4",
            return_value="11111111-1111-1111-1111-111111111111",
        ):
            self.assertEqual(expected, stage_authorization(client, authorization))

        staged = f"{AUTHORIZATION_PATH}.new-11111111-1111-1111-1111-111111111111"
        self.assertEqual(staged, client.pushes[0][1])
        publication = next(command for command in client.commands if f"mv {staged}" in command)
        self.assertIn(f"rm -f {AUTHORIZATION_PATH}", publication)
        self.assertIn(f"chmod 0600 {AUTHORIZATION_PATH}", publication)
        self.assertEqual(f"rm -f {staged}", client.commands[-1])

    def test_host_lease_is_one_shot_and_runs_handoff_verifier(self) -> None:
        calls: list[str] = []
        authorization_id = "11111111-1111-1111-1111-111111111111"
        with mock.patch(
            "tools.system_mode.authorization.secrets.token_hex",
            return_value="a" * 64,
        ):
            with HostLease(authorization_id, lambda: calls.append("verified"), timeout=5) as lease:
                noisy = socket.create_connection(("127.0.0.1", lease.host_port), timeout=2)
                noisy.sendall(b"incomplete")
                noisy.close()
                wrong = f"http://127.0.0.1:{lease.host_port}/wrong"
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(wrong, timeout=2)
                self.assertEqual(404, error.exception.code)
                url = f"http://127.0.0.1:{lease.host_port}/{authorization_id}"
                self.assertEqual(b"a" * 64, urllib.request.urlopen(url, timeout=2).read())
                lease.wait(2)
                self.assertTrue(lease.served)
                self.assertEqual(["verified"], calls)
                self.assertEqual(hashlib.sha256(b"a" * 64).hexdigest(), lease.nonce_sha256)
                with self.assertRaises((OSError, urllib.error.URLError)):
                    urllib.request.urlopen(url, timeout=1)


if __name__ == "__main__":
    unittest.main()
