use crate::consts::{MODULEMNT, MODULEROOT, MODULEUPGRADE, WORKERDIR};
use crate::daemon::MagiskD;
use crate::ffi::{ModuleInfo, exec_module_scripts, exec_script, get_magisk_tmp};
use crate::mount::setup_module_mount;
use crate::resetprop::load_prop_file;
use base::{
    DirEntry, Directory, FsPathBuilder, LoggedResult, OsResult, ResultExt, SilentLogExt, Utf8CStr,
    Utf8CStrBuf, Utf8CString, WalkResult, clone_attr, cstr, debug, error, info, libc, raw_cstr,
    warn,
};
use nix::fcntl::OFlag;
use nix::mount::MsFlags;
use nix::sys::stat::{SFlag, fstat};
use nix::unistd::UnlinkatFlags;
use std::collections::BTreeMap;
use std::os::fd::{AsRawFd, BorrowedFd, IntoRawFd};
use std::path::{Component, Path};
use std::sync::atomic::Ordering;

const MAGISK_BIN_INJECT_PARTITIONS: [&Utf8CStr; 4] = [
    cstr!("/system/"),
    cstr!("/vendor/"),
    cstr!("/product/"),
    cstr!("/system_ext/"),
];

const SECONDARY_READ_ONLY_PARTITIONS: [&Utf8CStr; 3] =
    [cstr!("/vendor"), cstr!("/product"), cstr!("/system_ext")];

type FsNodeMap = BTreeMap<String, FsNode>;

macro_rules! module_log {
    ($($args:tt)+) => {
        debug!("{:8}: {} <- {}", $($args)+)
    }
}

#[allow(unused_variables)]
fn bind_mount(reason: &str, src: &Utf8CStr, dest: &Utf8CStr, rec: bool) {
    module_log!(reason, dest, src);
    // Ignore any kind of error here. If a single bind mount fails due to selinux permissions or
    // kernel limitations, don't let it break module mount entirely.
    src.bind_mount_to(dest, rec).log_ok();
    dest.remount_mount_point_flags(MsFlags::MS_RDONLY).log_ok();
}

fn mount_dummy<'a>(
    reason: &str,
    src: &Utf8CStr,
    dest: &'a Utf8CStr,
    is_dir: bool,
) -> OsResult<'a, ()> {
    if is_dir {
        dest.mkdir(0o000)?;
    } else {
        dest.create(OFlag::O_CREAT | OFlag::O_RDONLY | OFlag::O_CLOEXEC, 0o000)?;
    }
    bind_mount(reason, src, dest, false);
    Ok(())
}

// File path that act like a stack, popping out the last element
// automatically when out of scope. Using Rust's lifetime mechanism,
// we can ensure the buffer will never be incorrectly copied or modified.
// After calling append or reborrow, the mutable reference's lifetime is
// "transferred" to the returned object, and the compiler will guarantee
// that the original mutable reference can only be reused if and only if
// the newly created instance is destroyed.
struct PathTracker<'a> {
    path: &'a mut dyn Utf8CStrBuf,
    len: usize,
}

impl PathTracker<'_> {
    fn from<'a>(path: &'a mut dyn Utf8CStrBuf) -> PathTracker<'a> {
        let len = path.len();
        PathTracker { path, len }
    }

    fn append(&mut self, name: &str) -> PathTracker<'_> {
        let len = self.path.len();
        self.path.append_path(name);
        PathTracker {
            path: self.path,
            len,
        }
    }

    fn reborrow(&mut self) -> PathTracker<'_> {
        Self::from(self.path)
    }
}

impl Drop for PathTracker<'_> {
    // Revert back to the original state after finish using the buffer
    fn drop(&mut self) {
        self.path.truncate(self.len);
    }
}

// The comments for this struct assume real = "/system/bin"
struct ModulePaths<'a> {
    real: PathTracker<'a>,
    module: PathTracker<'a>,
    module_mnt: PathTracker<'a>,
}

impl ModulePaths<'_> {
    fn new<'a>(
        real: &'a mut dyn Utf8CStrBuf,
        module: &'a mut dyn Utf8CStrBuf,
        module_mnt: &'a mut dyn Utf8CStrBuf,
    ) -> ModulePaths<'a> {
        real.append_path("/");
        module.append_path(MODULEROOT);
        module_mnt
            .append_path(get_magisk_tmp())
            .append_path(MODULEMNT);
        ModulePaths {
            real: PathTracker::from(real),
            module: PathTracker::from(module),
            module_mnt: PathTracker::from(module_mnt),
        }
    }

    fn set_module(&mut self, module: &str) -> ModulePaths<'_> {
        ModulePaths {
            real: self.real.reborrow(),
            module: self.module.append(module),
            module_mnt: self.module_mnt.append(module),
        }
    }

    fn append(&mut self, name: &str) -> ModulePaths<'_> {
        ModulePaths {
            real: self.real.append(name),
            module: self.module.append(name),
            module_mnt: self.module_mnt.append(name),
        }
    }

    // Returns "/system/bin"
    fn real(&self) -> &Utf8CStr {
        self.real.path
    }

    // Returns "/data/adb/modules/{module}/system/bin"
    fn module(&self) -> &Utf8CStr {
        self.module.path
    }

    // Returns "$MAGISK_TMP/.magisk/modules/{module}/system/bin"
    fn module_mnt(&self) -> &Utf8CStr {
        self.module_mnt.path
    }
}

