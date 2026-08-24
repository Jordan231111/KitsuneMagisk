import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import build
import tools.next_system_baseline as baseline

from tools.next_system_baseline import (
    BaselineError,
    cargo_lock_packages,
    read_manifest,
    verify_dependency_contract,
)


class NextSystemManifestTest(unittest.TestCase):
    def test_git_source_state_is_checked_and_build_races_remove_artifact(self):
        commit = "a" * 40
        clean_output = f"# branch.oid {commit}\n# branch.head next-system\n"
        clean = subprocess.CompletedProcess(
            ["git", "status"], 0, clean_output, ""
        )
        dirty = subprocess.CompletedProcess(
            ["git", "status"], 0, clean_output + "1 .M N... 100644 100644 "
            + f"100644 {commit} {commit} build.py\n", ""
        )
        failed = subprocess.CompletedProcess(
            ["git", "status"], 128, "", "not a repository"
        )
        with mock.patch.object(build.subprocess, "run", return_value=clean):
            self.assertEqual((commit, clean_output), build.git_source_state())
        with mock.patch.object(build.subprocess, "run", return_value=dirty):
            self.assertNotEqual((commit, clean_output), build.git_source_state())
        with mock.patch.object(build.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "not a repository"):
                build.git_source_state()

        source = Path("app/apk/build/outputs/apk/debug/apk-debug.apk")
        target = Path("out/app-debug.apk")
        with (
            mock.patch.object(
                build, "git_source_state", return_value=(commit, "changed")
            ),
            mock.patch.object(build, "rm") as remove,
            mock.patch.object(build, "error", side_effect=SystemExit(1)),
        ):
            with self.assertRaises(SystemExit):
                build.verify_source_state((commit, clean_output), source, target)
        self.assertEqual([mock.call(source), mock.call(target)], remove.call_args_list)

    def test_repository_build_caches_do_not_dirty_source_identity(self):
        patterns = Path(".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("/.gradle/", patterns)
        self.assertIn("/.sccache/", patterns)
        self.assertIn("__pycache__/", patterns)
        self.assertIn("*.py[cod]", patterns)

    def test_artifact_version_is_bound_to_current_head(self):
        commit = "12345678" + "a" * 32
        with mock.patch.object(baseline, "run", return_value=f"{commit}\n"):
            self.assertEqual(
                "30.7-kitsune-next.12345678",
                baseline.expected_source_version(Path(".")),
            )
        with mock.patch.object(baseline, "run", return_value="not-a-commit\n"):
            with self.assertRaisesRegex(BaselineError, "current source commit"):
                baseline.expected_source_version(Path("."))

    def test_instrumentation_variants_use_distinct_output_paths(self):
        for release, variant in ((False, "debug"), (True, "release")):
            with self.subTest(variant=variant):
                fake_args = SimpleNamespace(release=release)
                target = Path("out") / f"test-{variant}.apk"
                with (
                    mock.patch.object(build, "args", fake_args, create=True),
                    mock.patch.object(build, "config", {"outdir": Path("out")}),
                    mock.patch.object(build, "header"),
                    mock.patch.object(
                        build, "build_apk", return_value=target
                    ) as build_apk,
                    mock.patch.object(build, "cp") as copy_apk,
                ):
                    build.build_test()

                build_apk.assert_called_once_with(":test", f"test-{variant}.apk")
                copy_apk.assert_called_once_with(target, Path("out/test.apk"))
                self.assertEqual(release, fake_args.release)

    def test_manager_build_targets_the_final_output_path(self):
        for release, variant in ((False, "debug"), (True, "release")):
            with self.subTest(variant=variant):
                fake_args = SimpleNamespace(release=release)
                target = Path("out") / f"app-{variant}.apk"
                with (
                    mock.patch.object(build, "args", fake_args, create=True),
                    mock.patch.object(build, "config", {"outdir": Path("out")}),
                    mock.patch.object(build, "header"),
                    mock.patch.object(
                        build, "build_apk", return_value=target
                    ) as build_apk,
                    mock.patch.object(build, "cp"),
                ):
                    build.build_app()
                build_apk.assert_called_once_with(":apk", f"app-{variant}.apk")

    def test_avd_offline_mode_uses_the_resolved_ramdisk_path(self):
        source = Path("scripts/avd.sh").read_text(encoding="utf-8")
        setup = source[source.index("setup_emu()") : source.index("launch_emulator()")]
        self.assertIn("local installed_ramdisk=$3", setup)
        self.assertIn('setup_emu "$avd_pkg" "$ver" "$ramdisk"', source)
        self.assertNotIn("${avd_pkg//;", source)

    def test_avd_root_stress_is_timeout_bounded_and_checks_orphans(self):
        source = Path("scripts/test_common.sh").read_text(encoding="utf-8")
        self.assertIn("subprocess.TimeoutExpired", source)
        self.assertIn("AVD_STRESS_REQUEST_TIMEOUT", source)
        self.assertIn("assert_no_stale_su", source)
        self.assertIn("Stale MagiskSU process remained", source)
        self.assertNotIn(
            'run_root_stress_batch "$parallel" "$request_timeout" | tr', source
        )
        self.assertIn('raw=$(adb shell', source)
        self.assertIn('"$process/stat"', source)
        self.assertIn('unable-to-read-ppid', source)
        self.assertIn('init_daemon_count=$((init_daemon_count + 1))', source)
        self.assertGreaterEqual(source.count("__kitsune_avd_su_stress__"), 2)
        scanner = source[
            source.index("assert_no_stale_su()") : source.index("run_root_stress()")
        ]
        self.assertNotIn("awk", scanner)

    def test_gradle_identity_uses_exact_git_dirty_status(self):
        plugin = Path("app/buildSrc/src/main/java/Plugin.kt").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            '"git", "status", "--porcelain=v1", "--untracked-files=normal"',
            plugin,
        )
        self.assertIn("check(process.waitFor() == 0)", plugin)
        self.assertIn('findProperty("expectedSourceCommit")', plugin)
        self.assertNotIn('findProperty("sourceDirty")', plugin)

    def test_build_cargo_propagates_offline_failure_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "native" / "src").mkdir(parents=True)
            failure = subprocess.CompletedProcess(
                ["cargo", "metadata", "--offline"], 37
            )
            original_cwd = Path.cwd()
            try:
                os.chdir(root)
                with (
                    mock.patch.object(
                        build,
                        "args",
                        SimpleNamespace(
                            commands=["--", "metadata", "--offline"], verbose=0
                        ),
                        create=True,
                    ),
                    mock.patch.object(build, "ensure_paths"),
                    mock.patch.object(
                        build, "llvm_tool", return_value=Path("/ondk/bin/clang")
                    ),
                    mock.patch.object(
                        build, "rust_sysroot", Path("/ondk/rust"), create=True
                    ),
                    mock.patch.object(build, "execv", return_value=failure) as execv,
                ):
                    with self.assertRaises(SystemExit) as raised:
                        build.cargo_cli()
            finally:
                os.chdir(original_cwd)

            self.assertEqual(raised.exception.code, 37)
            self.assertEqual(execv.call_args.args[0], ["cargo", "metadata", "--offline"])

    def test_manifest_extension_deep_merges_maps_and_replaces_lists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "base.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "identity": {"name": "base", "code": 7},
                        "paths": ["base"],
                    }
                ),
                encoding="utf-8",
            )
            (root / "child.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "extends": "base.json",
                        "identity": {"name": "child"},
                        "paths": ["child"],
                    }
                ),
                encoding="utf-8",
            )

            manifest = read_manifest(root / "child.json")

            self.assertEqual(manifest["identity"], {"name": "child", "code": 7})
            self.assertEqual(manifest["paths"], ["child"])

    def test_manifest_extension_cycle_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.json").write_text(
                json.dumps({"schema": 1, "extends": "b.json"}), encoding="utf-8"
            )
            (root / "b.json").write_text(
                json.dumps({"schema": 1, "extends": "a.json"}), encoding="utf-8"
            )

            with self.assertRaisesRegex(BaselineError, "inheritance cycle"):
                read_manifest(root / "a.json")

    def test_dependency_contract_requires_and_forbids_exact_versions(self):
        contract = {
            "dependency_contract": {
                "required": [{"name": "safe", "version": "2.0.0"}],
                "forbidden": [{"name": "unsafe", "version": "1.0.0"}],
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "native" / "src" / "Cargo.lock"
            lock.parent.mkdir(parents=True)
            lock.write_text(
                'version = 4\n\n[[package]]\nname = "safe"\nversion = "2.0.0"\n',
                encoding="utf-8",
            )

            result = verify_dependency_contract(root, contract)

            self.assertTrue(result["enforced"])
            self.assertEqual(cargo_lock_packages(lock), {("safe", "2.0.0")})
            lock.write_text(
                lock.read_text(encoding="utf-8")
                + '\n[[package]]\nname = "unsafe"\nversion = "1.0.0"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(BaselineError, "present_forbidden"):
                verify_dependency_contract(root, contract)


if __name__ == "__main__":
    unittest.main()
