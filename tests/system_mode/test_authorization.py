from __future__ import annotations

import base64
import json
from pathlib import Path
import unittest

from tools.system_mode.authorization import build_authorization
from tools.system_mode.doctor import classify_report, fixture_report


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tools" / "system_mode" / "fixtures" / "mumu-writable.json"


def parse_authorization(raw: bytes) -> dict[str, str]:
    return dict(line.split("=", 1) for line in raw.decode("ascii").splitlines())


class SystemModeAuthorizationTest(unittest.TestCase):
    def supported_report(self) -> dict[str, object]:
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        return fixture_report(fixture["input"])

    def test_authorization_binds_recovery_to_the_exact_target(self) -> None:
        report = self.supported_report()
        raw_report = json.dumps(report, sort_keys=True).encode("utf-8")
        authorization = parse_authorization(build_authorization(report, raw_report))

        self.assertEqual("1", authorization["SCHEMA_VERSION"])
        self.assertEqual(
            report["device"]["fingerprint_sha256"],
            authorization["FINGERPRINT_SHA256"],
        )
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

    def test_authorization_rejects_unproven_recovery(self) -> None:
        report = self.supported_report()
        report["recovery"]["verified"] = False
        report["assessment"] = classify_report(report)
        with self.assertRaisesRegex(ValueError, "verified external recovery"):
            build_authorization(report, b"{}")

    def test_authorization_rejects_a_non_supported_report(self) -> None:
        report = self.supported_report()
        report["layout"]["system_fs_type"] = "erofs"
        report["assessment"] = classify_report(report)
        with self.assertRaisesRegex(ValueError, "not supported"):
            build_authorization(report, b"{}")


if __name__ == "__main__":
    unittest.main()