// The comments for this struct assume real = "/system/bin"
struct MountPaths<'a> {
    real: PathTracker<'a>,
    worker: PathTracker<'a>,
}

impl MountPaths<'_> {
    fn new<'a>(real: &'a mut dyn Utf8CStrBuf, worker: &'a mut dyn Utf8CStrBuf) -> MountPaths<'a> {
        real.append_path("/");
        worker.append_path(get_magisk_tmp()).append_path(WORKERDIR);
        MountPaths {
            real: PathTracker::from(real),
            worker: PathTracker::from(worker),
        }
    }

    fn append(&mut self, name: &str) -> MountPaths<'_> {
        MountPaths {
            real: self.real.append(name),
            worker: self.worker.append(name),
        }
    }

    fn reborrow(&mut self) -> MountPaths<'_> {
        MountPaths {
            real: self.real.reborrow(),
            worker: self.worker.reborrow(),
        }
    }

    // Returns "/system/bin"
    fn real(&self) -> &Utf8CStr {
        self.real.path
    }

    // Returns "$MAGISK_TMP/.magisk/worker/system/bin"
    fn worker(&self) -> &Utf8CStr {
        self.worker.path
    }
}

enum FsNode {
    Directory { children: FsNodeMap },
    File { src: Utf8CString },
    Symlink { target: Utf8CString },
    MagiskLink,
    Whiteout,
}

impl FsNode {
    fn new_dir() -> FsNode {
        FsNode::Directory {
            children: BTreeMap::new(),
        }
    }

    fn collect(&mut self, mut paths: ModulePaths) -> LoggedResult<()> {
        let FsNode::Directory { children } = self else {
            return Ok(());
        };
        let mut dir = Directory::open(paths.module())?;

        while let Some(entry) = dir.read()? {
            let entry_paths = paths.append(entry.name());
            let path = entry_paths.module();
            if entry.is_dir() {
                let node = children
                    .entry(entry.name().to_string())
                    .or_insert_with(FsNode::new_dir);
                node.collect(entry_paths)?;
            } else if entry.is_symlink() {
                // Read the link and store its target
                let mut link = cstr::buf::default();
                path.read_link(&mut link)?;
                children
                    .entry(entry.name().to_string())
                    .or_insert_with(|| FsNode::Symlink {
                        target: link.to_owned(),
                    });
            } else {
                if entry.is_char_device() {
                    let attr = path.get_attr()?;
                    if attr.is_whiteout() {
                        children
                            .entry(entry.name().to_string())
                            .or_insert_with(|| FsNode::Whiteout);
                        continue;
                    }
                }
                if entry_paths.real().exists() {
                    clone_attr(entry_paths.real(), path)?;
                }
                children
                    .entry(entry.name().to_string())
                    .or_insert_with(|| FsNode::File {
                        // Make sure to mount from module_mnt, not module
                        src: entry_paths.module_mnt().to_owned(),
                    });
            }
        }

        Ok(())
    }

    // The parent node has to be tmpfs if:
    // - Target does not exist
    // - Source or target is a symlink (since we cannot bind mount symlink)
    // - Source is whiteout (used for removal)
    fn parent_should_be_tmpfs(&self, target_path: &Utf8CStr) -> bool {
        match self {
            FsNode::Directory { .. } | FsNode::File { .. } => {
                if let Ok(attr) = target_path.get_attr() {
                    attr.is_symlink()
                } else {
                    true
                }
            }
            _ => true,
        }
    }

    fn children(&mut self) -> Option<&mut FsNodeMap> {
        match self {
            FsNode::Directory { children } => Some(children),
            _ => None,
        }
    }

