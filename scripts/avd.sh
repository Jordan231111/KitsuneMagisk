#!/usr/bin/env bash

set -e
. scripts/test_common.sh

gpu_mode="${AVD_GPU_MODE:-swiftshader_indirect}"
case "$gpu_mode" in
  auto|host|software|swiftshader|swangle|swiftshader_indirect) ;;
  *) print_error "! Unsupported AVD graphics mode"; exit 1 ;;
esac
emu_args_base="-no-window -no-audio -no-boot-anim -gpu $gpu_mode -no-snapshot -cores $core_count"
log_args=(-show-kernel -logcat '*:V')
emu_args=
emu_pid=
wait_pid=
owned_avd=
prior_boot_id=

avd_name="${MAGISK_AVD_NAME:-magisk-test}"
# Emulator 37.2+ accepts only even console ports in 5554..5584. MuMu uses the
# 5554/5555 pair on this Mac, so keep the disposable project lane on the next
# valid pair while still allowing callers to select another isolated port.
emu_port="${MAGISK_AVD_PORT:-5556}"
test_dir="${MAGISK_TEST_DIR:-.}"
image_dir=
debug_image=
release_image=
kernel_log="${AVD_KERNEL_LOG:-$test_dir/kernel.log}"
logcat_log="${AVD_LOGCAT_LOG:-$test_dir/logcat.log}"

atd_min_api=30
atd_max_api=36
huge_ram_min_api=26

case $(uname -m) in
  'arm64'|'aarch64')
    if [ -n "${FORCE_32_BIT:-}" ]; then
      echo "! ARM32 is not supported"
      exit 1
    fi
    arch=arm64-v8a
    ;;
  *)
    if [ -n "${FORCE_32_BIT:-}" ]; then
      arch=x86
    else
      arch=x86_64
    fi
    ;;
esac

stop_emulator() {
  if [ -z "$emu_pid" ]; then
    return
  fi
  if kill -0 "$emu_pid" >/dev/null 2>&1; then
    # Console commands require the emulator serial, not its ADB TCP alias.
    # Verify ownership before flushing and closing that console.
    python3 - "$emu_port" "$avd_name" <<'PY'
import subprocess
import sys

command = ["adb", "-s", f"emulator-{sys.argv[1]}"]
try:
    result = subprocess.run(command + ["emu", "avd", "name"],
                            capture_output=True, text=True, timeout=5)
    names = result.stdout.splitlines()
    if result.returncode == 0 and names and names[0].strip() == sys.argv[2]:
        subprocess.run(command + ["shell", "sync"], capture_output=True, timeout=10)
        subprocess.run(command + ["emu", "kill"], capture_output=True, timeout=5)
except (OSError, subprocess.TimeoutExpired):
    pass
PY
    local count=0
    while kill -0 "$emu_pid" >/dev/null 2>&1 && [ "$count" -lt 30 ]; do
      sleep 1
      count=$((count + 1))
    done
    if kill -0 "$emu_pid" >/dev/null 2>&1; then
      kill -TERM "$emu_pid" >/dev/null 2>&1 || true
    fi
  fi
  wait "$emu_pid" 2>/dev/null || true
  emu_pid=
}

cleanup() {
  stop_emulator
  if [ -n "$image_dir" ]; then
    rm -rf "$image_dir"
    image_dir=
  fi
  if [ -n "$owned_avd" ]; then
    "$avd" delete avd -n "$avd_name" >/dev/null 2>&1 || true
    owned_avd=
  fi
}

