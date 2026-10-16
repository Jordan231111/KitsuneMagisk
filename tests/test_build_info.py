import copy
import json
import shutil
import struct
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import build
from tools import build_info


class BuildInfoTest(unittest.TestCase):
    def setUp(self):
        self.data = build_info.read_spec()
        self.data.update(source_commit="a" * 40, source_digest="b" * 64,
                         source_dirty=False, version_name="0.1.0-dev-kitsune.aaaaaaaa",
                         release_tag="kitsune-canary-" + "a" * 40)

    def test_app_upgrade_does_not_change_module_epoch(self):
        before = build_info.variant_info(self.data, "debug")
        self.data["app_version_code"] += 1
        after = build_info.variant_info(self.data, "debug")
        self.assertNotEqual(before["build_id"], after["build_id"])
        self.assertEqual(before["compat_version_code"], after["compat_version_code"])
        self.assertEqual(before["protocol_version"], after["protocol_version"])

    def test_variant_capabilities_and_build_ids_are_distinct(self):
        debug = build_info.variant_info(self.data, "debug")
        release = build_info.variant_info(self.data, "release")
        self.assertNotEqual(debug["build_id"], release["build_id"])
        self.assertIn("kitsune.system-mode.transaction.v1", debug["capabilities"])
        self.assertNotIn("kitsune.system-mode.transaction.v1", release["capabilities"])
        self.assertIn("kitsune.early-mount.v2", debug["capabilities"])
        self.assertIn("kitsune.module-root.v2", debug["capabilities"])

    def test_shell_identity_preserves_distinct_integer_contracts(self):
        info = build_info.variant_info(self.data, "debug")
        result = subprocess.check_output([
            "sh", "-c", build_info.shell_info(info) +
            '\nprintf "%s\\n" "$MAGISK_VER_CODE" "$KITSUNE_APP_VERSION_CODE" '
            '"$KITSUNE_PROTOCOL_VERSION" "$KITSUNE_BUILD_ID" "$KITSUNE_SOURCE_DIRTY"',
        ], text=True).splitlines()
        self.assertEqual(result, ["31000", str(info["app_version_code"]), "1", info["build_id"], "false"])

    def test_invalid_specification_is_rejected(self):
        spec = build_info.read_spec()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for key, value in [("app_version_code", 31000), ("protocol_version", True),
                               ("upstream_commit", "bad"), ("product_version", "31.0"),
                               ("channel", "$(id)"), ("capabilities", ["future"]),
                               ("product_name", 'name"#;')]:
                with self.subTest(key=key):
                    altered = dict(spec, **{key: value})
                    (root / "kitsune-build.json").write_text(json.dumps(altered))
                    with self.assertRaises(ValueError):
                        build_info.read_spec(root)

    def test_upstream_epoch_and_tag_must_match_source(self):
        spec = build_info.read_spec()
        def git(root, *args):
            if args[0] == "show":
                return b"magisk.versionCode=99999\n"
            return b""
        with mock.patch.object(build_info, "git", side_effect=git):
            with self.assertRaisesRegex(ValueError, "Compatibility epoch"):
                build_info.identity()
        def wrong_tag(root, *args):
            if args[0] == "show":
                return f'magisk.versionCode={spec["compat_version_code"]}\n'.encode()
            return b"0" * 40
        with mock.patch.object(build_info, "git", side_effect=wrong_tag):
            with self.assertRaisesRegex(ValueError, "Upstream version"):
                build_info.identity()

    def test_fork_clone_does_not_require_upstream_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = build_info.read_spec()
            (root / "app").mkdir()
            (root / "app/gradle.properties").write_text(
                f'magisk.versionCode={spec["compat_version_code"]}\n')
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "add", "app/gradle.properties"], cwd=root, check=True)
            subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                            "commit", "-qm", "Upstream fixture"], cwd=root, check=True)
            spec["upstream_commit"] = build_info.git(root, "rev-parse", "HEAD").decode().strip()
            (root / "kitsune-build.json").write_text(json.dumps(spec))
            identity = build_info.identity(root)
            self.assertEqual(identity["upstream_commit"], spec["upstream_commit"])

    def test_existing_apk_tools_do_not_regenerate_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.prop"
            config.write_text(f"outdir={directory}/out\n")
            for action in (build.patch_avd_file, build.setup_avd, build.setup_ndk, build.cleanup):
                with self.subTest(action=action.__name__), \
                     mock.patch.object(build, "args", SimpleNamespace(func=action, build=False,
                                       release=False, config=config), create=True), \
                     mock.patch.object(build, "config", {}), \
                     mock.patch.object(build, "build_abis", {}, create=True), \
                     mock.patch.object(build_info, "generate", side_effect=AssertionError("Source build attempted")):
                    build.load_config()
                    self.assertNotIn("buildInfo", build.config)

    def test_building_before_patch_still_validates_source(self):
        def fail(message):
            raise ValueError(message)
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.prop"
            config.write_text(f"outdir={directory}/out\n")
            with mock.patch.object(build, "args", SimpleNamespace(func=build.patch_avd_file,
                                   build=True, release=False, config=config), create=True), \
                 mock.patch.object(build, "config", {}), \
                 mock.patch.object(build_info, "generate", side_effect=ValueError("Missing source history")), \
                 mock.patch.object(build, "error", side_effect=fail), \
                 self.assertRaisesRegex(ValueError, "Missing source history"):
                build.load_config()

    def test_dirty_build_id_changes_when_contents_change_at_same_head(self):
        for field in ("source_digest", "source_commit", "protocol_version"):
            with self.subTest(field=field):
                before = build_info.variant_info(self.data, "debug")
                altered = copy.deepcopy(self.data)
                altered[field] = 2 if field == "protocol_version" else "c" * len(altered[field])
                self.assertNotEqual(before["build_id"], build_info.variant_info(altered, "debug")["build_id"])

    def test_untracked_content_is_part_of_source_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "new.kt"
            def git(root, *args):
                if args == ("rev-parse", "HEAD"):
                    return b"a" * 40
                if args[0] == "status":
                    return b"?? new.kt\n"
                if args[:2] == ("ls-files", "--others"):
                    return b"new.kt\0"
                return b""
            with mock.patch.object(build_info, "git", side_effect=git):
                file.write_text("first")
                first = build_info.source_state(root)
                file.write_text("second")
                second = build_info.source_state(root)
            self.assertTrue(first["source_dirty"])
            self.assertEqual(first["source_commit"], second["source_commit"])
            self.assertNotEqual(first["source_digest"], second["source_digest"])

    def test_unchanged_generation_keeps_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "info.json"
            build_info.write_if_changed(path, "same\n")
            before = path.stat().st_mtime_ns
            build_info.write_if_changed(path, "same\n")
            self.assertEqual(before, path.stat().st_mtime_ns)

    def test_release_emulator_build_requires_signing_before_identity(self):
        def fail(message):
            raise ValueError(message)
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(build, "args", SimpleNamespace(func=build.patch_avd_file,
                                   build=True, release=True, config=Path(directory) / "missing.prop"), create=True), \
                 mock.patch.object(build, "config", {}), \
                 mock.patch.object(build_info, "generate") as generate, \
                 mock.patch.object(build, "error", side_effect=fail), \
                 self.assertRaisesRegex(ValueError, "all four external signing properties"):
                build.load_config()
            generate.assert_not_called()

    def test_release_signing_requires_external_complete_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "checkout"
            root.mkdir()
            key = parent / "key.p12"
            key.write_bytes(b"not a real key; path validation only")
            config = parent / "signing.prop"
            config.touch()
            fields = dict(keyStore=str(key), keyStorePass="test", keyAlias="test", keyPass="test")
            build_info.validate_signing_config(fields, config, root)
            for field in fields:
                missing = dict(fields)
                del missing[field]
                with self.subTest(missing=field), self.assertRaises(ValueError):
                    build_info.validate_signing_config(missing, config, root)
            with self.assertRaises(ValueError):
                build_info.validate_signing_config(dict(fields, keyAlias="androiddebugkey"), config, root)
            internal = root / "signing.prop"
            internal.touch()
            with self.assertRaises(ValueError):
                build_info.validate_signing_config(fields, internal, root)
            internal_key = root / "key.p12"
            internal_key.write_bytes(b"fixture")
            with self.assertRaises(ValueError):
                build_info.validate_signing_config(dict(fields, keyStore=str(internal_key)), config, root)

    @unittest.skipUnless(shutil.which("rustc"), "Requires Rust for the native APK parser regression")
    def test_native_manager_compatibility_is_distinct_from_bad_signature(self):
        source = (build_info.ROOT / "native/src/core/package.rs").read_text()
        parser = source[source.index("fn read_certificate("):source.index("fn find_apk_path(")]
        # Compile the production parser with standard-I/O adapters for its two
        # base reader traits; fixtures exercise real APK block/comment offsets.
        program = r'''
use std::fs::File;
use std::io::{self, BufRead, Cursor, Read, Seek, SeekFrom};
use std::mem::size_of_val;
const EOCD_MAGIC: u32 = 0x06054B50;
const APK_SIGNING_BLOCK_MAGIC: [u8; 16] = *b"APK Sig Block 42";
const SIGNATURE_SCHEME_V2_MAGIC: u32 = 0x7109871A;
const PROTOCOL_VERSION: i32 = 1;
macro_rules! bad_apk {
    ($msg:literal) => { io::Error::new(io::ErrorKind::InvalidData, $msg) };
}
trait ReadExt: Read {
    fn read_pod<T>(&mut self, value: &mut T) -> io::Result<()> {
        let bytes = unsafe {
            std::slice::from_raw_parts_mut((value as *mut T).cast(), size_of_val(value))
        };
        self.read_exact(bytes)
    }
}
impl<T: Read> ReadExt for T {}
trait BufReadExt: BufRead {
    fn for_each_prop(&mut self, mut f: impl FnMut(&str, &str) -> bool) {
        for line in self.lines() {
            let line = line.expect("fixture line");
            if let Some((key, value)) = line.split_once('=') {
                if !f(key, value) { break; }
            }
        }
    }
}
impl<T: BufRead> BufReadExt for T {}
'''
        program += parser + r'''
fn main() {
    let path = std::env::args().nth(1).expect("fixture path");
    let mut file = File::open(path).expect("fixture open");
    match read_certificate(&mut file, 31001) {
        Ok(cert) => assert_eq!(cert, b"test-certificate"),
        Err(e) => {
            assert_eq!(e.kind(), io::ErrorKind::Unsupported);
            std::process::exit(42);
        }
    }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "parser.rs").write_text(program)
            subprocess.run(["rustc", "--edition=2021", str(root / "parser.rs"), "-o", str(root / "parser")],
                           check=True, capture_output=True, timeout=60)
            cert = b"test-certificate"
            value = struct.pack("<IIIII", 0, 0, 0, 0, len(cert) + 4) + struct.pack("<I", len(cert)) + cert
            pair = struct.pack("<QI", len(value) + 4, 0x7109871A) + value
            size = len(pair) + 24
            block = struct.pack("<Q", size) + pair + struct.pack("<Q", size) + b"APK Sig Block 42"
            for code, protocol, expected in [(31001, 1, 0), (31002, 1, 0), (31000, 1, 42),
                                              (31001, 2, 42), (31001, 0, 42)]:
                with self.subTest(code=code, protocol=protocol):
                    comment = f"versionCode={code}\nprotocolVersion={protocol}\n".encode()
                    eocd = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, 0, 0, 0, len(block), len(comment))
                    (root / "fixture.apk").write_bytes(block + eocd + comment)
                    result = subprocess.run([str(root / "parser"), str(root / "fixture.apk")], capture_output=True)
                    self.assertEqual(expected, result.returncode, result.stderr.decode())


if __name__ == "__main__":
    unittest.main()
