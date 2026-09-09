import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
import zipfile

import build
import tools.next_system_baseline as baseline

from tools.next_system_baseline import (
    BaselineError,
    cargo_lock_packages,
    read_manifest,
    verify_fixture_contract,
    verify_dependency_contract,
)


class NextSystemManifestTest(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "linux", "Requires Linux proc cmdline")
    def test_busybox_handoff_preserves_arguments_and_status(self):
        source = Path("scripts/util_functions.sh").read_text()
        start = source.index("  export ASH_STANDALONE=1", source.index("ensure_bb()"))
        end = source.index("\n}", start)
        with tempfile.TemporaryDirectory(prefix="kitsune-arguments-") as temporary:
            root = Path(temporary)
            # Bash implements the NUL-delimited read used by BusyBox ash.
            busybox = root / "busybox"
            busybox.write_text('#!/bin/sh\nshift\nexec /bin/bash "$@"\n')
            busybox.chmod(0o755)
            script = root / "script one's test.sh"
            script.write_text(
                'bb=$KITSUNE_TEST_BB\nif [ "${ASH_STANDALONE:-}" != 1 ]; then\n'
                + source[start:end] + '\nfi\nprintf "%s\\0" "$@"\nexit 37\n'
            )
            arguments = ["one space", "one's quote", 'double"quote', "back\\slash",
                         "", "line\nend", "*wildcard*", "--option", "$(literal)"]
            env = os.environ | {"KITSUNE_TEST_BB": str(busybox), "ASH_STANDALONE": ""}
            result = subprocess.run(["sh", str(script), *arguments], env=env,
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 37, result.stderr)
            self.assertEqual(result.stdout, b"\0".join(a.encode() for a in arguments) + b"\0")

    def test_futility_checkout_preserves_binary_bytes(self):
        path = "tools/futility"
        raw = subprocess.check_output(["git", "hash-object", "--no-filters", path])
        normalized = subprocess.check_output(["git", "hash-object", f"--path={path}", path])
        self.assertEqual(raw, normalized)
        self.assertEqual(raw, subprocess.check_output(["git", "rev-parse", f"HEAD:{path}"]))

    def test_unloader_waits_for_specialization_and_releases_context_once(self):
        source = Path("native/src/core/zygisk/hook.cpp").read_text()
        start = source.index("DCL_HOOK_FUNC(static int, pthread_attr_destroy,")
        end = source.index("\n}\n", start) + 3
        program = r"""
#include <cassert>
#include <pthread.h>
#include <utility>
static int tid = 1, pid = 1, calls = 0, restores = 0, deleted = 0, unloaded = 0;
#define gettid() tid
#define getpid() pid
#define ZLOGV(...) ((void)0)
#define DCL_HOOK_FUNC(ret, name, ...) ret new_##name(__VA_ARGS__)
struct HookContext {
    bool should_unmap = false;
    bool can_restore = true;
    void *self_handle = reinterpret_cast<void *>(42);
    void restore_plt_hook() { ++restores; should_unmap = can_restore; }
    ~HookContext() { ++deleted; }
};
static HookContext *g_hook;
static int old_pthread_attr_destroy(pthread_attr_t *) { ++calls; return 19; }
static int dlclose(void *handle) { assert(handle == reinterpret_cast<void *>(42)); ++unloaded; return 0; }
""" + source[start:end] + r"""
int main() {
    g_hook = new HookContext;
    assert(new_pthread_attr_destroy(nullptr) == 19);
    assert(g_hook && restores == 0 && deleted == 0);
    g_hook->should_unmap = true;
    tid = 2;
    assert(new_pthread_attr_destroy(nullptr) == 19);
    assert(g_hook && restores == 0 && deleted == 0);
    tid = 1;
    g_hook->can_restore = false;
    assert(new_pthread_attr_destroy(nullptr) == 19);
    assert(!g_hook && restores == 1 && deleted == 1 && unloaded == 0);
    assert(new_pthread_attr_destroy(nullptr) == 19);
    assert(deleted == 1);
    g_hook = new HookContext;
    g_hook->should_unmap = true;
    assert(new_pthread_attr_destroy(nullptr) == 0);
    assert(!g_hook && restores == 2 && deleted == 2 && unloaded == 1);
    assert(calls == 5);
}
"""
        with tempfile.TemporaryDirectory(prefix="kitsune-unloader-") as temporary:
            root = Path(temporary)
            cpp, binary = root / "unloader.cpp", root / "unloader"
            cpp.write_text(program)
            subprocess.run(["c++", "-std=c++20", "-O2", str(cpp), "-o", str(binary)],
                           check=True, capture_output=True, text=True)
            subprocess.run([str(binary)], check=True, timeout=15)

    def test_zygote_name_copy_stops_at_guard_pages(self):
        source = Path("native/src/core/zygisk/hook.cpp").read_text()
        start = source.index("DCL_HOOK_FUNC(static size_t, legacy_strlcpy,")
        end = source.index("\n}\n", start) + 3
        function = source[start:end]
        program = """
#include <algorithm>
#include <cassert>
#include <cstddef>
#include <cstring>
#include <sys/mman.h>
#include <unistd.h>
#define DCL_HOOK_FUNC(ret, name, ...) ret new_##name(__VA_ARGS__)
""" + function + r"""
int main() {
    const size_t page = static_cast<size_t>(sysconf(_SC_PAGESIZE));
    auto allocate = [page] {
        auto p = static_cast<char *>(mmap(nullptr, page * 2, PROT_READ | PROT_WRITE,
                                          MAP_PRIVATE | MAP_ANON, -1, 0));
        assert(p != MAP_FAILED);
        assert(mprotect(p + page, page, PROT_NONE) == 0);
        return p;
    };
    char *source = allocate();
    char *destination = allocate();
    for (size_t length = 0; length < 256; ++length) {
        char *src = source + page - length - 1;
        memset(src, 'x', length);
        src[length] = '\0';
        assert(new_legacy_strlcpy(nullptr, src, 0) == length);
        for (size_t capacity = 1; capacity < 272; ++capacity) {
            char *dst = destination + page - capacity;
            memset(dst - 1, '#', capacity + 1);
            assert(new_legacy_strlcpy(dst, src, capacity) == length);
            const size_t copied = std::min(length, capacity - 1);
            for (size_t i = 0; i < copied; ++i) assert(dst[i] == 'x');
            assert(dst[copied] == '\0');
            assert(dst[-1] == '#');
            for (size_t i = copied + 1; i < capacity; ++i) assert(dst[i] == '#');
        }
    }
    assert(munmap(source, page * 2) == 0);
    assert(munmap(destination, page * 2) == 0);
}
"""
        with tempfile.TemporaryDirectory(prefix="kitsune-strlcpy-guard-") as temporary:
            root = Path(temporary)
            cpp = root / "guard.cpp"
            binary = root / "guard"
            cpp.write_text(program)
            subprocess.run(["c++", "-std=c++20", "-O2", str(cpp), "-o", str(binary)],
                           check=True, capture_output=True, text=True)
            subprocess.run([str(binary)], check=True, timeout=15)

    def test_git_source_state_is_checked_and_build_races_remove_artifact(self):
        commit = "a" * 40
        clean_output = f"# branch.oid {commit}\n# branch.head next-system\n"
        clean = subprocess.CompletedProcess(
            ["git", "status"], 0, clean_output, ""
        )
        dirty = subprocess.CompletedProcess(
            ["git", "status"], 0, clean_output + "1 .M N... 100644 100644 "
            + f"100644 {commit} {commit} build.py\n", ""
        )
        failed = subprocess.CompletedProcess(
            ["git", "status"], 128, "", "not a repository"
        )
        with mock.patch.object(build.subprocess, "run", return_value=clean):
            self.assertEqual((commit, clean_output), build.git_source_state())
        with mock.patch.object(build.subprocess, "run", return_value=dirty):
            self.assertNotEqual((commit, clean_output), build.git_source_state())
        with mock.patch.object(build.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "not a repository"):
                build.git_source_state()

        source = Path("app/apk/build/outputs/apk/debug/apk-debug.apk")
        target = Path("out/app-debug.apk")
        with (
            mock.patch.object(
                build, "git_source_state", return_value=(commit, "changed")
            ),
            mock.patch.object(build, "rm") as remove,
            mock.patch.object(build, "error", side_effect=SystemExit(1)),
        ):
            with self.assertRaises(SystemExit):
                build.verify_source_state((commit, clean_output), source, target)
        self.assertEqual([mock.call(source), mock.call(target)], remove.call_args_list)

    def test_repository_build_caches_do_not_dirty_source_identity(self):
        patterns = Path(".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("/.gradle/", patterns)
        self.assertIn("/.sccache/", patterns)
        self.assertIn("__pycache__/", patterns)
        self.assertIn("*.py[cod]", patterns)

    def test_artifact_version_is_bound_to_current_head(self):
        commit = "12345678" + "a" * 32
        with mock.patch.object(baseline, "run", return_value=f"{commit}\n"):
            self.assertEqual(
                "30.7-kitsune-next.12345678",
                baseline.expected_source_version(Path(".")),
            )
        with mock.patch.object(baseline, "run", return_value="not-a-commit\n"):
            with self.assertRaisesRegex(BaselineError, "current source commit"):
                baseline.expected_source_version(Path("."))

    def test_zygisk_fixture_contract_is_exact_and_exclusive(self):
        resident = b"resident\0"
        unload = b"unload\0"
        verify_fixture_contract(b"prefix" + resident + b"suffix", resident, (resident, unload))
        for payload in (
            b"missing",
            resident + resident,
            resident + unload,
            unload,
        ):
            with self.subTest(payload=payload), self.assertRaises(BaselineError):
                verify_fixture_contract(payload, resident, (resident, unload))

    def test_instrumentation_variants_use_distinct_output_paths(self):
        for release, variant in ((False, "debug"), (True, "release")):
            with self.subTest(variant=variant):
                fake_args = SimpleNamespace(release=release)
                target = Path("out") / f"test-{variant}.apk"
                with (
                    mock.patch.object(build, "args", fake_args, create=True),
                    mock.patch.object(build, "config", {"outdir": Path("out")}),
                    mock.patch.object(build, "header"),
                    mock.patch.object(
                        build, "remove_test_native_cache"
                    ) as remove_tree,
                    mock.patch.object(
                        build, "build_apk", return_value=target
                    ) as build_apk,
                    mock.patch.object(build, "cp") as copy_apk,
                ):
                    build.build_test()

                build_apk.assert_called_once_with(":test", f"test-{variant}.apk")
                self.assertEqual(
                    [
                        mock.call(Path("app/test/.cxx")),
                        mock.call(Path("app/test/build/intermediates/cxx")),
                    ],
                    remove_tree.call_args_list,
                )
                copy_apk.assert_called_once_with(target, Path("out/test.apk"))
                self.assertEqual(release, fake_args.release)

    def test_test_native_cache_cleanup_is_absent_safe_and_link_safe(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            build,
            "args",
            SimpleNamespace(verbose=0),
            create=True,
        ):
            root = Path(directory)
            missing = root / "missing"
            build.remove_test_native_cache(missing)

            generated = root / "generated"
            generated.mkdir()
            (generated / "object.o").write_bytes(b"generated")
            build.remove_test_native_cache(generated)
            self.assertFalse(generated.exists())

            target = root / "target"
            target.mkdir()
            redirected = root / "redirected"
            redirected.symlink_to(target, target_is_directory=True)
            with mock.patch.object(
                build,
                "error",
                side_effect=SystemExit(1),
            ), self.assertRaises(SystemExit):
                build.remove_test_native_cache(redirected)
            self.assertTrue(target.is_dir())

    def test_manager_build_targets_the_final_output_path(self):
        for release, variant in ((False, "debug"), (True, "release")):
            with self.subTest(variant=variant):
                fake_args = SimpleNamespace(release=release)
                target = Path("out") / f"app-{variant}.apk"
                with (
                    mock.patch.object(build, "args", fake_args, create=True),
                    mock.patch.object(build, "config", {"outdir": Path("out")}),
                    mock.patch.object(build, "header"),
                    mock.patch.object(
                        build, "build_apk", return_value=target
                    ) as build_apk,
                    mock.patch.object(build, "export_stub") as export_stub,
                    mock.patch.object(build, "rm") as remove_output,
                ):
                    build.build_app()
                build_apk.assert_called_once_with(":apk", f"app-{variant}.apk")
                remove_output.assert_called_once_with(
                    Path("out") / f"stub-{variant}.apk"
                )
                export_stub.assert_called_once_with(
                    target, Path("out") / f"stub-{variant}.apk"
                )

    def test_manager_exports_the_exact_embedded_stub_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = root / "manager.apk"
            target = root / "stub.apk"
            with zipfile.ZipFile(manager, "w") as archive:
                archive.writestr("assets/stub.apk", b"exact-stub")
            with mock.patch.object(build, "vprint"):
                build.export_stub(manager, target)
            self.assertEqual(b"exact-stub", target.read_bytes())

            missing = root / "missing.apk"
            with zipfile.ZipFile(missing, "w") as archive:
                archive.writestr("assets/not-stub.apk", b"wrong")
            with (
                mock.patch.object(build, "vprint"),
                mock.patch.object(build, "error", side_effect=SystemExit(1)),
            ):
                with self.assertRaises(SystemExit):
                    build.export_stub(missing, target)
            self.assertFalse(target.exists())

    def test_avd_offline_mode_uses_the_resolved_ramdisk_path(self):
        source = Path("scripts/avd.sh").read_text(encoding="utf-8")
        setup = source[source.index("setup_emu()") : source.index("launch_emulator()")]
        self.assertIn("local installed_ramdisk=$3", setup)
        self.assertIn('setup_emu "$avd_pkg" "$ver" "$ramdisk"', source)
        self.assertNotIn("${avd_pkg//;", source)

    def test_avd_boot_wait_bounds_shell_transport_and_recovers_it(self):
        source = Path("scripts/avd.sh").read_text(encoding="utf-8")
        waiter = source[
            source.index("start_boot_waiter()") : source.index("validate_emu_port()")
        ]
        self.assertIn("subprocess.TimeoutExpired", waiter)
        self.assertIn('run_adb(["reconnect"], False)', waiter)
        self.assertIn('"ANDROID_SERIAL"', waiter)
        self.assertIn("os.killpg(process_group, signal.SIGKILL)", waiter)
        self.assertIn("start_new_session=True", waiter)
        self.assertIn("pending_signal", waiter)
        self.assertIn("wait_process_group_gone", waiter)
        self.assertIn('["shell", "pm", "path", "android"]', waiter)
        self.assertNotIn("adb wait-for-device", waiter)

    def test_avd_waiter_connects_a_forgotten_owned_tcp_transport(self):
        source = Path("scripts/avd.sh").read_text(encoding="utf-8")
        waiter = source.split("python3 - <<'PY' &\n", 1)[1].split("\nPY\n", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_adb = root / "adb"
            fake_adb.write_text("""#!/usr/bin/env python3
import os
from pathlib import Path
import sys
marker = Path(os.environ['CONNECTED_MARKER'])
args = sys.argv[3:]
if args == ['connect', '127.0.0.1:5557']:
    marker.touch()
    print('connected')
elif not marker.exists():
    raise SystemExit(1)
elif args == ['exec-out', 'getprop', 'sys.boot_completed']:
    print('1')
elif args == ['shell', 'pm', 'path', 'android']:
    print('package:/system/framework/framework-res.apk')
else:
    raise SystemExit(1)
""")
            fake_adb.chmod(0o700)
            env = dict(os.environ, ANDROID_SERIAL="127.0.0.1:5557",
                       CONNECTED_MARKER=str(root / "connected"),
                       PATH=str(root) + os.pathsep + os.environ["PATH"])
            result = subprocess.run([sys.executable, "-c", waiter], env=env,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertTrue((root / "connected").exists())

    def test_avd_boot_waiter_reaps_a_hung_adb_process_group(self):
        source = Path("scripts/avd.sh").read_text(encoding="utf-8")
        waiter = source[
            source.index("start_boot_waiter()") : source.index("validate_emu_port()")
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adb_pid_file = root / "adb.pid"
            adb_child_pid_file = root / "adb-child.pid"
            waiter_pid_file = root / "waiter.pid"
            fake_adb = root / "adb"
            fake_adb.write_text(
                """#!/usr/bin/env python3
import os
from pathlib import Path
import signal
import time

def publish_pid(path):
    destination = Path(path)
    temporary = destination.with_name(destination.name + ".new")
    temporary.write_text(str(os.getpid()), encoding="ascii")
    os.replace(temporary, destination)

def stop(_signum, _frame):
    raise SystemExit(0)

for handled in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(handled, stop)
child = os.fork()
if child == 0:
    publish_pid(os.environ["FAKE_ADB_CHILD_PID"])
else:
    publish_pid(os.environ["FAKE_ADB_PID"])
while True:
    time.sleep(1)
""",
                encoding="utf-8",
            )
            fake_adb.chmod(0o700)
            runner = root / "runner.sh"
            runner.write_text(
                "#!/bin/bash\nset -eu\n"
                + waiter
                + '\nstart_boot_waiter\nprintf "%s\\n" "$wait_pid" '
                + '>"$WAITER_PID_FILE"\n'
                + 'wait "$wait_pid"\n',
                encoding="utf-8",
            )
            runner.chmod(0o700)
            env = os.environ.copy()
            env.update(
                {
                    "ANDROID_SERIAL": "emulator-5556",
                    "FAKE_ADB_CHILD_PID": str(adb_child_pid_file),
                    "FAKE_ADB_PID": str(adb_pid_file),
                    "PATH": f"{root}{os.pathsep}{env['PATH']}",
                    "WAITER_PID_FILE": str(waiter_pid_file),
                }
            )
            process = None
            waiter_pid = None
            adb_pid = None
            adb_child_pid = None

            def read_pid(path):
                try:
                    return int(path.read_text(encoding="ascii"))
                except (OSError, ValueError):
                    return None

            try:
                process = subprocess.Popen(
                    [str(runner)],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if waiter_pid is None:
                        waiter_pid = read_pid(waiter_pid_file)
                    if adb_pid is None:
                        adb_pid = read_pid(adb_pid_file)
                    if adb_child_pid is None:
                        adb_child_pid = read_pid(adb_child_pid_file)
                    if None not in (waiter_pid, adb_pid, adb_child_pid):
                        break
                    if process.poll() is not None:
                        break
                    time.sleep(0.01)
                self.assertIsNotNone(waiter_pid, "boot waiter did not start")
                self.assertIsNotNone(adb_pid, "fake adb did not start")
                self.assertIsNotNone(adb_child_pid, "fake adb child did not start")
                assert waiter_pid is not None
                assert adb_pid is not None
                assert adb_child_pid is not None
                os.kill(waiter_pid, signal.SIGTERM)
                process.communicate(timeout=5)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    try:
                        os.kill(adb_pid, 0)
                        os.kill(adb_child_pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.01)
                with self.assertRaises(ProcessLookupError):
                    os.kill(adb_pid, 0)
                with self.assertRaises(ProcessLookupError):
                    os.kill(adb_child_pid, 0)
            finally:
                if waiter_pid is None:
                    waiter_pid = read_pid(waiter_pid_file)
                if adb_pid is None:
                    adb_pid = read_pid(adb_pid_file)
                if adb_child_pid is None:
                    adb_child_pid = read_pid(adb_child_pid_file)
                if adb_pid is not None:
                    try:
                        os.killpg(adb_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                for pid in (waiter_pid, adb_pid, adb_child_pid):
                    if pid is None:
                        continue
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process is not None and process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.communicate(timeout=2)

    def test_native_crash_gate_rejects_crashes_and_unreadable_logs(self):
        source = Path("scripts/test_common.sh").read_text()
        script = source[source.index("assert_no_native_crashes()") : source.index("run_root_stress_batch()")]
        with tempfile.TemporaryDirectory(prefix="kitsune-crash-gate-") as temporary:
            root = Path(temporary)
            adb = root / "adb"
            adb.write_text("#!/usr/bin/env python3\nimport os, sys\n"
                           "assert sys.argv[1:] == ['-s', 'pinned-test', 'logcat', '-d', '-b', 'crash']\n"
                           "print(os.environ['TEST_CRASH_LOG'])\n"
                           "raise SystemExit(int(os.environ['TEST_ADB_STATUS']))\n")
            adb.chmod(0o700)
            for log, adb_status, expected in (
                ("", 0, 0), ("FATAL EXCEPTION: main", 0, 0),
                ("Fatal signal 11 (SIGSEGV), code 1", 0, 1),
                ("Fatal signal 6 (SIGABRT), code -1", 0, 1),
                ("Fatal signal 31 (SIGSYS), code 1", 0, 1),
                ("logcat unavailable", 7, 7),
            ):
                with self.subTest(log=log):
                    env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}",
                               ANDROID_SERIAL="pinned-test", TEST_CRASH_LOG=log,
                               TEST_ADB_STATUS=str(adb_status))
                    result = subprocess.run(["bash", "-c", script + "\nassert_no_native_crashes"],
                                            env=env, capture_output=True, text=True, timeout=20)
                    self.assertEqual(expected, result.returncode, result.stdout + result.stderr)

    def test_avd_root_stress_is_timeout_bounded_and_checks_orphans(self):
        source = Path("scripts/test_common.sh").read_text(encoding="utf-8")
        self.assertIn("subprocess.TimeoutExpired", source)
        self.assertIn("AVD_INSTRUMENT_TIMEOUT", source)
        self.assertIn("AVD_STRESS_REQUEST_TIMEOUT", source)
        self.assertIn("assert_no_stale_su", source)
        self.assertIn("Stale MagiskSU process remained", source)
        self.assertNotIn(
            'run_root_stress_batch "$parallel" "$request_timeout" | tr', source
        )
        self.assertIn('raw=$(adb shell', source)
        self.assertIn('"$process/stat"', source)
        self.assertIn('unable-to-read-ppid', source)
        self.assertIn('init_daemon_count=$((init_daemon_count + 1))', source)
        self.assertGreaterEqual(source.count("__kitsune_avd_su_stress__"), 2)
        scanner = source[
            source.index("assert_no_stale_su()") : source.index("run_root_stress()")
        ]
        self.assertNotIn("awk", scanner)

    def test_instrumentation_timeout_preserves_adb_failure_status(self):
        source = Path("scripts/test_common.sh").read_text(encoding="utf-8")
        runner = source[
            source.index("run_instrumentation()") : source.index("wait_for_pm()")
        ]
        self.assertIn("subprocess.TimeoutExpired", runner)
        self.assertIn('"adb", "-s", serial', runner)
        self.assertIn('raw=$(run_instrumentation "$1" "$2")', runner)
        self.assertNotIn("am instrument -w", runner)

        cuttlefish = Path("scripts/cuttlefish.sh").read_text(encoding="utf-8")
        self.assertIn("preexisting_serials=$(list_adb_serials)", cuttlefish)
        self.assertIn('pin_new_adb_serial "$preexisting_serials"', cuttlefish)
        self.assertIn('fields[0] not in baseline', cuttlefish)
        self.assertIn('getprop", "ro.hardware"', cuttlefish)
        self.assertIn('export ANDROID_SERIAL="$serial"', cuttlefish)
        self.assertIn('adb -s "$serial" get-state', cuttlefish)
        self.assertNotIn("ANDROID_SERIAL= adb wait-for-device", cuttlefish)

    def test_cuttlefish_waits_for_a_new_verified_transport(self):
        source = Path("scripts/cuttlefish.sh").read_text(encoding="utf-8")
        functions = source[
            source.index("list_adb_serials()") : source.index("setup_env()")
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_adb = root / "adb"
            count_file = root / "count"
            log_file = root / "commands.log"
            fake_adb.write_text(
                """#!/usr/bin/env python3
import os
from pathlib import Path
import sys

arguments = sys.argv[1:]
log = Path(os.environ["FAKE_ADB_LOG"])
with log.open("a", encoding="utf-8") as stream:
    stream.write(" ".join(arguments) + "\\n")
if arguments[:2] == ["-s", "127.0.0.1:16384"]:
    raise SystemExit(9)
if arguments == ["devices"]:
    if os.environ.get("FAKE_ADB_HANG_BASELINE") == "1" and not Path(
        os.environ["FAKE_ADB_COUNT"]
    ).exists():
        import time
        time.sleep(60)
    counter = Path(os.environ["FAKE_ADB_COUNT"])
    count = int(counter.read_text(encoding="ascii")) + 1 if counter.exists() else 1
    counter.write_text(str(count), encoding="ascii")
    print("List of devices attached")
    print("127.0.0.1:16384\tdevice")
    if count >= 3 and os.environ.get("FAKE_ADB_NEVER_NEW") != "1":
        print("cvd-01\tdevice")
    else:
        print("cvd-01\toffline")
    raise SystemExit(0)
if arguments == ["-s", "cvd-01", "shell", "getprop", "ro.hardware"]:
    print("cutf_cvm")
    raise SystemExit(7 if os.environ.get("FAKE_ADB_PROPERTY_NONZERO") == "1" else 0)
if arguments == ["-s", "cvd-01", "shell", "getprop", "ro.product.name"]:
    print("aosp_cf_x86_64_only_phone")
    raise SystemExit(0)
if arguments == ["-s", "cvd-01", "get-state"]:
    print("device")
    raise SystemExit(8 if os.environ.get("FAKE_ADB_STATE_NONZERO") == "1" else 0)
raise SystemExit(2)
""",
                encoding="utf-8",
            )
            fake_adb.chmod(0o700)
            runner = root / "runner.sh"
            runner.write_text(
                "#!/bin/bash\nset -eu\nboot_timeout=${BOOT_TIMEOUT:-5}\n"
                + functions
                + '\nbaseline=$(list_adb_serials)\n'
                + 'pin_new_adb_serial "$baseline"\n'
                + 'printf "%s\\n" "$ANDROID_SERIAL"\n',
                encoding="utf-8",
            )
            runner.chmod(0o700)
            env = os.environ.copy()
            env.update(
                {
                    "FAKE_ADB_COUNT": str(count_file),
                    "FAKE_ADB_LOG": str(log_file),
                    "PATH": f"{root}{os.pathsep}{env['PATH']}",
                }
            )
            result = subprocess.run(
                [str(runner)],
                check=False,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )
            self.assertEqual(
                0,
                result.returncode,
                result.stdout
                + "\n"
                + result.stderr
                + "\n"
                + (log_file.read_text(encoding="utf-8") if log_file.exists() else "")
                + "\n"
                + runner.read_text(encoding="utf-8"),
            )
            self.assertEqual("cvd-01", result.stdout.strip())
            commands = log_file.read_text(encoding="utf-8")
            self.assertNotIn("-s 127.0.0.1:16384", commands)
            self.assertIn("-s cvd-01 shell getprop ro.hardware", commands)

            for extra_environment in (
                {"BOOT_TIMEOUT": "1", "FAKE_ADB_NEVER_NEW": "1"},
                {"BOOT_TIMEOUT": "3", "FAKE_ADB_PROPERTY_NONZERO": "1"},
                {"BOOT_TIMEOUT": "3", "FAKE_ADB_STATE_NONZERO": "1"},
                {
                    "BOOT_TIMEOUT": "1",
                    "CVD_ADB_COMMAND_TIMEOUT": "0.2",
                    "FAKE_ADB_HANG_BASELINE": "1",
                },
            ):
                if count_file.exists():
                    count_file.unlink()
                if log_file.exists():
                    log_file.unlink()
                failure_environment = env | extra_environment
                started = time.monotonic()
                result = subprocess.run(
                    [str(runner)],
                    check=False,
                    env=failure_environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=6,
                )
                self.assertNotEqual(0, result.returncode, extra_environment)
                self.assertLess(time.monotonic() - started, 6)
                commands = (
                    log_file.read_text(encoding="utf-8")
                    if log_file.exists()
                    else ""
                )
                self.assertNotIn("-s 127.0.0.1:16384", commands)

    def test_artifact_verifier_requires_standalone_stub_parity(self):
        source = Path("tools/next_system_baseline.py").read_text(encoding="utf-8")
        self.assertIn("def inspect_stub_apk(", source)
        self.assertIn('archive.read("assets/stub.apk")', source)
        self.assertIn('outdir / "stub-debug.apk"', source)
        self.assertIn('outdir / "stub-release.apk"', source)
        self.assertIn('"stub_payloads_and_signers_match": True', source)

    def test_gradle_identity_uses_exact_git_dirty_status(self):
        plugin = Path("app/build-logic/src/main/java/Plugin.kt").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            '"git", "status", "--porcelain=v1", "--untracked-files=normal"',
            plugin,
        )
        self.assertIn("check(process.waitFor() == 0)", plugin)
        self.assertIn('findProperty("expectedSourceCommit")', plugin)
        self.assertNotIn('findProperty("sourceDirty")', plugin)

    def test_build_cargo_propagates_offline_failure_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "native" / "src").mkdir(parents=True)
            failure = subprocess.CompletedProcess(
                ["cargo", "metadata", "--offline"], 37
            )
            original_cwd = Path.cwd()
            try:
                os.chdir(root)
                with (
                    mock.patch.object(
                        build,
                        "args",
                        SimpleNamespace(
                            commands=["--", "metadata", "--offline"], verbose=0
                        ),
                        create=True,
                    ),
                    mock.patch.object(build, "ensure_cargo"),
                    mock.patch.object(
                        build, "llvm_tool", return_value=Path("/ondk/bin/clang")
                    ),
                    mock.patch.object(
                        build, "rust_sysroot", Path("/ondk/rust"), create=True
                    ),
                    mock.patch.object(build.subprocess, "run", return_value=failure) as execute,
                ):
                    with self.assertRaises(SystemExit) as raised:
                        build.cargo_cli()
            finally:
                os.chdir(original_cwd)

            self.assertEqual(raised.exception.code, 37)
            self.assertEqual(execute.call_args.args[0], ["cargo", "metadata", "--offline"])

    def test_manifest_extension_deep_merges_maps_and_replaces_lists(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "base.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "identity": {"name": "base", "code": 7},
                        "paths": ["base"],
                    }
                ),
                encoding="utf-8",
            )
            (root / "child.json").write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "extends": "base.json",
                        "identity": {"name": "child"},
                        "paths": ["child"],
                    }
                ),
                encoding="utf-8",
            )

            manifest = read_manifest(root / "child.json")

            self.assertEqual(manifest["identity"], {"name": "child", "code": 7})
            self.assertEqual(manifest["paths"], ["child"])

    def test_manifest_extension_cycle_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.json").write_text(
                json.dumps({"schema": 1, "extends": "b.json"}), encoding="utf-8"
            )
            (root / "b.json").write_text(
                json.dumps({"schema": 1, "extends": "a.json"}), encoding="utf-8"
            )

            with self.assertRaisesRegex(BaselineError, "inheritance cycle"):
                read_manifest(root / "a.json")

    def test_dependency_contract_requires_and_forbids_exact_versions(self):
        contract = {
            "dependency_contract": {
                "required": [{"name": "safe", "version": "2.0.0"}],
                "forbidden": [{"name": "unsafe", "version": "1.0.0"}],
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "native" / "src" / "Cargo.lock"
            lock.parent.mkdir(parents=True)
            lock.write_text(
                'version = 4\n\n[[package]]\nname = "safe"\nversion = "2.0.0"\n',
                encoding="utf-8",
            )

            result = verify_dependency_contract(root, contract)

            self.assertTrue(result["enforced"])
            self.assertEqual(cargo_lock_packages(lock), {("safe", "2.0.0")})
            lock.write_text(
                lock.read_text(encoding="utf-8")
                + '\n[[package]]\nname = "unsafe"\nversion = "1.0.0"\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(BaselineError, "present_forbidden"):
                verify_dependency_contract(root, contract)


if __name__ == "__main__":
    unittest.main()