test_error() {
  local status=$?
  trap - EXIT
  set +e
  print_error "! An error occurred"
  if [ -n "${AVD_TEST_LOG:-}" ] && [ -n "${ANDROID_SERIAL:-}" ]; then
    python3 - "$ANDROID_SERIAL" "$test_dir/logcat-failure.log" <<'PY'
from pathlib import Path
import subprocess
import sys

try:
    with Path(sys.argv[2]).open("w") as log:
        subprocess.run(["adb", "-s", sys.argv[1], "logcat", "-d", "-b", "all", "-v", "threadtime"],
                       stdout=log, stderr=subprocess.STDOUT, timeout=10)
except (OSError, subprocess.TimeoutExpired):
    pass

# These are disposable SDK guests. Preserve Android's own ANR stacks when
# a system dialog obscures a test, without changing or dismissing that dialog.
try:
    with Path(sys.argv[2]).with_name("anr-failure.log").open("w") as log:
        subprocess.run(["adb", "-s", sys.argv[1], "shell", "cat /data/anr/*"],
                       stdout=log, stderr=subprocess.STDOUT, timeout=10)
except (OSError, subprocess.TimeoutExpired):
    pass
PY
  fi
  if [ -n "$wait_pid" ]; then
    kill "$wait_pid" >/dev/null 2>&1 || true
    wait "$wait_pid" 2>/dev/null || true
    wait_pid=
  fi
  cleanup
  if [ "$status" -eq 0 ]; then
    status=1
  fi
  exit "$status"
}

start_boot_waiter() {
  python3 - <<'PY' &
import os
import signal
import subprocess
import sys
import time

serial = os.environ.get("ANDROID_SERIAL")
if not serial:
    raise SystemExit(2)
adb = ["adb", "-s", serial]
active = None
spawning = False
pending_signal = None


def process_group_exists(process_group):
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_process_group_gone(process_group, timeout):
    deadline = time.monotonic() + timeout
    while process_group_exists(process_group):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def stop_active():
    global active
    process = active
    if process is None:
        return True
    process_group = process.pid
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    if process_group_exists(process_group):
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        try:
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                active = None
                return False
    group_gone = wait_process_group_gone(process_group, 2)
    active = None
    return group_gone


def stop_waiter(signum, _frame):
    global pending_signal
    if spawning and active is None:
        pending_signal = signum
        return
    if not stop_active():
        raise SystemExit(125)
    raise SystemExit(128 + signum)


for handled_signal in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(handled_signal, stop_waiter)


def run_adb(arguments, capture):
    global active, pending_signal, spawning
    spawning = True
    try:
        process = subprocess.Popen(
            [*adb, *arguments],
            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
    except OSError:
        spawning = False
        if pending_signal is not None:
            signum = pending_signal
            pending_signal = None
            raise SystemExit(128 + signum)
        raise SystemExit(2)
    active = process
    spawning = False
    if pending_signal is not None:
        signum = pending_signal
        pending_signal = None
        stop_waiter(signum, None)
    try:
        stdout, _ = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        if not stop_active():
            raise SystemExit(125)
        return None
    if process_group_exists(process.pid):
        if not stop_active():
            raise SystemExit(125)
        return None
    active = None
    return process.returncode, stdout


while True:
    # Reconnecting cannot rediscover a transport that ADB has forgotten after
    # a previous QEMU process exited. Connect the owned listener explicitly.
    if serial.startswith("127.0.0.1:"):
        run_adb(["connect", serial], False)
    result = run_adb(["exec-out", "getprop", "sys.boot_completed"], True)
    if result is not None and result[0] == 0:
        if result[1].strip("\r\n") == "1":
            framework = run_adb(["shell", "pm", "path", "android"], True)
            if (
                framework is not None
                and framework[0] == 0
                and "package:" in framework[1]
            ):
                time.sleep(5)
                raise SystemExit(0)
            if framework is None or framework[0] != 0:
                run_adb(["reconnect"], False)
    else:
        run_adb(["reconnect"], False)
    time.sleep(2)
PY
  wait_pid=$!
}

validate_emu_port() {
  case "$emu_port" in
    *[!0-9]*|'') print_error "! Invalid AVD console port '$emu_port'"; return 1 ;;
  esac
  if [ "$emu_port" -lt 5554 ] || [ "$emu_port" -gt 5584 ] || [ $((emu_port % 2)) -ne 0 ]; then
    print_error "! AVD console port must be even and in 5554..5584"
    return 1
  fi
}

