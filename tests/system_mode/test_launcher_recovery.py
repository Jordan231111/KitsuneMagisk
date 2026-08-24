from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "scripts" / "kitsune_system_launcher.sh"
RESCUE = ROOT / "scripts" / "kitsune_system_rescue.sh"
VERIFIER = ROOT / "scripts" / "system_mode_verify.sh"


def function_body(source: str, name: str) -> str:
    marker = f"{name}()"
    start = source.index(marker)
    end = source.find("\n}\n\n", start)
    if end < 0:
        raise AssertionError(f"could not find end of {name}")
    return source[start : end + 3]


def run_shell(source: str, working: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", source, "launcher-test", str(working)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class SystemModeLauncherRecoveryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.launcher = LAUNCHER.read_text(encoding="utf-8")
        cls.rescue = RESCUE.read_text(encoding="utf-8")
        cls.verifier = VERIFIER.read_text(encoding="utf-8")
        cls.prepare = function_body(cls.launcher, "ksl_prepare")
        cls.run_prepare = function_body(cls.launcher, "ksl_run_prepare")
        cls.unwind = function_body(cls.launcher, "ksl_unwind_runtime")

    def test_every_prepare_failure_enters_unwind_then_managed_recovery(self) -> None:
        harness = textwrap.dedent(
            f"""
            set -u
            TEST_ROOT="$1"
            EVENTS="$TEST_ROOT/events"
            : >"$EVENTS"
            SM_INSTALL_ID=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
            SM_RUNTIME_PATH=/sbin
            SM_STATE=COMMITTED
            KSL_RUNTIME_MOUNT_LIST="$TEST_ROOT/mounts"
            KSL_BB=bb
            FAIL_AT="$2"

            bb() {{
              applet="$1"
              shift
              command "$applet" "$@"
            }}
            getprop() {{ printf '\n'; }}
            ksl_log() {{ :; }}
            ksl_prepare_runtime() {{
              printf 'runtime\n' >>"$EVENTS"
              [ "$FAIL_AT" != runtime ]
            }}
            ksl_apply_policy() {{
              printf 'policy\n' >>"$EVENTS"
              [ "$FAIL_AT" != policy ]
            }}
            sm_failpoint() {{
              printf 'failpoint:%s\n' "$1" >>"$EVENTS"
              [ "$FAIL_AT" != "$1" ]
            }}
            ksl_set_stage_ready() {{
              printf 'ready\n' >>"$EVENTS"
              [ "$FAIL_AT" != ready ]
            }}
            ksl_unwind_runtime() {{ printf 'unwind\n' >>"$EVENTS"; }}
            ksl_recover_pending() {{ printf 'recover\n' >>"$EVENTS"; }}
            ksl_reboot() {{ printf 'reboot\n' >>"$EVENTS"; }}
            sm_update_state() {{ printf 'state:%s\n' "$1" >>"$EVENTS"; SM_STATE="$1"; }}

            {self.prepare}
            {self.run_prepare}

            ksl_run_prepare
            """
        )
        for failure in ("runtime", "policy", "runtime-policy-applied", "ready"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(
                prefix="kitsune-launcher-prepare-"
            ) as temp:
                root = Path(temp)
                result = subprocess.run(
                    ["bash", "-c", harness, "launcher-test", str(root), failure],
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self.assertEqual(0, result.returncode, result.stderr)
                events = (root / "events").read_text(encoding="utf-8").splitlines()
                self.assertEqual(1, events.count("unwind"), events)
                self.assertEqual(1, events.count("recover"), events)
                self.assertLess(events.index("unwind"), events.index("recover"), events)
                self.assertNotIn("reboot", events)

    def test_failed_recovery_reboots_only_when_runtime_unwind_is_incomplete(self) -> None:
        common = textwrap.dedent(
            f"""
            set -u
            TEST_ROOT="$1"
            EVENTS="$TEST_ROOT/events"
            : >"$EVENTS"
            SM_STATE=COMMITTED
            KSL_BB=bb
            bb() {{ command "$@"; }}
            ksl_log() {{ :; }}
            ksl_prepare() {{ return 1; }}
            ksl_unwind_runtime() {{ printf 'unwind\n' >>"$EVENTS"; return "$UNWIND_RESULT"; }}
            ksl_recover_pending() {{ printf 'recover\n' >>"$EVENTS"; return 1; }}
            ksl_reboot() {{ printf 'reboot\n' >>"$EVENTS"; return 0; }}
            sm_update_state() {{ return 0; }}
            {self.run_prepare}
            if ksl_run_prepare; then exit 90; fi
            """
        )
        for unwind_result, should_reboot in ((0, False), (1, True)):
            with self.subTest(unwind_result=unwind_result), tempfile.TemporaryDirectory(
                prefix="kitsune-launcher-recovery-"
            ) as temp:
                script = f"UNWIND_RESULT={unwind_result}\n{common}"
                result = run_shell(script, Path(temp))
                self.assertEqual(0, result.returncode, result.stderr)
                events = (Path(temp) / "events").read_text(encoding="utf-8").splitlines()
                self.assertEqual(1, events.count("unwind"), events)
                self.assertEqual(1, events.count("recover"), events)
                self.assertEqual(should_reboot, "reboot" in events, events)
                self.assertLess(events.index("unwind"), events.index("recover"), events)

    def test_verified_runtime_failure_records_failed_before_single_reboot(self) -> None:
        harness = textwrap.dedent(
            f"""
            set -u
            TEST_ROOT="$1"
            EVENTS="$TEST_ROOT/events"
            : >"$EVENTS"
            SM_STATE=BOOT_VERIFIED
            KSL_BB=bb
            bb() {{ command "$@"; }}
            ksl_log() {{ :; }}
            ksl_prepare() {{ return 1; }}
            ksl_unwind_runtime() {{ printf 'unwind\n' >>"$EVENTS"; }}
            ksl_recover_pending() {{ printf 'unexpected-recovery\n' >>"$EVENTS"; }}
            sm_update_state() {{ printf 'state:%s\n' "$1" >>"$EVENTS"; SM_STATE="$1"; }}
            ksl_reboot() {{ printf 'reboot\n' >>"$EVENTS"; return 0; }}
            {self.run_prepare}
            if ksl_run_prepare; then exit 90; fi
            """
        )
        with tempfile.TemporaryDirectory(prefix="kitsune-launcher-verified-") as temp:
            result = run_shell(harness, Path(temp))
            self.assertEqual(0, result.returncode, result.stderr)
            events = (Path(temp) / "events").read_text(encoding="utf-8").splitlines()
        self.assertEqual(["unwind", "state:FAILED", "reboot"], events)

    def test_runtime_unwind_detaches_children_before_runtime_and_cleans_residue(self) -> None:
        harness = textwrap.dedent(
            f"""
            set -u
            TEST_ROOT="$1"
            EVENTS="$TEST_ROOT/events"
            : >"$EVENTS"
            KSL_BB=bb
            SM_INSTALL_ID=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
            SM_RUNTIME_PATH=/sbin
            KSL_RUNTIME_MOUNT_TARGET=/sbin
            KSL_RUNTIME_MOUNT_CREATED=true
            KSL_RUNTIME_MOUNT_OWNED=true
            KSL_RUNTIME_DIR_CREATED=false
            KSL_RUNTIME_MOUNT_LIST="$TEST_ROOT/mounts"
            KSL_SBIN_SYSROOT=/dev/kitsune-test-sysroot
            KSL_SBIN_SYSROOT_MOUNTED=true
            KSL_SBIN_BACKING=/dev/kitsune-test-backing
            printf '/sbin/first\n/sbin/.magisk/worker\n' >"$KSL_RUNTIME_MOUNT_LIST"

            bb() {{
              applet="$1"
              shift
              case "$applet" in
                rm) command rm "$@" ;;
                *) command "$applet" "$@" ;;
              esac
            }}
            ksl_runtime_mount_owned() {{ return 0; }}
            ksl_unmount_one() {{ printf 'unmount:%s\n' "$1" >>"$EVENTS"; }}
            sm_remove_tree_safe() {{ printf 'remove:%s\n' "$2" >>"$EVENTS"; }}
            {self.unwind}
            ksl_unwind_runtime
            """
        )
        with tempfile.TemporaryDirectory(prefix="kitsune-launcher-unwind-") as temp:
            root = Path(temp)
            result = run_shell(harness, root)
            self.assertEqual(0, result.returncode, result.stderr)
            events = (root / "events").read_text(encoding="utf-8").splitlines()
            self.assertFalse((root / "mounts").exists())
        self.assertLess(events.index("unmount:/sbin/.magisk/worker"), events.index("unmount:/sbin"))
        self.assertLess(events.index("unmount:/sbin"), events.index("unmount:/dev/kitsune-test-sysroot"))
        self.assertIn("remove:/dev/kitsune-test-sysroot", events)
        self.assertIn("remove:/dev/kitsune-test-backing", events)

    def test_launcher_exposes_fault_boundaries_after_each_irreversible_boot_step(self) -> None:
        for boundary in (
            "runtime-tmpfs-mounted",
            'runtime-copy:$file',
            "runtime-worker-mounted",
            "runtime-policy-applied",
        ):
            with self.subTest(boundary=boundary):
                self.assertIn(boundary, self.launcher)
        self.assertIn("prepare) ksl_run_prepare", self.launcher)

    def test_boot_entrypoints_hold_one_lock_and_release_it_on_every_managed_exit(self) -> None:
        for source, configure, acquire, load, exit_name in (
            (
                self.launcher,
                'sm_configure "$KSL_PAYLOAD"',
                'sm_acquire_lock "launcher-${1:-invalid}"',
                "sm_load_transaction",
                "ksl_exit",
            ),
            (
                self.rescue,
                'sm_configure "$KSR_PAYLOAD"',
                "sm_acquire_lock rescue",
                "sm_load_transaction",
                "ksr_exit",
            ),
            (
                self.verifier,
                'sm_configure "$SMV_PAYLOAD"',
                "sm_acquire_lock verify",
                "sm_load_transaction",
                "smv_exit",
            ),
        ):
            with self.subTest(exit_name=exit_name):
                self.assertLess(source.index(configure), source.index(acquire))
                self.assertLess(source.index(acquire), source.index(load))
                managed_exit = function_body(source, exit_name)
                self.assertLess(managed_exit.index("sm_release_lock"), managed_exit.index('exit "$result"'))
                post_acquire = source[source.index(acquire) :]
                direct_exits = [
                    line
                    for line in post_acquire.splitlines()
                    if line.strip().startswith("exit ")
                ]
                self.assertEqual([], direct_exits)

    def test_daemon_capable_launcher_children_do_not_inherit_the_lock(self) -> None:
        for command in (
            "--preinit-device 9>&-",
            "--live --magisk --apply \"$rule\" 9>&-",
            "--post-fs-data 9>&-",
            "--service 9>&-",
            "--boot-complete 9>&-",
        ):
            with self.subTest(command=command):
                self.assertIn(command, self.launcher)
        self.assertIn('magisk" --stop 9>&-', self.verifier)


if __name__ == "__main__":
    unittest.main()
