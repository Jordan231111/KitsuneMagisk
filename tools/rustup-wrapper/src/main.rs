use std::env;
use std::path::Path;
use std::process::{self, Command, ExitStatus, Stdio};

#[cfg(unix)]
use std::os::unix::process::ExitStatusExt;

use home::cargo_home;

/********************************
 * Why do we need this wrapper?
 ********************************
 *
 * The command `rustup component list` does not work with custom toolchains:
 * > error: toolchain 'magisk' does not support components
 *
 * However, this command is used by several IDEs to determine component
 * availability, such as clippy, rustfmt etc.
 * In this program, we use the output of the command with the nightly
 * channel if any `component` command failed.
*/

fn child_exit_code(status: ExitStatus) -> i32 {
    if let Some(code) = status.code() {
        return code;
    }

    #[cfg(unix)]
    {
        // Match the status convention used by POSIX shells for a process
        // terminated by a signal.
        128 + status.signal().unwrap_or(1)
    }

    #[cfg(not(unix))]
    {
        1
    }
}

fn finish(status: ExitStatus) -> std::io::Result<()> {
    if status.success() {
        Ok(())
    } else {
        process::exit(child_exit_code(status));
    }
}

fn main() -> std::io::Result<()> {
    let exe = env::args().next().unwrap();
    let exe = Path::new(&exe).file_name().unwrap().to_str().unwrap();
    let real_exe = cargo_home()?.join("bin").join(exe);
    let argv: Vec<String> = env::args().skip(1).collect();

    if exe.starts_with("rustup") && argv.iter().any(|s| s == "component") {
        let status = Command::new(&real_exe)
            .args(&argv)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()?;
        if !status.success() {
            let mut cmd = Command::new(&real_exe);
            // Hardcode to use the nightly channel
            cmd.arg("+nightly");
            // Remove any explicit channel specification
            cmd.args(argv.iter().filter(|s| !s.starts_with('+')));
            return finish(cmd.status()?);
        }
    }

    // Simply pass through
    finish(Command::new(&real_exe).args(argv.iter()).status()?)
}

#[cfg(test)]
mod tests {
    use super::child_exit_code;
    use std::process::ExitStatus;

    #[cfg(unix)]
    use std::os::unix::process::ExitStatusExt;

    #[cfg(windows)]
    use std::os::windows::process::ExitStatusExt;

    #[test]
    fn preserves_child_exit_code() {
        #[cfg(unix)]
        let status = ExitStatus::from_raw(37 << 8);
        #[cfg(windows)]
        let status = ExitStatus::from_raw(37);

        assert_eq!(child_exit_code(status), 37);
    }

    #[cfg(unix)]
    #[test]
    fn maps_signal_to_shell_status() {
        let status = ExitStatus::from_raw(15);
        assert_eq!(child_exit_code(status), 143);
    }
}
