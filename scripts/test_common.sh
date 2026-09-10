if [ -z "${ANDROID_HOME:-}" ]; then
  export ANDROID_HOME="${ANDROID_SDK_ROOT:-}"
fi

# Make sure paths are consistent
export ANDROID_USER_HOME="${ANDROID_USER_HOME:-$HOME/.android}"
export ANDROID_EMULATOR_HOME="${ANDROID_EMULATOR_HOME:-$ANDROID_USER_HOME}"
export ANDROID_AVD_HOME="${ANDROID_AVD_HOME:-$ANDROID_EMULATOR_HOME/avd}"
export PATH="$PATH:$ANDROID_HOME/platform-tools"

MAGISK_OUT_DIR="${MAGISK_OUT_DIR:-out}"
MAGISK_APP_PACKAGE="${MAGISK_APP_PACKAGE:-io.github.huskydg.magisk.next}"
MAGISK_TEST_PACKAGE="${MAGISK_TEST_PACKAGE:-$MAGISK_APP_PACKAGE.test}"

emu="$ANDROID_HOME/emulator/emulator"
sdk="$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager"
avd="$ANDROID_HOME/cmdline-tools/latest/bin/avdmanager"

boot_timeout="${AVD_BOOT_TIMEOUT:-180}"
instrument_timeout="${AVD_INSTRUMENT_TIMEOUT:-120}"

if command -v nproc >/dev/null 2>&1; then
  core_count=$(nproc)
elif command -v sysctl >/dev/null 2>&1; then
  core_count=$(sysctl -n hw.logicalcpu 2>/dev/null || echo 1)
else
  core_count=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)
fi
if [ "$core_count" -gt 8 ]; then
  core_count=8
fi

print_title() {
  echo -e "\n\033[44;39m${1}\033[0m\n"
}

print_error() {
  echo -e "\n\033[41;39m${1}\033[0m\n" >&2
}

# $1 = TestClass#method
# $2 = component
run_instrumentation() {
  python3 - "$1" "$2" "$instrument_timeout" <<'PY'
import os
import subprocess
import sys

test_class, component, raw_timeout = sys.argv[1:]
try:
    timeout = int(raw_timeout)
except ValueError:
    raise SystemExit(2)
if timeout < 1:
    raise SystemExit(2)
serial = os.environ.get("ANDROID_SERIAL")
if not serial:
    raise SystemExit(2)
command = [
    "adb", "-s", serial, "shell", "am", "instrument", "-w", "--user", "0",
    "-e", "class", test_class, component,
]


def stop_process(process):
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    try:
        output, _ = process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        output = ""
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            output, _ = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            return False, output
    return True, output


try:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
except OSError:
    raise SystemExit(2)
try:
    output, _ = process.communicate(timeout=timeout)
except subprocess.TimeoutExpired:
    stopped, output = stop_process(process)
    sys.stdout.write(output)
    raise SystemExit(124 if stopped else 125)
except KeyboardInterrupt:
    stopped, _ = stop_process(process)
    raise SystemExit(130 if stopped else 125)
sys.stdout.write(output)
raise SystemExit(process.returncode)
PY
}

am_instrument() {
  set +x
  local raw out status
  raw=$(run_instrumentation "$1" "$2") || {
    status=$?
    echo "$raw"
    set -x
    return "$status"
  }
  out=$(printf '%s' "$raw" | tr -d '\r')
  echo "$out"
  if grep -q 'OK (' <<< "$out"; then
    set -x
    return 0
  else
    set -x
    return 1
  fi
}

# $1 = pkg
wait_for_pm() {
  sleep 5
  adb shell pm uninstall $1 || true
}

assert_device_contract() {
  local actual
  if [ -n "${AVD_EXPECT_API:-}" ]; then
    actual=$(adb shell getprop ro.build.version.sdk | tr -d '\r')
    if [ "$actual" != "$AVD_EXPECT_API" ]; then
      print_error "Unexpected Android API: expected $AVD_EXPECT_API, found $actual"
      return 1
    fi
  fi
  if [ -n "${AVD_EXPECT_PAGE_SIZE:-}" ]; then
    actual=$(adb shell getconf PAGESIZE | tr -d '\r')
    if [ "$actual" != "$AVD_EXPECT_PAGE_SIZE" ]; then
      print_error "Unexpected guest page size: expected $AVD_EXPECT_PAGE_SIZE, found $actual"
      return 1
    fi
  fi
}

run_setup() {
  local variant=$1
  assert_device_contract
  # App migration needs a visible, awake device, including on Cuttlefish.
  adb shell settings put system screen_off_timeout 2147483647
  adb shell svc power stayon true
  adb shell input keyevent KEYCODE_WAKEUP
  adb shell 'PATH=$PATH:/debug_ramdisk magisk -v' | tr -d '\r'

  # Install the Magisk app
  adb install --no-streaming -r -g "$MAGISK_OUT_DIR/app-${variant}.apk"

  # Install the test app
  adb install --no-streaming -r -g "$MAGISK_OUT_DIR/test-${variant}.apk"

  local app="$MAGISK_TEST_PACKAGE/com.topjohnwu.magisk.test.AppTestRunner"

  # Run setup through the test app
  am_instrument '.Environment#setupEnvironment' $app
}