    fn commit(&mut self, mut path: MountPaths, is_root_dir: bool) -> LoggedResult<()> {
        match self {
            FsNode::Directory { children } => {
                let mut is_tmpfs = false;

                // First determine whether tmpfs is required
                children.retain(|name, node| {
                    if name == ".replace" {
                        return if is_root_dir {
                            warn!("Unable to replace '{}', ignore request", path.real());
                            false
                        } else {
                            is_tmpfs = true;
                            true
                        };
                    }

                    let path = path.append(name);
                    if node.parent_should_be_tmpfs(path.real()) {
                        if is_root_dir {
                            // Ignore the unsupported child node
                            warn!("Unable to add '{}', skipped", path.real());
                            return false;
                        }
                        is_tmpfs = true;
                    }
                    true
                });

                if is_tmpfs {
                    self.commit_tmpfs(path.reborrow())?;
                    // Transitioning from non-tmpfs to tmpfs, we need to actually mount the
                    // worker dir to dest after all children are committed.
                    bind_mount("move", path.worker(), path.real(), true);
                } else {
                    for (name, node) in children {
                        let path = path.append(name);
                        node.commit(path, false)?;
                    }
                }
            }
            FsNode::File { src } => {
                bind_mount("mount", src, path.real(), false);
            }
            _ => {
                error!("Unable to handle '{}': parent should be tmpfs", path.real());
            }
        }

        Ok(())
    }

    fn commit_tmpfs(&mut self, mut path: MountPaths) -> LoggedResult<()> {
        match self {
            FsNode::Directory { children } => {
                path.worker().mkdirs(0o000)?;
                if path.real().exists() {
                    clone_attr(path.real(), path.worker())?;
                } else if let Some(p) = path.worker().parent_dir() {
                    let parent = Utf8CString::from(p);
                    clone_attr(&parent, path.worker())?;
                }

                // Check whether a file named '.replace' exists
                if let Some(FsNode::File { src }) = children.remove(".replace")
                    && let Some(replace_dir) = src.parent_dir()
                {
                    for (name, node) in children {
                        let path = path.append(name);
                        match node {
                            FsNode::Directory { .. } => {
                                // For replace, we don't need to traverse any deeper for mirroring.
                                // We can simply just bind mount the module dir to worker dir.
                                let src = Utf8CString::from(replace_dir).join_path(name);
                                mount_dummy("mount", &src, path.worker(), true)?;
                            }
                            _ => node.commit_tmpfs(path)?,
                        }
                    }

                    // If performing replace, we skip mirroring
                    return Ok(());
                }

                // Traverse the real directory and mount mirror files
                if let Ok(mut dir) = Directory::open(path.real()) {
                    while let Ok(Some(entry)) = dir.read() {
                        if children.contains_key(entry.name().as_str()) {
                            // Should not be mirrored, next
                            continue;
                        }

                        let path = path.append(entry.name());

                        if entry.is_dir() {
                            // At the first glance, it looks like we can directly mount the
                            // real dir to worker dir as mirror. However, this should NOT be done,
                            // because init will track these mounts with dev.mnt, causing issues.
                            // We unfortunately have to traverse recursively for mirroring.
                            FsNode::new_dir().commit_tmpfs(path)?;
                        } else if entry.is_symlink() {
                            let mut link = cstr::buf::default();
                            entry.read_link(&mut link).log_ok();
                            FsNode::Symlink {
                                target: link.to_owned(),
                            }
                            .commit_tmpfs(path)?;
                        } else {
                            // Mount the mirror file
                            mount_dummy("mirror", path.real(), path.worker(), false)?;
                        }
                    }
                }

                // Finally, commit children
                for (name, node) in children {
                    let path = path.append(name);
                    node.commit_tmpfs(path)?;
                }
            }
            FsNode::File { src } => {
                mount_dummy("mount", src, path.worker(), false)?;
            }
            FsNode::Symlink { target } => {
                module_log!("mklink", path.worker(), target);
                path.worker().create_symlink_to(target)?;
                if path.real().exists() {
                    clone_attr(path.real(), path.worker())?;
                }
            }
            FsNode::MagiskLink => {
                if let Some(name) = path.real().file_name()
                    && name == "supolicy"
                {
                    module_log!("mklink", path.worker(), "./magiskpolicy");
                    path.worker().create_symlink_to(cstr!("./magiskpolicy"))?;
                } else {
                    module_log!("mklink", path.worker(), "./magisk");
                    path.worker().create_symlink_to(cstr!("./magisk"))?;
                }
            }
            FsNode::Whiteout => {
                module_log!("delete", path.real(), "null");
            }
        }
        Ok(())
    }
}

fn get_path_env() -> String {
    std::env::var_os("PATH")
        .and_then(|s| s.into_string().ok())
        .unwrap_or_default()
}

