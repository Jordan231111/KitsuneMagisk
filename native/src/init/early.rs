//! Early-mount v2: publish a bounded, complete snapshot before Android init.
//! Only existing regular files are replaced. Directory reconstruction belongs
//! to the ordinary module engine; it must not silently change early timing.
use crate::early_contract::{guard_services, safe_path, validate_rc};
use crate::ffi::{MagiskInit, check_safe_mode_key};
use base::nix::fcntl::OFlag;
use base::nix::mount::{MsFlags, mount};
use base::{
    FileAttr, LoggedResult, ResultExt, Utf8CStr, Utf8CString, cstr, debug, fd_get_attr,
    fd_get_secontext, fd_set_attr, warn,
};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::os::fd::AsRawFd;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::time::{Duration, Instant};

const STATE: &str = "/data/.magisk/preinit/kitsune-v2";
const PAYLOAD: &str = ".magisk/early-mount";
const MAX_BYTES: usize = 32 * 1024 * 1024;
const MAX_FILES: usize = 512;

pub struct EarlyContext(Utf8CString, fs::File);

fn write_control(path: &str, bytes: &[u8]) -> std::io::Result<()> {
    let mut file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)?;
    std::io::Write::write_all(&mut file, bytes)
}

fn checked_file(path: &str, budget: &mut usize) -> Option<Vec<u8>> {
    for parent in std::path::Path::new(path).ancestors().skip(1) {
        if !parent.as_os_str().is_empty()
            && fs::symlink_metadata(parent).ok()?.file_type().is_symlink()
        {
            return None;
        }
    }
    let metadata = fs::symlink_metadata(path).ok()?;
    if !metadata.is_file() || metadata.uid() != 0 || metadata.mode() & 0o022 != 0 {
        return None;
    }
    let size = usize::try_from(metadata.len()).ok()?;
    *budget = budget.checked_add(size)?;
    if *budget > MAX_BYTES {
        return None;
    }
    let data = fs::read(path).ok()?;
    (data.len() == size).then_some(data)
}

fn target_path(path: &str) -> Option<String> {
    let (mut partition, mut rest) = path.split_once('/')?;
    // Preserve the historical system/vendor spelling on split-partition
    // devices, following only Android's known reciprocal aliases.
    if partition == "system"
        && let Some((name, tail)) = rest.split_once('/')
        && matches!(name, "vendor" | "product" | "system_ext")
        && let Ok(link) = fs::read_link(format!("/system/{name}"))
        && (link == std::path::Path::new(&format!("/{name}"))
            || link == std::path::Path::new(&format!("../{name}")))
    {
        partition = name;
        rest = tail;
    }
    if !matches!(
        partition,
        "system" | "vendor" | "product" | "system_ext" | "odm"
    ) {
        return None;
    }
    // Standard Android partition aliases are supported; arbitrary symlink traversal is not.
    let root = format!("/{partition}");
    let root = if fs::symlink_metadata(&root).ok()?.file_type().is_symlink() {
        let link = fs::read_link(&root).ok()?;
        let expected = format!("/system/{partition}");
        if link != std::path::Path::new(&expected)
            && link != std::path::Path::new(expected.trim_start_matches('/'))
        {
            return None;
        }
        expected
    } else {
        root
    };
    let target = format!("{root}/{rest}");
    if rest.starts_with("etc/init/")
        || rest.starts_with("etc/selinux/")
        || rest.starts_with("bin/init")
        || rest.starts_with("bin/linker")
    {
        return None;
    }
    let mut parent = std::path::Path::new(&target);
    loop {
        if fs::symlink_metadata(parent).ok()?.file_type().is_symlink() {
            return None;
        }
        if let Some(p) = parent.parent() {
            parent = p;
        } else {
            break;
        }
    }
    fs::metadata(&target).ok()?.is_file().then_some(target)
}

