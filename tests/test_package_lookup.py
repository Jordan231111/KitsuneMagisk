"""Run the production package lookup with a disappearing directory entry."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PackageLookupTest(unittest.TestCase):
    @unittest.skipUnless((Path(os.environ.get("ANDROID_HOME", "")) / "ndk/magisk/ONDK_VERSION").is_file(),
                         "Requires configured ONDK")
    def test_package_removal_does_not_hide_another_installed_manager(self):
        source = (ROOT / "native/src/core/package.rs").read_text()
        lookup = source[source.index("fn find_apk_path_in("):source.index("\nenum Status")]
        # Adapt only the filesystem/string boundary. The lookup is unchanged.
        harness = r'''
#![allow(dead_code, non_camel_case_types)]
use std::collections::VecDeque;
type Utf8CString = String;
type LoggedResult<T> = Result<T, Error>;
mod cstr { pub type buf = String; }
macro_rules! cstr { ($s:literal) => { $s }; }
#[derive(Debug, PartialEq, Clone, Copy)]
enum Errno { ENOENT, EACCES }
#[derive(Debug)]
struct Error { errno: Errno }
struct Entry { name: String, child: Result<Directory, Error> }
impl Entry {
    fn name(&self) -> &str { &self.name }
    fn is_dir(&self) -> bool { true }
    fn resolve_path(&self, buf: &mut String) -> LoggedResult<()> {
        *buf = format!("/data/app/{}", self.name);
        Ok(())
    }
    fn open_as_dir(self) -> LoggedResult<Directory> { self.child }
}
struct Directory { entries: VecDeque<Entry> }
impl Directory {
    fn open(_: &str) -> LoggedResult<Self> { unreachable!() }
    fn read(&mut self) -> LoggedResult<Option<Entry>> { Ok(self.entries.pop_front()) }
    fn new(entries: Vec<Entry>) -> Self { Self { entries: entries.into() } }
}
fn entry(name: &str, children: Vec<Entry>) -> Entry {
    Entry { name: name.into(), child: Ok(Directory::new(children)) }
}
fn missing(errno: Errno) -> Entry {
    Entry { name: "~~removed-original".into(), child: Err(Error { errno }) }
}
'''
        main = r'''
fn main() {
    let pkg = "repackaged.io.github.jordan231111.magisk";
    let manager = || entry("~~hidden", vec![entry(&format!("{pkg}-token"), vec![])]);
    // The original package was listed, then removed before openat.
    let mut dir = Directory::new(vec![missing(Errno::ENOENT), manager()]);
    assert_eq!(find_apk_path_in(&mut dir, pkg).unwrap(),
               format!("/data/app/{pkg}-token/base.apk"));
    // Pre-Android 11 layout and exact package-prefix boundaries.
    let mut dir = Directory::new(vec![entry(pkg, vec![]),
        entry(&format!("{pkg}.other-token"), vec![]), entry(&format!("{pkg}-legacy"), vec![])]);
    assert_eq!(find_apk_path_in(&mut dir, pkg).unwrap(),
               format!("/data/app/{pkg}-legacy/base.apk"));
    let mut dir = Directory::new(vec![missing(Errno::ENOENT)]);
    assert!(find_apk_path_in(&mut dir, pkg).unwrap().is_empty());
    // A failed scan must remain an error, not a claim that the manager is absent.
    let mut dir = Directory::new(vec![missing(Errno::EACCES), manager()]);
    assert_eq!(find_apk_path_in(&mut dir, pkg).unwrap_err().errno, Errno::EACCES);
}
'''
        with tempfile.TemporaryDirectory(prefix="kitsune-package-lookup-") as directory:
            path = Path(directory)
            (path / "test.rs").write_text(harness + lookup + main)
            result = subprocess.run([str(ROOT / "scripts/env.py"), "rustc", "--edition=2024",
                                     str(path / "test.rs"), "-o", str(path / "test")],
                                    capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            subprocess.run([str(path / "test")], check=True, timeout=10)