# Bash 3.2 has no `wait -n` or `wait -p`; poll only the two child PIDs we own.
wait_emu() {
  start_boot_waiter
  local started now status
  started=$(python3 -c 'import time; print(int(time.monotonic()))')

  while kill -0 "$wait_pid" >/dev/null 2>&1; do
    if ! kill -0 "$emu_pid" >/dev/null 2>&1; then
      kill "$wait_pid" >/dev/null 2>&1 || true
      wait "$wait_pid" 2>/dev/null || true
      wait_pid=
      return 1
    fi
    now=$(python3 -c 'import time; print(int(time.monotonic()))')
    if [ $((now - started)) -ge "$boot_timeout" ]; then
      kill "$wait_pid" >/dev/null 2>&1 || true
      wait "$wait_pid" 2>/dev/null || true
      wait_pid=
      return 124
    fi
    sleep 1
  done

  set +e
  wait "$wait_pid"
  status=$?
  set -e
  wait_pid=
  return "$status"
}

dump_vars() {
  local val name
  for name in "$@" emu_args; do
    eval val=\$$name
    echo "$name=\"$val\";"
  done
}

resolve_vars() {
  set +x
  local arg_list="$1"
  local ver=$2
  local type=$3

  local api
  case $ver in
    TiramisuPrivacySandbox) api=33 ;;
    UpsideDownCakePrivacySandbox) api=34 ;;
    VanillaIceCream) api=35 ;;
    Baklava) api=36 ;;
    CinnamonBun) api=37 ;;
    *CANARY) api=10000 ;;
    *)
      if [[ $ver =~ ^[0-9]+([.][0-9]+)?$ ]]; then
        api=$ver
      else
        print_error "! Unknown system image version '$ver'"
        exit 1
      fi
      ;;
  esac

  local api_major=${api%%.*}
  if [ -z "$type" ]; then
    if [ "$api_major" -ge "$atd_min_api" ] && [ "$api_major" -le "$atd_max_api" ]; then
      type='aosp_atd'
    elif [ "$api_major" -gt "$atd_max_api" ]; then
      type='google_apis'
    else
      type='default'
    fi
  fi

  local memory
  if [ "$api_major" -lt "$huge_ram_min_api" ]; then
    memory=3072
  else
    memory=8192
  fi
  emu_args="$emu_args_base -memory $memory -partition-size 2048"

  local avd_pkg="system-images;android-$ver;$type;$arch"
  local sys_img_dir="$ANDROID_HOME/system-images/android-$ver/$type/$arch"
  local ramdisk="$sys_img_dir/ramdisk.img"

  dump_vars $arg_list
}

dl_emu() {
  local avd_pkg=$1
  yes | "$sdk" --licenses >/dev/null 2>&1
  "$sdk" --channel=3 platform-tools emulator "$avd_pkg"
}

setup_emu() {
  local avd_pkg=$1
  local ver=$2
  local installed_ramdisk=$3
  if [ -z "${AVD_TEST_SKIP_DOWNLOAD:-}" ]; then
    dl_emu "$avd_pkg"
  else
    [ -x "$emu" ] && [ -x "$avd" ] && [ -f "$installed_ramdisk" ] || {
      print_error "! Offline AVD inputs are incomplete for $avd_pkg"
      return 1
    }
  fi
  mkdir -p "$ANDROID_AVD_HOME" "$test_dir"
  if [ -e "$ANDROID_AVD_HOME/$avd_name.ini" ] || [ -d "$ANDROID_AVD_HOME/$avd_name.avd" ]; then
    print_error "! Refusing to replace existing AVD '$avd_name'"
    return 1
  fi
  owned_avd=1
  local profile=()
  if [ -n "${MAGISK_AVD_DEVICE:-}" ]; then
    profile=(--device "$MAGISK_AVD_DEVICE")
  fi
  echo no | "$avd" create avd -f -n "$avd_name" -k "$avd_pkg" "${profile[@]}"

  # avdmanager is outdated, it might not set the proper target.
  local ini="$ANDROID_AVD_HOME/$avd_name.ini"
  sed "s:^[[:space:]]*target[[:space:]]*=.*:target=android-$ver:g" "$ini" > "$ini.new"
  mv "$ini.new" "$ini"
}

