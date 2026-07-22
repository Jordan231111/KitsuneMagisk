#!/usr/bin/env bash

emu="$ANDROID_SDK_ROOT/emulator/emulator"
avd="$ANDROID_SDK_ROOT/cmdline-tools/latest/bin/avdmanager"
sdk="$ANDROID_SDK_ROOT/cmdline-tools/latest/bin/sdkmanager"
emulator_port="${KITSUNE_AVD_PORT:-5554}"
avd_name="${KITSUNE_AVD_NAME:-kitsune-test-$emulator_port-$$}"
image_type_override="${KITSUNE_AVD_IMAGE_TYPE:-}"
boot_timeout="${KITSUNE_AVD_BOOT_TIMEOUT:-600}"
show_kernel="${KITSUNE_AVD_SHOW_KERNEL:-1}"
emu_pid=
emu_args=()
avd_created=false

case "$emulator_port" in
  ''|*[!0-9]*)
    echo "KITSUNE_AVD_PORT must be an even emulator console port" >&2
    exit 2
    ;;
esac
if [ "$emulator_port" -lt 5554 ] || [ "$emulator_port" -gt 5682 ] || [ $((emulator_port % 2)) -ne 0 ]; then
  echo "KITSUNE_AVD_PORT must be an even emulator console port from 5554 through 5682" >&2
  exit 2
fi
case "$avd_name" in
  ''|*[!A-Za-z0-9._-]*)
    echo "KITSUNE_AVD_NAME may contain only letters, digits, dot, underscore, and hyphen" >&2
    exit 2
    ;;
esac
case "$image_type_override" in
  *[!A-Za-z0-9_]*)
    echo "KITSUNE_AVD_IMAGE_TYPE may contain only letters, digits, and underscore" >&2
    exit 2
    ;;
esac
case "$boot_timeout" in
  ''|*[!0-9]*|0)
    echo "KITSUNE_AVD_BOOT_TIMEOUT must be a positive integer" >&2
    exit 2
    ;;
esac
case "${KITSUNE_AVD_MEMORY_MB:-1}" in
  ''|*[!0-9]*|0)
    echo "KITSUNE_AVD_MEMORY_MB must be a positive integer" >&2
    exit 2
    ;;
esac
case "$show_kernel" in
  0|1) ;;
  *)
    echo "KITSUNE_AVD_SHOW_KERNEL must be 0 or 1" >&2
    exit 2
    ;;
esac
if [ "$#" -gt 1 ]; then
  echo "Usage: $0 [ANDROID_API_LEVEL]" >&2
  exit 2
fi
case "${1:-23}" in
  ''|*[!0-9]*|0)
    echo "ANDROID_API_LEVEL must be a positive integer" >&2
    exit 2
    ;;
esac
adb_cmd=(adb -s "emulator-$emulator_port")

export PATH="$PATH:$ANDROID_SDK_ROOT/platform-tools"
export ANDROID_SDK_HOME="${ANDROID_SDK_HOME:-$ANDROID_SDK_ROOT}"
# build.py invokes adb internally while preparing the patch payload. Pin those
# calls to this AVD as well; otherwise any second attached device makes ABI
# discovery ambiguous and can receive unintended commands.
export ANDROID_SERIAL="emulator-$emulator_port"

# We test at least these API levels for the following reason

# API 23: legacy rootfs w/o Treble
# API 26: legacy rootfs with Treble
# API 28: legacy system-as-root
# API 29: 2 Stage Init
# API 35: latest Android

api_list='23 26 28 29 35'

atd_min_api=30
atd_max_api=35
huge_ram_min_api=26

print_title() {
  echo -e "\n\033[44;39m${1}\033[0m\n"
}

print_error() {
  echo -e "\n\033[41;39m${1}\033[0m\n"
}

cleanup() {
  local restore_ok=true
  print_error "! An error occurred when testing $pkg"

  # Stop the owned emulator before restoring the shared SDK image.
  stop_emu

  # Only restore the current API being tested, if variables are set. Never
  # discard the sole backup after a failed copy or byte comparison.
  if [ -n "$ramdisk" ] && [ -n "$features" ]; then
    if ! restore_avd; then
      restore_ok=false
      print_error "SDK image restoration failed; preserving .bak recovery files"
    fi
  fi

  if [ "$restore_ok" = true ]; then
    cleanup_avd_backups
  fi
  if [ "$avd_created" = true ]; then
    "$avd" delete avd -n "$avd_name" 2>/dev/null || true
    avd_created=false
  fi
  trap - EXIT
  exit 1
}