fn inject_magisk_bins(system: &mut FsNode, is_emulator: bool) {
    fn inject(children: &mut FsNodeMap) {
        let mut path = cstr::buf::default().join_path(get_magisk_tmp());

        // Inject binaries

        let len = path.len();
        path.append_path("magisk");
        children.insert(
            "magisk".to_string(),
            FsNode::File {
                src: path.to_owned(),
            },
        );

        path.truncate(len);
        path.append_path("magiskpolicy");
        children.insert(
            "magiskpolicy".to_string(),
            FsNode::File {
                src: path.to_owned(),
            },
        );

        // Inject applet symlinks
        children.insert("su".to_string(), FsNode::MagiskLink);
        children.insert("resetprop".to_string(), FsNode::MagiskLink);
        children.insert("supolicy".to_string(), FsNode::MagiskLink);
    }

    // Strip /system prefix to insert correct node
    fn strip_system_prefix(orig_item: &str) -> String {
        match orig_item.strip_prefix("/system/") {
            Some(rest) => format!("/{rest}"),
            None => orig_item.to_string(),
        }
    }

    let path_env = get_path_env();
    let mut candidates = vec![];

    for orig_item in path_env.split(':') {
        // Filter non-suitable paths
        if !MAGISK_BIN_INJECT_PARTITIONS
            .iter()
            .any(|p| orig_item.starts_with(p.as_str()))
        {
            continue;
        }
        // Flatten apex path is not suitable too
        if orig_item.starts_with("/system/apex/") {
            continue;
        }

        // We want to keep /system/xbin/su on emulators (for debugging)
        if is_emulator && orig_item.starts_with("/system/xbin") {
            continue;
        }

        // Override existing su first
        let su_path = Utf8CString::from(format!("{orig_item}/su"));
        if su_path.exists() {
            let item = strip_system_prefix(orig_item);
            candidates.push((item, 0));
            break;
        }

        let path = Utf8CString::from(orig_item);
        if let Ok(attr) = path.get_attr()
            && (attr.st.st_mode & 0x0001) != 0
            && let Ok(mut dir) = Directory::open(&path)
        {
            let mut count = 0;
            if dir
                .pre_order_walk(|e| {
                    if e.is_file() {
                        count += 1;
                    }
                    Ok(WalkResult::Continue)
                })
                .is_err()
            {
                // Skip, we cannot ensure the result is correct
                continue;
            }
            let item = strip_system_prefix(orig_item);
            candidates.push((item, count));
        }
    }

    // Sort by amount of files
    candidates.sort_by_key(|&(_, count)| count);

    'path_loop: for candidate in candidates {
        let components = Path::new(&candidate.0)
            .components()
            .filter(|c| matches!(c, Component::Normal(_)))
            .filter_map(|c| c.as_os_str().to_str());

        let mut curr = match system {
            FsNode::Directory { children } => children,
            _ => continue,
        };

        for dir in components {
            let node = curr.entry(dir.to_owned()).or_insert_with(FsNode::new_dir);
            match node {
                FsNode::Directory { children } => curr = children,
                _ => continue 'path_loop,
            }
        }

        // Found a suitable path, done
        inject(curr);
        return;
    }

    // If still not found, directly inject into /system/bin
    let node = system
        .children()
        .map(|c| c.entry("bin".to_string()).or_insert_with(FsNode::new_dir));
    if let Some(FsNode::Directory { children }) = node {
        inject(children)
    }
}

fn inject_zygisk_bins(name: &str, system: &mut FsNode) {
    #[cfg(target_pointer_width = "64")]
    let has_32_bit = cstr!("/system/bin/linker").exists();

    #[cfg(target_pointer_width = "32")]
    let has_32_bit = true;

    if has_32_bit {
        let lib = system
            .children()
            .map(|c| c.entry("lib".to_string()).or_insert_with(FsNode::new_dir));
        if let Some(FsNode::Directory { children }) = lib {
            let mut bin_path = cstr::buf::default().join_path(get_magisk_tmp());

            #[cfg(target_pointer_width = "64")]
            bin_path.append_path("magisk32");

            #[cfg(target_pointer_width = "32")]
            bin_path.append_path("magisk");

            // There are some devices that announce ABI as 64 bit only, but ship with linker
            // because they make use of a special 32 bit to 64 bit translator (such as tango).
            // In this case, magisk32 does not exist, so inserting it will cause bind mount
            // failure and affect module mount. Native bridge injection does not support these
            // kind of translators anyway, so simply check if magisk32 exists here.
            if bin_path.exists() {
                children.insert(
                    name.to_string(),
                    FsNode::File {
                        src: bin_path.to_owned(),
                    },
                );
            }
        }
    }

    #[cfg(target_pointer_width = "64")]
    if cstr!("/system/bin/linker64").exists() {
        let lib64 = system
            .children()
            .map(|c| c.entry("lib64".to_string()).or_insert_with(FsNode::new_dir));
        if let Some(FsNode::Directory { children }) = lib64 {
            let bin_path = cstr::buf::default()
                .join_path(get_magisk_tmp())
                .join_path("magisk");

            children.insert(
                name.to_string(),
                FsNode::File {
                    src: bin_path.to_owned(),
                },
            );
        }
    }
}