launch_emulator() {
  local image_args=$1
  if [ -n "${AVD_TEST_LOG:-}" ]; then
    rm -f "$kernel_log" "$logcat_log"
    "$emu" "@$avd_name" $emu_args "${log_args[@]}" -logcat-output "$logcat_log" \
      $image_args >"$kernel_log" 2>&1 &
  else
    "$emu" "@$avd_name" $emu_args $image_args >/dev/null 2>&1 &
  fi
  emu_pid=$!
  wait_emu || return $?
  prior_boot_id=$(python3 - "$avd_name" "$prior_boot_id" <<'PY'
import os
import subprocess
import sys
import uuid

expected, previous = sys.argv[1:]
adb = ["adb", "-s", os.environ["ANDROID_SERIAL"], "exec-out"]
def read(*args):
    return subprocess.check_output(adb + list(args), text=True, timeout=10).strip("\r\n")
name = read("getprop", "ro.boot.qemu.avd_name")
if not name:
    name = read("getprop", "ro.kernel.qemu.avd_name")
boot = read("cat", "/proc/sys/kernel/random/boot_id")
if name != expected or str(uuid.UUID(boot)) != boot or boot == previous:
    raise SystemExit("New boot identity does not match the owned AVD")
print(boot)
PY
  )
  if [ "${AVD_DISABLE_STORE_UPDATES:-0}" = 1 ]; then
    # Each variant wipes userdata, so restore this test setting on every boot.
    # Play Services updates kill dependent hidden-manager test processes.
    printf 'boot=%s\n' "$prior_boot_id" >> "$test_dir/store-updater-state.txt"
    adb shell pm disable-user --user 0 com.android.vending \
      >> "$test_dir/store-updater-state.txt"
    adb shell pm list packages -d --user 0 | tr -d '\r' | \
      grep -Fx 'package:com.android.vending' >> "$test_dir/store-updater-state.txt"
  fi
}

test_emu() {
  local variant=$1
  local image
  if [ "$variant" = debug ]; then
    image=$debug_image
  else
    image=$release_image
  fi

  # Each signer/variant gets clean userdata; reboot coverage happens inside the lane.
  launch_emulator "-wipe-data -ramdisk $image -feature -SystemAsRoot"
  run_setup "$variant"
  if [ -n "${AVD_QUALIFICATION_DIR:-}" ]; then
    python3 tools/device_qualification.py capture --serial "$ANDROID_SERIAL" \
      --apk "$MAGISK_OUT_DIR/app-$variant.apk" --phase setup \
      --output "$AVD_QUALIFICATION_DIR/$avd_name-$variant.json"
  fi
  if [ -n "${AVD_BOOT_FIXTURES:-}" ]; then
    python3 tools/boot_qualification.py --serial "$ANDROID_SERIAL" \
      --apk "$MAGISK_OUT_DIR/app-$variant.apk" \
      --output "$test_dir/boot-fixtures-$variant.json"
  fi

  # Restart the owned process, preserving userdata and the patched boot image.
  stop_emulator
  launch_emulator "-ramdisk $image -feature -SystemAsRoot"
  run_tests
  if [ "${AVD_BOOT_FIXTURES:-0}" = 1 ]; then
    python3 tools/property_qualification.py --serial "$ANDROID_SERIAL" \
      --apk "$MAGISK_OUT_DIR/app-$variant.apk" \
      --output "$test_dir/property-fixtures-$variant.json"
    assert_no_native_crashes
  fi
  if [ -n "${AVD_QUALIFICATION_DIR:-}" ]; then
    python3 tools/device_qualification.py capture --serial "$ANDROID_SERIAL" \
      --apk "$MAGISK_OUT_DIR/app-$variant.apk" --phase accepted \
      --output "$AVD_QUALIFICATION_DIR/$avd_name-$variant.json"
  fi
  stop_emulator
}

capture_stock_controls() {
  local count=${AVD_STOCK_BOOT_COUNT:-0} index=1 directory="$test_dir/stock-controls"
  case "$count" in 0) return ;; [1-8]) ;; *) return 1 ;; esac
  mkdir -p "$directory"
  python3 - "$1" > "$directory/ramdisk.sha256" <<'PYHASH'
import hashlib
from pathlib import Path
import sys
print(hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest())
PYHASH
  if [ "${AVD_STOCK_PROVISIONING_BARRIER:-0}" = 1 ]; then
    # Android 6 delays this PackageManager write for ten seconds. Boot completion
    # alone does not prove SdkSetup's disabled component survived to disk.
    local stock_uid
    stock_uid=$(adb shell id -u | tr -d '\r')
    adb root > "$directory/provisioning-adb.txt"
    adb wait-for-device
    [ "$(adb shell id -u | tr -d '\r')" = 0 ] || return 1
    python3 - "$ANDROID_SERIAL" "$directory/provisioning.json" <<'PYPROVISION'
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

