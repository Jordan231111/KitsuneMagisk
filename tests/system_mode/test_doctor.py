from __future__ import annotations

import json
import copy
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.system_mode.doctor import (
    AdbClient,
    CommandResult,
    QualificationEvidence,
    _effective_mount,
    _ext4_features,
    _select_init_directory,
    _versioned_install_config,
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


class RootTransportTest(unittest.TestCase):
    def test_root_adb_and_su_preserve_command_and_status(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            marker = directory / "su-called"
            for uid in (0, 2000):
                with self.subTest(uid=uid):
                    (directory / "id").write_text(f"#!/bin/sh\nprintf '{uid}\\n'\n")
                    (directory / "su").write_text(
                        '#!/bin/sh\n: > "$SU_MARKER"\nshift\nexec sh -c "$1"\n'
                    )
                    for name in ("id", "su"):
                        (directory / name).chmod(0o700)
                    env = dict(os.environ, PATH=f"{directory}:/usr/bin:/bin", SU_MARKER=str(marker))

                    def execute(*args, **kwargs):
                        self.assertEqual(args[0], "shell")
                        result = subprocess.run(
                            ["sh", "-c", args[1]], env=env, capture_output=True, text=True
                        )
                        return CommandResult(result.stdout, result.stderr, result.returncode)

                    client = AdbClient(serial="test")
                    with patch.object(client, "_command", side_effect=execute):
                        result = client.shell('printf "%s" "literal \' value"; exit 37', root=True)
                    self.assertEqual(result.stdout, "literal ' value")
                    self.assertEqual(result.returncode, 37)
                    self.assertEqual(marker.exists(), uid != 0)


class VersionedInstallProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.uuid = "11111111-1111-4111-8111-111111111111"
        self.path = "/system/etc/init/magisk/install-manifest.json"
        self.config = "/system/etc/init/magisk/versions/" + self.uuid + "/config"
        schema = json.loads((SCHEMAS / "install-manifest-v1.schema.json").read_text())
        target = {key: "a" * 64 for key in schema["properties"]["target"]["required"]}
        target.update(authorization_id=self.uuid, api=32, abis=["arm64-v8a"])
        self.manifest = {
            "schema_version": 1, "install_id": self.uuid, "state": "BOOT_VERIFIED",
            "product": {"name": "KitsuneMagisk", "version": "30.7-kitsune-test",
                        "version_code": 30700, "source_commit": "a" * 40,
                        "upstream_base": "b" * 40, "artifact_sha256": "c" * 64},
            "target": target,
            "strategies": {
                "init": "/system/etc/init/magisk.rc", "selinux": "disabled:",
                "policy_mutated": False, "legacy_migration": False,
                "runtime_tmpfs": "/debug_ramdisk", "preinit_device": None,
                "preinit_directory": None, "active_payload": self.config.rsplit("/", 1)[0],
                "rescue_payload": "/system/etc/init/.kitsune-system-mode-rescue/versions/" + self.uuid,
            },
            "ownership_inventory_sha256": "a" * 64, "originals_inventory_sha256": "b" * 64,
            "secure_dir_metadata_sha256": "c" * 64,
            "mutable_namespaces": schema["properties"]["mutable_namespaces"]["items"]["enum"],
            "payload": [], "originals": [], "journal": [],
            "backup": {"external": True, "location": "/external/backup",
                       "sha256": "d" * 64, "restore_command": "restore backup"},
        }
        validate_schema_instance(self.manifest, schema)

    def record(self, path):
        return {"kind": "file", "readable": True, "resolved_path": path,
                "uid": 0, "mode": "0600"}

    def probe(self, *, manifest=None, record=None, config_record=None,
              config="SYSTEMMODE=true\nRECOVERYMODE=false\n", version="30.7-kitsune-test:MAGISK:D"):
        text = json.dumps(self.manifest) if manifest is None else manifest
        records = {self.config: config_record or self.record(self.config)}
        with patch("tools.system_mode.doctor._read_optional",
                   side_effect=lambda client, path, **kw: text if path == self.path else config), \
             patch("tools.system_mode.doctor._probe_paths", return_value=records):
            return _versioned_install_config(
                object(), self.path, record or self.record(self.path), version, 30700, root=True
            )

    def test_verified_versioned_config_is_recognized_without_legacy_flat_file(self):
        self.assertEqual(self.config, self.probe())

    def test_incomplete_malformed_and_mismatched_manifests_remain_blocked(self):
        for field, value in (("state", "COMMITTED"), ("schema_version", 2)):
            manifest = copy.deepcopy(self.manifest)
            manifest[field] = value
            self.assertIsNone(self.probe(manifest=json.dumps(manifest)))
        for text in ("{}", "{", json.dumps(self.manifest)[:-1] + ',"state":"BOOT_VERIFIED"}'):
            self.assertIsNone(self.probe(manifest=text))
        self.assertIsNone(self.probe(version="other:MAGISK:D"))
        manifest = copy.deepcopy(self.manifest)
        manifest["strategies"]["active_payload"] = "/data/local/tmp/../../payload"
        self.assertIsNone(self.probe(manifest=json.dumps(manifest)))

    def test_redirected_or_writable_markers_and_conflicting_config_are_rejected(self):
        for field, value in (("uid", 2000), ("mode", "0666"),
                             ("resolved_path", "/data/local/tmp/fake"), ("kind", "symlink")):
            record = self.record(self.path)
            record[field] = value
            self.assertIsNone(self.probe(record=record))
            config_record = self.record(self.config)
            config_record[field] = value
            self.assertIsNone(self.probe(config_record=config_record))
        for config in ("", "SYSTEMMODE=false", "SYSTEMMODE=true\nSYSTEMMODE=false"):
            self.assertIsNone(self.probe(config=config))


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
        for candidate in candidates:
            candidate.update(uid=0, mode="0755", resolved_path=candidate["path"])
        self.assertEqual("/system/etc/init/hw", _select_init_directory(candidates))

    def test_init_selection_rejects_unsafe_or_unknown_ownership(self) -> None:
        fixture = json.loads((FIXTURES / "mumu-writable.json").read_text())
        for changes in ({"uid": 65534}, {"uid": None}, {"mode": "0775"},
                        {"mode": None}, {"resolved_path": "/data/local/tmp/init"}):
            with self.subTest(changes=changes):
                report = fixture_report(fixture["input"])
                report["init"]["candidate_directories"][0].update(changes)
                self.assertIsNone(_select_init_directory(report["init"]["candidate_directories"]))
                self.assertIn("INIT_PATH_UNAVAILABLE", classify_report(report)["reason_codes"])

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
                # Archives retain what the old probe actually observed. They
                # cannot supply ownership evidence added by the live probe.
                current = classify_report(record["doctor"])
                if current != record["doctor"]["assessment"]:
                    with self.assertRaisesRegex(ValueError, "stored assessment"):
                        validate_report(record["doctor"])
                    self.assertNotEqual("supported", current["verdict"])
                else:
                    validate_report(record["doctor"])
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
