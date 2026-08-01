from __future__ import annotations

from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

from tools.security_lab.artifact_contract import (
    ABI_ELF_IDENTITIES,
    ArtifactContractError,
    REQUIRED_APP_LIBRARIES,
    analyze_apk,
    daemon_identity,
    elf_identity_and_load_alignments,
    elf_load_alignments,
    missing_required_libraries,
    utility_identity,
    verify_contract,
    verify_system_mode_surface,
    verify_version_identity,
    zip_entry_data_offset,
)


def elf64(*alignments: int, machine: int = 183) -> bytes:
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", header, 18, machine)
    struct.pack_into("<Q", header, 32, 64)
    struct.pack_into("<H", header, 54, 56)
    struct.pack_into("<H", header, 56, len(alignments))
    entries = bytearray()
    for alignment in alignments:
        entry = bytearray(56)
        struct.pack_into("<I", entry, 0, 1)
        struct.pack_into("<Q", entry, 48, alignment)
        entries.extend(entry)
    return bytes(header + entries)


def elf32(*alignments: int, machine: int = 40) -> bytes:
    header = bytearray(52)
    header[:6] = b"\x7fELF\x01\x01"
    struct.pack_into("<H", header, 18, machine)
    struct.pack_into("<I", header, 28, 52)
    struct.pack_into("<H", header, 42, 32)
    struct.pack_into("<H", header, 44, len(alignments))
    entries = bytearray()
    for alignment in alignments:
        entry = bytearray(32)
        struct.pack_into("<I", entry, 0, 1)
        struct.pack_into("<I", entry, 28, alignment)
        entries.extend(entry)
    return bytes(header + entries)


def with_debug_mode_marker(script: bytes) -> bytes:
    return script.replace(b":MAGISK:D\n", b":MAGISK:D \n")


class ElfAlignmentContractTest(unittest.TestCase):
    def test_reads_every_load_segment_alignment(self) -> None:
        self.assertEqual([0x4000, 0x10000], elf_load_alignments(elf64(0x4000, 0x10000)))
        self.assertEqual([0x4000], elf_load_alignments(elf32(0x4000)))

    def test_reads_class_and_machine_for_every_supported_abi(self) -> None:
        for abi, (elf_class, machine) in ABI_ELF_IDENTITIES.items():
            data = (
                elf32(0x4000, machine=machine)
                if elf_class == 1
                else elf64(0x4000, machine=machine)
            )
            self.assertEqual(
                (elf_class, machine, [0x4000]),
                elf_identity_and_load_alignments(data),
                abi,
            )

    def test_rejects_truncated_program_header_table(self) -> None:
        with self.assertRaisesRegex(ArtifactContractError, "truncated"):
            elf_load_alignments(elf64(0x4000)[:-1])

    def test_rejects_non_elf_input(self) -> None:
        with self.assertRaisesRegex(ArtifactContractError, "not an ELF"):
            elf_load_alignments(b"not an elf")

    def test_required_payload_matrix_detects_a_missing_abi_library(self) -> None:
        native = {
            f"lib/{abi}/{library}": [0x4000]
            for abi, libraries in REQUIRED_APP_LIBRARIES.items()
            for library in libraries
        }
        self.assertEqual({}, missing_required_libraries(native))
        native.pop("lib/x86_64/libmagisk64.so")
        self.assertEqual(
            {"x86_64": ["libmagisk64.so"]},
            missing_required_libraries(native),
        )


class VersionIdentityContractTest(unittest.TestCase):
    utility = b"MAGISK_VER='31.0-kitsune'\nMAGISK_VER_CODE=31000\n"
    release = b"prefix\x0031.0-kitsune:MAGISK:R (31000)\x00suffix"

    def test_parses_utility_and_daemon_identity(self) -> None:
        self.assertEqual(("31.0-kitsune", 31000), utility_identity(self.utility))
        self.assertEqual(
            ("31.0-kitsune", 31000, "R"), daemon_identity(self.release)
        )

    def test_accepts_one_coherent_identity_across_abis(self) -> None:
        self.assertEqual(
            {"name": "31.0-kitsune", "code": 31000, "daemon_mode": "R"},
            verify_version_identity(
                self.utility,
                {"arm64": self.release, "x86_64": self.release},
                "R",
            ),
        )

    def test_rejects_stale_native_version(self) -> None:
        stale = b"bcdf65f0:MAGISK:R (31000)\x00"
        with self.assertRaisesRegex(ArtifactContractError, "manager/native"):
            verify_version_identity(self.utility, {"arm64": stale}, "R")

    def test_rejects_identity_without_kitsune_integration_marker(self) -> None:
        utility = b"MAGISK_VER='a669bbbc'\nMAGISK_VER_CODE=31000\n"
        daemon = b"a669bbbc:MAGISK:R (31000)\x00"
        with self.assertRaisesRegex(ArtifactContractError, "Kitsune identity"):
            verify_version_identity(utility, {"arm64": daemon}, "R")

    def test_rejects_wrong_variant_mode(self) -> None:
        with self.assertRaisesRegex(ArtifactContractError, "expects daemon mode"):
            verify_version_identity(self.utility, {"arm64": self.release}, "D")


