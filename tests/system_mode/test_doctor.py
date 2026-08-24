from __future__ import annotations

import json
from pathlib import Path
import unittest

from tools.system_mode.doctor import (
    CommandResult,
    QualificationEvidence,
    _effective_mount,
    _ext4_features,
    _select_init_directory,
    classify_report,
    fixture_report,
    load_fixture,
    parse_mountinfo,
    validate_report,
)
from tools.system_mode.schema_validation import SchemaValidationError, validate_schema_instance


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tools" / "system_mode" / "fixtures"
SCHEMAS = ROOT / "tools" / "system_mode" / "schemas"
CONTRACTS = ROOT / "tools" / "system_mode" / "contracts"
RECORDS = ROOT / "compatibility" / "records"


class DoctorFixtureTest(unittest.TestCase):
    def test_all_classifier_fixtures(self) -> None:
        paths = sorted(FIXTURES.glob("*.json"))
        self.assertGreaterEqual(len(paths), 5)
        for path in paths:
            with self.subTest(path=path.name):
                fixture = load_fixture(path)
                actual = classify_report(fixture["report"])
                for field in ("verdict", "primary_reason", "reason_codes"):
                    self.assertEqual(fixture["expected"][field], actual[field])

    def test_validation_rejects_unknown_reason_code(self) -> None:
        fixture = json.loads((FIXTURES / "ldplayer-writable.json").read_text())
        report = fixture_report(fixture["input"])
        report["assessment"]["reason_codes"] = ["MADE_UP"]
        report["assessment"]["primary_reason"] = "MADE_UP"
        with self.assertRaisesRegex(ValueError, "unknown reason"):
            validate_report(report)

    def test_evidence_requires_complete_recovery_tuple(self) -> None:
        evidence = QualificationEvidence(
            snapshot_id="snapshot-1",
            backup_location="external-backup-1",
            backup_digest="a" * 64,
            recovery_verified=True,
        )
        self.assertFalse(evidence.recovery_is_proven)

        report = fixture_report(
            json.loads((FIXTURES / "mumu-writable.json").read_text())["input"]
        )
        report["recovery"] = {
            "snapshot_id": "snapshot-1",
            "backup_location": "external-backup-1",
            "backup_digest": None,
            "restore_command": "restore snapshot-1",
            "qualification_record": None,
            "qualification_sha256": None,
            "adapter_id": None,
            "instance_identity_sha256": None,
            "verified": True,
        }
        with self.assertRaisesRegex(ValueError, "complete recovery tuple"):
            validate_report(report)

    def test_persistence_requires_probe_and_cold_boot(self) -> None:
        self.assertFalse(QualificationEvidence(backing_write_probe="passed").persistence_is_proven)
        self.assertFalse(
            QualificationEvidence(backing_write_probe="passed", cold_boots=2).persistence_is_proven
        )
        self.assertTrue(
            QualificationEvidence(backing_write_probe="passed", cold_boots=3).persistence_is_proven
        )

        report = fixture_report(
            json.loads((FIXTURES / "mumu-writable.json").read_text())["input"]
        )
        report["persistence"] = {
            "backing_write_probe": "not_run",
            "cold_boots": 0,
            "host_restarts": 0,
            "proven": True,
        }
        with self.assertRaisesRegex(ValueError, "passed probe and three cold boots"):
            validate_report(report)

    def test_supported_fixture_requires_a_writable_init_candidate(self) -> None:
        fixture = json.loads((FIXTURES / "mumu-writable.json").read_text())
        report = fixture_report(fixture["input"])
        report["init"]["candidate_directories"][0]["permission_writable"] = False
        assessment = classify_report(report)
        self.assertEqual("blocked", assessment["verdict"])
        self.assertIn("INIT_PATH_UNAVAILABLE", assessment["reason_codes"])

    def test_init_selection_skips_an_earlier_read_only_directory(self) -> None:
        candidates = [
            {
                "path": "/system/etc/init",
                "kind": "directory",
                "readable": True,
                "permission_writable": False,
            },
            {
                "path": "/system/etc/init/hw",
                "kind": "directory",
                "readable": True,
                "permission_writable": True,
            },
        ]
        self.assertEqual("/system/etc/init/hw", _select_init_directory(candidates))

    def test_validation_rejects_a_stale_assessment(self) -> None:
        fixture = json.loads((FIXTURES / "mumu-writable.json").read_text())
        report = fixture_report(fixture["input"])
        report["layout"]["system_fs_type"] = "erofs"
        with self.assertRaisesRegex(ValueError, "stored assessment"):
            validate_report(report)

    def test_device_page_size_must_be_a_positive_power_of_two(self) -> None:
        fixture = json.loads((FIXTURES / "mumu-writable.json").read_text())
        report = fixture_report(fixture["input"])
        self.assertEqual(4096, report["device"]["page_size"])
        for invalid in (0, -4096, 12288):
            with self.subTest(page_size=invalid):
                report["device"]["page_size"] = invalid
                with self.assertRaisesRegex(ValueError, "power-of-two page size"):
                    validate_report(report)

    def test_android_6_is_explicitly_blocked_for_system_mode(self) -> None:
        fixture = json.loads((FIXTURES / "mumu-writable.json").read_text())
        report = fixture_report(fixture["input"])
        report["device"]["api"] = 23
        report["assessment"] = classify_report(report)
        self.assertEqual("blocked", report["assessment"]["verdict"])
        self.assertEqual("ANDROID_API_UNSUPPORTED", report["assessment"]["primary_reason"])
        validate_report(report)

    def test_active_ordinary_magisk_is_blocked_before_authorization(self) -> None:
        fixture = json.loads((FIXTURES / "mumu-writable.json").read_text())
        fixture["input"]["existing_install"] = {
            "detected": True,
            "active_magisk": True,
            "ordinary_magisk": True,
            "system_mode": False,
            "manifest_present": False,
        }
        report = fixture_report(fixture["input"])
        assessment = classify_report(report)
        self.assertEqual("blocked", assessment["verdict"])
        self.assertIn("ORDINARY_MAGISK_ALREADY_INSTALLED", assessment["reason_codes"])
        self.assertNotIn("EXISTING_INSTALL_UNRECOGNIZED", assessment["reason_codes"])

    def test_incomplete_existing_system_mode_markers_fail_closed(self) -> None:
        fixture = json.loads((FIXTURES / "mumu-writable.json").read_text())
        fixture["input"]["existing_install"] = {
            "detected": True,
            "active_magisk": False,
            "ordinary_magisk": False,
            "system_mode": False,
            "manifest_present": True,
        }
        assessment = classify_report(fixture_report(fixture["input"]))
        self.assertEqual("blocked", assessment["verdict"])
        self.assertIn("EXISTING_INSTALL_UNRECOGNIZED", assessment["reason_codes"])

    def test_existing_install_evidence_cannot_contradict_itself(self) -> None:
        fixture = json.loads((FIXTURES / "mumu-writable.json").read_text())
        report = fixture_report(fixture["input"])
        report["existing_install"].update(
            detected=True,
            active_magisk=True,
            ordinary_magisk=False,
            system_mode=False,
        )
        report["assessment"] = classify_report(report)
        with self.assertRaisesRegex(ValueError, "ordinary_magisk contradicts"):
            validate_report(report)

    def test_supported_ext4_fixture_requires_feature_evidence(self) -> None:
        fixture = json.loads((FIXTURES / "mumu-writable.json").read_text())
        for key in ("ext4_features", "ext4_features_probe", "shared_blocks"):
            fixture["input"]["layout"].pop(key)
        assessment = classify_report(fixture_report(fixture["input"]))
        self.assertEqual("needs_evidence", assessment["verdict"])
        self.assertEqual("EXT4_FEATURES_UNPROVEN", assessment["primary_reason"])

    def test_raw_ext4_superblock_detects_shared_blocks_without_tune2fs(self) -> None:
        class FakeClient:
            def shell(self, command: str, *, root: bool) -> CommandResult:
                if command.startswith("tune2fs"):
                    return CommandResult("", "not found", 1)
                return CommandResult("ef53 16384", "", 0)

        features, status, method, flags = _ext4_features(
            FakeClient(),  # type: ignore[arg-type]
            "/dev/block/dm-0",
            root=True,
        )
        self.assertEqual(["shared_blocks"], features)
        self.assertEqual("observed", status)
        self.assertEqual("superblock", method)
        self.assertEqual(0x4000, flags)