fn upgrade_modules() -> LoggedResult<()> {
    let mut upgrade = Directory::open(cstr!(MODULEUPGRADE)).silent()?;
    let root = Directory::open(cstr!(MODULEROOT))?;
    while let Some(e) = upgrade.read()? {
        if !e.is_dir() {
            continue;
        }
        let module_name = e.name();
        let mut disable = false;
        // Cleanup old module if exists
        if root.contains_path(module_name) {
            let module = root.open_as_dir_at(module_name)?;
            // If the old module is disabled, we need to also disable the new one
            disable = module.contains_path(cstr!("disable"));
            module.remove_all()?;
            root.unlink_at(module_name, UnlinkatFlags::RemoveDir)?;
        }
        info!("Upgrade / New module: {module_name}");
        e.rename_to(&root, module_name)?;
        if disable {
            let path = cstr::buf::default()
                .join_path(module_name)
                .join_path("disable");
            let _ = root.open_as_file_at(
                &path,
                OFlag::O_RDONLY | OFlag::O_CREAT | OFlag::O_CLOEXEC,
                0,
            )?;
        }
    }
    upgrade.remove_all()?;
    cstr!(MODULEUPGRADE).remove()?;
    Ok(())
}

fn for_each_module(mut func: impl FnMut(&DirEntry) -> LoggedResult<()>) -> LoggedResult<()> {
    let mut root = Directory::open(cstr!(MODULEROOT))?;
    while let Some(ref e) = root.read()? {
        if e.is_dir() && e.name() != ".core" {
            func(e)?;
        }
    }
    Ok(())
}

pub fn disable_modules() {
    for_each_module(|e| {
        let dir = e.open_as_dir()?;
        dir.open_as_file_at(
            cstr!("disable"),
            OFlag::O_RDONLY | OFlag::O_CREAT | OFlag::O_CLOEXEC,
            0,
        )?;
        Ok(())
    })
    .log_ok();
}

fn run_uninstall_script(module_name: &Utf8CStr) {
    let script = cstr::buf::default()
        .join_path(MODULEROOT)
        .join_path(module_name)
        .join_path("uninstall.sh");
    exec_script(&script);
}

fn pread_exact(fd: i32, mut data: &mut [u8], mut offset: libc::off_t) -> bool {
    while !data.is_empty() {
        let read = unsafe { libc::pread(fd, data.as_mut_ptr().cast(), data.len(), offset) };
        if read > 0 {
            let read = read as usize;
            let Ok(read_offset) = libc::off_t::try_from(read) else {
                return false;
            };
            let Some(next_offset) = offset.checked_add(read_offset) else {
                return false;
            };
            offset = next_offset;
            data = &mut data[read..];
        } else if read < 0 && std::io::Error::last_os_error().raw_os_error() == Some(libc::EINTR) {
            continue;
        } else {
            return false;
        }
    }
    true
}

