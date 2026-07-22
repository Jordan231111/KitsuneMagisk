from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.security_lab.git import GitRepository
from tools.security_lab.upstream_ledger import _toolchain_snapshot, build_ledger


def git(path: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return proc.stdout.strip()


def commit(path: Path, message: str, files: dict[str, str]) -> str:
    for name, contents in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-m", message)
    return git(path, "rev-parse", "HEAD")


class UpstreamLedgerTest(unittest.TestCase):
    def test_sensitive_commits_receive_a_disposition_and_gates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-ledger-") as temp:
            root = Path(temp)
            git(root, "init", "-b", "base")
            git(root, "config", "user.name", "Kitsune Test")
            git(root, "config", "user.email", "kitsune@example.test")
            base = commit(root, "base", {"README.md": "base\n"})

            git(root, "switch", "-c", "fork")
            commit(root, "fork socket", {"native/src/core/socket.cpp": "fork\n"})

            git(root, "switch", "-c", "stable", base)
            previous_stable = commit(root, "stable docs", {"docs/note.md": "stable\n"})
            stable = commit(root, "stable boot", {"native/src/boot/parser.rs": "stable\n"})

            git(root, "switch", "-c", "master", stable)
            commit(root, "master updater", {"app/core/NetworkService.kt": "master\n"})

            policy = {
                "official_repository": "https://example.test/upstream.git",
                "stable_tag": "stable",
                "previous_stable_tag": previous_stable,
                "previous_stable_commit": previous_stable,
                "stable_commit": stable,
                "audited_common_ancestor": base,
                "dispositions": {
                    lane: {"decision": f"{lane}-decision", "rationale": f"{lane}-reason"}
                    for lane in ("stable", "master", "fork")
                },
                "path_rules": [
                    {
                        "id": "boot",
                        "patterns": ["native/src/boot/**"],
                        "owner": "native-boot",
                        "sensitive": True,
                        "gates": ["boot-corpus"],
                    },
                    {
                        "id": "core",
                        "patterns": ["native/src/core/**"],
                        "owner": "native-core",
                        "sensitive": True,
                        "gates": ["daemon-test"],
                    },
                    {
                        "id": "network",
                        "patterns": ["app/**/NetworkService.kt"],
                        "owner": "app-update",
                        "sensitive": True,
                        "gates": ["redirect-test"],
                    },
                    {
                        "id": "docs",
                        "patterns": ["docs/**", "README.md"],
                        "owner": "docs",
                        "sensitive": False,
                        "gates": [],
                    },
                ],
            }
            ledger = build_ledger(
                GitRepository(root),
                policy,
                fork_ref="fork",
                stable_ref="stable",
                master_ref="master",
                previous_stable_ref=previous_stable,
                release={
                    "tag_name": "stable",
                    "published_at": "2026-01-01T00:00:00Z",
                    "html_url": "https://example.test/release",
                    "draft": False,
                    "prerelease": False,
                },
            )

            self.assertEqual(base, ledger["common_ancestor"])
            self.assertEqual({"left": 1, "right": 2}, ledger["counts"]["fork_vs_stable"])
            self.assertEqual(1, ledger["ranges"]["stable"]["sensitive_commit_count"])
            self.assertEqual(1, ledger["ranges"]["master"]["sensitive_commit_count"])
            self.assertEqual(1, ledger["ranges"]["fork"]["sensitive_commit_count"])
            self.assertEqual(
                1,
                ledger["ranges"]["release_to_release"]["sensitive_commit_count"],
            )
            for lane in ("stable", "master", "fork"):
                record = ledger["ranges"][lane]["sensitive_commits"][0]
                self.assertEqual(f"{lane}-decision", record["disposition"]["decision"])
                self.assertTrue(record["ownership"]["required_gates"])
            json.dumps(ledger)

    def test_release_change_is_machine_visible(self) -> None:
        policy = json.loads(
            (Path(__file__).resolve().parents[2] / "security" / "upstream-policy.json").read_text()
        )
        self.assertEqual("v30.7", policy["stable_tag"])
        self.assertEqual("v30.6", policy["previous_stable_tag"])
        self.assertRegex(policy["stable_commit"], r"^[a-f0-9]{40}$")
        self.assertRegex(policy["previous_stable_commit"], r"^[a-f0-9]{40}$")
        self.assertRegex(policy["fork_baseline_commit"], r"^[a-f0-9]{40}$")

    def test_frozen_rust_edition_and_unpinned_toolchain_are_visible(self) -> None:
        policy = json.loads(
            (Path(__file__).resolve().parents[2] / "security" / "upstream-policy.json").read_text()
        )
        snapshot = _toolchain_snapshot(
            GitRepository(Path(__file__).resolve().parents[2]),
            policy["fork_baseline_commit"],
        )
        self.assertEqual(["2021"], snapshot["rust_editions"])
        self.assertIsNone(snapshot["rust_toolchain"])
        self.assertFalse(snapshot["rust_toolchain_pinned"])


if __name__ == "__main__":
    unittest.main()
