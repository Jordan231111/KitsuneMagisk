use super::SuInfo;
use super::consent::{decode_response, read_response};
use super::db::RootSettings;
use crate::consts::{INTERNAL_DIR, MAGISK_FILE_CON};
use crate::daemon::{MagiskD, to_user_id};
use crate::db::DbArg::Integer;
use crate::db::SqliteReturn;
use crate::ffi::{SuPolicy, SuRequest, get_magisk_tmp};
use ExtraVal::{Bool, Int, IntList, Str};
use base::{BytesExt, FileAttr, LoggedResult, ResultExt, Utf8CStrBuf, cstr, fork_dont_care, warn};
use nix::poll::{PollFd, PollFlags, PollTimeout};
use nix::sys::signal::SigSet;
use num_traits::AsPrimitive;
use std::fmt::Write;
use std::io::Write as IoWrite;
use std::os::fd::AsFd;
use std::os::unix::net::{UCred, UnixListener, UnixStream};
use std::os::unix::process::CommandExt;
use std::process::{Command, Stdio, exit};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

struct Extra<'a> {
    key: &'static str,
    value: ExtraVal<'a>,
}

enum ExtraVal<'a> {
    Int(i32),
    Bool(bool),
    Str(&'a str),
    IntList(&'a [u32]),
}

fn app_process() -> Command {
    let mut cmd = Command::new("/system/bin/app_process");
    // Daemon workers block all signals. ART needs its synchronous fault handlers
    // for managed code, so restore the mask in the child, as exec_command does.
    // SAFETY: pthread_sigmask is async-signal-safe; no allocation or logging runs
    // between fork and exec. The daemon worker keeps its original signal mask.
    unsafe {
        cmd.pre_exec(|| {
            SigSet::empty()
                .thread_set_mask()
                .map_err(std::io::Error::from)
        });
    }
    cmd
}

impl Extra<'_> {
    fn add_intent(&self, cmd: &mut Command) {
        match self.value {
            Int(i) => {
                cmd.args(["--ei", self.key, &i.to_string()]);
            }
            Bool(b) => {
                cmd.args(["--ez", self.key, &b.to_string()]);
            }
            Str(s) => {
                cmd.args(["--es", self.key, s]);
            }
            IntList(list) => {
                cmd.args(["--es", self.key]);
                let mut tmp = String::new();
                list.iter().for_each(|i| {
                    write!(&mut tmp, "{i},").ok();
                });
                tmp.pop();
                cmd.arg(&tmp);
            }
        }
    }

    fn add_bind(&self, cmd: &mut Command) {
        let mut tmp: String;
        match self.value {
            Int(i) => {
                tmp = format!("{}:i:{}", self.key, i);
            }
            Bool(b) => {
                tmp = format!("{}:b:{}", self.key, b);
            }
            Str(s) => {
                let s = s.replace("\\", "\\\\").replace(":", "\\:");
                tmp = format!("{}:s:{}", self.key, s);
            }
            IntList(list) => {
                tmp = format!("{}:s:", self.key);
                if !list.is_empty() {
                    list.iter().for_each(|i| {
                        write!(&mut tmp, "{i},").ok();
                    });
                    tmp.pop();
                }
            }
        }
        cmd.args(["--extra", &tmp]);
    }

    fn add_bind_legacy(&self, cmd: &mut Command) {
        match self.value {
            Str(s) => {
                let tmp = format!("{}:s:{}", self.key, s);
                cmd.args(["--extra", &tmp]);
            }
            _ => self.add_bind(cmd),
        }
    }
}

pub(super) struct SuAppContext<'a> {
    pub(super) daemon: &'a MagiskD,
    pub(super) client: &'a UnixStream,
    pub(super) cred: UCred,
    pub(super) request: &'a SuRequest,
    pub(super) info: &'a SuInfo,
    pub(super) settings: &'a mut RootSettings,
    pub(super) sdk_int: i32,
}

