#!/usr/bin/env bash

set -xe
. scripts/test_common.sh

cvd_args="-daemon -enable_sandbox=false -memory_mb=8192 -report_anonymous_usage_stats=n -cpus=$core_count"
# Guest software rendering can block the UI during headless Compose tests.
cvd_args+=" -gpu_mode=gfxstream_guest_angle_host_swiftshader"
magisk_args='-init_boot_image=magisk_patched.img'

cleanup() {
  print_error "! An error occurred"
  run_cvd_bin stop_cvd || true
  rm -f magisk_patched.img*
}

run_cvd_bin() {
  local exe=$1
  shift
  HOME=$CF_HOME $CF_HOME/bin/$exe "$@"
}

list_adb_serials() {
  python3 - <<'PY'
import os
import subprocess

environment = os.environ.copy()
environment.pop("ANDROID_SERIAL", None)
try:
    timeout = float(environment.get("CVD_ADB_COMMAND_TIMEOUT", "10"))
except ValueError:
    raise SystemExit(2)
if timeout <= 0:
    raise SystemExit(2)
try:
    result = subprocess.run(
        ["adb", "devices"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
        env=environment,
    )
except subprocess.TimeoutExpired:
    raise SystemExit(124)
if result.returncode != 0:
    raise SystemExit(result.returncode)
for line in result.stdout.splitlines()[1:]:
    fields = line.split()
    if len(fields) >= 2 and fields[1] == "device":
        print(fields[0])
PY
}

pin_new_adb_serial() {
  local baseline=$1 serial state
  serial=$(python3 - "$boot_timeout" "$baseline" <<'PY'
import os
import subprocess
import sys
import time

timeout = int(sys.argv[1])
baseline = set(sys.argv[2].splitlines())
environment = os.environ.copy()
environment.pop("ANDROID_SERIAL", None)
deadline = time.monotonic() + timeout
try:
    command_timeout = float(environment.get("CVD_ADB_COMMAND_TIMEOUT", "10"))
except ValueError:
    raise SystemExit(2)
if command_timeout <= 0:
    raise SystemExit(2)


def run_adb(arguments):
    try:
        return subprocess.run(
            ["adb", *arguments],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=min(command_timeout, max(0.1, deadline - time.monotonic())),
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return None


while time.monotonic() < deadline:
    result = run_adb(["devices"])
    if result is None or result.returncode != 0:
        time.sleep(1)
        continue
    online = []
    for line in result.stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "device" and fields[0] not in baseline:
            online.append(fields[0])
    if len(online) > 1:
        print("multiple new online ADB transports appeared", file=sys.stderr)
        raise SystemExit(3)
    if len(online) == 1:
        serial = online[0]
        hardware_result = run_adb(["-s", serial, "shell", "getprop", "ro.hardware"])
        product_result = run_adb(
            ["-s", serial, "shell", "getprop", "ro.product.name"]
        )
        if (
            hardware_result is None
            or product_result is None
            or hardware_result.returncode != 0
            or product_result.returncode != 0
        ):
            time.sleep(1)
            continue
        hardware = hardware_result.stdout.strip().lower()
        product = product_result.stdout.strip().lower()
        if not hardware or not product:
            time.sleep(1)
            continue
        if not (
            ("cutf" in hardware or "vsoc" in hardware)
            and ("_cf_" in product or product.startswith("aosp_cf"))
        ):
            print(
                f"new ADB transport {serial} is not Cuttlefish: "
                f"hardware={hardware!r}, product={product!r}",
                file=sys.stderr,
            )
            raise SystemExit(4)
        print(serial)
        raise SystemExit(0)
    time.sleep(1)
print("no new Cuttlefish ADB transport appeared", file=sys.stderr)
raise SystemExit(124)
PY
  ) || {
    print_error "! Cuttlefish ADB target did not appear safely"
    return 1
  }
  case "$serial" in
    ""|unknown|offline|*[![:alnum:].:_-]*)
      print_error "! Invalid Cuttlefish ADB serial '$serial'"
      return 1
      ;;
  esac
  state=$(adb -s "$serial" get-state 2>/dev/null) || {
    print_error "! Cannot validate Cuttlefish ADB serial '$serial'"
    return 1
  }
  if [ "$state" != device ]; then
    print_error "! Cannot validate Cuttlefish ADB serial '$serial'"
    return 1
  fi
  export ANDROID_SERIAL="$serial"
}

setup_env() {
  curl -LO https://github.com/topjohnwu/magisk-files/releases/download/files/cuttlefish-base_1.2.0_amd64.deb
  sudo apt-get update
  sudo dpkg -i ./cuttlefish-base_*_*64.deb || sudo apt-get install -f
  rm cuttlefish-base_*_*64.deb
  sudo usermod -aG kvm,cvdnetwork,render $USER
  yes | "$sdk" --licenses > /dev/null
  "$sdk" --channel=3 platform-tools
  adb kill-server
  adb start-server
}

download_cf() {
  local branch=$1
  local device=$2

  if [ -z $branch ]; then
    branch='aosp-android-latest-release'
  fi
  if [ -z $device ]; then
    device='aosp_cf_x86_64_only_phone'
  fi
  local target="${device}-userdebug"

  local build_id=$(curl -sL https://ci.android.com/builds/branches/${branch}/status.json | \
    jq -r ".targets[] | select(.name == \"$target\") | .last_known_good_build")
  local sys_img_url="https://ci.android.com/builds/submitted/${build_id}/${target}/latest/raw/${device}-img-${build_id}.zip"
  local host_pkg_url="https://ci.android.com/builds/submitted/${build_id}/${target}/latest/raw/cvd-host_package.tar.gz"

  print_title "* Download $target ($build_id) images"
  curl -L $sys_img_url -o aosp_cf_phone-img.zip
  curl -LO $host_pkg_url
  rm -rf $CF_HOME
  mkdir -p $CF_HOME
  tar xvf cvd-host_package.tar.gz -C $CF_HOME
  unzip aosp_cf_phone-img.zip -d $CF_HOME
  rm -f cvd-host_package.tar.gz aosp_cf_phone-img.zip
}

test_cf() {
  local variant=$1

  run_cvd_bin stop_cvd || true

  print_title "* Testing $variant builds"
  timeout $boot_timeout bash -c "run_cvd_bin launch_cvd $cvd_args $magisk_args -resume=false"
  adb wait-for-device
  run_setup $variant

  adb reboot
  sleep 5
  run_cvd_bin stop_cvd || true

  timeout $boot_timeout bash -c "run_cvd_bin launch_cvd $cvd_args $magisk_args"
  adb wait-for-device
  run_tests
}

test_main() {
  local preexisting_serials
  preexisting_serials=$(list_adb_serials)

  # Launch stock cuttlefish
  run_cvd_bin launch_cvd $cvd_args -resume=false
  pin_new_adb_serial "$preexisting_serials"

  # Patch and test debug build
  ./build.py -v avd_patch "$CF_HOME/init_boot.img" magisk_patched.img
  test_cf debug

  # Patch and test release build
  ./build.py -vr avd_patch "$CF_HOME/init_boot.img" magisk_patched.img
  test_cf release

  # Cleanup
  run_cvd_bin stop_cvd || true
  rm -f magisk_patched.img*
}

if [ -z $CF_HOME ]; then
  print_error "! Environment variable CF_HOME is required"
  exit 1
fi

case "$1" in
  setup )
    setup_env
    ;;
  download )
    download_cf $2 $3
    ;;
  test )
    trap cleanup EXIT
    export -f run_cvd_bin
    test_main
    trap - EXIT
    ;;
  * )
    exit 1
    ;;
esac