command = ["adb", "-s", sys.argv[1], "exec-out", "cat",
           "/data/system/users/0/package-restrictions.xml"]
started = time.monotonic()
while time.monotonic() - started < 45:
    try:
        result = subprocess.run(command, capture_output=True, timeout=5)
        if result.returncode == 0:
            document = ET.fromstring(result.stdout)
            package = next((p for p in document.findall("pkg")
                            if p.get("name") == "com.android.sdksetup"), None)
            if package is not None and any(
                    item.get("name") == "com.android.sdksetup.DefaultActivity"
                    for item in package.findall("disabled-components/item")):
                Path(sys.argv[2]).write_text(json.dumps({
                    "schema": 1, "sdk_setup_disabled_on_disk": True,
                    "package_restrictions_sha256": hashlib.sha256(result.stdout).hexdigest(),
                    "wait_seconds": round(time.monotonic() - started, 3),
                }, indent=2) + "\n")
                break
    except (subprocess.TimeoutExpired, ET.ParseError):
        pass
    time.sleep(0.2)
else:
    raise SystemExit("SDK setup state was not persisted before stock shutdown")
PYPROVISION
    if [ "$stock_uid" != 0 ]; then
      adb unroot >> "$directory/provisioning-adb.txt"
      adb wait-for-device
    fi
  fi
  while [ "$index" -le "$count" ]; do
    adb shell getprop > "$directory/boot-$index.properties"
    adb shell cat /proc/sys/kernel/random/boot_id > "$directory/boot-$index.id"
    if [ "${AVD_STOCK_AM_ITERATIONS:-0}" != 0 ]; then
      # Compare ART command startup/shutdown before installing any Magisk code.
      adb root > "$directory/boot-$index-am-root.txt"
      adb wait-for-device
      [ "$(adb shell id -u | tr -d '\r')" = 0 ] || return 1
      python3 - "$ANDROID_SERIAL" "$AVD_STOCK_AM_ITERATIONS" \
        "$directory/boot-$index-am.json" <<'PYAM'
import json
from pathlib import Path
import subprocess
import sys

serial, raw_count, output = sys.argv[1:]
count = int(raw_count)
if not 1 <= count <= 100:
    raise SystemExit("Invalid stock AM control count")
command = ["adb", "-s", serial, "shell",
           "CLASSPATH=/system/framework/am.jar /system/bin/app_process "
           "/system/bin com.android.commands.am.Am help"]
results = []
try:
    for index in range(count):
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        results.append({"iteration": index + 1, "exit_code": result.returncode,
                        "stderr": result.stderr[-4096:]})
        if result.returncode:
            raise SystemExit("Stock app_process control failed")
finally:
    Path(output).write_text(json.dumps(results, indent=2) + "\n")
PYAM
      adb logcat -d -b crash -v threadtime > "$directory/boot-$index-am.crash"
      assert_no_native_crashes
      adb unroot >> "$directory/boot-$index-am-root.txt"
      adb wait-for-device
    fi
    adb logcat -d -b all -v threadtime > "$directory/boot-$index.log"
    adb logcat -d -b crash -v threadtime > "$directory/boot-$index.crash"
    if [ "${AVD_STOCK_FRAMEWORK_RESTART:-0}" = 1 ]; then
      # Exercise the stock graphics service across a framework restart, too.
      # Retain the preceding untouched cold-boot logs separately. No Magisk
      # payload or patched image has been installed at this point.
      adb root > "$directory/boot-$index-framework.txt"
      adb wait-for-device
      [ "$(adb shell id -u | tr -d '\r')" = 0 ] || return 1
      adb shell setprop sys.boot_completed 0
      adb shell stop
      # stop returns after sending init's control messages. Older init can
      # still be reaping zygote and running its onrestart actions at this
      # point; starting immediately can leave the new services stopped.
      python3 - "$ANDROID_SERIAL" <<'PYSTOP'
import subprocess
import sys
import time