fn valid_zygisk_elf(fd: i32, file_size: i64, elf_class: u8, machine: u16) -> bool {
    let Ok(file_size) = u64::try_from(file_size) else {
        return false;
    };
    let header_size = if elf_class == 1 { 52 } else { 64 };
    let phdr_size = if elf_class == 1 { 32 } else { 56 };
    if file_size < header_size {
        return false;
    }

    let mut header = [0_u8; 64];
    if !pread_exact(fd, &mut header[..header_size as usize], 0)
        || header[..4] != *b"\x7fELF"
        || header[4] != elf_class
        || header[5] != 1
        || header[6] != 1
        || u16::from_le_bytes([header[16], header[17]]) != 3
        || u16::from_le_bytes([header[18], header[19]]) != machine
        || u32::from_le_bytes([header[20], header[21], header[22], header[23]]) != 1
    {
        return false;
    }

    let (phoff, ehsize, phentsize, phnum) = if elf_class == 1 {
        (
            u32::from_le_bytes([header[28], header[29], header[30], header[31]]) as u64,
            u16::from_le_bytes([header[40], header[41]]) as u64,
            u16::from_le_bytes([header[42], header[43]]) as u64,
            u16::from_le_bytes([header[44], header[45]]) as u64,
        )
    } else {
        (
            u64::from_le_bytes([
                header[32], header[33], header[34], header[35], header[36], header[37], header[38],
                header[39],
            ]),
            u16::from_le_bytes([header[52], header[53]]) as u64,
            u16::from_le_bytes([header[54], header[55]]) as u64,
            u16::from_le_bytes([header[56], header[57]]) as u64,
        )
    };
    if ehsize != header_size || phoff < ehsize || phentsize != phdr_size || phnum == 0 {
        return false;
    }
    let Some(phdr_bytes) = phentsize.checked_mul(phnum) else {
        return false;
    };
    // Match the platform linker's bounded program-header allocation.
    if phdr_bytes > 64 * 1024 {
        return false;
    }
    let Some(phdr_end) = phoff.checked_add(phdr_bytes) else {
        return false;
    };
    if phdr_end > file_size {
        return false;
    }

    let mut has_load = false;
    let mut load_start = u64::MAX;
    let mut load_end = 0_u64;
    let mut phdr = [0_u8; 56];
    for index in 0..phnum {
        let offset = phoff + index * phentsize;
        let Ok(offset) = libc::off_t::try_from(offset) else {
            return false;
        };
        if !pread_exact(fd, &mut phdr[..phdr_size as usize], offset) {
            return false;
        }

        let ph_type = u32::from_le_bytes([phdr[0], phdr[1], phdr[2], phdr[3]]);
        let (segment_offset, virtual_address, file_bytes, memory_bytes) = if elf_class == 1 {
            (
                u32::from_le_bytes([phdr[4], phdr[5], phdr[6], phdr[7]]) as u64,
                u32::from_le_bytes([phdr[8], phdr[9], phdr[10], phdr[11]]) as u64,
                u32::from_le_bytes([phdr[16], phdr[17], phdr[18], phdr[19]]) as u64,
                u32::from_le_bytes([phdr[20], phdr[21], phdr[22], phdr[23]]) as u64,
            )
        } else {
            (
                u64::from_le_bytes([
                    phdr[8], phdr[9], phdr[10], phdr[11], phdr[12], phdr[13], phdr[14], phdr[15],
                ]),
                u64::from_le_bytes([
                    phdr[16], phdr[17], phdr[18], phdr[19], phdr[20], phdr[21], phdr[22], phdr[23],
                ]),
                u64::from_le_bytes([
                    phdr[32], phdr[33], phdr[34], phdr[35], phdr[36], phdr[37], phdr[38], phdr[39],
                ]),
                u64::from_le_bytes([
                    phdr[40], phdr[41], phdr[42], phdr[43], phdr[44], phdr[45], phdr[46], phdr[47],
                ]),
            )
        };
        let Some(segment_end) = segment_offset.checked_add(file_bytes) else {
            return false;
        };
        if segment_end > file_size {
            return false;
        }
        // PT_LOAD is the only program-header type required here.
        if ph_type == 1 {
            let address_limit = if elf_class == 1 {
                u32::MAX as u64
            } else {
                u64::MAX
            };
            let Some(file_memory_end) = virtual_address.checked_add(file_bytes) else {
                return false;
            };
            let Some(memory_end) = virtual_address.checked_add(memory_bytes) else {
                return false;
            };
            // Legacy bionic rounds these unchecked values up to a page. Use
            // the maximum Android ELF page size so every supported runtime is safe.
            let Some(rounded_memory_end) = memory_end.checked_add(64 * 1024 - 1) else {
                return false;
            };
            if file_bytes > memory_bytes
                || file_memory_end > address_limit
                || rounded_memory_end > address_limit
            {
                return false;
            }
            if file_bytes > 0 {
                has_load = true;
                load_start = load_start.min(virtual_address & !(64 * 1024 - 1));
                load_end = load_end.max(rounded_memory_end & !(64 * 1024 - 1));
            }
        }
    }
    has_load && load_end > load_start
}

fn valid_zygisk_fd(fd: i32, elf_class: u8, machine: u16) -> bool {
    let source = unsafe { BorrowedFd::borrow_raw(fd) };
    let Ok(attr) = fstat(source).log() else {
        return false;
    };
    SFlag::from_bits_truncate(attr.st_mode as libc::mode_t) == SFlag::S_IFREG
        && valid_zygisk_elf(fd, attr.st_size, elf_class, machine)
}

fn copy_module_to_memfd(memfd: i32, fd: i32, elf_class: u8, machine: u16) -> bool {
    let source = unsafe { BorrowedFd::borrow_raw(fd) };
    let Ok(source_attr) = fstat(source).log() else {
        return false;
    };
    if source_attr.st_size <= 0 {
        return false;
    }
    #[cfg(target_pointer_width = "32")]
    let Ok(source_size) = libc::off_t::try_from(source_attr.st_size) else {
        return false;
    };
    #[cfg(target_pointer_width = "64")]
    let source_size = source_attr.st_size;

    let mut offset: libc::off_t = 0;
    while offset < source_size {
        let remaining = (source_size - offset) as u64;
        let count = remaining.min(usize::MAX as u64) as usize;
        let sent = unsafe { libc::sendfile(memfd, fd, &mut offset, count) };
        if sent > 0 {
            continue;
        }
        if sent < 0 && std::io::Error::last_os_error().raw_os_error() == Some(libc::EINTR) {
            continue;
        }
        return false;
    }

    let destination = unsafe { BorrowedFd::borrow_raw(memfd) };
    let Ok(destination_attr) = fstat(destination).log() else {
        return false;
    };
    destination_attr.st_size == source_attr.st_size
        && valid_zygisk_elf(memfd, destination_attr.st_size, elf_class, machine)
        && unsafe { libc::lseek(fd, 0, libc::SEEK_SET) } == 0
        && unsafe { libc::lseek(memfd, 0, libc::SEEK_SET) } == 0
}

