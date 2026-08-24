#!/usr/bin/env bash

set -e
. scripts/test_common.sh

emu_args_base="-no-window -no-audio -no-boot-anim -gpu swiftshader_indirect -read-only -no-snapshot -cores $core_count"
log_args="-show-kernel -logcat ''"
emu_args=
emu_pid=
wait_pid=
owned_avd=

avd_name="${MAGISK_AVD_NAME:-magisk-test}"
# Emulator 37.2+ accepts only even console ports in 5554..5584. MuMu uses the
# 5554/5555 pair on this Mac, so keep the disposable project lane on the next
# valid pair while still allowing callers to select another isolated port.
emu_port="${MAGISK_AVD_PORT:-5556}"
test_dir="${MAGISK_TEST_DIR:-.}"
debug_image="$test_dir/magisk_debug.img"
release_image="$test_dir/magisk_release.img"
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
    kill -INT "$emu_pid" >/dev/null 2>&1 || true
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
  rm -f "$debug_image" "$release_image"
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

wait_for_boot() {
  adb wait-for-device
  while true; do
    local result
    result=$(adb exec-out getprop sys.boot_completed | tr -d '\r') || return 1
    if [ "$result" = "1" ]; then
      return 0
    fi
    sleep 2
  done
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
  wait_for_boot &
  wait_pid=$!
  local started now status
  started=$(date +%s)

  while kill -0 "$wait_pid" >/dev/null 2>&1; do
    if ! kill -0 "$emu_pid" >/dev/null 2>&1; then
      kill "$wait_pid" >/dev/null 2>&1 || true
      wait "$wait_pid" 2>/dev/null || true
      wait_pid=
      return 1
    fi
    now=$(date +%s)
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
  emu_args="$emu_args_base -memory $memory"

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
  echo no | "$avd" create avd -f -n "$avd_name" -k "$avd_pkg"

  # avdmanager is outdated, it might not set the proper target.
  local ini="$ANDROID_AVD_HOME/$avd_name.ini"
  sed "s:^[[:space:]]*target[[:space:]]*=.*:target=android-$ver:g" "$ini" > "$ini.new"
  mv "$ini.new" "$ini"
}

launch_emulator() {
  local image_args=$1
  if [ -n "${AVD_TEST_LOG:-}" ]; then
    rm -f "$kernel_log" "$logcat_log"
    "$emu" "@$avd_name" $emu_args $log_args -logcat-output "$logcat_log" \
      $image_args >"$kernel_log" 2>&1 &
  else
    "$emu" "@$avd_name" $emu_args $image_args >/dev/null 2>&1 &
  fi
  emu_pid=$!
  wait_emu
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

  adb reboot
  wait_emu
  run_tests
  stop_emulator
}

test_main() {
  local ver avd_pkg ramdisk
  eval "$(resolve_vars 'ver avd_pkg ramdisk' "$1" "${2:-}")"

  validate_emu_port
  emu_args="$emu_args -port $emu_port"
  export ANDROID_SERIAL="emulator-$emu_port"
  setup_emu "$avd_pkg" "$ver" "$ramdisk"
  adb start-server >/dev/null

  print_title "* Launching $avd_pkg"
  launch_emulator ""

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
  export ANDROID_SERIAL="emulator-$emu_port"
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