stop_emu() {
  if [ -n "$emu_pid" ]; then
    local pid=$emu_pid
    local deadline
    emu_pid=
    kill -INT "$pid" 2>/dev/null || true
    deadline=$((SECONDS + 15))
    while kill -0 "$pid" 2>/dev/null && [ "$SECONDS" -lt "$deadline" ]; do
      sleep 1
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -TERM "$pid" 2>/dev/null || true
      deadline=$((SECONDS + 5))
      while kill -0 "$pid" 2>/dev/null && [ "$SECONDS" -lt "$deadline" ]; do
        sleep 1
      done
    fi
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
    wait "$pid" 2>/dev/null || true
  fi
}

cleanup_avd_backups() {
  if [ -n "$ramdisk" ]; then
    rm -f -- "${ramdisk}.bak"
  fi
  if [ -n "$features" ]; then
    rm -f -- "${features}.bak"
  fi
}

set_api_env() {
  local memory
  local type='default'
  if [ -n "$image_type_override" ]; then
    type="$image_type_override"
  elif [ "$1" -ge "$atd_min_api" ] && [ "$1" -le "$atd_max_api" ]; then
    # Use the lightweight ATD images if possible
    type='aosp_atd'
  fi
  # Old Linux kernels will not boot with memory larger than 3GB
  if [ "$1" -lt "$huge_ram_min_api" ]; then
    memory=3072
  else
    memory=8192
  fi
  memory="${KITSUNE_AVD_MEMORY_MB:-$memory}"
  emu_args=(
    -no-window
    -no-audio
    -no-boot-anim
    -no-metrics
    -gpu swiftshader_indirect
    -read-only
    -no-snapshot
    -memory "$memory"
    -port "$emulator_port"
  )
  if [ "$show_kernel" != 0 ]; then
    emu_args+=( -show-kernel )
  fi
  pkg="system-images;android-$1;$type;$arch"
  local img_dir="$ANDROID_SDK_ROOT/system-images/android-$1/$type/$arch"
  ramdisk="$img_dir/ramdisk.img"
  features="$img_dir/advancedFeatures.ini"
}

restore_avd() {
  local failed=false
  if [ -f "${ramdisk}.bak" ]; then
    if ! cp "${ramdisk}.bak" "$ramdisk" || ! cmp -s "${ramdisk}.bak" "$ramdisk"; then
      echo "Failed to restore and verify $ramdisk" >&2
      failed=true
    fi
  fi
  if [ -f "${features}.bak" ]; then
    if ! cp "${features}.bak" "$features" || ! cmp -s "${features}.bak" "$features"; then
      echo "Failed to restore and verify $features" >&2
      failed=true
    fi
  fi
  [ "$failed" = false ]
}

wait_emu() {
  local property=$1
  local expected=$2
  local deadline=$((SECONDS + boot_timeout))
  local result

  # This polling loop works with macOS's Bash 3.2 and checks only the explicit
  # AVD serial, so another connected target can never receive these commands.
  while kill -0 "$emu_pid" 2>/dev/null; do
    result=$("${adb_cmd[@]}" exec-out getprop "$property" 2>/dev/null | tr -d '\r') || true
    if [ "$result" = "$expected" ]; then
      return 0
    fi
    if [ "$SECONDS" -ge "$deadline" ]; then
      return 1
    fi
    sleep 2
  done
  return 1
}

run_content_cmd() {
  local method=$1
  local deadline=$((SECONDS + boot_timeout))
  local out

  while kill -0 "$emu_pid" 2>/dev/null; do
    if ! out=$("${adb_cmd[@]}" shell echo "'content call --uri content://io.github.huskydg.magisk.provider --method $method'" \| /system/xbin/su 2>&1); then
      out=
    fi
    printf '%s\n' "$out" >&2
    if ! grep -q 'Bundle\[' <<< "$out"; then
      # The call failed, wait a while and retry later
      if [ "$SECONDS" -ge "$deadline" ]; then
        return 1
      fi
      sleep 30
    else
      grep -q 'result=true' <<< "$out"
      return $?
    fi
  done
  return 1
}

