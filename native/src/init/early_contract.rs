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

fn valid_rc_line(line: &str) -> bool {
    // Android init only quotes with double quotes and supports line folding.
    // Reject folding/escapes so options cannot turn into service arguments.
    if line.contains('\\')
        || line.bytes().filter(|&b| b == b'"').count() % 2 != 0
        || line.contains("$${MAGISKTMP}")
    {
        return false;
    }
    let mut remaining = line;
    while let Some((_, tail)) = remaining.split_once('$') {
        if let Some(rest) = tail.strip_prefix('$') {
            remaining = rest;
        } else if let Some(rest) = tail.strip_prefix('{') {
            let Some((name, rest)) = rest.split_once('}') else {
                return false;
            };
            if name.is_empty()
                || name.len() > 128
                || !name
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
            {
                return false;
            }
            remaining = rest;
        } else {
            return false;
        }
    }
    true
}

pub fn guard_services(text: &str, guard: &str) -> Option<String> {
    if !guard.starts_with('/') || !safe_path(guard.trim_start_matches('/')) {
        return None;
    }
    let mut output = String::new();
    for line in text.lines() {
        let trimmed = line.trim_start();
        if let Some(rest) = trimmed.strip_prefix("service")
            && rest.starts_with(|c: char| c.is_ascii_whitespace())
        {
            let rest = rest.trim_start();
            let split = rest.find(|c: char| c.is_ascii_whitespace())?;
            output.push_str(&format!(
                "service {} /system/bin/sh {} {}\n",
                &rest[..split],
                guard,
                rest[split..].trim_start()
            ));
        } else {
            output.push_str(line);
            output.push('\n');
        }
    }
    Some(output)
}

// This deliberately excludes arbitrary init commands, imports, critical services,
// blocking exec/insmod and overrides of Android services. A v2 service is namespaced,
// explicitly started, oneshot and bounded by Android init's timeout_period.
pub fn validate_rc(text: &str, owner: &str) -> bool {
    if text.len() > 16 * 1024
        || text.chars().any(|c| {
            (c.is_control() || c.is_whitespace()) && !matches!(c, ' ' | '\t' | '\r' | '\n')
        })
    {
        return false;
    }
    let prefix = format!("kitsune_{owner}_");
    let mut services = BTreeSet::new();
    let mut starts = Vec::new();
    let mut service = false;
    let mut bounded = false;
    let mut oneshot = false;
    let mut disabled = false;
    let mut labelled = false;
    let mut action = false;
    for line in text
        .lines()
        .map(str::trim)
        .filter(|s| !s.is_empty() && !s.starts_with('#'))
    {
        if line.len() > 1024 || !valid_rc_line(line) {
            return false;
        }
        let words: Vec<_> = line.split_whitespace().collect();
        match words.as_slice() {
            ["service", name, exe, ..] => {
                if service && !(bounded && oneshot && disabled && labelled) {
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
                labelled = false;
            }
            ["on", "early-init" | "init" | "post-fs" | "boot"] => {
                if service && !(bounded && oneshot && disabled && labelled) {
                    return false;
                }
                service = false;
                action = true;
            }
            ["oneshot"] if service => oneshot = true,
            ["disabled"] if service => disabled = true,
            ["timeout_period", "10"] if service => bounded = true,
            ["user", "root"] | ["group", "root"] if service => (),
            ["seclabel", "u:r:magisk:s0"] if service => labelled = true,
            ["start", name] if action => starts.push(*name),
            ["setprop", name, _]
                if action
                    && name.starts_with("sys.kitsune.")
                    && name
                        .bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b)) =>
            {}
            _ => return false,
        }
    }
    (!service || (bounded && oneshot && disabled && labelled))
        && starts.iter().all(|n| services.contains(n))
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
        let rc = "service kitsune_demo_probe /system/bin/sh /system/etc/probe.sh\n oneshot\n disabled\n timeout_period 10\n seclabel u:r:magisk:s0\non early-init\n start kitsune_demo_probe\n setprop sys.kitsune.probe ready\n";
        assert!(validate_rc(rc, "demo"));
        for old in [
            "oneshot",
            "disabled",
            "timeout_period 10",
            "seclabel u:r:magisk:s0",
        ] {
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
    fn init_lexing_cannot_hide_required_options() {
        let rc = "service kitsune_demo_probe /system/bin/sh -c \"echo ok\"\n oneshot\n disabled\n timeout_period 10\n seclabel u:r:magisk:s0\n";
        assert!(validate_rc(rc, "demo"));
        assert!(!validate_rc(
            &rc.replace("echo ok\"", "echo ok\" \\"),
            "demo"
        ));
        assert!(!validate_rc(
            &rc.replace("oneshot", "oneshot\u{a0}"),
            "demo"
        ));
        assert!(!validate_rc(&rc.replace("oneshot", "oneshot\u{c}"), "demo"));
        assert!(!validate_rc(&rc.replace("echo ok", "echo $(id)"), "demo"));
        assert!(validate_rc(&rc.replace("echo ok", "echo $$(id)"), "demo"));
        assert!(validate_rc(
            &rc.replace("echo ok", "echo ${ro.hardware}"),
            "demo"
        ));
        assert!(!validate_rc(
            &rc.replace("echo ok", "echo ${bad property}"),
            "demo"
        ));
        assert!(!validate_rc(&"#".repeat(16385), "demo"));
    }

    #[test]
    fn service_guard_preserves_arguments_and_options() {
        let rc = "service\tkitsune_demo_probe /system/bin/sh -c \"echo $$(id)\"\n oneshot\n disabled\n timeout_period 10\n seclabel u:r:magisk:s0\n";
        assert!(validate_rc(rc, "demo"));
        let guarded =
            guard_services(rc, "/debug_ramdisk/.magisk/early-mount/exec.sh").expect("guard");
        assert!(guarded.starts_with("service kitsune_demo_probe /system/bin/sh /debug_ramdisk/.magisk/early-mount/exec.sh /system/bin/sh -c \"echo $$(id)\""));
        assert!(validate_rc(&guarded, "demo"));
        assert!(guard_services(rc, "/bad path/script").is_none());
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
