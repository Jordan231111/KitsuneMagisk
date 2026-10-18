"""Exercise the production zygote peer gate against synthetic proc/sys trees."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

from tests.test_selinux_guard import function, prefix, s as disabled

ROOT = Path(__file__).resolve().parents[1]


class ZygoteIdentityTest(unittest.TestCase):
    @unittest.skipUnless((Path(os.environ.get("ANDROID_HOME", "")) / "ndk/magisk/ONDK_VERSION").is_file(),
                         "Requires configured ONDK; also runs in the artifact build job")
    def test_unlabeled_peers_require_root_and_verified_ancestry(self):
        permissive = function('native/src/core/selinux.rs', 'pub(crate) fn unlabeled_permissive()')
        permissive = permissive.replace('unlabeled_permissive()', 'unlabeled_permissive(root: &std::path::Path)')
        permissive = permissive.replace('std::fs::read_to_string("/proc/1/attr/current")',
                                        'std::fs::read_to_string(root.join("proc/1/attr/current"))')
        permissive = permissive.replace('std::fs::read_to_string(path)',
                                        "std::fs::read_to_string(root.join(path.trim_start_matches('/')))")
        peer = function('native/src/core/daemon.rs', 'fn is_zygote_peer(')
        peer = peer.replace('context: &str)', 'context: &str, root: &std::path::Path)')
        peer = peer.replace('crate::selinux::selinux_disabled()', 'selinux_disabled(root)')
        peer = peer.replace('crate::selinux::unlabeled_permissive()', 'unlabeled_permissive(root)')
        identity = function('native/src/core/daemon.rs', 'fn is_unlabeled_zygote(')
        identity = identity.replace('format!("/proc/{pid}")',
                                    'format!("{}/proc/{pid}", std::env::var("KITSUNE_ZYGOTE_TEST_ROOT").unwrap())')
        main = r'''
use std::path::Path;
fn write(root: &Path, path: &str, data: &str) {
    let path = root.join(path);
    std::fs::create_dir_all(path.parent().unwrap()).unwrap();
    std::fs::write(path, data).unwrap();
}
fn process(root: &Path, pid: i32, name: &str, exe: &str, parent: i32, uid: &str) {
    write(root, &format!("proc/{pid}/cmdline"), &format!("{name}\0"));
    write(root, &format!("proc/{pid}/status"), &format!("Name:\t{name}\nUid:\t{uid}\n"));
    write(root, &format!("proc/{pid}/stat"), &format!("{pid} (name with ) inside) S {parent} 0 0\n"));
    let link = root.join(format!("proc/{pid}/exe"));
    std::fs::remove_file(&link).ok();
    std::os::unix::fs::symlink(exe, link).unwrap();
}
fn main() {
    let root = std::path::PathBuf::from(std::env::var("KITSUNE_ZYGOTE_TEST_ROOT").unwrap());
    let root = root.as_path();
    let kernel = "u:r:kernel:s0";
    let exe = "/system/bin/app_process64";
    write(root, "proc/1/attr/current", "u:r:kernel:s0\0");
    write(root, "proc/self/attr/current", "u:r:kernel:s0\0");
    write(root, "sys/fs/selinux/enforce", "0");
    process(root, 100, "zygote64", exe, 1, "0 0 0 0");
    process(root, 101, "zygote64", exe, 100, "0 0 0 0");
    process(root, 102, "usap64", exe, 100, "0 0 0 0");
    for pid in [100, 101, 102] {
        assert!(is_zygote_peer(0, pid, kernel, root));
        assert!(!is_zygote_peer(2000, pid, kernel, root));
        assert!(!is_zygote_peer(0, pid, "u:r:shell:s0", root));
    }
    // Normal SELinux domain authentication remains authoritative.
    assert!(is_zygote_peer(1000, 101, "u:r:zygote:s0", root));
    for pid in [-1, 0, 1, 999] { assert!(!is_zygote_peer(0, pid, kernel, root)); }
    for mode in ["1", "garbage", "", "0 extra"] {
        write(root, "sys/fs/selinux/enforce", mode);
        assert!(!is_zygote_peer(0, 101, kernel, root));
    }
    write(root, "sys/fs/selinux/enforce", "0");
    write(root, "selinux/enforce", "1");
    assert!(!is_zygote_peer(0, 101, kernel, root));
    std::fs::remove_file(root.join("selinux/enforce")).unwrap();
    for context in ["u:r:init:s0\0", "", "invalid"] {
        write(root, "proc/1/attr/current", context);
        assert!(!is_zygote_peer(0, 101, kernel, root));
    }
    write(root, "proc/1/attr/current", "u:r:kernel:s0\0");
    for (name, image, parent, uid) in [
        ("ordinary_app", exe, 100, "0 0 0 0"),
        ("zygote64", "/data/local/tmp/app_process64", 100, "0 0 0 0"),
        ("zygote64", exe, 100, "10000 10000 10000 10000"),
        ("zygote64", exe, 100, "2000 0 0 0"),
        ("zygote64", exe, 100, "0 0 0"),
        ("zygote64", exe, 999, "0 0 0 0"),
        ("usap64", exe, 1, "0 0 0 0"),
    ] {
        process(root, 101, name, image, parent, uid);
        assert!(!is_zygote_peer(0, 101, kernel, root));
    }
    process(root, 101, "zygote64", exe, 100, "0 0 0 0");
    process(root, 103, "zygote64", exe, 101, "0 0 0 0");
    assert!(!is_zygote_peer(0, 103, kernel, root));
    process(root, 100, "zygote64", exe, 1, "1000 1000 1000 1000");
    assert!(!is_zygote_peer(0, 101, kernel, root));
    process(root, 100, "zygote64", "/system/bin/app_process32", 1, "0 0 0 0");
    assert!(!is_zygote_peer(0, 101, kernel, root));
    process(root, 100, "zygote64", exe, 1, "0 0 0 0");
    write(root, "proc/101/stat", "malformed");
    assert!(!is_zygote_peer(0, 101, kernel, root));
    process(root, 101, "zygote64", exe, 100, "0 0 0 0");
    std::fs::remove_file(root.join("sys/fs/selinux/enforce")).unwrap();
    std::fs::create_dir(root.join("sys/fs/selinux/enforce")).unwrap();
    assert!(!is_zygote_peer(0, 101, kernel, root));
    std::fs::remove_dir(root.join("sys/fs/selinux/enforce")).unwrap();
    assert!(!is_zygote_peer(0, 101, kernel, root));
    // The same ancestry proof covers a truly disabled SELinux environment.
    write(root, "proc/self/attr/current", "");
    assert!(is_zygote_peer(0, 101, "", root));
    assert!(!is_zygote_peer(2000, 101, "", root));
}
'''
        with tempfile.TemporaryDirectory(prefix='kitsune-zygote-identity-') as directory:
            path = Path(directory)
            source = path / 'test.rs'
            source.write_text(prefix + disabled + permissive + peer + identity + main)
            compiled = subprocess.run([str(ROOT / 'scripts/env.py'), 'rustc', '--edition=2024',
                                       str(source), '-o', str(path / 'test')], capture_output=True, text=True)
            self.assertEqual(0, compiled.returncode, compiled.stdout + compiled.stderr)
            subprocess.run([str(path / 'test')], check=True, timeout=10,
                           env=dict(os.environ, KITSUNE_ZYGOTE_TEST_ROOT=str(path / 'fs')))