test_emu() {
  local variant=$1
  local api=$2

  print_title "* Testing $pkg ($variant)"

  "$emu" "@$avd_name" "${emu_args[@]}" &
  emu_pid=$!
  if ! wait_emu sys.boot_completed 1; then
    print_error "Failed to boot $variant image for $pkg"
    return 1
  fi

  if ! "${adb_cmd[@]}" shell magisk -v; then
    return 1
  fi

  # Install the Magisk app
  if ! "${adb_cmd[@]}" install -r -g "out/app-${variant}.apk"; then
    return 1
  fi

  # Use the app to run setup and reboot
  if ! run_content_cmd setup; then
    return 1
  fi

  if ! "${adb_cmd[@]}" reboot || ! wait_emu sys.boot_completed 1; then
    return 1
  fi

  # Run app tests
  if ! run_content_cmd test; then
    return 1
  fi
  local root_result
  if ! root_result=$("${adb_cmd[@]}" shell echo 'su -c id' \| /system/xbin/su 2000 2>&1); then
    return 1
  fi
  printf '%s\n' "$root_result" >&2
  grep -q 'uid=0' <<< "$root_result"

}


run_test() {
  local api=$1
  local avd_list

  set_api_env $api

  # Setup emulator
  if ! "$sdk" --channel=3 "$pkg"; then
    return 1
  fi
  if [ -e "${ramdisk}.bak" ] || [ -L "${ramdisk}.bak" ] ||
     [ -e "${features}.bak" ] || [ -L "${features}.bak" ]; then
    print_error "Refusing to trust or overwrite a pre-existing SDK image backup"
    return 1
  fi
  if ! avd_list=$("$emu" -list-avds); then
    print_error "Failed to list existing AVDs"
    return 1
  fi
  if grep -Fqx -- "$avd_name" <<< "$avd_list"; then
    print_error "Refusing to replace the pre-existing AVD $avd_name"
    return 1
  fi
  if ! echo no | "$avd" create avd -n "$avd_name" -k "$pkg"; then
    return 1
  fi
  avd_created=true

  # Launch stock emulator
  print_title "* Launching $pkg"
  restore_avd
  "$emu" "@$avd_name" "${emu_args[@]}" &
  emu_pid=$!
  if ! wait_emu init.svc.bootanim stopped; then
    print_error "Failed to boot emulator for $pkg"
    return 1
  fi

  # Patch and test debug build
  if ! ./build.py avd_patch -s "$ramdisk"; then
    print_error "Failed to patch ramdisk for debug build"
    stop_emu
    return 1
  fi
  stop_emu
  if ! test_emu debug $api; then
    print_error "Debug build test failed for $pkg"
    return 1
  fi

  # Re-patch and test release build
  if ! ./build.py -r avd_patch -s "$ramdisk"; then
    print_error "Failed to patch ramdisk for release build"
    stop_emu
    return 1
  fi
  stop_emu
  if ! test_emu release $api; then
    print_error "Release build test failed for $pkg"
    return 1
  fi

  # Cleanup
  stop_emu
  if ! restore_avd; then
    print_error "Failed to restore the stock SDK image; preserving recovery backups"
    return 1
  fi
  cleanup_avd_backups
  if ! "$avd" delete avd -n "$avd_name"; then
    print_error "Failed to delete temporary AVD $avd_name"
    return 1
  fi
  avd_created=false
}

trap cleanup EXIT

set -xe

case $(uname -m) in
  'arm64'|'aarch64')
    arch=arm64-v8a
    ;;
  *)
    arch=x86_64
    ;;
esac

yes | "$sdk" --licenses > /dev/null
"$sdk" --channel=3 tools platform-tools emulator

if [ -n "$1" ]; then
  if ! run_test $1; then
    print_error "Test failed for API $1"
    exit 1
  fi
else
  for api in $api_list; do
    if ! run_test $api; then
      print_error "Test failed for API $api"
      exit 1
    fi
  done
fi

if [ "$avd_created" = true ]; then
  "$avd" delete avd -n "$avd_name" 2>/dev/null || true
  avd_created=false
fi

trap - EXIT