impl SuAppContext<'_> {
    fn exec_cmd(&self, action: &'static str, extras: &[Extra], use_provider: bool) {
        let user = to_user_id(self.info.eval_uid);
        let user = user.to_string();

        if use_provider && cstr!("/system/framework/content.jar").exists() {
            let provider = format!("content://{}.provider", self.info.mgr_pkg);
            let mut cmd = app_process();
            cmd.args([
                "/system/bin",
                "com.android.commands.content.Content",
                "call",
                "--uri",
                &provider,
                "--user",
                &user,
                "--method",
                action,
            ]);
            if self.sdk_int >= 30 {
                extras.iter().for_each(|e| e.add_bind(&mut cmd))
            } else {
                extras.iter().for_each(|e| e.add_bind_legacy(&mut cmd))
            }
            cmd.env("CLASSPATH", "/system/framework/content.jar");

            if let Ok(output) = cmd.output()
                && output.status.success()
                && !output.stderr.contains(b"Error")
                && !output.stdout.contains(b"Error")
            {
                // The provider call succeed
                return;
            }
        }

        let mut cmd = app_process();
        cmd.args([
            "/system/bin",
            "com.android.commands.am.Am",
            "start",
            "-p",
            &self.info.mgr_pkg,
            "--user",
            &user,
            "-a",
            "android.intent.action.VIEW",
            "-f",
            // FLAG_ACTIVITY_NEW_TASK|FLAG_ACTIVITY_MULTIPLE_TASK|
            // FLAG_ACTIVITY_EXCLUDE_FROM_RECENTS|FLAG_INCLUDE_STOPPED_PACKAGES
            "0x18800020",
            "--es",
            "action",
            action,
        ]);
        extras.iter().for_each(|e| e.add_intent(&mut cmd));
        cmd.env("CLASSPATH", "/system/framework/am.jar");

        // Bound launcher failures as well as the consent socket wait.
        cmd.stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        for _ in 0..3 {
            let Ok(mut child) = cmd.spawn().log() else {
                continue;
            };
            let launch_deadline = Instant::now() + Duration::from_secs(3);
            loop {
                if let Ok(Some(_)) = child.try_wait() {
                    if let Ok(output) = child.wait_with_output().log() {
                        if output.status.success()
                            && !output.stdout.is_empty()
                            && !output.stdout.contains(b"Error")
                            && !output.stderr.contains(b"Error")
                        {
                            return;
                        }
                        warn!("su: manager launch failed ({})", output.status);
                        // Do not log intent arguments: log/notify can include a
                        // user's command. Keep framework diagnostics bounded.
                        if action == "request" {
                            let end = output.stderr.len().min(1024);
                            warn!("{}", String::from_utf8_lossy(&output.stderr[..end]));
                        }
                    }
                    break;
                }
                if Instant::now() >= launch_deadline {
                    warn!("su: manager launch timed out");
                    child.kill().ok();
                    child.wait().ok();
                    break;
                }
                std::thread::sleep(Duration::from_millis(20));
            }
        }
    }

    fn app_request(&mut self) {
        // A single accepted connection binds metadata, consent and persistence.
        // Sequence numbers prevent a late activity from answering a reused PID.
        static SEQUENCE: AtomicU64 = AtomicU64::new(0);
        let mut path = cstr::buf::new::<108>();
        path.write_fmt(format_args!(
            "{}/{}/su_{}_{}",
            get_magisk_tmp(),
            INTERNAL_DIR,
            self.cred.pid.unwrap_or(-1),
            SEQUENCE.fetch_add(1, Ordering::Relaxed)
        ))
        .ok();
        let deadline = Instant::now() + Duration::from_secs(70);
        let result = (|| -> LoggedResult<SuPolicy> {
            let listener = UnixListener::bind(path.as_str()).log()?;
            let mut attr = FileAttr::new();
            attr.st.st_mode = 0o600;
            attr.st.st_uid = self.info.mgr_uid.as_();
            attr.st.st_gid = self.info.mgr_uid.as_();
            attr.con.push_str(MAGISK_FILE_CON);
            path.set_attr(&attr)?;
            listener.set_nonblocking(true).log()?;
            self.exec_cmd(
                "request",
                &[Extra {
                    key: "socket",
                    value: Str(&path),
                }],
                false,
            );
            let remaining = || deadline.saturating_duration_since(Instant::now());
            let mut pfd = [PollFd::new(listener.as_fd(), PollFlags::POLLIN)];
            let wait = PollTimeout::try_from(remaining()).unwrap_or(PollTimeout::ZERO);
            if nix::poll::poll(&mut pfd, wait).log()? == 0 {
                return Ok(SuPolicy::Deny);
            }
            let (mut stream, _) = listener.accept().log()?;
            path.remove().log_ok();
            if stream.peer_cred().log()?.uid != self.info.mgr_uid as u32 || remaining().is_zero() {
                return Ok(SuPolicy::Deny);
            }
            stream.set_read_timeout(Some(remaining())).log()?;
            stream.set_write_timeout(Some(remaining())).log()?;
            let cfg = self.daemon.get_db_settings()?;
            if std::mem::discriminant(&cfg.root_access)
                != std::mem::discriminant(&self.info.cfg.root_access)
                || std::mem::discriminant(&cfg.multiuser_mode)
                    != std::mem::discriminant(&self.info.cfg.multiuser_mode)
            {
                return Ok(SuPolicy::Deny);
            }
            // Big endian, versioned and fixed length. Intent extras never name a UID.
            for value in [
                0x4b535531,
                self.cred.uid as i32,
                self.cred.pid.unwrap_or(-1),
                i32::from(cfg.su_auth),
                remaining().as_millis() as i32,
            ] {
                stream.write_all(&value.to_be_bytes()).log()?;
            }
            let response = read_response(&mut stream, deadline).log()?;
            let mut pfd = [PollFd::new(self.client.as_fd(), PollFlags::empty())];
            let disconnected = nix::poll::poll(&mut pfd, PollTimeout::ZERO).log()? != 0;
            let current = self.daemon.get_db_settings()?;
            let Some((policy, seconds)) = decode_response(response, cfg.su_auth || current.su_auth)
            else {
                return Ok(SuPolicy::Deny);
            };
            if remaining().is_zero()
                || disconnected
                || std::mem::discriminant(&cfg.root_access)
                    != std::mem::discriminant(&current.root_access)
                || std::mem::discriminant(&cfg.multiuser_mode)
                    != std::mem::discriminant(&current.multiuser_mode)
                || !crate::ffi::sulist_allows_uid(self.info.uid)
            {
                return Ok(SuPolicy::Deny);
            }
            if seconds >= 0 {
                // Preserve logging/notification preferences on an existing UID.
                self.daemon
                    .db_exec(
                        "INSERT OR REPLACE INTO policies (uid,policy,until,logging,notification) \
                     VALUES (?,?,CASE WHEN ?=0 THEN 0 ELSE strftime('%s','now')+? END, \
                     COALESCE((SELECT logging FROM policies WHERE uid=?),1), \
                     COALESCE((SELECT notification FROM policies WHERE uid=?),1))",
                        &[
                            Integer(self.info.eval_uid as i64),
                            Integer(policy as i64),
                            Integer(seconds as i64),
                            Integer(seconds as i64),
                            Integer(self.info.eval_uid as i64),
                            Integer(self.info.eval_uid as i64),
                        ],
                    )
                    .sql_result()
                    .log()?;
            }
            stream.write_all(&policy.to_be_bytes()).log()?;
            Ok(SuPolicy { repr: policy })
        })();
        if path.exists() {
            path.remove().log_ok();
        }
        self.settings.policy = result.unwrap_or(SuPolicy::Deny);
    }

    fn app_notify(&self) {
        let extras = [
            Extra {
                key: "from.uid",
                value: Int(self.cred.uid.as_()),
            },
            Extra {
                key: "pid",
                value: Int(self.cred.pid.unwrap_or(-1).as_()),
            },
            Extra {
                key: "policy",
                value: Int(self.settings.policy.repr),
            },
        ];
        self.exec_cmd("notify", &extras, true);
    }

    fn app_log(&self) {
        let command = if self.request.command.is_empty() {
            &self.request.shell
        } else {
            &self.request.command
        };
        let extras = [
            Extra {
                key: "from.uid",
                value: Int(self.cred.uid.as_()),
            },
            Extra {
                key: "to.uid",
                value: Int(self.request.target_uid),
            },
            Extra {
                key: "pid",
                value: Int(self.cred.pid.unwrap_or(-1).as_()),
            },
            Extra {
                key: "policy",
                value: Int(self.settings.policy.repr),
            },
            Extra {
                key: "target",
                value: Int(self.request.target_pid),
            },
            Extra {
                key: "context",
                value: Str(&self.request.context),
            },
            Extra {
                key: "gids",
                value: IntList(&self.request.gids),
            },
            Extra {
                key: "command",
                value: Str(command),
            },
            Extra {
                key: "notify",
                value: Bool(self.settings.notify),
            },
        ];
        self.exec_cmd("log", &extras, true);
    }

    pub(super) fn connect_app(&mut self) {
        // If policy is undetermined, show dialog for user consent
        if self.settings.policy == SuPolicy::Query {
            self.app_request();
        }

        if !self.settings.log && !self.settings.notify {
            return;
        }

        if fork_dont_care() != 0 {
            return;
        }

        // Notify su usage to application
        if self.settings.log {
            self.app_log();
        } else if self.settings.notify {
            self.app_notify();
        }

        exit(0);
    }
}
