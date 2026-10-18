from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.port_ledger import LedgerError, audit, audit_assets, history, read_json, validate_ledger


SEED = {"SYS-001": ("Persistent install", "PR7")}


class PortLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="kitsune-port-ledger-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.git("init", "-q")
        self.base = self.commit({"native/install.sh": "original\n"})
        self.endpoint = self.commit({"native/install.sh": "changed\n"}, self.base)
        # A published branch can contain behavior absent from the final endpoint.
        self.published = self.commit({"native/install.sh": "original\n",
                                      "native/removed.sh": "older release only\n"}, self.base)
        self.ledger = {
            "schema": 1, "ancestor": self.base, "upstream": self.base,
            "endpoint": self.endpoint, "candidate": self.endpoint,
            "observations": [{"id": "older-release", "kind": "git", "label": "old release",
                              "commit": self.published}],
            "features": [{
                "id": "SYS-001", "behavior": "Persistent install", "historical_status": ["E", "O"],
                "upstream_overlap": "No persistent install in the base", "affected_users": "System Mode users",
                "migration": "Retain current line until qualification", "decision": "deferred-to-PR15",
                "owner_pr": 15, "evidence": ["older-release"],
                "first_shipped": "older-release", "last_shipped": "older-release",
                "upstream_evidence": ["fixture base has no persistent install"],
            }],
            "path_reviews": [],
        }
        (self.root / "DEVELOPMENT_ROADMAP.md").write_text(
            "| Ledger tag / observed incarnation | Exact commit |\n"
            f"| old release | `{self.published}` |\n\n"
            "## Mandatory PR8 atomic seed\n| `SYS-001` | Persistent install | PR7 |\n"
            "# Proposed pull-request sequence\n", encoding="utf-8",
        )

    def git(self, *args: str) -> str:
        env = dict(os.environ, GIT_AUTHOR_NAME="Test", GIT_COMMITTER_NAME="Test",
                   GIT_AUTHOR_EMAIL="test@example.invalid", GIT_COMMITTER_EMAIL="test@example.invalid")
        return subprocess.check_output(["git", *args], cwd=self.root, env=env).decode("utf-8").strip()

    def commit(self, files: dict[str, str], parent: str | None = None) -> str:
        for path, content in files.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self.git("add", "native")
        tree = self.git("write-tree")
        return self.git("commit-tree", tree, *(["-p", parent] if parent else []), "-m", "fixture")

    def review(self) -> None:
        self.ledger["path_reviews"] = [
            {"commit": item["commit"], **path, "features": ["SYS-001"],
             "rationale": "Install behavior is assigned to the qualification owner"}
            for item in history(self.root, self.ledger) for path in item["paths"]
        ]

    def test_audit_includes_behavior_from_a_published_side_branch(self) -> None:
        report = audit(self.root, self.ledger)
        self.assertFalse(report["passed"])
        self.assertEqual({self.endpoint, self.published}, {row["commit"] for row in report["history"]})
        self.assertIn("native/removed.sh", {gap["path"] for gap in report["gaps"]})

    def test_path_reviews_are_bound_to_the_exact_patch_and_cannot_go_stale(self) -> None:
        self.review()
        self.assertTrue(audit(self.root, self.ledger)["passed"])
        self.ledger["path_reviews"][0]["patch_sha256"] = "0" * 64
        self.assertIn("changed-review", {row["kind"] for row in audit(self.root, self.ledger)["gaps"]})
        self.ledger["path_reviews"][0]["commit"] = self.base
        kinds = {row["kind"] for row in audit(self.root, self.ledger)["gaps"]}
        self.assertTrue({"stale-review", "unreviewed-path"} <= kinds)

    def test_release_rejects_deferral_even_after_history_is_fully_accounted_for(self) -> None:
        self.review()
        self.assertTrue(audit(self.root, self.ledger)["passed"])
        release = audit(self.root, self.ledger, release=True)
        self.assertFalse(release["passed"])
        self.assertEqual([{ "kind": "deferred-feature", "feature": "SYS-001" }], release["gaps"])

    def test_unavailable_target_remains_a_release_blocker(self) -> None:
        self.review()
        row = self.ledger["features"][0]
        row.update(decision="unqualified-target", owner_pr=16, qualification_blocker={
            "target": "Physical recovery target", "reason": "No matching target available",
            "evidence": "docs/device-qualification.md", "implementation_retained": True,
        })
        evidence = self.root / "docs/device-qualification.md"
        evidence.parent.mkdir()
        evidence.write_text("No matching recovery target was available.\n")
        self.assertTrue(audit(self.root, self.ledger)["passed"])
        release = audit(self.root, self.ledger, release=True)
        self.assertFalse(release["passed"])
        self.assertEqual([{"kind": "unqualified-target", "feature": "SYS-001"}], release["gaps"])
        for update in ({"owner_pr": 15}, {"qualification_blocker": {}},
                       {"acceptance_evidence": "claimed pass"}):
            with self.subTest(update=update):
                ledger = copy.deepcopy(self.ledger)
                ledger["features"][0].update(update)
                with self.assertRaises(LedgerError):
                    validate_ledger(ledger, SEED)
        evidence.unlink()
        self.assertFalse(audit(self.root, self.ledger)["passed"])

    def test_archive_digest_must_survive_git_text_conversion(self) -> None:
        self.review()
        (self.root / ".gitattributes").write_bytes(b"* text eol=lf\n")
        archive = self.root / "release.json"
        data = b'{"version": 1}\r\n'
        archive.write_bytes(data)
        self.ledger["observations"].append({
            "id": "release-record", "kind": "archive", "label": "Release byte record",
            "path": "release.json", "origin": "fixture archive",
            "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        })
        gaps = audit(self.root, self.ledger)["gaps"]
        self.assertEqual([{"kind": "archive-checkout-changes-bytes",
                           "observation": "release-record"}], gaps)
        # Byte archives can opt out of conversion; generated records can use LF.
        (self.root / ".gitattributes").write_bytes(b"* text eol=lf\n/release.json -text\n")
        self.assertTrue(audit(self.root, self.ledger)["passed"])

    def test_missing_seed_unknown_evidence_and_unnumbered_deferrals_fail(self) -> None:
        for update in ({"id": "SYS-002"}, {"evidence": ["invented"]},
                       {"decision": "triage"}, {"decision": "deferred-to-PR16", "owner_pr": 16},
                       {"decision": "deferred-to-PR14"}):
            with self.subTest(update=update):
                ledger = copy.deepcopy(self.ledger)
                ledger["features"][0].update(update)
                with self.assertRaises(LedgerError):
                    validate_ledger(ledger, SEED)

    def test_removing_a_frozen_release_does_not_shrink_the_audit_universe(self) -> None:
        self.ledger["observations"][0]["commit"] = self.endpoint
        with self.assertRaisesRegex(LedgerError, "missing frozen release"):
            audit(self.root, self.ledger)

    def test_a_source_reference_does_not_prove_runtime_acceptance(self) -> None:
        self.ledger["features"][0]["decision"] = "inherited-and-tested"
        with self.assertRaisesRegex(LedgerError, "acceptance evidence"):
            validate_ledger(self.ledger, SEED)

    def test_duplicate_release_names_keep_distinct_source_incarnations(self) -> None:
        self.ledger["observations"].append({"id": "rebuilt-release", "kind": "git", "label": "old release",
                                            "commit": self.endpoint})
        validate_ledger(self.ledger, SEED)
        self.ledger["observations"][1]["id"] = "older-release"
        with self.assertRaisesRegex(LedgerError, "duplicate observation"):
            validate_ledger(self.ledger, SEED)

    def test_archived_metadata_is_checked_by_size_and_digest(self) -> None:
        self.review()
        path = self.root / "channel.json"
        content = b'{"version":"historical"}\n'
        self.ledger["observations"].append({
            "id": "channel", "kind": "archive", "label": "Archived channel",
            "path": "channel.json", "origin": "https://example.invalid/immutable/source",
            "sha256": hashlib.sha256(content).hexdigest(), "size": len(content),
        })
        self.assertEqual("missing-archive", audit(self.root, self.ledger)["gaps"][0]["kind"])
        path.write_bytes(content)
        self.assertTrue(audit(self.root, self.ledger)["passed"])
        path.write_bytes(content.replace(b"historical", b"substitute"))
        self.assertEqual("changed-archive", audit(self.root, self.ledger)["gaps"][0]["kind"])

    def test_asset_provenance_gaps_cannot_be_hidden_by_complete_git_reviews(self) -> None:
        self.review()
        self.ledger["provenance_gaps"] = ["Original Full APK is unavailable"]
        self.assertEqual("provenance", audit(self.root, self.ledger)["gaps"][0]["kind"])

    def test_accounted_archive_limit_still_blocks_release(self) -> None:
        self.review()
        self.ledger["archive_limits"] = [{
            "id": "lost-publisher", "features": ["SYS-001"], "owner_pr": 10,
            "evidence": ["older-release"], "finding": "Only an archived mirror survives",
            "disposition": "Archive only; qualify owned-source replacement and migration in PR10",
        }]
        self.assertTrue(audit(self.root, self.ledger)["passed"])
        self.assertIn("unresolved-archive-disposition",
                      {r["kind"] for r in audit(self.root, self.ledger, release=True)["gaps"]})
        self.ledger["archive_limits"][0]["evidence"] = ["invented"]
        with self.assertRaisesRegex(LedgerError, "valid evidence"):
            validate_ledger(self.ledger, SEED)

    def test_shipment_bounds_must_belong_to_the_feature_evidence(self) -> None:
        self.ledger["observations"].append({"id": "unrelated", "kind": "git",
                                            "label": "unrelated", "commit": self.base})
        self.ledger["features"][0]["first_shipped"] = "unrelated"
        with self.assertRaisesRegex(LedgerError, "included in its evidence"):
            validate_ledger(self.ledger, SEED)

    def test_path_accounting_does_not_replace_shipment_or_semantic_review(self) -> None:
        self.review()
        self.ledger["features"][0].update(first_shipped=None, upstream_evidence=[])
        kinds = {row["kind"] for row in audit(self.root, self.ledger)["gaps"]}
        self.assertEqual({"unverified-shipment", "unreviewed-upstream-behavior"}, kinds)

    def test_json_duplicates_and_archive_path_escape_fail(self) -> None:
        path = self.root / "ledger.json"
        path.write_text('{"schema":1,"schema":1}', encoding="utf-8")
        with self.assertRaisesRegex(LedgerError, "duplicate JSON"):
            read_json(path)
        for unsafe in ("../channel.json", "/channel.json", "C:/channel.json", "a\\channel.json"):
            with self.subTest(path=unsafe):
                self.ledger["observations"] = [{
                    "id": "older-release", "kind": "archive", "label": "Channel",
                    "path": unsafe, "origin": "archive", "size": 1, "sha256": "a" * 64,
                }]
                with self.assertRaisesRegex(LedgerError, "repository-relative"):
                    validate_ledger(self.ledger, SEED)


class AssetInventoryTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="kitsune-asset-audit-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.ledger = {"observations": []}
        self.commit = "1" * 40
        self.metadata_path = f"docs/port-history/metadata/{self.commit}/canary.json"
        self.assets = []
        self.refs = []
        entries = []
        metadata = {}
        for role, filename, digest in (("magisk", "app.apk", "a"), ("stub", "stub.apk", "b")):
            url = f"https://example.invalid/{filename}"
            metadata[role] = {"link": url}
            self.assets.append({"file": filename, "size": 3, "sha256": digest * 64,
                                "archive_url": url})
            self.refs.append({"metadata_commit": self.commit, "metadata_path": "canary.json",
                              "role": role, "advertised_url": url, "status": "archived",
                              "git_blob": digest * 40, "size": 3, "sha256": digest * 64})
            entries.append({"path": filename, "type": "blob", "sha": digest * 40, "size": 3})
        self.write("metadata", metadata, self.metadata_path)
        obs = self.ledger["observations"][-1]
        self.index = [{"commit": self.commit, "path": "canary.json", "status": "archived",
                       "size": obs["size"], "sha256": obs["sha256"]}]
        self.references = {"trees": [{"commit": self.commit, "entries": entries}], "references": self.refs}
        self.write("full-lite-assets", {"artifacts": self.assets})
        self.write("moved-k4-assets", {"artifacts": []})
        self.write("original-stable", {"artifacts": [], "dead_endpoints": []})
        self.write("module-template", {"size": 7, "sha256": "c" * 64,
                                       "archive_url": "https://example.invalid/module.zip"})
        self.write("metadata-index", self.index)
        self.write("asset-references", self.references)
        self.roadmap = "| Archived artifact | Size | SHA-256 |\n| App | `3` | `" + "a" * 64 + "` |\n"

    def write(self, key: str, value: object, path: str | None = None) -> None:
        path = path or f"{key}.json"
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        data = (json.dumps(value) + "\n").encode()
        target.write_bytes(data)
        self.ledger["observations"] = [r for r in self.ledger["observations"] if r["id"] != key]
        self.ledger["observations"].append({
            "id": key, "kind": "archive", "path": path, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })

    def gaps(self) -> set[str]:
        return {r["kind"] for r in audit_assets(self.root, self.ledger, self.roadmap)}

    def test_complete_immutable_references_pass(self) -> None:
        self.assertEqual(set(), self.gaps())

    def test_removing_a_record_does_not_remove_a_frozen_artifact(self) -> None:
        self.write("full-lite-assets", {"artifacts": self.assets[1:]})
        self.assertEqual({"missing-frozen-artifact", "unbound-asset-reference"}, self.gaps())

    def test_dropped_or_redirected_reference_fails(self) -> None:
        self.references["references"] = self.refs[1:]
        self.write("asset-references", self.references)
        self.assertEqual({"unaccounted-asset-reference"}, self.gaps())
        self.refs[0]["advertised_url"] = "https://example.invalid/substitute.apk"
        self.references["references"] = self.refs
        self.write("asset-references", self.references)
        self.assertEqual({"unaccounted-asset-reference"}, self.gaps())

    def test_blob_mismatch_cannot_be_hidden_by_matching_version_label(self) -> None:
        self.refs[0]["git_blob"] = "f" * 40
        self.write("asset-references", self.references)
        self.assertEqual({"unbound-asset-reference"}, self.gaps())

    def test_missing_endpoint_requires_failed_retrieval_evidence(self) -> None:
        self.refs[0].update(status="lost-mutable-endpoint", evidence="original-stable", owner_pr=10,
                            disposition="Retire dead endpoint; no replacement-byte identity inferred")
        self.write("asset-references", self.references)
        self.assertEqual({"unproven-lost-asset"}, self.gaps())
        self.write("original-stable", {"artifacts": [], "dead_endpoints": [{
            "url": self.refs[0]["advertised_url"], "http_status": 404,
        }]})
        self.assertEqual(set(), self.gaps())

    def test_metadata_absence_must_agree_with_the_immutable_tree(self) -> None:
        self.index.append({"commit": self.commit, "path": "app.apk", "status": "absent-at-commit",
                           "http_status": 404})
        self.write("metadata-index", self.index)
        self.assertEqual({"unproven-metadata-absence"}, self.gaps())


if __name__ == "__main__":
    unittest.main()