run_tests() {
  adb logcat -d -b all -v threadtime > "$MAGISK_OUT_DIR/native-boot.log"
  # The tested boot can return to the keyguard even when setup woke the device.
  adb shell input keyevent KEYCODE_WAKEUP
  case $(adb shell getprop ro.build.version.sdk | tr -d '\r') in
    23|24|25) adb shell input keyevent KEYCODE_MENU ;;
    *) adb shell wm dismiss-keyguard ;;
  esac

  local pkg="$MAGISK_TEST_PACKAGE"
  local self="$pkg/com.topjohnwu.magisk.test.TestRunner"
  local app="$pkg/com.topjohnwu.magisk.test.AppTestRunner"
  local stub="repackaged.$pkg/com.topjohnwu.magisk.test.AppTestRunner"

  # Run app tests
  am_instrument '.MagiskAppTest,.AdditionalTest,.TerminalTest,.FlashIntentTest' $app

  # Test app hiding
  am_instrument '.AppMigrationTest#testAppHide' $self

  # Make sure it still works
  am_instrument '.MagiskAppTest,.FlashIntentTest' $stub

  # Test app restore
  am_instrument '.AppMigrationTest#testAppRestore' $self

  # Make sure it still works
  am_instrument '.MagiskAppTest' $app

  run_root_stress
  assert_no_native_crashes
}

assert_no_native_crashes() {
  python3 - "$ANDROID_SERIAL" "${MAGISK_OUT_DIR:-out}/native-boot.log" <<'PYCODE'
from pathlib import Path
import re
import subprocess
import sys


def adb(*args):
    try:
        result = subprocess.run(
            ["adb", "-s", sys.argv[1], *args],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"Cannot inspect native crash state: {error}", file=sys.stderr)
        raise SystemExit(2)
    if result.returncode:
        print(result.stderr or result.stdout, file=sys.stderr)
        raise SystemExit(result.returncode)
    return result.stdout


# Narrow obsolete SDK-image failure signatures with stock control evidence.
# The evidence document distinguishes reproduction from inference. Never exempt
# app/zygote/root-service crashes, later crashes, or arbitrary device images.
# See docs/system-mode/sdk-native-boot-controls.md.
def stock_boot_failure(first, record, boot):
    if first not in boot:
        return None
    fingerprint = re.search(r"Build fingerprint: '([^']+)'", record)
    if not fingerprint:
        return None
    fingerprint = fingerprint[1]
    media_images = {
        "Android/sdk_phone_x86_64/generic_x86_64:7.0/NYC/4174735:userdebug/test-keys",
        "Android/sdk_phone_x86_64/generic_x86_64:7.1.1/NYC/4931657:userdebug/test-keys",
    }
    graphics_images = {
        "Android/sdk_phone_x86/generic_x86:9/PSR1.180720.012/4923214:userdebug/test-keys",
        "Android/sdk_phone_x86_64/generic_x86_64:9/PSR1.180720.012/4923214:userdebug/test-keys",
    }
    if (fingerprint in graphics_images and "(surfaceflinger)" in first
            and "/system/bin/surfaceflinger" in record
            and "Failed HIDL return status not checked" in record
            and "DEAD_OBJECT" in record and "Composer::getActiveConfig" in record):
        return "SurfaceFlinger"
    if (fingerprint in media_images and "(mediaextractor)" in first
            and "/system/lib/libminijail.so (log_sigsys_handler+" in record):
        pid = re.match(r"\S+\s+\S+\s+(\d+)\s+\d+\s+F\s+libc", first)
        if pid and re.search(r"^\S+\s+\S+\s+" + pid[1]
                + r"\s+\d+\s+E\s+media\.extractor\s*:\s*"
                  r"libminijail: blocked syscall: nanosleep$", boot, re.MULTILINE):
            return "media.extractor"
    return None


crash = adb("logcat", "-d", "-b", "crash", "-v", "threadtime")
try:
    boot = Path(sys.argv[2]).read_text()
except OSError:
    boot = ""
matches = list(re.finditer(r"^.*Fatal signal \d+ \(SIG(\w+)\).*$", crash, re.MULTILINE))
recovered = set()
for index, match in enumerate(matches):
    if match[1] not in {"SEGV", "ABRT", "BUS", "ILL", "FPE", "TRAP", "SYS", "STKFLT"}:
        continue
    end = matches[index + 1].start() if index + 1 < len(matches) else len(crash)
    record = crash[match.start():end]
    service = stock_boot_failure(match[0], record, boot)
    if not service:
        print(record)
        raise SystemExit("Unexpected native crash during emulator tests")
    recovered.add(service)
for service in sorted(recovered):
    if ": found" not in adb("shell", "service", "check", service):
        raise SystemExit(f"Stock SDK service did not recover: {service}")
    print(f"Known stock SDK boot failure recovered before acceptance: {service}")
PYCODE
}

