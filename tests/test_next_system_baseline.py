import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import build

from tools.next_system_baseline import (
    BaselineError,
    cargo_lock_packages,
    read_manifest,
    verify_dependency_contract,
)


class NextSystemManifestTest(unittest.TestCase):
    def test_instrumentation_variants_use_distinct_output_paths(self):
        for release, variant in ((False, "debug"), (True, "release")):
            with self.subTest(variant=variant):
                fake_args = SimpleNamespace(release=release)
                target = Path("out") / f"test-{variant}.apk"
                with (
                    mock.patch.object(build, "args", fake_args, create=True),
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
