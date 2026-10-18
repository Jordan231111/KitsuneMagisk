use crate::consts::{DATABIN, LOG_PIPE, MAGISK_LOG_CON, MAGISKDB, MODULEROOT, SECURE_DIR};
use crate::ffi::get_magisk_tmp;
use base::{Directory, FsPathBuilder, LoggedResult, ResultExt, Utf8CStr, Utf8CStrBuf, cstr, libc};
use nix::fcntl::OFlag;
use std::io::Write;

pub(crate) fn selinux_disabled() -> bool {
    // Permission and I/O errors are not evidence that SELinux is disabled.
    for path in ["/sys/fs/selinux/enforce", "/selinux/enforce"] {
        match std::fs::metadata(path) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            _ => return false,
        }
    }
    // Containers can expose another LSM's attr/current (for example AppArmor).
    // The kernel's active-LSM list distinguishes that from hidden SELinux state.
    match std::fs::read_to_string("/sys/kernel/security/lsm") {
        Ok(lsms) => {
            let lsms = lsms.trim();
            return !lsms.is_empty()
                && lsms.split(',').all(|name| {
                    !name.is_empty()
                        && name != "selinux"
                        && name.bytes().all(|b| b.is_ascii_lowercase() || b == b'_')
                });
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
        Err(_) => return false,
    }
    match std::fs::read_to_string("/proc/self/attr/current") {
        Ok(context) => {
            let context = context.trim_matches(['\0', '\n']);
            if context.is_empty() {
                return true;
            }
            // Waydroid can hide securityfs while retaining AppArmor labels.
            // Require the named AppArmor context and a complete filesystem
            // list without selinuxfs, which enabled SELinux registers at boot.
            let Ok(apparmor) = std::fs::read_to_string("/proc/self/attr/apparmor/current") else {
                return false;
            };
            if context.contains(':') || apparmor.trim_matches(['\0', '\n']) != context {
                return false;
            }
            let Ok(filesystems) = std::fs::read_to_string("/proc/filesystems") else {
                return false;
            };
            let mut proc_seen = false;
            for line in filesystems.lines() {
                let mut fields = line.split_ascii_whitespace();
                let Some(first) = fields.next() else {
                    return false;
                };
                let nodev = first == "nodev";
                let name = if nodev {
                    let Some(name) = fields.next() else {
                        return false;
                    };
                    name
                } else {
                    first
                };
                if fields.next().is_some()
                    || name == "selinuxfs"
                    || !name.bytes().all(|b| {
                        b.is_ascii_lowercase()
                            || b.is_ascii_digit()
                            || matches!(b, b'_' | b'-' | b'.' | b'+')
                    })
                {
                    return false;
                }
                proc_seen |= nodev && name == "proc";
            }
            proc_seen
        }
        Err(e) => matches!(
            e.raw_os_error(),
            Some(libc::ENOENT | libc::EINVAL | libc::ENODEV)
        ),
    }
}

pub(crate) fn unlabeled_permissive() -> bool {
    // Some emulator images load a policy but leave init and its descendants in
    // the kernel domain. This is not disabled SELinux, and never covers enforcing.
    let Ok(context) = std::fs::read_to_string("/proc/1/attr/current") else {
        return false;
    };
    if context.trim_matches(['\0', '\n']) != "u:r:kernel:s0" {
        return false;
    }
    let mut found = false;
    for path in ["/sys/fs/selinux/enforce", "/selinux/enforce"] {
        match std::fs::read_to_string(path) {
            Ok(mode) if mode.trim() == "0" => found = true,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
            _ => return false,
        }
    }
    found
}

const UNLABEL_CON: &Utf8CStr = cstr!("u:object_r:unlabeled:s0");
const SYSTEM_CON: &Utf8CStr = cstr!("u:object_r:system_file:s0");
const ADB_CON: &Utf8CStr = cstr!("u:object_r:adb_data_file:s0");
const ROOT_CON: &Utf8CStr = cstr!("u:object_r:rootfs:s0");

