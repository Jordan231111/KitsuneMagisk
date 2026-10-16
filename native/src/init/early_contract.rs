use std::collections::BTreeSet;

pub fn safe_path(path: &str) -> bool {
    !path.is_empty()
        && path.len() < 2048
        && path.split('/').all(|part| {
            !part.is_empty()
                && part != "."
                && part != ".."
                && part
                    .bytes()
                    .all(|c| c.is_ascii_alphanumeric() || b"._-+@".contains(&c))
        })
}

// This deliberately excludes arbitrary init commands, imports, critical services,
// blocking exec/insmod and overrides of Android services. A v2 service is namespaced,
// explicitly started, oneshot and bounded by Android init's timeout_period.
pub fn validate_rc(text: &str, owner: &str) -> bool {
    let prefix = format!("kitsune_{owner}_");
    let mut services = BTreeSet::new();
    let mut starts = Vec::new();
    let mut service = false;
    let mut bounded = false;
    let mut oneshot = false;
    let mut disabled = false;
    let mut action = false;
    for line in text
        .lines()
        .map(str::trim)
        .filter(|s| !s.is_empty() && !s.starts_with('#'))
    {
        if line.len() > 1024 || line.chars().any(|c| c.is_control()) {
            return false;
        }
        let words: Vec<_> = line.split_whitespace().collect();
        match words.as_slice() {
            ["service", name, exe, ..] => {
                if service && !(bounded && oneshot && disabled) {
                    return false;
                }
                if !name.starts_with(&prefix)
                    || !safe_path(name)
                    || name.contains('/')
                    || !services.insert(*name)
                    || !exe.starts_with("/system/bin/")
                    || !safe_path(exe.trim_start_matches('/'))
                {
                    return false;
                }
                service = true;
                action = false;
                bounded = false;
                oneshot = false;
                disabled = false;
            }
            ["on", "early-init" | "init" | "post-fs" | "boot"] => {
                if service && !(bounded && oneshot && disabled) {
                    return false;
                }
                service = false;
                action = true;
            }
            ["oneshot"] if service => oneshot = true,
            ["disabled"] if service => disabled = true,
            ["timeout_period", "10"] if service => bounded = true,
            ["user", "root"] | ["group", "root"] | ["seclabel", "u:r:magisk:s0"] if service => (),
            ["start", name] if action => starts.push(*name),
            ["setprop", name, _] if action && name.starts_with("sys.kitsune.") => (),
            _ => return false,
        }
    }
    (!service || (bounded && oneshot && disabled)) && starts.iter().all(|n| services.contains(n))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_escaping_or_ambiguous_paths() {
        for path in [
            "",
            "/system/etc/a",
            "system/../a",
            "system//a",
            "system/a\nb",
            "system/a b",
            "system/./a",
        ] {
            assert!(!safe_path(path), "{path:?}");
        }
        assert!(safe_path("system/etc/vintf/manifest.xml"));
    }
    #[test]
    fn bounded_namespaced_service() {
        let rc = "service kitsune_demo_probe /system/bin/sh /system/etc/probe.sh\n oneshot\n disabled\n timeout_period 10\non early-init\n start kitsune_demo_probe\n setprop sys.kitsune.probe ready\n";
        assert!(validate_rc(rc, "demo"));
        for old in ["oneshot", "disabled", "timeout_period 10"] {
            assert!(!validate_rc(&rc.replace(old, ""), "demo"));
        }
        for replacement in [
            "critical",
            "reboot_on_failure reboot",
            "override",
            "timeout_period 0",
        ] {
            assert!(!validate_rc(
                &rc.replace("timeout_period 10", replacement),
                "demo"
            ));
        }
        assert!(!validate_rc(
            &rc.replace("kitsune_demo_probe", "zygote"),
            "demo"
        ));
        assert!(!validate_rc(
            &rc.replace("/system/bin/sh", "/system/bin/../../data/x"),
            "demo"
        ));
    }
    #[test]
    fn rejects_blocking_commands_and_unowned_services() {
        for command in [
            "exec /system/bin/sleep 1000",
            "insmod /data/a.ko",
            "start zygote",
            "setprop ctl.stop zygote",
            "import /data/evil.rc",
        ] {
            assert!(!validate_rc(
                &format!("on early-init\n {command}\n"),
                "demo"
            ));
        }
        assert!(!validate_rc(
            "on property:sys.kitsune.loop=*\n setprop sys.kitsune.loop 1\n",
            "demo"
        ));
    }
}