pub fn remove_modules() {
    for_each_module(|e| {
        let dir = e.open_as_dir()?;
        if dir.contains_path(cstr!("uninstall.sh")) {
            run_uninstall_script(e.name());
        }
        Ok(())
    })
    .log_ok();
    cstr!(MODULEROOT).remove_all().log_ok();
}

fn collect_modules(zygisk_enabled: bool, open_zygisk: bool) -> Vec<ModuleInfo> {
    let mut modules = Vec::new();

    #[allow(unused_mut)] // It's possible that z32 and z64 are unused
    for_each_module(|e| {
        let name = e.name();
        let dir = e.open_as_dir()?;
        if dir.contains_path(cstr!("remove")) {
            info!("{name}: remove");
            if dir.contains_path(cstr!("uninstall.sh")) {
                run_uninstall_script(name);
            }
            dir.remove_all()?;
            e.unlink()?;
            return Ok(());
        }
        dir.unlink_at(cstr!("update"), UnlinkatFlags::NoRemoveDir)
            .ok();
        if dir.contains_path(cstr!("disable")) {
            return Ok(());
        }

        let mut z32 = -1;
        let mut z64 = -1;

        let is_zygisk = dir.contains_path(cstr!("zygisk"));

        if zygisk_enabled {
            // Riru and its modules are not compatible with zygisk
            if name == "riru-core" || dir.contains_path(cstr!("riru")) {
                return Ok(());
            }

            fn open_fd_safe(dir: &Directory, name: &Utf8CStr, elf_class: u8, machine: u16) -> i32 {
                let Ok(fd) = dir
                    .open_as_file_at(
                        name,
                        OFlag::O_RDONLY | OFlag::O_CLOEXEC | OFlag::O_NONBLOCK,
                        0,
                    )
                    .log()
                else {
                    return -1;
                };
                if !valid_zygisk_fd(fd.as_raw_fd(), elf_class, machine) {
                    warn!("{name}: invalid Zygisk ELF");
                    return -1;
                }
                fd.into_raw_fd()
            }

            if open_zygisk && is_zygisk {
                #[cfg(target_arch = "arm")]
                {
                    z32 = open_fd_safe(&dir, cstr!("zygisk/armeabi-v7a.so"), 1, 40);
                }
                #[cfg(target_arch = "aarch64")]
                {
                    z32 = open_fd_safe(&dir, cstr!("zygisk/armeabi-v7a.so"), 1, 40);
                    z64 = open_fd_safe(&dir, cstr!("zygisk/arm64-v8a.so"), 2, 183);
                }
                #[cfg(target_arch = "x86")]
                {
                    z32 = open_fd_safe(&dir, cstr!("zygisk/x86.so"), 1, 3);
                }
                #[cfg(target_arch = "x86_64")]
                {
                    z32 = open_fd_safe(&dir, cstr!("zygisk/x86.so"), 1, 3);
                    z64 = open_fd_safe(&dir, cstr!("zygisk/x86_64.so"), 2, 62);
                }
                #[cfg(target_arch = "riscv64")]
                {
                    z64 = open_fd_safe(&dir, cstr!("zygisk/riscv64.so"), 2, 243);
                }
                dir.unlink_at(cstr!("zygisk/unloaded"), UnlinkatFlags::NoRemoveDir)
                    .ok();
            }
        } else {
            // Ignore zygisk modules when zygisk is not enabled
            if is_zygisk {
                info!("{name}: ignore");
                return Ok(());
            }
        }
        modules.push(ModuleInfo {
            name: name.to_string(),
            z32,
            z64,
        });
        Ok(())
    })
    .log_ok();

    if zygisk_enabled && open_zygisk {
        let mut use_memfd = true;
        let mut convert_to_memfd = |fd: i32, elf_class: u8, machine: u16| -> i32 {
            if fd < 0 {
                return fd;
            }
            if use_memfd {
                let memfd = unsafe {
                    libc::syscall(
                        libc::SYS_memfd_create,
                        raw_cstr!("jit-cache"),
                        libc::MFD_CLOEXEC,
                    ) as i32
                };
                if memfd >= 0 {
                    if copy_module_to_memfd(memfd, fd, elf_class, machine) {
                        unsafe {
                            libc::close(fd);
                        }
                        return memfd;
                    } else {
                        unsafe {
                            libc::close(memfd);
                        }
                        // Some kernels/filesystems reject sendfile to a memfd. Preserve
                        // compatibility only if the exact source FD is still valid.
                        use_memfd = false;
                        if valid_zygisk_fd(fd, elf_class, machine) {
                            return fd;
                        }
                        unsafe {
                            libc::close(fd);
                        }
                        warn!("Zygisk module changed while preparing memfd");
                        return -1;
                    }
                }
                // Some error occurred, don't try again
                use_memfd = false;
            }
            if valid_zygisk_fd(fd, elf_class, machine) {
                fd
            } else {
                unsafe {
                    libc::close(fd);
                }
                warn!("Zygisk module changed before use");
                -1
            }
        };

        modules.iter_mut().for_each(|m| {
            #[cfg(target_arch = "arm")]
            {
                m.z32 = convert_to_memfd(m.z32, 1, 40);
            }
            #[cfg(target_arch = "aarch64")]
            {
                m.z32 = convert_to_memfd(m.z32, 1, 40);
                m.z64 = convert_to_memfd(m.z64, 2, 183);
            }
            #[cfg(target_arch = "x86")]
            {
                m.z32 = convert_to_memfd(m.z32, 1, 3);
            }
            #[cfg(target_arch = "x86_64")]
            {
                m.z32 = convert_to_memfd(m.z32, 1, 3);
                m.z64 = convert_to_memfd(m.z64, 2, 62);
            }
            #[cfg(target_arch = "riscv64")]
            {
                m.z64 = convert_to_memfd(m.z64, 2, 243);
            }
        });
    }

    modules
}

