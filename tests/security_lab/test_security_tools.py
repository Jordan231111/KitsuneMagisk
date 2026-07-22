from __future__ import annotations

import copy
import json
from pathlib import Path
import random
import unittest

from tools.security_lab.inventory import (
    _gradle_catalog,
    _merge_gradle_packages,
    _resolved_gradle_packages,
    build_spdx,
)
from tools.security_lab.licenses import _map_license
from tools.security_lab.rustsec import build_report
from tools.security_lab.signature_contract import analyze_ref
from tools.security_lab.submodules import parse_raw_gitlinks, validate_missing
from tools.security_lab.git import GitRepository
from tools.system_mode.doctor import (
    _effective_mount,
    classify_report,
    fixture_report,
    parse_mountinfo,
    validate_report,
)


ROOT = Path(__file__).resolve().parents[2]


class InventoryParserTest(unittest.TestCase):
    def test_every_indexed_finding_has_a_unique_readable_proof(self) -> None:
        index = json.loads((ROOT / "security" / "findings.json").read_text(encoding="utf-8"))
        ids = [finding["id"] for finding in index["findings"]]
        self.assertEqual(len(ids), len(set(ids)))
        for finding_id in ids:
            with self.subTest(finding=finding_id):
                proofs = list((ROOT / "security" / "findings").glob(f"{finding_id}-*.md"))
                self.assertEqual(1, len(proofs))

    def test_pom_license_aliases_are_spdx_normalized(self) -> None:
        self.assertEqual("Apache-2.0", _map_license(["Apache License 2.0"]))
        self.assertEqual("Apache-2.0", _map_license(["The Apache License, Version 2.0"]))
        self.assertEqual("MIT", _map_license(["Bouncy Castle Licence"]))

    def test_gradle_resolution_tracks_selected_versions(self) -> None:
        report = r"""
+--- com.example:alpha:1.0 -> 1.2
|    \--- com.example:child:2.0
\--- androidx.core:core-ktx:1.13.1
"""
        packages = _resolved_gradle_packages(report)
        versions = {(item["name"], item["version"]) for item in packages}
        self.assertEqual(
            {
                ("com.example:alpha", "1.2"),
                ("com.example:child", "2.0"),
                ("androidx.core:core-ktx", "1.13.1"),
            },
            versions,
        )

    def test_gradle_inventory_retains_build_plugins_and_selected_versions(self) -> None:
        declared = [
            {
                "ecosystem": "gradle",
                "name": "com.example:runtime",
                "version": "1.0",
                "source": "build.gradle.kts",
                "checksum": None,
                "direct": True,
            },
            {
                "ecosystem": "gradle",
                "name": "com.example:build-plugin",
                "version": "2.0",
                "source": "buildSrc/build.gradle.kts",
                "checksum": None,
                "direct": True,
            },
        ]
        resolved = _resolved_gradle_packages(
            "+--- com.example:runtime:1.0 -> 1.1\n"
            "\\--- com.example:transitive:3.0\n"
        )
        merged = _merge_gradle_packages(declared, resolved)
        self.assertEqual(
            {
                ("com.example:runtime", "1.1", True),
                ("com.example:transitive", "3.0", False),
                ("com.example:build-plugin", "2.0", True),
            },
            {(item["name"], item["version"], item["direct"]) for item in merged},
        )

    def test_version_catalog_resolves_version_references(self) -> None:
        packages = _gradle_catalog(
            """
[versions]
okhttp = "4.12.0"
[libraries]
okhttp = { module = "com.squareup.okhttp3:okhttp", version.ref = "okhttp" }
direct = "junit:junit:4.13.2"
"""
        )
        self.assertEqual(
            {
                ("com.squareup.okhttp3:okhttp", "4.12.0"),
                ("junit:junit", "4.13.2"),
            },
            {(item["name"], item["version"]) for item in packages},
        )

    def test_spdx_ids_are_unique(self) -> None:
        inventory = {
            "baseline": {
                "commit": "a" * 40,
                "packages": [
                    {
                        "ecosystem": "cargo",
                        "name": "same",
                        "version": "1.0",
                        "source": "registry+https://github.com/rust-lang/crates.io-index",
                        "checksum": None,
                        "license": "MIT",
                    },
                    {
                        "ecosystem": "gradle",
                        "name": "same:same",
                        "version": "1.0",
                        "source": "declared",
                        "checksum": None,
                        "license": "Apache-2.0",
                    },
                ],
            }
        }
        spdx = build_spdx(inventory)
        ids = [package["SPDXID"] for package in spdx["packages"]]
        self.assertEqual(len(ids), len(set(ids)))
        refs = {
            package["name"]: package["externalRefs"][0]["referenceLocator"]
            for package in spdx["packages"]
        }
        self.assertEqual("pkg:cargo/same@1.0", refs["cargo:same"])
        self.assertEqual("pkg:maven/same/same@1.0", refs["gradle:same:same"])