class MountInfoTest(unittest.TestCase):
    SAMPLE = """\
21 1 0:20 / / ro,relatime shared:1 - tmpfs tmpfs ro,seclabel
23 21 8:1 / /system rw,noatime shared:2 - ext4 /dev/block/sda1 rw,seclabel
91 23 0:38 /system/etc/security/cacerts /system/etc/security/cacerts rw,relatime - tmpfs worker ro,seclabel
"""

    def test_mountinfo_preserves_view_and_backing_flags(self) -> None:
        mounts = parse_mountinfo(self.SAMPLE)
        self.assertEqual(3, len(mounts))
        system = mounts[1]
        self.assertEqual("/system", system["mount_point"])
        self.assertIn("rw", system["mount_options"])
        self.assertIn("rw", system["super_options"])
        overlay = mounts[2]
        self.assertIn("rw", overlay["mount_options"])
        self.assertIn("ro", overlay["super_options"])

    def test_mountinfo_decodes_kernel_path_escapes(self) -> None:
        record = parse_mountinfo(
            r"42 35 8:1 /source\040root /system\040copy rw - ext4 /dev/block/by-name/system\040copy rw"
        )[0]
        self.assertEqual("/source root", record["root"])
        self.assertEqual("/system copy", record["mount_point"])
        self.assertEqual("/dev/block/by-name/system copy", record["source"])

    def test_effective_mount_prefers_the_later_stacked_mount(self) -> None:
        mounts = parse_mountinfo(
            """\
23 21 8:1 / /system rw - ext4 /dev/block/sda1 rw
92 21 0:38 / /system rw - overlay overlay rw
"""
        )
        effective = _effective_mount("/system/bin", mounts)
        self.assertIsNotNone(effective)
        self.assertEqual("overlay", effective["fs_type"])