fn restore_syscon_from_unlabeled(
    path: &mut dyn Utf8CStrBuf,
    con: &mut dyn Utf8CStrBuf,
) -> LoggedResult<()> {
    let dir_path_len = path.len();
    if path.get_secontext(con).log().is_ok() && con.as_str() == UNLABEL_CON {
        path.set_secontext(SYSTEM_CON)?;
    }
    let mut dir = Directory::open(path)?;
    while let Some(ref e) = dir.read()? {
        path.truncate(dir_path_len);
        path.append_path(e.name());
        if e.is_dir() {
            restore_syscon_from_unlabeled(path, con)?;
        } else if (e.is_file() || e.is_symlink())
            && path.get_secontext(con).log().is_ok()
            && con.as_str() == UNLABEL_CON
        {
            path.set_secontext(SYSTEM_CON)?;
        }
    }
    Ok(())
}

fn restore_syscon(path: &mut dyn Utf8CStrBuf) -> LoggedResult<()> {
    let dir_path_len = path.len();
    path.set_secontext(SYSTEM_CON)?;
    unsafe { libc::lchown(path.as_ptr(), 0, 0) };
    let mut dir = Directory::open(path)?;
    while let Some(ref e) = dir.read()? {
        path.truncate(dir_path_len);
        path.append_path(e.name());
        if e.is_dir() {
            restore_syscon(path)?;
        } else if e.is_file() || e.is_symlink() {
            path.set_secontext(SYSTEM_CON)?;
            unsafe { libc::lchown(path.as_ptr(), 0, 0) };
        }
    }
    Ok(())
}

pub(crate) fn restorecon() {
    unsafe { libc::chmod(cstr!(MAGISKDB).as_ptr(), 0o000) };
    if selinux_disabled() {
        return;
    }
    if let Ok(mut file) = cstr!("/sys/fs/selinux/context")
        .open(OFlag::O_WRONLY | OFlag::O_CLOEXEC)
        .log()
        && file.write_all(ADB_CON.as_bytes_with_nul()).is_ok()
    {
        cstr!(SECURE_DIR).set_secontext(ADB_CON).log_ok();
    }

    let mut path = cstr::buf::default();
    let mut con = cstr::buf::new::<1024>();
    path.push_str(MODULEROOT);
    path.set_secontext(SYSTEM_CON).log_ok();
    restore_syscon_from_unlabeled(&mut path, &mut con).log_ok();

    path.clear();
    path.push_str(DATABIN);
    restore_syscon(&mut path).log_ok();
    unsafe { libc::chmod(cstr!(MAGISKDB).as_ptr(), 0o000) };
}

pub(crate) fn restore_tmpcon() -> LoggedResult<()> {
    if selinux_disabled() {
        return Ok(());
    }
    let tmp = get_magisk_tmp();
    if tmp == "/sbin" {
        tmp.set_secontext(ROOT_CON)?;
    } else {
        unsafe { libc::chmod(tmp.as_ptr(), 0o711) };
    }

    let mut path = cstr::buf::default();
    let mut dir = Directory::open(tmp)?;
    while let Some(ref e) = dir.read()? {
        if !e.is_symlink() {
            e.resolve_path(&mut path)?;
            path.set_secontext(SYSTEM_CON).log_ok();
        }
    }

    path.clear();
    path.append_path(tmp).append_path(LOG_PIPE);
    path.set_secontext(cstr!(MAGISK_LOG_CON))?;

    Ok(())
}

pub(crate) fn lgetfilecon(path: &Utf8CStr, con: &mut [u8]) -> bool {
    let mut con = cstr::buf::wrap(con);
    path.get_secontext(&mut con).is_ok()
}

pub(crate) fn setfilecon(path: &Utf8CStr, con: &Utf8CStr) -> bool {
    selinux_disabled() || path.follow_link().set_secontext(con).is_ok()
}