impl MagiskInit {
    pub(crate) fn load_early_mount(&mut self, tmp_dir: &Utf8CStr) -> Vec<String> {
        write_control(".magisk/early-mount-api", b"2\n").log_ok();
        if !std::path::Path::new(STATE).exists() {
            return Vec::new();
        }
        if check_safe_mode_key() {
            write_control(".magisk/early-safe-mode", b"1\n").log_ok();
            return Vec::new();
        }
        if std::path::Path::new(&format!("{STATE}/disabled")).exists() {
            warn!("early-mount: snapshot disabled");
            return Vec::new();
        }
        match self.prepare_early_mount(tmp_dir) {
            Some(rc) => rc,
            None => {
                write_control(".magisk/early-snapshot-invalid", b"1\n").log_ok();
                warn!("early-mount: rejected incomplete or unsupported snapshot");
                Vec::new()
            }
        }
    }

    fn prepare_early_mount(&mut self, tmp_dir: &Utf8CStr) -> Option<Vec<String>> {
        let start = Instant::now();
        let generation = fs::read_link(format!("{STATE}/current")).ok()?;
        let generation = generation.to_str()?;
        if !generation.starts_with("gen.") || !safe_path(generation) || generation.contains('/') {
            return None;
        }
        let root = format!("{STATE}/{generation}");
        if fs::read_to_string(format!("{root}/version")).ok()?.trim() != "2" {
            return None;
        }
        let mut budget = 0;
        let manifest = checked_file(&format!("{root}/manifest"), &mut budget)?;
        let manifest = std::str::from_utf8(&manifest).ok()?;
        let mut mounts = BTreeMap::new();
        let mut scripts = BTreeMap::new();
        let mut script_bytes = 0;
        let mut seen = BTreeSet::new();
        let mut services = BTreeSet::new();
        for line in manifest.lines() {
            if seen.len() >= MAX_FILES || start.elapsed() > Duration::from_secs(2) {
                return None;
            }
            let (digest, path) = line.split_once("  ./")?;
            if digest.len() != 64 || !safe_path(path) || !seen.insert(path) {
                return None;
            }
            let data = checked_file(&format!("{root}/{path}"), &mut budget)?;
            if Sha256::digest(&data)
                .iter()
                .map(|b| format!("{b:02x}"))
                .collect::<String>()
                != digest
            {
                return None;
            }
            let (owner, path) = if let Some(path) = path.strip_prefix("global/") {
                ("global", path)
            } else if let Some(path) = path.strip_prefix("modules/") {
                let (owner, path) = path.split_once('/')?;
                if path == "module.prop" {
                    continue;
                }
                (owner, path.strip_prefix("early-mount/")?)
            } else if matches!(path, "version" | "sepolicy.rule") {
                continue;
            } else {
                return None;
            };
            if let Some(name) = path.strip_prefix("initrc.d/") {
                let text = String::from_utf8(data).ok()?;
                script_bytes += text.len();
                if script_bytes > 64 * 1024 {
                    return None;
                }
                if !name.ends_with(".rc") || !validate_rc(&text, owner) {
                    return None;
                }
                // timeout_period is part of the qualified modern init contract.
                if !std::path::Path::new("/system/etc/init/hw/init.rc").exists() {
                    return None;
                }
                for line in text.lines() {
                    let words: Vec<_> = line.split_whitespace().collect();
                    if words.first() == Some(&"service")
                        && !services.insert(words.get(1)?.to_string())
                    {
                        return None;
                    }
                }
                if services.len() > 32 {
                    return None;
                }
                scripts.insert(format!("{owner}/{name}"), text);
                continue;
            }
            let target = target_path(path)?;
            if let Some((previous, _)) = mounts.insert(target.clone(), (owner.to_string(), data)) {
                warn!(
                    "early-mount: {} conflicts with {} at {}",
                    owner, previous, target
                );
                return None;
            }
        }
        // Symlinks, devices, .replace and empty directories are not encoded as
        // replacement files. The publisher rejects these unsupported operations.
        let payload = cstr!(PAYLOAD);
        payload.mkdirs(0o700).ok()?;
        mount(
            Some("magisk"),
            PAYLOAD,
            Some("tmpfs"),
            MsFlags::empty(),
            Some("mode=700,size=40m"),
        )
        .ok()?;
        if payload.set_mount_private(true).is_err() {
            payload.unmount().log_ok();
            return None;
        }
        let mut mounted: Vec<Utf8CString> = Vec::new();
        let mut contexts = Vec::new();
        // The stock debug ramdisk temporarily covers the final runtime path.
        // Stage and relabel through the inherited cwd; RC runs after it is exposed.
        let runtime_dir = format!("{tmp_dir}/{PAYLOAD}");
        let result = (|| -> LoggedResult<()> {
            debug!("early-mount: staging for {}", runtime_dir);
            if !scripts.is_empty() && runtime_dir.starts_with("/magisk/tmp/") {
                return base::log_err!("early-mount: RC requires a stable runtime path");
            }
            let guard = format!(
                "#!/system/bin/sh\n[ -f '{0}/ready' ] && [ ! -e '{0}/../early-safe-mode' ] || exit 1\nexec \"$@\"\n",
                runtime_dir
            );
            write_control(concat!(".magisk/early-mount", "/exec.sh"), guard.as_bytes()).log()?;
            for (index, (target, (_owner, bytes))) in mounts.iter().enumerate() {
                if start.elapsed() > Duration::from_secs(2) {
                    return base::log_err!("early-mount: publication budget exceeded");
                }
                let source = Utf8CString::from(format!("{PAYLOAD}/{index}"));
                let target = Utf8CString::from(target.as_str());
                let mut file = fs::OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .mode(0o600)
                    .open(source.as_str())
                    .log()?;
                std::io::Write::write_all(&mut file, bytes).log()?;
                // Some kernels return "unlabeled" until the first policy load.
                // Keep the original inode open across the bind mount and read
                // its resolved context after policy loading has completed.
                let original =
                    target.open(OFlag::O_RDONLY | OFlag::O_CLOEXEC | OFlag::O_NOFOLLOW)?;
                let mut attr = FileAttr::new();
                attr.st = fd_get_attr(original.as_raw_fd())?.st;
                fd_set_attr(file.as_raw_fd(), &attr)?;
                source.bind_mount_to(&target, false)?;
                mounted.push(target.to_owned());
                // Public targets are immutable immediately. Relabel the same inode
                // through its private writable source once SELinux has loaded.
                target.remount_mount_point_flags(MsFlags::MS_RDONLY)?;
                contexts.push(EarlyContext(source.to_owned(), original));
                debug!("early-mount: {} published {}", _owner, source);
            }
            Ok(())
        })();
        if result.is_err() {
            for target in mounted.iter().rev() {
                target.unmount().log_ok();
            }
            payload.unmount().log_ok();
            return None;
        }
        self.early_dir = PAYLOAD.to_string();
        self.early_targets = mounted.iter().map(|p| p.to_string()).collect();
        self.early_contexts = contexts;
        debug!(
            "early-mount: {} files, {} init scripts before init",
            mounts.len(),
            scripts.len()
        );
        scripts
            .into_values()
            .map(|text| guard_services(&text, &format!("{}/exec.sh", runtime_dir)))
            .collect()
    }
    pub(crate) fn finish_early_mounts(&self) {
        if self.early_dir.is_empty() {
            return;
        }
        let contexts_ready = self
            .early_contexts
            .iter()
            .all(|EarlyContext(path, original)| {
                let mut context = cstr::buf::default();
                if fd_get_secontext(original.as_raw_fd(), &mut context)
                    .log()
                    .is_err()
                    || !context.as_str().contains(':')
                {
                    warn!(
                        "early-mount: invalid original context [{}] for {}",
                        context, path
                    );
                    return false;
                }
                debug!("early-mount: {} context [{}]", path, context);
                path.set_secontext(&context).log().is_ok()
            });
        let directory = Utf8CString::from(self.early_dir.as_str());
        if contexts_ready
            && write_control(&format!("{}/ready", self.early_dir), b"1\n")
                .log()
                .is_ok()
            && directory
                .remount_mount_flags(MsFlags::MS_RDONLY)
                .log()
                .is_ok()
        {
            return;
        }
        for target in self.early_targets.iter().rev() {
            Utf8CString::from(target.as_str()).unmount().log_ok();
        }
        directory.unmount().log_ok();
        write_control(".magisk/early-snapshot-invalid", b"1\n").log_ok();
        warn!("early-mount: policy context publication failed; plan removed");
    }
}