class ContractTest(unittest.TestCase):
    def test_schema_constants_and_enums_do_not_alias_booleans_to_numbers(self) -> None:
        for instance, schema in (
            (True, {"const": 1}),
            (False, {"const": 0}),
            (True, {"enum": [1, 2]}),
            (0, {"enum": [False]}),
        ):
            with self.subTest(instance=instance, schema=schema):
                with self.assertRaises(SchemaValidationError):
                    validate_schema_instance(instance, schema)
        validate_schema_instance(1.0, {"const": 1})

    def test_every_contract_and_schema_is_valid_json(self) -> None:
        for path in sorted(list(SCHEMAS.glob("*.json")) + list(CONTRACTS.glob("*.json"))):
            with self.subTest(path=path.name):
                json.loads(path.read_text())

    def test_reason_codes_are_unique_and_supported_is_non_blocking(self) -> None:
        contract = json.loads((CONTRACTS / "reason-codes.json").read_text())
        codes = [item["code"] for item in contract["codes"]]
        self.assertEqual(len(codes), len(set(codes)))
        supported = next(item for item in contract["codes"] if item["code"] == "SUPPORTED")
        self.assertFalse(supported["blocking"])

    def test_state_machine_contains_required_release_states(self) -> None:
        machine = json.loads((CONTRACTS / "install-state-machine.json").read_text())
        required = {
            "PREFLIGHTED",
            "STAGED",
            "COMMITTED",
            "BOOT_VERIFIED",
            "ROLLBACK_REQUIRED",
            "UNINSTALLED",
        }
        self.assertTrue(required.issubset(machine["states"]))
        transitions = {(item["from"], item["to"]) for item in machine["transitions"]}
        self.assertIn(("UNINSTALLED", "PREFLIGHTED"), transitions)
        self.assertIn(("COMMITTED", "BOOT_VERIFIED"), transitions)
        self.assertIn(("ROLLING_BACK", "UNINSTALLED"), transitions)

    def test_characterization_records_keep_unrun_lifecycle_explicit(self) -> None:
        paths = sorted(RECORDS.glob("*.json"))
        doctor_schema = json.loads((SCHEMAS / "doctor-v1.schema.json").read_text())
        self.assertGreaterEqual(len(paths), 1)
        for path in paths:
            with self.subTest(path=path.name):
                record = json.loads(path.read_text())
                self.assertEqual(1, record["schema_version"])
                validate_schema_instance(record["doctor"], doctor_schema)
                validate_report(record["doctor"])
                self.assertEqual(classify_report(record["doctor"]), record["doctor"]["assessment"])
                if not record["clean_snapshot"]:
                    self.assertNotEqual("supported", record["qualification_status"])
                if record["qualification_status"] == "supported":
                    self.assertTrue(record["clean_snapshot"])
                    self.assertRegex(record["artifact"]["apk_sha256"], r"^[a-f0-9]{64}$")
                    self.assertEqual("supported", record["doctor"]["assessment"]["verdict"])
                    self.assertGreaterEqual(record["lifecycle"]["cold_boots"], 3)
                    for field in (
                        "install",
                        "upgrade",
                        "reinstall",
                        "module_smoke",
                        "root_policy_smoke",
                        "uninstall",
                        "snapshot_restore",
                    ):
                        self.assertEqual("passed", record["lifecycle"][field])


if __name__ == "__main__":
    unittest.main()
