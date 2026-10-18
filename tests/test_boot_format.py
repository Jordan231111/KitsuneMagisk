"""Classify independent compression headers with the production Rust detector."""
from pathlib import Path
import bz2
import gzip
import lzma
import os
import struct
import subprocess
import tempfile
import textwrap
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless((Path(os.environ.get("ANDROID_HOME", "")) / "ndk/magisk/ONDK_VERSION").is_file(),
                     "Requires configured ONDK")
class BootFormatTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="kitsune-boot-format-")
        cls.work = Path(cls.directory.name)
        source = (ROOT / "native/src/boot/format.rs").read_text()
        source = source[source.index("const CHROMEOS_MAGIC"):]
        bridge = (ROOT / "native/src/boot/lib.rs").read_text()
        start = bridge.index("    enum FileFormat {")
        enum = textwrap.dedent(bridge[start:bridge.index("\n    }", start) + 6])
        enum = enum.replace("enum FileFormat", "pub enum FileFormat", 1)
        harness = "#![allow(dead_code, non_camel_case_types)]\n"
        harness += "mod ffi { #[derive(Debug)]\n" + enum + "\n}\nuse ffi::FileFormat;\n"
        harness += source + r'''
fn main() -> std::io::Result<()> {
    let path = std::env::args().nth(1)
        .ok_or_else(|| std::io::Error::other("input required"))?;
    println!("{:?}", check_fmt(&std::fs::read(path)?));
    Ok(())
}
'''
        (cls.work / "format.rs").write_text(harness)
        result = subprocess.run([str(ROOT / "scripts/env.py"), "rustc", "--edition=2024",
                                 str(cls.work / "format.rs"), "-o", str(cls.work / "format")],
                                capture_output=True, text=True)
        if result.returncode:
            cls.directory.cleanup()
            raise RuntimeError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def classify(self, data):
        path = self.work / "input"
        path.write_bytes(data)
        return subprocess.check_output([str(self.work / "format"), str(path)],
                                       text=True, timeout=10).strip()

    def test_standard_streams_including_empty_input(self):
        for name, compress in (("GZIP", gzip.compress), ("XZ", lzma.compress),
                               ("BZIP2", bz2.compress)):
            for data in (b"", b"kernel fixture" * 100):
                with self.subTest(format=name, empty=not data):
                    self.assertEqual(name, self.classify(compress(data)))

    def test_xz_flags_and_crc_use_independent_oracle(self):
        for check in (0, 1, 4, 10):
            flags = bytes((0, check))
            header = b"\xfd7zXZ\0" + flags + struct.pack("<I", zlib.crc32(flags))
            self.assertEqual("XZ", self.classify(header))
            invalid = bytearray(header)
            invalid[8] ^= 1
            self.assertNotEqual("XZ", self.classify(invalid))
        for flags in (b"\x01\x00", b"\x00\x10", b"\x00\x02"):
            header = b"\xfd7zXZ\0" + flags + struct.pack("<I", zlib.crc32(flags))
            self.assertNotEqual("XZ", self.classify(header))

    def test_short_or_invalid_headers_do_not_match(self):
        cases = [("GZIP", b"\x1f\x8b"), ("XZ", b"\xfd7zXZ\0"),
                 ("BZIP2", b"BZh"), ("BZIP2", b"BZh0\x31\x41\x59\x26\x53\x59"),
                 ("LZOP", b"\x89LZOinvalid")]
        invalid_gzip = bytearray(gzip.compress(b"fixture"))
        invalid_gzip[3] |= 0x20
        cases.append(("GZIP", invalid_gzip))
        for kind, data in cases:
            with self.subTest(format=kind, data=data):
                self.assertNotEqual(kind, self.classify(data))
        self.assertEqual("UNKNOWN", self.classify(b""))
        self.assertEqual("GZIP", self.classify(b"\x1f\x9e"))
