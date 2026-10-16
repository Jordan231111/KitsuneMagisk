"""Run the actual module store and early-RC parser with interruption fixtures."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

@unittest.skipUnless((Path(os.environ.get("ANDROID_HOME", "")) / "ndk/magisk/ONDK_VERSION").is_file(), "Requires ONDK")
class ModuleContractTest(unittest.TestCase):
    def test_native_contract(self):
        with tempfile.TemporaryDirectory(prefix="kitsune-module-contract-") as temporary:
            path = Path(temporary)
            source = path / "main.rs"
            source.write_text(f'''#[path = "{ROOT}/native/src/init/early_contract.rs"] mod early;
#[path = "{ROOT}/native/src/core/module_store.rs"] mod store;
#[test]
fn publication_and_recovery() {{
    use std::fs;
    let root = std::path::Path::new("{path}/modules");
    let pending = std::path::Path::new("{path}/pending");
    fs::create_dir_all(root.join("demo")).unwrap();
    fs::create_dir_all(pending.join("demo")).unwrap();
    fs::write(root.join("demo/value"), "old").unwrap();
    fs::write(root.join("demo/disable"), "").unwrap();
    fs::write(pending.join("demo/value"), "new").unwrap();
    store::publish(root, &pending.join("demo"), "demo").unwrap();
    assert_eq!(fs::read_to_string(root.join("demo/value")).unwrap(), "new");
    assert!(root.join("demo/disable").exists());
    assert!(!root.join(".rollback-demo").exists());
    // Power loss after preserving old module, before publishing replacement.
    fs::rename(root.join("demo"), root.join(".rollback-demo")).unwrap();
    store::recover(root, ".rollback-").unwrap();
    assert_eq!(fs::read_to_string(root.join("demo/value")).unwrap(), "new");
    // Power loss after publication, before removing old module.
    fs::create_dir(root.join(".rollback-demo")).unwrap();
    fs::write(root.join(".rollback-demo/value"), "old").unwrap();
    store::recover(root, ".rollback-").unwrap();
    assert_eq!(fs::read_to_string(root.join("demo/value")).unwrap(), "new");
    assert!(!root.join(".rollback-demo").exists());
    assert!(store::publish(root, &pending.join("missing"), "demo").is_err());
    assert_eq!(fs::read_to_string(root.join("demo/value")).unwrap(), "new");
    assert!(store::publish(root, &pending, "../escape").is_err());
    // Interrupted replacement of a pending (not yet booted) update.
    fs::create_dir(pending.join(".previous-demo")).unwrap();
    fs::write(pending.join(".previous-demo/value"), "pending").unwrap();
    store::recover(pending, ".previous-").unwrap();
    assert_eq!(fs::read_to_string(pending.join("demo/value")).unwrap(), "pending");
}}
''')
            binary = path / "module-test"
            result = subprocess.run([str(ROOT / "scripts/env.py"), "rustc", "--edition=2024", "--test", str(source), "-o", str(binary)], capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)

class ModuleTemplateTest(unittest.TestCase):
    def test_capabilities_and_runtime_route_both_gate_installation(self):
        with tempfile.TemporaryDirectory(prefix="kitsune-template-") as temporary:
            module = Path(temporary)
            (module / "root").mkdir()
            (module / "early-mount").mkdir()
            (module / "boot-completed.sh").touch()
            script = '''
magisk() {
  case "$1" in
    --features) printf '%s\\n' "$CAPABILITIES" ;;
    --early-mount-status) return "$ROUTE_STATUS" ;;
    *) return 1 ;;
  esac
}
abort() { echo "$*" >&2; exit 42; }
. "$TEMPLATE"
'''
            caps = "kitsune.module-root.v2\nkitsune.early-mount.v2\nkitsune.boot-completed.v1"
            env = dict(os.environ, MODPATH=str(module),
                       TEMPLATE=str(ROOT / "tools/module-template/customize.sh"))
            for capabilities, route, expected in ((caps, "0", 0), (caps, "1", 42),
                                                  ("", "0", 42),
                                                  (caps.replace("kitsune.module-root.v2", "kitsune.module-root.v1"), "0", 42)):
                result = subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True,
                                        env=dict(env, CAPABILITIES=capabilities, ROUTE_STATUS=route))
                self.assertEqual(expected, result.returncode, result.stderr)


class PreinitStorageTest(unittest.TestCase):
    def test_legacy_persist_aliases_and_read_only_fallback(self):
        source=(ROOT/'native/src/init/mount.cpp').read_text()
        start=source.index('void MagiskInit::mount_preinit_dir() noexcept {')
        end=source.index('\nbool MagiskInit::mount_system_root()',start)
        program=r'''
#include <cassert>
#include <cstdio>
#include <cstdint>
#include <string>
#include <vector>
#include <strings.h>
using namespace std;
#define LOGE(...) ((void)0)
#define LOGW(...) ((void)0)
#define LOGD(...) ((void)0)
#define PLOGE(...) ((void)0)
#define S_IFBLK 060000
#define MS_RDONLY 1
#define MS_BIND 4096
#define MNT_DETACH 2
#define F_OK 0
#define PREINITDEV "device/preinit"
#define MIRRDIR "mirror"
#define PREINITMIRR "preinit"
uint64_t makedev(int major,int minor) { return (uint64_t(major)<<32)|minor; }
int nodes=0, mounts=0, binds=0;
bool mounted=false;
uint64_t device=makedev(8,1);
struct devinfo { int major,minor; char partname[32],dmname[32]; };
vector<devinfo> dev_list;
int strscpy(char *out,const char *in,size_t size) { return snprintf(out,size,"%s",in); }
int xmknod(const char*,int,uint64_t) { ++nodes; return 0; }
int xmkdir(const char*,int) { return 0; }
int xmkdirs(const char*,int) { return 0; }
int xmount(const char*,const char*,const char*,unsigned long flags,const void*) {
    assert(flags==MS_BIND); ++binds; return 0;
}
int mount(const char*,const char*,const char*,unsigned long flags,const void*) {
    assert(flags==MS_RDONLY); ++mounts; return -1;
}
int access(const char*,int) { return -1; }
int xumount2(const char*,int) { return 0; }
string resolve_preinit_dir(const char*) { return "owned/magisk"; }
namespace rust { bool is_device_mounted(uint64_t,string &path) { path="/data"; return mounted; } }
struct MagiskInit {
    string preinit_dev="sda1";
    uint64_t find_block(const char*) { return device; }
    void mount_preinit_dir() noexcept;
};
''' + source[start:end] + r'''
int main() {
    MagiskInit init;
    for (auto entry : vector<devinfo>{{8,1,"persist",""},{8,1,"PERSIST_A",""},{8,1,"","persist"}}) {
        dev_list={entry}; nodes=mounts=binds=0;
        init.mount_preinit_dir();
        assert(nodes==1 && mounts==0 && binds==0);
    }
    // An unrelated persist disk must not block the selected metadata device.
    dev_list={{8,2,"persist",""},{8,1,"metadata",""}};
    nodes=mounts=binds=0;
    init.mount_preinit_dir();
    assert(nodes==1 && mounts==2 && binds==0);
    mounted=true; nodes=mounts=binds=0;
    init.mount_preinit_dir();
    assert(nodes==1 && mounts==0 && binds==1);
    mounted=false; device=0; nodes=mounts=binds=0;
    init.mount_preinit_dir();
    assert(nodes==0 && mounts==0 && binds==0);
}
'''
        with tempfile.TemporaryDirectory(prefix='kitsune-preinit-storage-') as temporary:
            path=Path(temporary); cpp=path/'main.cpp'; binary=path/'test'
            cpp.write_text(program)
            result=subprocess.run(['c++','-std=c++20',str(cpp),'-o',str(binary)],capture_output=True,text=True)
            self.assertEqual(0,result.returncode,result.stderr)
            result=subprocess.run([str(binary)],capture_output=True,text=True,timeout=10)
            self.assertEqual(0,result.returncode,result.stderr)
