from __future__ import annotations

import gzip
from pathlib import Path
import shlex
import subprocess
import struct
import unittest
from unittest.mock import Mock, patch

from tools.security_lab.device_corpus import (
    Adb,
    CorpusFailure,
    _command_result,
    _binary_paths,
    _safe_remote_root,
    make_dtb_tail_boot_image,
    make_minimal_boot_image,
    mutate,
    seed_corpora,
)


ROOT = Path(__file__).resolve().parents[2]


class DeviceCorpusTest(unittest.TestCase):
    def test_adb_output_replaces_non_utf8_device_bytes(self) -> None:
        adb = Adb("adb", "serial", 10)
        completed = subprocess.CompletedProcess([], 0, "bounded-\ufffd-name", "")
        with patch(
            "tools.security_lab.device_corpus.subprocess.run",
            return_value=completed,
        ) as run:
            result = adb.run("shell", "true")
        self.assertEqual("bounded-\ufffd-name", result.stdout)
        self.assertEqual("utf-8", run.call_args.kwargs["encoding"])
        self.assertEqual("replace", run.call_args.kwargs["errors"])

    def test_adb_shell_preserves_the_complete_remote_script_argument(self) -> None:
        adb = Adb("adb", "serial", 10)
        command = "mkdir -p /data/local/tmp/example && cd /data/local/tmp/example"
        completed = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(adb, "run", return_value=completed) as run:
            adb.shell(command)
        self.assertEqual(("shell", "sh", "-c", shlex.quote(command)), run.call_args.args)

    def test_known_good_roundtrip_requires_a_zero_exit(self) -> None:
        adb = Mock()
        adb.shell.return_value = subprocess.CompletedProcess([], 1, "", "repack failed")
        with self.assertRaisesRegex(CorpusFailure, "roundtrip failed"):
            _command_result(
                adb,
                "false",
                case_id="roundtrip",
                require_success=True,
            )

    def test_minimal_boot_image_has_consistent_v0_layout(self) -> None:
        image = make_minimal_boot_image()
        self.assertEqual(b"ANDROID!", image[:8])
        kernel_size = struct.unpack_from("<I", image, 8)[0]
        ramdisk_size = struct.unpack_from("<I", image, 16)[0]
        page_size = struct.unpack_from("<I", image, 36)[0]
        self.assertEqual(2048, page_size)
        self.assertGreater(kernel_size, 0)
        self.assertGreater(ramdisk_size, 0)
        ramdisk_offset = page_size + ((kernel_size + page_size - 1) // page_size) * page_size
        ramdisk = image[ramdisk_offset : ramdisk_offset + ramdisk_size]
        self.assertIn(b"TRAILER!!!", gzip.decompress(ramdisk))

    def test_dtb_magic_at_kernel_tail_stays_inside_declared_kernel(self) -> None:
        image = make_dtb_tail_boot_image()
        page_size = struct.unpack_from("<I", image, 36)[0]
        self.assertEqual(4, struct.unpack_from("<I", image, 8)[0])
        self.assertEqual(b"\xd0\x0d\xfe\xed", image[page_size : page_size + 4])

    def test_binary_directory_rejects_an_untrusted_device_abi(self) -> None:
        with self.assertRaisesRegex(CorpusFailure, "unsupported device ABI"):
            _binary_paths(Path("native/out"), "../../host-path")

    def test_mutation_is_seeded_and_size_bounded(self) -> None:
        first = mutate(b"ANDROID!" + b"x" * 100, 100, random_seed=1234)
        second = mutate(b"ANDROID!" + b"x" * 100, 100, random_seed=1234)
        self.assertEqual(first, second)
        self.assertTrue(all(len(value) <= 2 * 1024 * 1024 for value in first))
        self.assertGreater(len(set(first)), 80)

    def test_seed_corpora_cover_boot_and_policy(self) -> None:
        corpora = seed_corpora()
        self.assertGreaterEqual(len(corpora["boot"]), 8)
        self.assertGreaterEqual(len(corpora["policy"]), 6)
        self.assertTrue(any(value.startswith(b"ANDROID!") for value in corpora["boot"]))
        self.assertIn(b"ANDROID!", corpora["boot"])

    def test_remote_cleanup_root_is_narrow(self) -> None:
        self.assertEqual(
            "/data/local/tmp/kitsune-security-abc123",
            _safe_remote_root("/data/local/tmp/kitsune-security-abc123"),
        )
        for unsafe in (
            "/data/local/tmp",
            "/data/local/tmp/other",
            "/data/local/tmp/kitsune-security-../escape",
            "/data/adb/kitsune-security-test",
            "/",
        ):
            with self.subTest(path=unsafe), self.assertRaises(ValueError):
                _safe_remote_root(unsafe)

    def test_disposable_avd_uses_one_owned_explicit_home(self) -> None:
        source = (ROOT / "scripts" / "security_avd_test.sh").read_text(
            encoding="utf-8"
        )
        export = 'export ANDROID_AVD_HOME="$avd_home"'
        create = '-p "$avd_home/$name.avd"'
        self.assertIn(export, source)
        self.assertIn(create, source)
        self.assertIn('.kitsune-security-owned', source)
        self.assertNotIn('"$sdk" --channel=3 tools ', source)
        self.assertLess(source.index(export), source.index(create))


if __name__ == "__main__":
    unittest.main()