class ApkArtifactContractTest(unittest.TestCase):
    def test_reads_native_library_data_offset_from_local_zip_header(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-zip-offset-") as temp:
            apk = Path(temp) / "offset.apk"
            name = "lib/arm64-v8a/libexample.so"
            base_offset = 30 + len(name.encode("utf-8"))
            padding = (-base_offset) % 16_384
            self.assertGreaterEqual(padding, 4)
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_STORED
            info.extra = struct.pack("<HH", 0xFFFF, padding - 4) + bytes(padding - 4)
            with zipfile.ZipFile(apk, "w") as archive:
                archive.writestr(info, elf64(0x4000))
            with zipfile.ZipFile(apk) as archive:
                offset = zip_entry_data_offset(archive, archive.getinfo(name))
            self.assertEqual(0, offset % 16_384)

    def test_rejects_unaligned_uncompressed_native_library_in_zip(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-unaligned-apk-") as temp:
            root = Path(temp)
            apk = root / "stub-debug.apk"
            signer = root / "apksigner"
            signer.touch()
            with zipfile.ZipFile(apk, "w") as archive:
                archive.writestr(
                    "lib/arm64-v8a/libexample.so",
                    elf64(0x4000),
                    compress_type=zipfile.ZIP_STORED,
                )
            with self.assertRaisesRegex(ArtifactContractError, "ZIP offset"):
                analyze_apk(apk, signer, 16_384)

    def test_system_mode_entry_points_are_variant_gated(self) -> None:
        recovery = with_debug_mode_marker(b"""
"$MODE_BINARY" -c
:MAGISK:D
remove_system_su
rm -f ./manager.sh
unzip -oj archive res/raw/manager.sh
[ -f ./manager.sh ]
. ./manager.sh || abort
""")
        addon = with_debug_mode_marker(b"""
"$mode_binary" -c
:MAGISK:D
remove_system_su
rm -f ./manager.sh
unzip -oj archive res/raw/manager.sh
[ -f ./manager.sh ]
. ./manager.sh || abort
system_apk=$MAGISKBIN/magisk.apk
direct_install_system "$MAGISKBIN"
""")
        common = {
            "META-INF/com/google/android/updater-script": recovery,
            "assets/addon.d.sh": addon,
        }
        debug = dict(common)
        debug["res/raw/manager.sh"] = b'"$mode_binary" -c\n:MAGISK:D '

        self.assertTrue(
            verify_system_mode_surface(debug, "D")["debug_manager_present"]
        )
        self.assertFalse(
            verify_system_mode_surface(common, "R")["debug_manager_present"]
        )

    def test_system_mode_contract_rejects_a_stale_manager_source(self) -> None:
        unsafe = with_debug_mode_marker(b"""
"$MODE_BINARY" -c
:MAGISK:D
remove_system_su
unzip -oj archive res/raw/manager.sh
[ -f ./manager.sh ]
. ./manager.sh || abort
""")
        with self.assertRaisesRegex(ArtifactContractError, "stale or missing"):
            verify_system_mode_surface(
                {
                    "META-INF/com/google/android/updater-script": unsafe,
                    "assets/addon.d.sh": unsafe,
                },
                "R",
            )

    def test_system_mode_contract_rejects_daemon_version_gate(self) -> None:
        recovery = with_debug_mode_marker(b"""
"$MODE_BINARY" -v
:MAGISK:D
remove_system_su
rm -f ./manager.sh
unzip -oj archive res/raw/manager.sh
[ -f ./manager.sh ]
. ./manager.sh || abort
""")
        addon = recovery.replace(b"$MODE_BINARY", b"$mode_binary")
        with self.assertRaisesRegex(ArtifactContractError, "before mutation"):
            verify_system_mode_surface(
                {
                    "META-INF/com/google/android/updater-script": recovery,
                    "assets/addon.d.sh": addon,
                },
                "R",
            )

    def test_duplicate_zip_entries_are_rejected_before_analysis(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-duplicate-apk-") as temp:
            root = Path(temp)
            apk = root / "stub-debug.apk"
            signer = root / "apksigner"
            signer.touch()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(apk, "w") as archive:
                    archive.writestr("classes.dex", b"first")
                    archive.writestr("classes.dex", b"second")
            with self.assertRaisesRegex(ArtifactContractError, "duplicate ZIP"):
                analyze_apk(apk, signer, 16_384)

    @patch("tools.security_lab.artifact_contract.analyze_apk")
    def test_expected_release_certificate_is_exact_and_rejected_keys_still_fail(
        self, analyze
    ) -> None:
        release_certificate = "b" * 64
        artifact = {
            "path": "/tmp/stub-release.apk",
            "size": 1,
            "sha256": "c" * 64,
            "certificate_sha256": release_certificate,
            "native_libraries": {},
        }
        analyze.return_value = artifact
        with tempfile.TemporaryDirectory(prefix="kitsune-signer-contract-") as temp:
            signer = Path(temp) / "apksigner"
            signer.touch()
            report = verify_contract(
                [Path("stub-release.apk")], signer, 16_384, set(), release_certificate.upper()
            )
            self.assertTrue(report["validation"]["passed"])

            with self.assertRaisesRegex(ArtifactContractError, "production identity"):
                verify_contract(
                    [Path("stub-release.apk")], signer, 16_384, set(), "d" * 64
                )
            with self.assertRaisesRegex(ArtifactContractError, "forbidden signer"):
                verify_contract(
                    [Path("stub-release.apk")],
                    signer,
                    16_384,
                    {release_certificate},
                )


if __name__ == "__main__":
    unittest.main()