class RustSecPolicyTest(unittest.TestCase):
    def test_unknown_advisory_fails_closed(self) -> None:
        audit = {
            "database": {"advisory-count": 1, "last-commit": "a" * 40},
            "lockfile": {"dependency-count": 1},
            "vulnerabilities": {
                "list": [
                    {
                        "advisory": {"id": "RUSTSEC-2099-0001", "title": "future"},
                        "package": {"name": "future", "version": "1.0"},
                    }
                ]
            },
            "warnings": {},
        }
        policy = {"audit_ignore_allowed": False, "advisories": {}}
        _report, errors = build_report(audit, policy, root=ROOT)
        self.assertEqual(1, len(errors))
        self.assertIn("undisposed advisory", errors[0])


class SubmoduleAuditTest(unittest.TestCase):
    def test_raw_gitlink_parser_keeps_both_sides_of_a_pin_change(self) -> None:
        old = "1" * 40
        new = "2" * 40
        output = f":160000 160000 {old} {new} M\tnative/src/external/example\n"
        self.assertEqual(
            [
                ("native/src/external/example", old),
                ("native/src/external/example", new),
            ],
            parse_raw_gitlinks(output),
        )

    def test_new_or_restored_missing_gitlinks_fail_closed(self) -> None:
        report = {
            "gitlinks": [
                {"path": "one", "commit": "1" * 40, "remote_reachable": False},
                {"path": "new", "commit": "2" * 40, "remote_reachable": False},
            ]
        }
        policy = {
            "expected_unavailable": [
                {"path": "one", "commit": "1" * 40},
                {"path": "restored", "commit": "3" * 40},
            ]
        }
        errors = validate_missing(report, policy)
        self.assertEqual(2, len(errors))
        self.assertFalse(report["validation"]["passed"])
        self.assertEqual("new", report["validation"]["unknown_unavailable"][0]["path"])
        self.assertEqual(
            "restored",
            report["validation"]["restored_but_still_disposed"][0]["path"],
        )


class SignatureContractTest(unittest.TestCase):
    def test_current_bypass_and_pristine_stable_enforcement_are_explicit(self) -> None:
        repo = GitRepository(ROOT)
        current = analyze_ref(repo, "f943ecddd11d0e648877ffb1f917c16767b8f7cc")
        stable = analyze_ref(repo, "v30.7")
        self.assertTrue(current["global_bypass_detected"])
        self.assertFalse(current["release_signature_enforced"])
        self.assertTrue(stable["release_signature_enforced"])
        self.assertTrue(stable["normal_replacement_rejected"])
        self.assertTrue(stable["hidden_dyn_replacement_rejected"])
        self.assertTrue(stable["hidden_identity_bound"])
        self.assertTrue(stable["trusted_stub_recovery_present"])


class DoctorPropertyTest(unittest.TestCase):
    def test_seeded_mountinfo_mutations_do_not_confuse_stack_selection(self) -> None:
        randomizer = random.Random(0xD0C70A)
        for index in range(500):
            mount_point = f"/system/dir\\040{index}"
            base_id = 100 + index * 2
            lines = [
                f"{base_id} 1 8:1 / {mount_point} ro,nodev - ext4 /dev/block/dm-{index} ro",
                f"{base_id + 1} 1 0:42 / {mount_point} rw - overlay overlay rw",
            ]
            if randomizer.choice((True, False)):
                lines.reverse()
            mounts = parse_mountinfo("\n".join(lines))
            effective = _effective_mount(f"/system/dir {index}/bin/tool", mounts)
            self.assertIsNotNone(effective)
            expected = mounts[-1]
            self.assertEqual(expected["mount_id"], effective["mount_id"])

    def test_classifier_is_deterministic_across_seeded_capability_mutations(self) -> None:
        fixture_path = ROOT / "tools" / "system_mode" / "fixtures" / "mumu-writable.json"
        values = json.loads(fixture_path.read_text(encoding="utf-8"))["input"]
        randomizer = random.Random(0x51A7E)
        for _ in range(350):
            mutated = copy.deepcopy(values)
            mutated["layout"]["system_fs_type"] = randomizer.choice(
                ["ext4", "erofs", "squashfs", "overlay"]
            )
            mutated["layout"]["verity"] = randomizer.choice(["enforcing", "disabled", "unknown"])
            mutated["bootstrap"]["available"] = randomizer.choice([True, False])
            mutated["selinux"]["strategy"] = randomizer.choice(
                ["precompiled", "monolithic", "unsupported"]
            )
            report = fixture_report(mutated)
            first = classify_report(report)
            second = classify_report(copy.deepcopy(report))
            self.assertEqual(first, second)
            report["assessment"] = first
            validate_report(report)


if __name__ == "__main__":
    unittest.main()
