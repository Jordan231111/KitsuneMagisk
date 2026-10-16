//! Same-filesystem module publication with recovery after either rename.
use std::fs::{self, File};
use std::io;
use std::path::Path;

fn sync_dir(path: &Path) -> io::Result<()> {
    File::open(path)?.sync_all()
}

pub fn recover(root: &Path, prefix: &str) -> io::Result<()> {
    if !root.exists() {
        return Ok(());
    }
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        let name = entry.file_name();
        let Some(name) = name.to_str().and_then(|n| n.strip_prefix(prefix)) else {
            continue;
        };
        if name.is_empty() || name.starts_with('.') || !entry.file_type()?.is_dir() {
            continue;
        }
        let target = root.join(name);
        if target.try_exists()? {
            fs::remove_dir_all(entry.path())?;
        } else {
            fs::rename(entry.path(), target)?;
        }
        sync_dir(root)?;
    }
    Ok(())
}

pub fn publish(root: &Path, source: &Path, name: &str) -> io::Result<()> {
    if name.is_empty() || name.starts_with('.') || name.contains('/') {
        return Err(io::ErrorKind::InvalidInput.into());
    }
    let target = root.join(name);
    let backup = root.join(format!(".rollback-{name}"));
    if backup.try_exists()? {
        return Err(io::ErrorKind::AlreadyExists.into());
    }
    if !fs::symlink_metadata(source)?.is_dir() {
        return Err(io::ErrorKind::InvalidInput.into());
    }
    let existed = target.try_exists()?;
    if existed {
        if !fs::symlink_metadata(&target)?.is_dir() {
            return Err(io::ErrorKind::InvalidInput.into());
        }
        // Preserve user intent before publication, not after a potentially interrupted rename.
        if target.join("disable").exists() {
            File::create(source.join("disable"))?.sync_all()?;
            sync_dir(source)?;
        }
        fs::rename(&target, &backup)?;
        sync_dir(root)?;
    }
    if let Err(error) = fs::rename(source, &target) {
        if existed {
            fs::rename(&backup, &target)?;
            sync_dir(root)?;
        }
        return Err(error);
    }
    sync_dir(root)?;
    if let Some(parent) = source.parent() {
        sync_dir(parent)?;
    }
    if existed {
        fs::remove_dir_all(backup)?;
        sync_dir(root)?;
    }
    Ok(())
}
