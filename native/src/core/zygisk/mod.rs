mod daemon;
mod provider;

use crate::thread::ThreadPool;
use base::{LibcReturn, libc};
pub use daemon::{ZygiskState, zygisk_should_load_module};
use std::mem::MaybeUninit;
use std::os::fd::RawFd;

#[unsafe(no_mangle)]
extern "C" fn exec_companion_entry(client: RawFd, companion_handler: extern "C" fn(RawFd)) {
    ThreadPool::exec_task(move || {
        let identity = |fd| {
            let mut st = MaybeUninit::<libc::stat>::uninit();
            unsafe { libc::fstat(fd, st.as_mut_ptr()) }.check_err().ok()?;
            let st = unsafe { st.assume_init() };
            Some((st.st_dev, st.st_ino))
        };
        // Socket identity does not require a SELinux label. Context queries
        // can fail with EOPNOTSUPP on otherwise valid sockets in containers.
        let Some(before) = identity(client) else {
            unsafe { libc::close(client) };
            return;
        };

        companion_handler(client);

        // Only close client if it is the same file so we don't
        // accidentally close a re-used file descriptor.
        // This check is required because the module companion
        // handler could've closed the file descriptor already.
        if identity(client) == Some(before) {
            unsafe { libc::close(client) };
        }
    });
}