command = ["adb", "-s", sys.argv[1], "shell",
           "getprop init.svc.zygote; getprop init.svc.zygote_secondary; "
           "getprop init.svc.surfaceflinger; getprop init.svc.netd"]
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    states = result.stdout.replace("\r", "").splitlines()
    if result.returncode == 0 and len(states) == 4 and states[0] == "stopped" \
            and all(state in ("", "stopped") for state in states[1:]):
        break
    time.sleep(0.2)
else:
    raise SystemExit("Stock framework services did not stop")
PYSTOP
      adb shell start
      wait_emu
      adb shell am start -W -a android.settings.DISPLAY_SETTINGS \
        >> "$directory/boot-$index-framework.txt"
      sleep 3
      adb shell input keyevent KEYCODE_HOME
      adb shell dumpsys display > "$directory/boot-$index-display.txt"
      adb logcat -d -b all -v threadtime > "$directory/boot-$index-framework.log"
      adb logcat -d -b crash -v threadtime > "$directory/boot-$index-framework.crash"
      adb unroot >> "$directory/boot-$index-framework.txt"
      adb wait-for-device
    fi
    if [ "$index" -lt "$count" ]; then
      stop_emulator
      launch_emulator ""
    fi
    index=$((index + 1))
  done
}

test_main() {
  local ver avd_pkg ramdisk
  eval "$(resolve_vars 'ver avd_pkg ramdisk' "$1" "${2:-}")"

  validate_emu_port
  emu_args="$emu_args -port $emu_port"
  export ANDROID_SERIAL="127.0.0.1:$((emu_port + 1))"
  setup_emu "$avd_pkg" "$ver" "$ramdisk"
  adb start-server >/dev/null

  print_title "* Launching $avd_pkg"
  launch_emulator ""
  capture_stock_controls "$ramdisk"

  # The emulator resolves modem profiles beside the selected ramdisk.
  # Preserve that SDK layout without writing into the shared system image.
  image_dir=$(mktemp -d "$test_dir/.magisk-ramdisk.XXXXXX")
  debug_image="$image_dir/magisk_debug.img"
  release_image="$image_dir/magisk_release.img"
  if [ -d "${ramdisk%/*}/data" ]; then
    ln -s "$(cd "${ramdisk%/*}/data" && pwd -P)" "$image_dir/data"
  fi

  local build=(./build.py)
  if [ -n "${MAGISK_BUILD_CONFIG:-}" ]; then
    build+=( -c "$MAGISK_BUILD_CONFIG" )
  fi
  if [ -z "${AVD_TEST_SKIP_DEBUG:-}" ]; then
    "${build[@]}" -v avd_patch "$ramdisk" "$debug_image"
  fi
  if [ -z "${AVD_TEST_SKIP_RELEASE:-}" ]; then
    "${build[@]}" -vr avd_patch "$ramdisk" "$release_image"
  fi
  stop_emulator

  if [ -z "${AVD_TEST_SKIP_DEBUG:-}" ]; then
    print_title "* Testing $avd_pkg (debug)"
    test_emu debug
  fi
  if [ -z "${AVD_TEST_SKIP_RELEASE:-}" ]; then
    print_title "* Testing $avd_pkg (release)"
    test_emu release
  fi
  cleanup
}

run_main() {
  local ver avd_pkg ramdisk
  eval "$(resolve_vars 'ver avd_pkg ramdisk' "$1" "${2:-}")"
  validate_emu_port
  emu_args="$emu_args -port $emu_port"
  export ANDROID_SERIAL="127.0.0.1:$((emu_port + 1))"
  setup_emu "$avd_pkg" "$ver" "$ramdisk"
  print_title "* Launching $avd_pkg"
  "$emu" "@$avd_name" $emu_args
  cleanup
}

dl_main() {
  local avd_pkg
  eval "$(resolve_vars 'avd_pkg' "$1" "${2:-}")"
  print_title "* Downloading $avd_pkg"
  dl_emu "$avd_pkg"
}

case "${1:-}" in
  test)
    shift
    trap test_error EXIT
    set -x
    test_main "$@"
    ;;
  run)
    shift
    trap cleanup EXIT
    run_main "$@"
    ;;
  dl)
    shift
    dl_main "$@"
    ;;
  *)
    print_error "Unknown argument '${1:-}'"
    exit 1
    ;;
esac

trap - EXIT
