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
am_instrument() {
  set +x
  local out
  out=$(adb shell am instrument -w --user 0 -e class "$1" "$2" | tr -d '\r')
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
  adb shell 'PATH=$PATH:/debug_ramdisk magisk -v' | tr -d '\r'

  # Install the Magisk app
  adb install -r -g "$MAGISK_OUT_DIR/app-${variant}.apk"

  # Install the test app
  adb install -r -g "$MAGISK_OUT_DIR/test-${variant}.apk"

  local app="$MAGISK_TEST_PACKAGE/com.topjohnwu.magisk.test.AppTestRunner"

  # Run setup through the test app
  am_instrument '.Environment#setupEnvironment' $app
}

run_tests() {
  local pkg="$MAGISK_TEST_PACKAGE"
  local self="$pkg/com.topjohnwu.magisk.test.TestRunner"
  local app="$pkg/com.topjohnwu.magisk.test.AppTestRunner"
  local stub="repackaged.$pkg/com.topjohnwu.magisk.test.AppTestRunner"

  # Run app tests
  am_instrument '.MagiskAppTest,.AdditionalTest' $app

  # Test app hiding
  am_instrument '.AppMigrationTest#testAppHide' $self

  # Make sure it still works
  am_instrument '.MagiskAppTest' $stub

  # Test app restore
  am_instrument '.AppMigrationTest#testAppRestore' $self

  # Make sure it still works
  am_instrument '.MagiskAppTest' $app

  run_root_stress
}

run_root_stress() {
  local iterations="${AVD_STRESS_ITERATIONS:-0}"
  local parallel="${AVD_STRESS_PARALLEL:-6}"
  case "$iterations:$parallel" in
    *[!0-9:]*|:*|*:0)
      print_error "Invalid AVD stress configuration: $iterations iterations, $parallel parallel"
      return 1
      ;;
  esac
  if [ "$iterations" -eq 0 ]; then
    return
  fi

  print_title "* Stressing MagiskSU ($iterations x $parallel concurrent requests)"
  local iteration out root_count version
  iteration=0
  while [ "$iteration" -lt "$iterations" ]; do
    out=$(adb shell "i=0; while [ \$i -lt $parallel ]; do (su -c id) & i=\$((i + 1)); done; wait" | tr -d '\r')
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
}