impl MagiskD {
    pub fn handle_modules(&self) {
        setup_module_mount();
        upgrade_modules().ok();

        let zygisk = self.zygisk_enabled.load(Ordering::Acquire);
        let modules = collect_modules(zygisk, false);
        exec_module_scripts(cstr!("post-fs-data"), &modules);

        // Recollect modules (module scripts could remove itself)
        let modules = collect_modules(zygisk, true);
        self.apply_modules(&modules);

        self.module_list.set(modules).ok();
    }

    fn apply_modules(&self, module_list: &[ModuleInfo]) {
        let mut system = FsNode::new_dir();

        // Create buffers for paths
        let mut buf1 = cstr::buf::dynamic(256);
        let mut buf2 = cstr::buf::dynamic(256);
        let mut buf3 = cstr::buf::dynamic(256);

        let mut paths = ModulePaths::new(&mut buf1, &mut buf2, &mut buf3);

        // Step 1: Create virtual filesystem tree
        //
        // In this step, there is zero logic applied during tree construction; we simply collect and
        // record the union of all module filesystem trees under each of their /system directory.

        for info in module_list {
            let mut paths = paths.set_module(&info.name);

            // Read props
            let prop = paths.append("system.prop");
            if prop.module().exists() {
                load_prop_file(prop.module());
            }
            drop(prop);

            // Check whether skip mounting
            let skip = paths.append("skip_mount");
            if skip.module().exists() {
                continue;
            }
            drop(skip);

            // Double check whether the system folder exists
            let sys = paths.append("system");
            if sys.module().exists() {
                info!("{}: loading module files", &info.name);
                system.collect(sys).log_ok();
            }
        }

        // Step 2: Inject custom files
        //
        // Magisk provides some built-in functionality that requires augmenting the filesystem.
        // We expose several cmdline tools (e.g. su) into PATH, and the zygisk shared library
        // has to also be added into the default LD_LIBRARY_PATH for code injection.
        // We directly inject file nodes into the virtual filesystem tree we built in the previous
        // step, treating Magisk just like a special "module".

        if get_magisk_tmp() != "/sbin" || get_path_env().split(":").all(|s| s != "/sbin") {
            inject_magisk_bins(&mut system, self.is_emulator);
        }

        // Handle zygisk
        if self.zygisk_enabled.load(Ordering::Acquire) {
            let mut zygisk = self.zygisk.lock();
            zygisk.set_prop();
            inject_zygisk_bins(&zygisk.lib_name, &mut system);
        }

        // Step 3: Extract all supported read-only partition roots
        //
        // For simplicity and backwards compatibility on older Android versions, when constructing
        // Magisk modules, we always assume that there is only a single read-only partition mounted
        // at /system. However, on modern Android there are actually multiple read-only partitions
        // mounted at their respective paths. We need to extract these subtrees out of the main
        // tree and treat them as individual trees.

        let mut roots = BTreeMap::new(); /* mapOf(partition_name -> FsNode) */
        if let FsNode::Directory { children } = &mut system {
            for dir in SECONDARY_READ_ONLY_PARTITIONS {
                // Only treat these nodes as root iff it is actually a directory in rootdir
                if let Ok(attr) = dir.get_attr()
                    && attr.is_dir()
                {
                    let name = dir.trim_start_matches('/');
                    if let Some(root) = children.remove(name) {
                        roots.insert(name, root);
                    }
                }
            }
        }
        roots.insert("system", system);

        drop(paths);
        let mut paths = MountPaths::new(&mut buf1, &mut buf2);

        for (dir, mut root) in roots {
            // Step 4: Convert virtual filesystem tree into concrete operations
            //
            // Compare the virtual filesystem tree we constructed against the real filesystem
            // structure on-device to generate a series of "operations".
            // The "core" of the logic is to decide which directories need to be rebuilt in the
            // tmpfs worker directory, and real sub-nodes need to be mirrored inside it.

            let paths = paths.append(dir);
            root.commit(paths, true).log_ok();
        }
    }
}
