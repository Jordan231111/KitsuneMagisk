from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

# Bridge the host's stat layout without reimplementing the production identity
# check. The Rust dispatcher below is extracted unchanged from the core source.
BRIDGE = r'''
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/xattr.h>
#include <unistd.h>

struct identity_stat { uint64_t st_dev, st_ino; };
static int fail_stat;
void test_fail_stat(int fail) { fail_stat = fail; }
int test_fstat(int fd, struct identity_stat *out) {
    if (fail_stat) { errno = EIO; return -1; }
    struct stat st;
    int result = fstat(fd, &st);
    if (result == 0) { out->st_dev = st.st_dev; out->st_ino = st.st_ino; }
    return result;
}
int test_is_open(int fd) { return fcntl(fd, F_GETFD) >= 0; }
int test_label(int fd) {
    char context[128];
#ifdef __APPLE__
    ssize_t result = fgetxattr(fd, "security.selinux", context, sizeof(context), 0, 0);
#else
    ssize_t result = fgetxattr(fd, "security.selinux", context, sizeof(context));
#endif
    return result < 0 ? -errno : (int) result;
}
'''

RUST = r'''
use std::fs::File;
use std::io::Read;
use std::mem::MaybeUninit;
use std::os::fd::{AsRawFd, IntoRawFd, RawFd};
use std::os::unix::net::UnixStream;
use std::sync::atomic::{AtomicI32, AtomicUsize, Ordering};
use std::time::Duration;
use base::LibcReturn;

mod base {
    pub trait LibcReturn { fn check_err(self) -> Result<i32, ()>; }
    impl LibcReturn for i32 {
        fn check_err(self) -> Result<i32, ()> { if self < 0 { Err(()) } else { Ok(self) } }
    }
}
mod libc {
    #[repr(C)]
    #[allow(non_camel_case_types)]
    pub struct stat { pub st_dev: u64, pub st_ino: u64 }
    unsafe extern "C" {
        #[link_name = "test_fstat"]
        pub fn fstat(fd: i32, st: *mut stat) -> i32;
        pub fn close(fd: i32) -> i32;
        pub fn dup2(from: i32, to: i32) -> i32;
        pub fn write(fd: i32, data: *const u8, count: usize) -> isize;
        pub fn test_fail_stat(fail: i32);
        pub fn test_is_open(fd: i32) -> i32;
        pub fn test_label(fd: i32) -> i32;
    }
}
struct ThreadPool;
impl ThreadPool {
    fn exec_task(task: impl FnOnce() + Send + 'static) { task(); }
}
static CALLED: AtomicUsize = AtomicUsize::new(0);
static REPLACEMENT: AtomicI32 = AtomicI32::new(-1);
extern "C" fn reply(fd: i32) {
    CALLED.fetch_add(1, Ordering::SeqCst);
    assert_eq!(unsafe { libc::write(fd, b"reply".as_ptr(), 5) }, 5);
}
extern "C" fn close_client(fd: i32) {
    CALLED.fetch_add(1, Ordering::SeqCst);
    assert_eq!(unsafe { libc::close(fd) }, 0);
}
extern "C" fn reuse_client(fd: i32) {
    CALLED.fetch_add(1, Ordering::SeqCst);
    assert_eq!(unsafe { libc::dup2(REPLACEMENT.load(Ordering::SeqCst), fd) }, fd);
}
fn pair() -> (i32, UnixStream) {
    let (client, peer) = UnixStream::pair().expect("socket pair");
    peer.set_read_timeout(Some(Duration::from_secs(2))).expect("bounded read");
    (client.into_raw_fd(), peer)
}
fn main() {
    let (fd, mut peer) = pair();
    println!("Socket SELinux-label result: {}", unsafe { libc::test_label(fd) });
    exec_companion_entry(fd, reply);
    assert_eq!(CALLED.load(Ordering::SeqCst), 1);
    let mut bytes = Vec::new();
    peer.read_to_end(&mut bytes).expect("reply and EOF");
    assert_eq!(bytes, b"reply");
    assert_eq!(unsafe { libc::test_is_open(fd) }, 0);

    let (fd, mut peer) = pair();
    exec_companion_entry(fd, close_client);
    assert_eq!(CALLED.load(Ordering::SeqCst), 2);
    assert_eq!(peer.read(&mut [0_u8; 1]).expect("closed callback"), 0);
    assert_eq!(unsafe { libc::test_is_open(fd) }, 0);

    let replacement = File::open("/dev/null").expect("replacement");
    REPLACEMENT.store(replacement.as_raw_fd(), Ordering::SeqCst);
    let (fd, mut peer) = pair();
    exec_companion_entry(fd, reuse_client);
    assert_eq!(CALLED.load(Ordering::SeqCst), 3);
    assert_eq!(unsafe { libc::test_is_open(fd) }, 1, "reused descriptor was closed");
    assert_eq!(peer.read(&mut [0_u8; 1]).expect("replaced socket"), 0);
    assert_eq!(unsafe { libc::close(fd) }, 0);

    let (fd, mut peer) = pair();
    unsafe { libc::test_fail_stat(1) };
    exec_companion_entry(fd, reply);
    unsafe { libc::test_fail_stat(0) };
    assert_eq!(CALLED.load(Ordering::SeqCst), 3, "callback ran without identity");
    assert_eq!(peer.read(&mut [0_u8; 1]).expect("failed metadata closed"), 0);
    assert_eq!(unsafe { libc::test_is_open(fd) }, 0);
    exec_companion_entry(-1, reply);
    assert_eq!(CALLED.load(Ordering::SeqCst), 3);
    println!("Companion delivery, close, reuse and metadata-error checks passed");
}
'''


class ZygiskCompanionTest(unittest.TestCase):
    def test_socket_identity_does_not_require_security_labels(self):
        source = (ROOT / 'native/src/core/zygisk/mod.rs').read_text()
        dispatcher = source[source.index('#[unsafe(no_mangle)]'):]
        with tempfile.TemporaryDirectory(prefix='kitsune-companion-') as temporary:
            root = Path(temporary)
            (root / 'bridge.c').write_text(BRIDGE)
            (root / 'test.rs').write_text(RUST + dispatcher)
            subprocess.run(['cc', '-c', str(root / 'bridge.c'), '-o', str(root / 'bridge.o')], check=True)
            subprocess.run(['rustc', '--edition=2024', str(root / 'test.rs'), '-C',
                            'link-arg=' + str(root / 'bridge.o'), '-o', str(root / 'test')], check=True)
            subprocess.run([str(root / 'test')], check=True, timeout=15)
