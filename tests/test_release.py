import copy
import datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

from tools import build_info, release


class ReleaseQualificationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kitsune-release-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.info = {"source_commit": "a" * 40, "source_dirty": False,
                     "product_version": "0.1.0-dev", "capabilities": [], "debug_capabilities": []}
        self.artifacts = {"debug": {"sha256": "b" * 64}, "release": {"sha256": "c" * 64}}
        self.record = {"schema": release.device_qualification.SCHEMA,
                       "artifact": {"sha256": "c" * 64, "build": build_info.variant_info(self.info, "release")},
                       "guest": {"api": 35, "abi": "arm64-v8a", "page_size": 16384, "selinux": "Enforcing"},
                       "boots": [{"id": str(uuid.uuid4()), "phase": phase} for phase in ("setup", "accepted")]}
        self.policy = {"runtime": {"avd": {"variant": "release", "guest": dict(self.record["guest"]), "cold_boots": 2}}}
        self.acceptance = {"schema": 1, "source_commit": self.info["source_commit"],
                           "checks": {name: {"command": "test-command", "exit_code": 0,
                                             "log": self.save(f"{name}.log", b"test output\n")}
                                      for name in release.CHECKS},
                           "runtime": {"avd": self.save("avd.json", self.record)},
                           "mumu_lifecycle": self.save("lifecycle.json", {
                               "source_commit": self.info["source_commit"], "apk_sha256": "b" * 64,
                               **{key: True for key in ("suite_passed", "upgrade_preserved_data", "vendor_root_disabled",
                                                       "uninstall_passed", "original_disks_restored", "original_settings_restored")}})}

    def save(self, name, data):
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
        (self.root / name).write_bytes(data)
        return {"path": name, "sha256": hashlib.sha256(data).hexdigest()}

    def verify(self):
        release.verify_acceptance(self.acceptance, self.root, self.info, self.artifacts, self.policy)

    def test_exact_accepted_matrix(self):
        self.verify()

    def test_changed_evidence_is_rejected(self):
        (self.root / "avd.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "Evidence bytes changed"):
            self.verify()

    def test_missing_failed_or_empty_host_check_is_rejected(self):
        for change in ("missing", "failed", "empty", "boolean"):
            original = copy.deepcopy(self.acceptance)
            if change == "missing":
                del self.acceptance["checks"]["host"]
            elif change == "empty":
                self.acceptance["checks"]["host"]["log"] = self.save("empty.log", b"")
            else:
                self.acceptance["checks"]["host"]["exit_code"] = False if change == "boolean" else 1
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.verify()
            self.acceptance = original

    def test_runtime_requires_exact_apk_and_build_identity(self):
        for field, value in (("sha256", "d" * 64), ("build", {**self.record["artifact"]["build"], "source_dirty": True})):
            record = copy.deepcopy(self.record)
            record["artifact"][field] = value
            self.acceptance["runtime"]["avd"] = self.save("avd.json", record)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "different bytes"):
                self.verify()

    def test_runtime_cannot_reuse_boots_or_change_target(self):
        for change in ("same-boot", "incomplete", "page-size", "selinux"):
            record = copy.deepcopy(self.record)
            if change == "same-boot":
                record["boots"][1]["id"] = record["boots"][0]["id"]
            elif change == "incomplete":
                record["boots"][1]["phase"] = "cold-boot"
            elif change == "page-size":
                record["guest"]["page_size"] = 4096
            else:
                record["guest"]["selinux"] = "Permissive"
            self.acceptance["runtime"]["avd"] = self.save("avd.json", record)
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.verify()

    def test_unrestored_emulator_is_not_accepted(self):
        entry = self.acceptance["mumu_lifecycle"]
        data = json.loads(release.read_evidence(entry, self.root))
        data["original_disks_restored"] = False
        self.acceptance["mumu_lifecycle"] = self.save("lifecycle.json", data)
        with self.assertRaisesRegex(ValueError, "restoration"):
            self.verify()

    def test_old_source_and_missing_runtime_are_rejected(self):
        self.acceptance["source_commit"] = "e" * 40
        with self.assertRaises(ValueError):
            self.verify()
        self.acceptance["source_commit"] = self.info["source_commit"]
        self.acceptance["runtime"].clear()
        with self.assertRaises(ValueError):
            self.verify()

    def test_evidence_cannot_escape_through_paths_or_links(self):
        entry = self.save("log", b"log")
        with tempfile.TemporaryDirectory() as outside:
            file = Path(outside) / "log"
            file.write_bytes(b"log")
            (self.root / "link").symlink_to(file)
            for path in (str(file), "../log", "link"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    release.read_evidence({**entry, "path": path}, self.root)

    def audit_fixture(self):
        audits = {}
        for index, lock in enumerate(release.LOCKFILES):
            path = self.root / lock
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("lockfile fixture")
            report = {"vulnerabilities": {"count": 0, "found": False, "list": []}, "warnings": {}}
            audits[lock] = {"lock_sha256": release.baseline.sha256(path),
                            "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                            "report": self.save(f"audit-{index}.json", report)}
        return {"audits": audits}

    def test_audit_requires_all_current_lockfiles(self):
        evidence = self.audit_fixture()
        release.verify_audits(evidence, self.root, self.root)
        (self.root / release.LOCKFILES[0]).write_text("changed dependency")
        with self.assertRaisesRegex(ValueError, "predates"):
            release.verify_audits(evidence, self.root, self.root)

    def test_audit_exception_is_limited_to_native_rsa(self):
        evidence = self.audit_fixture()
        report = {"vulnerabilities": {"count": 1, "found": True, "list": [
            {"advisory": {"id": "RUSTSEC-2023-0071"}, "package": {"name": "rsa"}}]}, "warnings": {}}
        entry = self.save("rsa.json", report)
        evidence["audits"][release.LOCKFILES[0]]["report"] = entry
        release.verify_audits(evidence, self.root, self.root)
        evidence["audits"][release.LOCKFILES[1]]["report"] = entry
        with self.assertRaisesRegex(ValueError, "Unreviewed"):
            release.verify_audits(evidence, self.root, self.root)

    def test_stale_audit_is_rejected(self):
        evidence = self.audit_fixture()
        evidence["audits"][release.LOCKFILES[0]]["checked_at"] = "2020-01-01T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "seven days"):
            release.verify_audits(evidence, self.root, self.root)

    def test_receipt_rejects_changed_bytes_and_stable_relabel(self):
        apk, stub = self.root / "app.apk", self.root / "stub.apk"
        apk.write_bytes(b"tested manager")
        stub.write_bytes(b"tested stub")
        receipt = {"schema": "kitsune.release.v1", "source": self.info,
                   "eligible_channels": ["beta", "stable"], "production_blockers": ["physical qualification"],
                   "historical_gate": {"passed": False},
                   "verified_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   "artifacts": {"release": {"sha256": release.baseline.sha256(apk)},
                                 "stub_release": {"sha256": release.baseline.sha256(stub)}}}
        release.verify_receipt(receipt, self.info, apk, stub, "beta")
        with self.assertRaisesRegex(ValueError, "unresolved"):
            release.verify_receipt(receipt, self.info, apk, stub, "stable")
        apk.write_bytes(b"rebuilt manager")
        with self.assertRaisesRegex(ValueError, "different|differ"):
            release.verify_receipt(receipt, self.info, apk, stub, "beta")

    def test_skipped_ci_jobs_do_not_pass_product_gate(self):
        run = {"event": "pull_request", "conclusion": "success", "status": "completed",
               "path": ".github/workflows/build.yml", "head_sha": self.info["source_commit"], "html_url": "test"}
        jobs = {"total_count": 2, "jobs": [{"name": "Product gate", "conclusion": "success"},
                                          {"name": "Device tests", "conclusion": "skipped"}]}
        with mock.patch.object(release.subprocess, "check_output", side_effect=[json.dumps(run), json.dumps(jobs)]):
            with self.assertRaisesRegex(ValueError, "skipped"):
                release.verify_ci(1, self.info, "test/repository")


if __name__ == "__main__":
    unittest.main()