run_root_stress_batch() {
  local parallel=$1 timeout_seconds=$2
  python3 - "$parallel" "$timeout_seconds" <<'PY'
import subprocess
import sys

parallel = int(sys.argv[1])
timeout = int(sys.argv[2])
command = (
    f"i=0; while [ $i -lt {parallel} ]; do "
    "(su -c 'id; : __kitsune_avd_su_stress__') & "
    "i=$((i + 1)); done; wait"
)
try:
    result = subprocess.run(
        ["adb", "shell", command],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )
except subprocess.TimeoutExpired as exc:
    if exc.stdout:
        output = exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout
        sys.stdout.write(output)
    raise SystemExit(124)
sys.stdout.write(result.stdout)
raise SystemExit(result.returncode)
PY
}

assert_no_stale_su() {
  local raw stale
  raw=$(adb shell '
    init_daemon_count=0
    for process in /proc/[0-9]*; do
      [ -d "$process" ] || continue
      name=$(cat "$process/comm" 2>/dev/null) || continue
      command=$(tr "\\000" " " <"$process/cmdline" 2>/dev/null)
      is_stress_su=false
      case "$command" in
        *__kitsune_avd_su_stress__*) is_stress_su=true ;;
      esac
      if [ "$name" = su ] && [ "$is_stress_su" = true ]; then
        echo "${process##*/}:?:$name:$(cat "$process/wchan" 2>/dev/null):$command"
      elif [ "$name" = magiskd ]; then
        stat_line=
        ppid=
        # Parse after the final ") " so even unusual process names cannot shift
        # the fixed state/PPID fields. read is a shell builtin on Android 6.
        IFS= read -r stat_line 2>/dev/null <"$process/stat" || true
        stat_rest=${stat_line##*) }
        if [ "$stat_rest" != "$stat_line" ]; then
          stat_rest=${stat_rest#* }
          ppid=${stat_rest%% *}
        fi
        case "$ppid" in
          ""|*[!0-9]*) ppid= ;;
        esac
        current_name=$(cat "$process/comm" 2>/dev/null) || current_name=
        if [ -z "$current_name" ] && [ ! -d "$process" ]; then
          continue
        fi
        if [ "$current_name" != magiskd ]; then
          continue
        fi
        if [ -z "$ppid" ]; then
          # A process that disappeared during the scan is not stale. A live
          # process whose parent cannot be inspected makes the check unknown.
          [ -d "$process" ] || continue
          echo "${process##*/}:?:$name:unable-to-read-ppid:$command"
          exit 75
        fi
        if [ "$ppid" = 1 ]; then
          init_daemon_count=$((init_daemon_count + 1))
        fi
        if [ "$ppid" != 1 ] || [ "$init_daemon_count" -gt 1 ]; then
          echo "${process##*/}:$ppid:$name:$(cat "$process/wchan" 2>/dev/null):$command"
        fi
      fi
    done
    if [ "$init_daemon_count" -ne 1 ]; then
      echo "?:1:magiskd:expected-one-init-daemon:found-$init_daemon_count"
      exit 75
    fi
  ') || {
    echo "$raw"
    print_error "Unable to inspect MagiskSU processes after concurrency stress"
    return 1
  }
  stale=$(printf '%s' "$raw" | tr -d '\r')
  if [ -n "$stale" ]; then
    echo "$stale"
    print_error "Stale MagiskSU process remained after concurrency stress"
    return 1
  fi
}

run_root_stress() {
  local iterations="${AVD_STRESS_ITERATIONS:-0}"
  local parallel="${AVD_STRESS_PARALLEL:-6}"
  local request_timeout="${AVD_STRESS_REQUEST_TIMEOUT:-30}"
  case "$iterations:$parallel:$request_timeout" in
    *[!0-9:]*|::*|:*:|*:0:*|*:*:0)
      print_error "Invalid AVD stress configuration: $iterations iterations, $parallel parallel, ${request_timeout}s timeout"
      return 1
      ;;
  esac
  if [ "$iterations" -eq 0 ]; then
    return
  fi

  print_title "* Stressing MagiskSU ($iterations x $parallel concurrent requests)"
  local iteration raw out root_count version
  iteration=0
  while [ "$iteration" -lt "$iterations" ]; do
    raw=$(run_root_stress_batch "$parallel" "$request_timeout") || {
      echo "$raw"
      assert_no_stale_su || true
      print_error "Concurrent MagiskSU stress timed out or failed at iteration $iteration"
      return 1
    }
    out=$(printf '%s' "$raw" | tr -d '\r')
    # PTYs on some real-device shells can interleave multiple results on one line.
    root_count=$(printf '%s' "$out" | awk '{ total += gsub(/uid=0/, "") } END { print total + 0 }')
    if [ "$root_count" -ne "$parallel" ]; then
      echo "$out"
      print_error "Concurrent MagiskSU stress failed at iteration $iteration"
      return 1
    fi
    version=$(adb shell 'PATH=$PATH:/debug_ramdisk magisk -V' | tr -d '\r' | tail -n 1)
    if [ "$version" != "30700" ]; then
      print_error "Magisk daemon version changed during stress: $version"
      return 1
    fi
    iteration=$((iteration + 1))
  done
  assert_no_stale_su
}
