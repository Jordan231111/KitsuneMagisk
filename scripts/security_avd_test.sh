#!/usr/bin/env bash

# Run opt-in UBSan parser binaries on a disposable stock Android Studio AVD.
# Unlike avd_test.sh, this never patches a shared SDK image.

set -eu

emu="$ANDROID_SDK_ROOT/emulator/emulator"
avd="$ANDROID_SDK_ROOT/cmdline-tools/latest/bin/avdmanager"
sdk="$ANDROID_SDK_ROOT/cmdline-tools/latest/bin/sdkmanager"
api="${1:-35}"
port="${KITSUNE_SECURITY_AVD_PORT:-5562}"
name="${KITSUNE_SECURITY_AVD_NAME:-kitsune-security-$port-$$}"
image_type="${KITSUNE_SECURITY_AVD_IMAGE_TYPE:-google_apis}"
timeout="${KITSUNE_SECURITY_AVD_TIMEOUT:-600}"
iterations="${KITSUNE_SECURITY_CORPUS_ITERATIONS:-32}"
memory="${KITSUNE_SECURITY_AVD_MEMORY_MB:-4096}"
created=false
emu_pid=
avd_home=

case "$api:$port:$timeout:$iterations:$memory" in
  *[!0-9:]*|:*|*::*)
    echo "API, port, timeout, iterations, and memory must be numeric" >&2
    exit 2
    ;;
esac
if [ "$port" -lt 5554 ] || [ "$port" -gt 5682 ] || [ $((port % 2)) -ne 0 ]; then
  echo "KITSUNE_SECURITY_AVD_PORT must be an even port from 5554 through 5682" >&2
  exit 2
fi
if [ "$api" -lt 23 ] || [ "$timeout" -le 0 ] || [ "$memory" -le 0 ] || [ "$iterations" -gt 256 ]; then
  echo "invalid security AVD limits" >&2
  exit 2
fi
case "$name:$image_type" in
  *[!A-Za-z0-9._:-]*)
    echo "AVD name or image type contains unsafe characters" >&2
    exit 2
    ;;
esac

case $(uname -m) in
  arm64|aarch64) arch=arm64-v8a ;;
  *) arch=x86_64 ;;
esac

pkg="system-images;android-$api;$image_type;$arch"
serial="emulator-$port"
adb="$ANDROID_SDK_ROOT/platform-tools/adb"
adb_cmd=("$adb" -s "$serial")

stop_emulator() {
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

remove_avd_home() {
  [ -n "$avd_home" ] || return 0
  if [ ! -f "$avd_home/.kitsune-security-owned" ]; then
    echo "refusing to remove unowned AVD home $avd_home" >&2
    return 1
  fi
  rm -rf -- "$avd_home"
  avd_home=
}

cleanup() {
  local status=$?
  stop_emulator
  if [ "$created" = true ]; then
    "$avd" delete avd -n "$name" >/dev/null 2>&1 || true
    created=false
  fi
  if ! remove_avd_home; then
    [ "$status" -ne 0 ] || status=1
  fi
  trap - EXIT INT TERM
  exit "$status"
}
trap cleanup EXIT INT TERM

avd_home=$(mktemp -d "${TMPDIR:-/tmp}/kitsune-security-avd.XXXXXX")
touch "$avd_home/.kitsune-security-owned"
export ANDROID_AVD_HOME="$avd_home"

yes | "$sdk" --licenses >/dev/null
"$sdk" --channel=3 platform-tools emulator "$pkg"
if "$emu" -list-avds | grep -Fqx -- "$name"; then
  echo "refusing to replace existing AVD $name" >&2
  exit 1
fi
echo no | "$avd" create avd -n "$name" -k "$pkg" -p "$avd_home/$name.avd"
created=true

"$emu" "@$name" \
  -no-window -no-audio -no-boot-anim -no-metrics \
  -gpu swiftshader_indirect -read-only -no-snapshot \
  -memory "$memory" -port "$port" &
emu_pid=$!

deadline=$((SECONDS + timeout))
while kill -0 "$emu_pid" 2>/dev/null; do
  booted=$("${adb_cmd[@]}" exec-out getprop sys.boot_completed 2>/dev/null | tr -d '\r') || true
  [ "$booted" = 1 ] && break
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "security AVD timed out" >&2
    exit 1
  fi
  sleep 2
done
[ "${booted:-}" = 1 ] || exit 1

python3 -m tools.security_lab.device_corpus \
  --adb "$adb" \
  --serial "$serial" \
  --binary-dir native/out \
  --iterations "$iterations" \
  --output "out/security-corpus-ubsan-api${api}-${arch}.json"

stop_emulator
"$avd" delete avd -n "$name"
created=false
remove_avd_home
trap - EXIT INT TERM
