from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

from tools.system_mode.schema_validation import validate_schema_instance


ROOT = Path(__file__).resolve().parents[2]
TRANSACTION = ROOT / "scripts" / "system_mode_transaction.sh"
MANIFEST_SCHEMA = ROOT / "tools" / "system_mode" / "schemas" / "install-manifest-v1.schema.json"


SHELL_PREAMBLE = r"""
set -eu
. "$1"
TEST_ROOT="$2"
SM_SETUP_MARKER="$TEST_ROOT/data/adb/.kitsune-system-mode-setup-v1.env"
SM_ROLLBACK_TERMINAL="$TEST_ROOT/data/adb/.kitsune-system-mode-rollback-v1.env"
SM_PRIOR_TRANSACTION="$TEST_ROOT/data/adb/.kitsune-system-mode-prior-v1.env"
SM_SECURE_DIR_METADATA="$TEST_ROOT/secure-dir.env"
SM_SECURE_DIR_SHA256="$(printf '%064d' 0)"
SM_MOUNTINFO_FILE="$TEST_ROOT/mountinfo"
SM_SNAPSHOT_SETTLE_SECONDS=0
SM_INSTALL_ID=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
mkdir -p "$TEST_ROOT/data/adb"
: >"$SM_MOUNTINFO_FILE"

bb() {
  applet="$1"
  shift
  case "$applet" in
    sha256sum)
      python3 -c '
import hashlib
import sys

paths = sys.argv[1:] or ["-"]
for path in paths:
    if path == "-":
        stream = sys.stdin.buffer
    else:
        stream = open(path, "rb")
    digest = hashlib.sha256()
    try:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    finally:
        if path != "-":
            stream.close()
    print(f"{digest.hexdigest()}  {path}")
' "$@"
      ;;
    stat)
      if [ "$1" = -c ]; then
        format="$2"
        path="$3"
        python3 -c '
import os
import stat
import sys

fmt, path = sys.argv[1:]
value = os.lstat(path)
fields = {
    "%s": value.st_size,
    "%a": format(stat.S_IMODE(value.st_mode), "o"),
    "%u": value.st_uid,
    "%g": value.st_gid,
}
if fmt not in fields:
    raise SystemExit(2)
print(fields[fmt])
' "$format" "$path"
      else
        command stat "$@"
      fi
      ;;
    flock)
      python3 - "$SM_LOCK_ROOT" "$@" <<'PY'
import fcntl
import os
from pathlib import Path
import signal
import sys
import time

owner = os.getppid()
registry = Path(sys.argv[1]) / f".test-flock.{owner}"
args = sys.argv[2:]
unlock = "-u" in args
nonblocking = "-n" in args
fd = int(args[-1])
if unlock:
    try:
        keeper = int(registry.read_text(encoding="ascii"))
    except (FileNotFoundError, ValueError):
        raise SystemExit(1)
    try:
        os.kill(keeper, signal.SIGTERM)
    except ProcessLookupError:
        pass
    for _ in range(200):
        if not registry.exists():
            break
        time.sleep(0.01)
    raise SystemExit(0)

operation = fcntl.LOCK_EX
if nonblocking:
    operation |= fcntl.LOCK_NB
try:
    fcntl.flock(fd, operation)
except BlockingIOError:
    raise SystemExit(1)

keeper = os.fork()
if keeper:
    registry.write_text(str(keeper), encoding="ascii")
    raise SystemExit(0)

running = True
def stop(_signum, _frame):
    global running
    running = False

signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
for descriptor in range(0, 256):
    if descriptor == fd:
        continue
    try:
        os.close(descriptor)
    except OSError:
        pass
while running:
    try:
        os.kill(owner, 0)
    except ProcessLookupError:
        break
    time.sleep(0.01)
try:
    os.close(fd)
finally:
    try:
        registry.unlink()
    except FileNotFoundError:
        pass
os._exit(0)
PY
      ;;
    *) (command "$applet" "$@") ;;
  esac
  # Keep a background harness shell from tail-execing the applet. Production
  # uses the monolithic Magisk BusyBox binary and does not need this test shim.
  status=$?
  return "$status"
}

SM_BB=bb
sm_fsync() { return 0; }
sm_fsync_tree() { return 0; }
sm_log() { :; }
# The host test process cannot create uid-0 files. Preserve every marker
# shape/mode check while treating the isolated harness owner as Android root.
sm_marker_file_safe() {
  [ -f "$1" ] && [ ! -L "$1" ] || return 1
  mode="$(bb stat -c %a "$1")" || return 1
  sm_reject_unsafe_mode "$mode"
}

test_use_host_lock_storage() {
  SM_LOCK_ROOT="$TEST_ROOT/dev"
  SM_LOCK_FILE="$SM_LOCK_ROOT/.kitsune-system-mode-transaction-v1.lock"
  SM_REMOUNT_FILE="$SM_LOCK_ROOT/.kitsune-system-mode-remount-v1.env"
  mkdir -p "$SM_LOCK_ROOT"
  chmod 0700 "$SM_LOCK_ROOT"
  TEST_BOOT_ID=11111111-1111-4111-8111-111111111111
  sm_current_boot_id() { printf '%s\n' "$TEST_BOOT_ID"; }
  sm_lock_root_safe() {
    [ -d "$SM_LOCK_ROOT" ] && [ ! -L "$SM_LOCK_ROOT" ] || return 1
    mode="$(bb stat -c %a "$SM_LOCK_ROOT")" || return 1
    sm_reject_unsafe_mode "$mode"
  }
  sm_lock_file_safe() {
    [ -f "$SM_LOCK_FILE" ] && [ ! -L "$SM_LOCK_FILE" ] || return 1
    [ "$(bb stat -c %a "$SM_LOCK_FILE")" = 600 ]
  }
  sm_remount_file_safe() {
    [ -f "$1" ] && [ ! -L "$1" ] || return 1
    [ "$(bb stat -c %a "$1")" = 600 ]
  }
}

test_accept_host_authorization() {
  sm_authorization_file_safe() {
    [ -f "$1" ] && [ ! -L "$1" ] || return 1
    [ "$(bb stat -c %a "$1")" = 600 ]
  }
}

test_write_receipt() {
  file="$1"
  install="$2"
  transaction_id="$3"
  state="$4"
  mkdir -p "$(dirname "$file")"
  {
    printf 'SCHEMA_VERSION=1\n'
    printf 'INSTALL_ID=%s\n' "$install"
    printf 'TRANSACTION_ID=%s\n' "$transaction_id"
    printf 'STATE=%s\n' "$state"
  } >"$file"
  chmod 0600 "$file"
}

test_write_current_receipt() {
  test_write_receipt "$SM_TRANSACTION_FILE" "$SM_INSTALL_ID" "$SM_TRANSACTION_ID" "$1"
}

test_update_state() {
  SM_STATE="$1"
  test_write_current_receipt "$SM_STATE"
}

test_prepare_secure_dir() {
  secure="$(sm_real_path /data/adb)"
  mkdir -p "$secure" "$(dirname "$SM_SECURE_DIR_METADATA")"
  chmod 0700 "$secure"
  {
    printf 'SCHEMA_VERSION=1\n'
    printf 'PATH=/data/adb\n'
    printf 'UID=%s\n' "$(id -u)"
    printf 'GID=%s\n' "$(id -g)"
    printf 'MODE=700\n'
    printf 'CONTEXT_B64=LQ==\n'
  } >"$SM_SECURE_DIR_METADATA"
  chmod 0600 "$SM_SECURE_DIR_METADATA"
  SM_SECURE_DIR_SHA256="$(printf '%064d' 0)"
}
"""


def run_harness(body: str, root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", SHELL_PREAMBLE + textwrap.dedent(body), "transaction", str(TRANSACTION), str(root)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class SystemModeTransactionTest(unittest.TestCase):
    def test_fresh_preflight_rollback_removes_only_its_empty_state_containers(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-fresh-preflight-") as temp:
            result = run_harness(
                r"""
                SM_STATE_DIR="$TEST_ROOT/data/adb/kitsune/system-mode"
                SM_TRANSACTION_FILE="$SM_STATE_DIR/transaction.env"
                SM_MANIFEST_COPY="$SM_STATE_DIR/install-manifest.json"
                SM_OWNERSHIP_FILE="$SM_STATE_DIR/ownership.tsv"
                SM_ORIGINAL_FILE="$SM_STATE_DIR/originals.tsv"
                SM_JOURNAL_FILE="$SM_STATE_DIR/journal.tsv"
                SM_BOOT_PROOF="$SM_STATE_DIR/boot-verified.env"
                SM_SYSTEM_DIR=/system/etc/init/magisk
                SM_INIT_PATH=/system/etc/init/magisk.rc
                SM_STAGING_PATH=/system/etc/init/.magisk.kitsune-stage-test
                SM_TRANSACTION_ID=12345678-1234-1234-1234-123456789abc
                SM_ROLLBACK_DIR="$SM_STATE_DIR/rollback/$SM_TRANSACTION_ID"
                sm_real_path() { printf '%s/root%s\n' "$TEST_ROOT" "$1"; }
                sm_fsync() { return 0; }

                mkdir -p "$SM_ROLLBACK_DIR"
                sm_snapshot_state_metadata
                test_write_current_receipt PREFLIGHTED
                sm_restore_preflight
                test ! -e "$SM_STATE_DIR"
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_fresh_staged_rollback_removes_state_after_restoring_absent_boot_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-fresh-staged-") as temp:
            result = run_harness(
                r"""
                SM_STATE_DIR="$TEST_ROOT/data/adb/kitsune/system-mode"
                SM_TRANSACTION_FILE="$SM_STATE_DIR/transaction.env"
                SM_MANIFEST_COPY="$SM_STATE_DIR/install-manifest.json"
                SM_OWNERSHIP_FILE="$SM_STATE_DIR/ownership.tsv"
                SM_ORIGINAL_FILE="$SM_STATE_DIR/originals.tsv"
                SM_JOURNAL_FILE="$SM_STATE_DIR/journal.tsv"
                SM_BOOT_PROOF="$SM_STATE_DIR/boot-verified.env"
                SM_SYSTEM_DIR=/system/etc/init/magisk
                SM_INIT_PATH=/system/etc/init/magisk.rc
                SM_POLICY_PATH=
                SM_POLICY_MUTATED=false
                SM_LEGACY_MIGRATION=false
                SM_TRANSACTION_ID=12345678-1234-1234-1234-123456789abc
                SM_STAGING_PATH=/system/etc/init/.magisk.kitsune-stage-test
                SM_ROLLBACK_DIR="$SM_STATE_DIR/rollback/$SM_TRANSACTION_ID"
                SM_PRIOR_STATE=UNINSTALLED
                sm_configure_rescue_paths
                sm_real_path() { printf '%s/root%s\n' "$TEST_ROOT" "$1"; }
                sm_update_state() { test_update_state "$1"; }
                sm_fsync() { return 0; }

                mkdir -p "$SM_ROLLBACK_DIR" "$TEST_ROOT/root/system/etc/init"
                sm_snapshot_state_metadata
                test_prepare_secure_dir
                sm_snapshot_all
                mkdir -p "$TEST_ROOT/root$SM_SYSTEM_DIR" "$TEST_ROOT/root$SM_RESCUE_DIR"
                printf new >"$TEST_ROOT/root$SM_SYSTEM_DIR/file"
                printf new >"$TEST_ROOT/root$SM_INIT_PATH"
                printf new >"$TEST_ROOT/root$SM_RESCUE_DIR/busybox"
                printf new >"$TEST_ROOT/root$SM_RESCUE_RC"
                test_write_current_receipt STAGED

                sm_restore_snapshot
                test ! -e "$TEST_ROOT/root$SM_SYSTEM_DIR"
                test ! -e "$TEST_ROOT/root$SM_INIT_PATH"
                test ! -e "$TEST_ROOT/root$SM_RESCUE_DIR"
                test ! -e "$TEST_ROOT/root$SM_RESCUE_RC"
                test ! -e "$SM_STATE_DIR"
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_receipt_lookup_rejects_duplicate_keys(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-keys-") as temp:
            result = run_harness(
                r"""
                receipt="$TEST_ROOT/transaction.env"
                {
                  printf 'STATE=COMMITTED\n'
                  printf 'STATE=BOOT_VERIFIED\n'
                } >"$receipt"
                ! sm_get STATE "$receipt"
                test -z "$(sm_get OPTIONAL "$receipt")"
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_original_inventory_rejects_a_missing_or_redirected_absence_marker(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-original-marker-") as temp:
            result = run_harness(
                r"""
                SM_STATE_DIR="$TEST_ROOT/state"
                SM_ORIGINAL_FILE="$SM_STATE_DIR/originals.tsv"
                SM_OWNERSHIP_FILE="$SM_STATE_DIR/ownership.tsv"
                SM_MANIFEST_SHA256="$(printf '%064d' 0)"
                SM_ORIGINALS_SHA256="$(printf '%064d' 0)"
                SM_SYSTEM_DIR=/system/etc/init/magisk
                SM_INIT_PATH=/system/etc/init/magisk.rc
                SM_POLICY_PATH=
                sm_configure_rescue_paths
                sm_real_path() { printf '%s/root%s\n' "$TEST_ROOT" "$1"; }
                mkdir -p "$TEST_ROOT/root/system/etc/init"

                for label in payload init_rc bootanim runtime magisk_db magisk_db_wal magisk_db_shm \
                  modules modules_update post_fs_data service preinit_rule magisk_log magisk_log_bak \
                  addon_script addon_dir rescue_rc rescue_dir; do
                  mkdir -p "$SM_STATE_DIR/original/$label"
                  printf absent >"$SM_STATE_DIR/original/$label/absent"
                done
                {
                  printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/payload/data\n' "$SM_SYSTEM_DIR"
                  printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/init_rc/data\n' "$SM_INIT_PATH"
                  printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/bootanim/data\n' /system/etc/init/bootanim.rc
                  printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/runtime/data\n' /data/adb/magisk
                  printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/magisk_db/data\n' /data/adb/magisk.db
                  printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/magisk_db_wal/data\n' /data/adb/magisk.db-wal
                  printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/magisk_db_shm/data\n' /data/adb/magisk.db-shm
                  printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/modules/data\n' /data/adb/modules
                  printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/modules_update/data\n' /data/adb/modules_update
                  printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/post_fs_data/data\n' /data/adb/post-fs-data.d
                  printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/service/data\n' /data/adb/service.d
                  printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/preinit_rule/data\n' /data/adb/sepolicy.rule
                  printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/magisk_log/data\n' /cache/magisk.log
                  printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/magisk_log_bak/data\n' /cache/magisk.log.bak
                  printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/addon_script/data\n' /system/addon.d/99-magisk.sh
                  printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/addon_dir/data\n' /system/addon.d/magisk
                  printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/rescue_rc/data\n' "$SM_RESCUE_RC"
                  printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/rescue_dir/data\n' "$SM_RESCUE_DIR"
                } >"$SM_ORIGINAL_FILE"
                sm_validate_originals

                rm "$SM_STATE_DIR/original/runtime/absent"
                ! sm_validate_originals
                printf absent >"$SM_STATE_DIR/original/runtime/absent"
                rm "$SM_STATE_DIR/original/rescue_rc/absent"
                ln -s "$SM_STATE_DIR/original/rescue_dir/absent" \
                  "$SM_STATE_DIR/original/rescue_rc/absent"
                ! sm_validate_originals
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_path_digest_distinguishes_link_semantics_and_cleans_failed_work(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-digest-") as temp:
            result = run_harness(
                r"""
                SM_STATE_DIR="$TEST_ROOT/state"
                SM_TRANSACTION_ID=12345678-1234-1234-1234-123456789abc
                mkdir -p "$SM_STATE_DIR" "$TEST_ROOT/tree"
                printf target-bytes >"$TEST_ROOT/tree/target"
                ln -s target "$TEST_ROOT/tree/link"
                link_digest="$(sm_digest_path "$TEST_ROOT/tree/link")"
                expected="$(printf 'link:target' | bb sha256sum | awk '{ print $1 }')"
                test "$link_digest" = "$expected"
                test "$link_digest" != "$(sm_digest_path "$TEST_ROOT/tree/target")"

                printf unreadable >"$TEST_ROOT/tree/blocked"
                chmod 000 "$TEST_ROOT/tree/blocked"
                ! sm_digest_path "$TEST_ROOT/tree" >/dev/null 2>&1
                chmod 600 "$TEST_ROOT/tree/blocked"
                rm "$TEST_ROOT/tree/blocked"
                test -z "$(find "$SM_STATE_DIR" -name '.kitsune-system-mode-digest.*' -print -quit)"
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_recovery_cleanup_removes_every_exact_staging_and_short_write_name(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-staging-cleanup-") as temp:
            result = run_harness(
                r"""
                SM_STATE_DIR="$TEST_ROOT/state"
                SM_TRANSACTION_FILE="$SM_STATE_DIR/transaction.env"
                SM_MANIFEST_COPY="$SM_STATE_DIR/install-manifest.json"
                SM_OWNERSHIP_FILE="$SM_STATE_DIR/ownership.tsv"
                SM_ORIGINAL_FILE="$SM_STATE_DIR/originals.tsv"
                SM_JOURNAL_FILE="$SM_STATE_DIR/journal.tsv"
                SM_BOOT_PROOF="$SM_STATE_DIR/boot-verified.env"
                SM_SECURE_DIR_METADATA="$SM_STATE_DIR/secure-dir.env"
                SM_SYSTEM_DIR=/system/etc/init/magisk
                SM_INIT_PATH=/system/etc/init/magisk.rc
                SM_POLICY_PATH=/vendor/etc/selinux/precompiled_sepolicy
                SM_POLICY_MUTATED=true
                SM_TRANSACTION_ID=12345678-1234-1234-1234-123456789abc
                SM_STAGING_PATH=/system/etc/init/.magisk.kitsune-stage-$SM_TRANSACTION_ID
                sm_configure_rescue_paths
                sm_real_path() { printf '%s/root%s\n' "$TEST_ROOT" "$1"; }
                sm_runtime_stage_path() { printf '%s/runtime-stage\n' "$TEST_ROOT"; }
                sm_runtime_old_path() { printf '%s/runtime-old\n' "$TEST_ROOT"; }
                sm_remove_tree_safe() { "$SM_BB" rm -rf "$3"; }
                mkdir -p "$SM_STATE_DIR"

                mapped_paths="
                  $TEST_ROOT/root$SM_STAGING_PATH
                  $TEST_ROOT/root$SM_INIT_PATH.kitsune-new
                  $TEST_ROOT/root$(sm_rescue_stage_path)
                  $TEST_ROOT/root$SM_RESCUE_RC.kitsune-new
                  $TEST_ROOT/root/system/etc/init/bootanim.rc.kitsune-stock-new
                  $TEST_ROOT/root/system/addon.d/.99-magisk.sh.kitsune-new
                  $TEST_ROOT/root$SM_POLICY_PATH.kitsune-original-new
                  $TEST_ROOT/root$SM_SYSTEM_DIR/.install-manifest.json.new
                  $TEST_ROOT/root$SM_SYSTEM_DIR/.install-manifest.state-new
                  $TEST_ROOT/runtime-stage
                  $TEST_ROOT/runtime-old
                "
                state_paths="
                  $SM_STATE_DIR/.transaction.env.new
                  $SM_STATE_DIR/.install-manifest.json.new
                  $SM_STATE_DIR/.install-manifest.copy.new
                  $SM_STATE_DIR/.install-manifest.state-new
                  $SM_STATE_DIR/.boot-verified.env.new
                  $SM_STATE_DIR/.secure-dir.env.new
                  $SM_STATE_DIR/.policy.original
                  $SM_STATE_DIR/.bootanim.original
                  $SM_ORIGINAL_FILE.new
                  $SM_OWNERSHIP_FILE.new
                  $SM_JOURNAL_FILE.new
                  $SM_STATE_DIR/.journal-removals.new
                "
                for path in $mapped_paths $state_paths; do
                  mkdir -p "$(dirname "$path")"
                  printf interrupted >"$path"
                  printf short >"$path.kitsune-short"
                done
                printf keep >"$SM_STATE_DIR/.secure-dir.env.new.keep"
                printf keep >"$TEST_ROOT/root$SM_POLICY_PATH.kitsune-original-new.keep"

                sm_cleanup_staging
                for path in $mapped_paths $state_paths; do
                  test ! -e "$path"
                  test ! -e "$path.kitsune-short"
                done
                test "$(cat "$SM_STATE_DIR/.secure-dir.env.new.keep")" = keep
                test "$(cat "$TEST_ROOT/root$SM_POLICY_PATH.kitsune-original-new.keep")" = keep
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_live_policy_legacy_launcher_needs_no_persistent_policy_sidecar(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-legacy-live-policy-") as temp:
            result = run_harness(
                r"""
                SM_SYSTEM_DIR=/system/etc/init/magisk
                sm_real_path() { printf '%s/root%s\n' "$TEST_ROOT" "$1"; }
                is_rootfs() { return 1; }
                # The host fixture's owner stands in for Android root.
                sm_legacy_regular_file() {
                  [ -f "$1" ] && [ ! -L "$1" ] || return 1
                  sm_reject_unsafe_mode "$(bb stat -c %a "$1")"
                }
                rc="$(sm_real_path "$SM_SYSTEM_DIR.rc")"
                policy="$(sm_real_path /vendor/etc/selinux/precompiled_sepolicy)"
                mkdir -p "$(dirname "$rc")" "$(dirname "$policy")"
                printf original-policy >"$policy"
                before="$(sm_sha256_file "$policy")"
                cat >"$rc" <<'RC'
on post-fs-data
    start logd
    exec u:r:su:s0 root root -- /system/etc/init/magisk/magiskpolicy --live --magisk
    exec u:r:su:s0 root root -- /system/etc/init/magisk/magisk64 --auto-selinux --setup-sbin /system/etc/init/magisk /sbin
    exec u:r:su:s0 root root -- /sbin/magisk --auto-selinux --post-fs-data
on nonencrypted
    exec u:r:su:s0 root root -- /sbin/magisk --auto-selinux --service
RC
                chmod 0644 "$rc"
                sm_find_legacy_policy_sidecar
                test -z "$SM_LEGACY_POLICY_PATH"
                test "$(sm_sha256_file "$policy")" = "$before"
                cp "$rc" "$TEST_ROOT/valid.rc"
                printf '    write /vendor/etc/selinux/precompiled_sepolicy modified\n' >>"$rc"
                ! sm_find_legacy_policy_sidecar
                sed 's/--live --magisk/--load \/vendor\/etc\/selinux\/precompiled_sepolicy --save \/vendor\/etc\/selinux\/precompiled_sepolicy --magisk/' \
                  "$TEST_ROOT/valid.rc" >"$rc"
                ! sm_find_legacy_policy_sidecar
                rm "$rc"
                ln -s "$TEST_ROOT/valid.rc" "$rc"
                ! sm_find_legacy_policy_sidecar
                test "$(sm_sha256_file "$policy")" = "$before"
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_legacy_policy_restore_uses_the_recorded_original(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-policy-restore-") as temp:
            result = run_harness(
                r"""
                SM_STATE_DIR="$TEST_ROOT/state"
                SM_ORIGINAL_FILE="$SM_STATE_DIR/originals.tsv"
                SM_POLICY_MUTATED=true
                SM_LEGACY_MIGRATION=false
                SM_POLICY_PATH=/vendor/etc/selinux/precompiled_sepolicy
                policy="$TEST_ROOT/root$SM_POLICY_PATH"
                original="$SM_STATE_DIR/original/policy/data"
                mkdir -p "$(dirname "$policy")" "$(dirname "$original")"
                printf patched-policy >"$policy"
                printf stock-policy >"$original"
                digest="$(sm_digest_path "$original")"
                owner_uid="$(id -u)"
                owner_gid="$(id -g)"
                printf '%s\ttrue\t%s\t12\t0644\t%s\t%s\t-\toriginal/policy/data\n' \
                  "$SM_POLICY_PATH" "$digest" "$owner_uid" "$owner_gid" >"$SM_ORIGINAL_FILE"
                sm_real_path() { printf '%s/root%s\n' "$TEST_ROOT" "$1"; }
                sm_atomic_publish() { mv "$1" "$2"; }

                sm_restore_legacy_policy
                test "$(cat "$policy")" = stock-policy
                test "$(sm_digest_path "$policy")" = "$digest"
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_only_one_cold_boot_attempt_is_allowed_before_rollback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-boot-attempt-") as temp:
            result = run_harness(
                r"""
                SM_STATE=COMMITTED
                SM_COMMIT_BOOT_ID=11111111-1111-1111-1111-111111111111
                SM_BOOT_ATTEMPT_ID=
                LIVE_BOOT_ID=22222222-2222-2222-2222-222222222222
                sm_load_transaction() { return 0; }
                sm_current_boot_id() { printf '%s\n' "$LIVE_BOOT_ID"; }
                sm_write_transaction() { WRITTEN_ATTEMPT="$SM_BOOT_ATTEMPT_ID"; }
                sm_update_state() { SM_STATE="$1"; }

                sm_register_boot_attempt
                test "$SM_BOOT_ATTEMPT_ID" = "$LIVE_BOOT_ID"
                test "$WRITTEN_ATTEMPT" = "$LIVE_BOOT_ID"

                # A duplicate init trigger in the same boot is harmless.
                sm_register_boot_attempt
                test "$SM_STATE" = COMMITTED

                # A different boot without BOOT_VERIFIED is a failed attempt.
                LIVE_BOOT_ID=33333333-3333-3333-3333-333333333333
                if sm_register_boot_attempt; then
                  exit 1
                else
                  test "$?" = 2
                fi
                test "$SM_STATE" = ROLLBACK_REQUIRED
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_receipt_rejects_redirected_paths_and_a_different_live_target(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-receipt-") as temp:
            result = run_harness(
                r"""
                SM_STATE_DIR="$TEST_ROOT/state"
                SM_TRANSACTION_FILE="$SM_STATE_DIR/transaction.env"
                SM_SYSTEM_DIR=/system/etc/init/magisk
                transaction_id=12345678-1234-1234-1234-123456789abc
                install_id=abcdefab-cdef-abcd-efab-cdefabcdefab
                LIVE_ABIS=arm64-v8a
                getprop() {
                  case "$1" in
                    ro.build.fingerprint) printf '%s\n' exact/fingerprint ;;
                    ro.build.version.sdk) printf '%s\n' 32 ;;
                    ro.product.cpu.abilist) printf '%s\n' "$LIVE_ABIS" ;;
                    ro.product.cpu.abi) printf '%s\n' arm64-v8a ;;
                  esac
                }
                b64() { printf '%s' "$1" | base64 | tr -d '\n'; }
                fingerprint="$(printf '%s' exact/fingerprint | bb sha256sum | awk '{ print $1 }')"
                mkdir -p "$SM_STATE_DIR"
                sm_validate_state_storage() { return 0; }
                {
                  printf 'SCHEMA_VERSION=1\n'
                  printf 'INSTALL_ID=%s\n' "$install_id"
                  printf 'TRANSACTION_ID=%s\n' "$transaction_id"
                  printf 'STATE=BOOT_VERIFIED\n'
                  printf 'PRIOR_STATE=UNINSTALLED\n'
                  printf 'FINGERPRINT_SHA256=%s\n' "$fingerprint"
                  printf 'TARGET_API=32\n'
                  printf 'REPORT_SHA256=%064d\n' 0
                  printf 'BACKUP_SHA256=%064d\n' 0
                  printf 'MANIFEST_SHA256=%064d\n' 1
                  printf 'OWNERSHIP_SHA256=%064d\n' 2
                  printf 'ADAPTER_ID_B64=%s\n' "$(b64 mumu-1.4.46)"
                  printf 'TARGET_ABIS_B64=%s\n' "$(b64 arm64-v8a)"
                  printf 'SNAPSHOT_ID_B64=%s\n' "$(b64 snapshot)"
                  printf 'BACKUP_LOCATION_B64=%s\n' "$(b64 external-backup)"
                  printf 'RESTORE_COMMAND_B64=%s\n' "$(b64 'restore exact backup')"
                  printf 'INIT_PATH=/system/etc/init/magisk.rc\n'
                  printf 'POLICY_PATH=\n'
                  printf 'POLICY_SOURCE=\n'
                  printf 'RUNTIME_PATH=/sbin\n'
                  printf 'SELINUX_STRATEGY=disabled\n'
                  printf 'SOURCE_COMMIT=%040d\n' 0
                  printf 'UPSTREAM_BASE=%040d\n' 0
                  printf 'ARTIFACT_SHA256=%064d\n' 0
                  printf 'PRODUCT_VERSION_B64=%s\n' "$(b64 test-version)"
                  printf 'COMMIT_BOOT_ID=11111111-1111-1111-1111-111111111111\n'
                  printf 'ROLLBACK_DIR=%s/rollback/%s\n' "$SM_STATE_DIR" "$transaction_id"
                  printf 'STAGING_PATH=/system/etc/init/.magisk.kitsune-stage-%s\n' "$transaction_id"
                } >"$SM_TRANSACTION_FILE"

                sm_load_transaction

                sed 's#ROLLBACK_DIR=.*#ROLLBACK_DIR=/data/adb/kitsune/system-mode/rollback/../escaped#' \
                  "$SM_TRANSACTION_FILE" >"$SM_TRANSACTION_FILE.bad"
                mv "$SM_TRANSACTION_FILE.bad" "$SM_TRANSACTION_FILE"
                ! sm_load_transaction

                sed "s#ROLLBACK_DIR=.*#ROLLBACK_DIR=$SM_STATE_DIR/rollback/$transaction_id#" \
                  "$SM_TRANSACTION_FILE" >"$SM_TRANSACTION_FILE.good"
                mv "$SM_TRANSACTION_FILE.good" "$SM_TRANSACTION_FILE"
                LIVE_ABIS=x86_64
                ! sm_load_transaction
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_atomic_publish_fault_matrix_preserves_or_reports_the_boundary(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-atomic-") as temp:
            result = run_harness(
                r"""
                stage="$TEST_ROOT/stage"
                destination="$TEST_ROOT/destination"
                for fault in enospc erofs short-write fsync-file rename; do
                  printf new >"$stage"
                  printf old >"$destination"
                  KITSUNE_SYSTEM_MODE_FAIL_AT="$fault:atomic-test"
                  export KITSUNE_SYSTEM_MODE_FAIL_AT
                  ! sm_atomic_publish "$stage" "$destination" atomic-test
                  test "$(cat "$destination")" = old
                done

                printf new >"$stage"
                printf old >"$destination"
                KITSUNE_SYSTEM_MODE_FAIL_AT=fsync-parent:atomic-test
                export KITSUNE_SYSTEM_MODE_FAIL_AT
                ! sm_atomic_publish "$stage" "$destination" atomic-test
                test "$(cat "$destination")" = new

                printf final >"$stage"
                unset KITSUNE_SYSTEM_MODE_FAIL_AT
                sm_atomic_publish "$stage" "$destination" atomic-test
                test "$(cat "$destination")" = final
                test ! -e "$stage"
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_process_death_occurs_only_after_the_publish_is_visible(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-death-") as temp:
            root = Path(temp)
            result = run_harness(
                r"""
                printf committed >"$TEST_ROOT/stage"
                KITSUNE_SYSTEM_MODE_FAIL_AT=process-death:atomic-test
                export KITSUNE_SYSTEM_MODE_FAIL_AT
                sm_atomic_publish "$TEST_ROOT/stage" "$TEST_ROOT/destination" atomic-test
                """,
                root,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertEqual("committed", (root / "destination").read_text(encoding="utf-8"))

    def test_state_metadata_snapshot_restores_an_upgrade_exactly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-state-") as temp:
            result = run_harness(
                r"""
                SM_STATE_DIR="$TEST_ROOT/state"
                SM_TRANSACTION_FILE="$SM_STATE_DIR/transaction.env"
                SM_MANIFEST_COPY="$SM_STATE_DIR/install-manifest.json"
                SM_OWNERSHIP_FILE="$SM_STATE_DIR/ownership.tsv"
                SM_ORIGINAL_FILE="$SM_STATE_DIR/originals.tsv"
                SM_JOURNAL_FILE="$SM_STATE_DIR/journal.tsv"
                SM_BOOT_PROOF="$SM_STATE_DIR/boot-verified.env"
                SM_TRANSACTION_ID=12345678-1234-1234-1234-123456789abc
                SM_ROLLBACK_DIR="$SM_STATE_DIR/rollback/$SM_TRANSACTION_ID"
                mkdir -p "$SM_STATE_DIR/original"
                test_write_receipt "$SM_TRANSACTION_FILE" \
                  bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb \
                  cccccccc-cccc-cccc-cccc-cccccccccccc BOOT_VERIFIED
                printf prior-manifest >"$SM_MANIFEST_COPY"
                printf prior-ownership >"$SM_OWNERSHIP_FILE"
                printf prior-originals >"$SM_ORIGINAL_FILE"
                printf prior-journal >"$SM_JOURNAL_FILE"
                printf prior-proof >"$SM_BOOT_PROOF"
                printf prior-backup >"$SM_STATE_DIR/original/data"

                sm_snapshot_state_metadata
                test_write_current_receipt ROLLING_BACK
                printf new-manifest >"$SM_MANIFEST_COPY"
                rm -f "$SM_OWNERSHIP_FILE" "$SM_BOOT_PROOF"
                printf new-backup >"$SM_STATE_DIR/original/data"
                sm_restore_state_metadata

                test "$(sm_get TRANSACTION_ID "$SM_TRANSACTION_FILE")" = "$SM_TRANSACTION_ID"
                test "$(cat "$SM_MANIFEST_COPY")" = prior-manifest
                test "$(cat "$SM_OWNERSHIP_FILE")" = prior-ownership
                test "$(cat "$SM_ORIGINAL_FILE")" = prior-originals
                test "$(cat "$SM_JOURNAL_FILE")" = prior-journal
                test "$(cat "$SM_BOOT_PROOF")" = prior-proof
                test "$(cat "$SM_STATE_DIR/original/data")" = prior-backup
                sm_finish_rollback_storage
                test "$(sm_get TRANSACTION_ID "$SM_TRANSACTION_FILE")" = \
                  cccccccc-cccc-cccc-cccc-cccccccccccc
                test "$(sm_get STATE "$SM_TRANSACTION_FILE")" = BOOT_VERIFIED
                test ! -e "$SM_ROLLBACK_DIR"
                test ! -e "$SM_ROLLBACK_TERMINAL"
                test ! -e "$SM_PRIOR_TRANSACTION"
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_reverse_snapshot_restores_every_persistent_root_and_receipt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-rollback-") as temp:
            result = run_harness(
                r"""
                SM_STATE_DIR="$TEST_ROOT/state"
                SM_TRANSACTION_FILE="$SM_STATE_DIR/transaction.env"
                SM_MANIFEST_COPY="$SM_STATE_DIR/install-manifest.json"
                SM_OWNERSHIP_FILE="$SM_STATE_DIR/ownership.tsv"
                SM_ORIGINAL_FILE="$SM_STATE_DIR/originals.tsv"
                SM_JOURNAL_FILE="$SM_STATE_DIR/journal.tsv"
                SM_BOOT_PROOF="$SM_STATE_DIR/boot-verified.env"
                SM_SYSTEM_DIR=/system/etc/init/magisk
                SM_INIT_PATH=/system/etc/init/magisk.rc
                SM_POLICY_PATH=
                SM_TRANSACTION_ID=12345678-1234-1234-1234-123456789abc
                SM_STAGING_PATH=/system/etc/init/.magisk.kitsune-stage-test
                SM_ROLLBACK_DIR="$SM_STATE_DIR/rollback/$SM_TRANSACTION_ID"
                SM_PRIOR_STATE=BOOT_VERIFIED
                sm_configure_rescue_paths
                SM_RESCUE_PAYLOAD="$SM_RESCUE_PAYLOAD_PREFIX/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
                sm_real_path() { printf '%s%s\n' "$TEST_ROOT/root" "$1"; }
                sm_update_state() { test_update_state "$1"; }

                mkdir -p "$SM_STATE_DIR/original" "$TEST_ROOT/root/system/etc/init/magisk" \
                  "$TEST_ROOT/root/data/adb/magisk" "$TEST_ROOT/root/system/addon.d/magisk" \
                  "$TEST_ROOT/root$SM_RESCUE_PAYLOAD"
                test_write_receipt "$SM_TRANSACTION_FILE" \
                  bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb \
                  cccccccc-cccc-cccc-cccc-cccccccccccc BOOT_VERIFIED
                printf prior-manifest >"$SM_MANIFEST_COPY"
                printf prior-ownership >"$SM_OWNERSHIP_FILE"
                printf prior-originals >"$SM_ORIGINAL_FILE"
                printf prior-journal >"$SM_JOURNAL_FILE"
                printf prior-proof >"$SM_BOOT_PROOF"
                printf prior-backup >"$SM_STATE_DIR/original/data"
                printf payload-old >"$TEST_ROOT/root/system/etc/init/magisk/file"
                printf legacy-old >"$TEST_ROOT/root/system/etc/init/magisk.rc"
                printf bootanim-old >"$TEST_ROOT/root/system/etc/init/bootanim.rc"
                printf bootanim-gz-old >"$TEST_ROOT/root/system/etc/init/bootanim.rc.gz"
                printf runtime-old >"$TEST_ROOT/root/data/adb/magisk/file"
                printf addon-old >"$TEST_ROOT/root/system/addon.d/99-magisk.sh"
                printf addon-dir-old >"$TEST_ROOT/root/system/addon.d/magisk/file"
                printf rescue-old >"$TEST_ROOT/root$SM_RESCUE_PAYLOAD/busybox"
                printf rescue-rc-old >"$TEST_ROOT/root$SM_RESCUE_RC"

                test_prepare_secure_dir
                sm_snapshot_state_metadata
                sm_snapshot_all
                test_write_current_receipt STAGED
                printf payload-new >"$TEST_ROOT/root/system/etc/init/magisk/file"
                printf legacy-new >"$TEST_ROOT/root/system/etc/init/magisk.rc"
                printf bootanim-new >"$TEST_ROOT/root/system/etc/init/bootanim.rc"
                printf runtime-new >"$TEST_ROOT/root/data/adb/magisk/file"
                printf addon-new >"$TEST_ROOT/root/system/addon.d/99-magisk.sh"
                printf addon-dir-new >"$TEST_ROOT/root/system/addon.d/magisk/file"
                printf rescue-new >"$TEST_ROOT/root$SM_RESCUE_PAYLOAD/busybox"
                printf rescue-rc-new >"$TEST_ROOT/root$SM_RESCUE_RC"

                sm_restore_snapshot
                test "$(cat "$TEST_ROOT/root/system/etc/init/magisk/file")" = payload-old
                test "$(cat "$TEST_ROOT/root/system/etc/init/magisk.rc")" = legacy-old
                test "$(cat "$TEST_ROOT/root/system/etc/init/bootanim.rc")" = bootanim-old
                test "$(cat "$TEST_ROOT/root/system/etc/init/bootanim.rc.gz")" = bootanim-gz-old
                test "$(cat "$TEST_ROOT/root/data/adb/magisk/file")" = runtime-old
                test "$(cat "$TEST_ROOT/root/system/addon.d/99-magisk.sh")" = addon-old
                test "$(cat "$TEST_ROOT/root/system/addon.d/magisk/file")" = addon-dir-old
                test "$(cat "$TEST_ROOT/root$SM_RESCUE_PAYLOAD/busybox")" = rescue-old
                test "$(cat "$TEST_ROOT/root$SM_RESCUE_RC")" = rescue-rc-old
                test "$(sm_get TRANSACTION_ID "$SM_TRANSACTION_FILE")" = \
                  cccccccc-cccc-cccc-cccc-cccccccccccc
                test "$(sm_get STATE "$SM_TRANSACTION_FILE")" = BOOT_VERIFIED
                test ! -e "$SM_ROLLBACK_DIR"
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_reverse_snapshot_accepts_an_optional_tree_with_no_parent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-absent-") as temp:
            result = run_harness(
                r"""
                SM_STATE_DIR="$TEST_ROOT/state"
                SM_TRANSACTION_FILE="$SM_STATE_DIR/transaction.env"
                SM_MANIFEST_COPY="$SM_STATE_DIR/install-manifest.json"
                SM_OWNERSHIP_FILE="$SM_STATE_DIR/ownership.tsv"
                SM_ORIGINAL_FILE="$SM_STATE_DIR/originals.tsv"
                SM_JOURNAL_FILE="$SM_STATE_DIR/journal.tsv"
                SM_BOOT_PROOF="$SM_STATE_DIR/boot-verified.env"
                SM_SYSTEM_DIR=/system/etc/init/magisk
                SM_INIT_PATH=/system/etc/init/magisk.rc
                SM_POLICY_PATH=
                SM_TRANSACTION_ID=12345678-1234-1234-1234-123456789abc
                SM_STAGING_PATH=/system/etc/init/.magisk.kitsune-stage-test
                SM_ROLLBACK_DIR="$SM_STATE_DIR/rollback/$SM_TRANSACTION_ID"
                sm_real_path() { printf '%s%s\n' "$TEST_ROOT/root" "$1"; }
                sm_update_state() { test_update_state "$1"; }

                mkdir -p "$SM_STATE_DIR/original" \
                  "$TEST_ROOT/root/system/etc/init/magisk" \
                  "$TEST_ROOT/root/data/adb/magisk"
                test_write_receipt "$SM_TRANSACTION_FILE" \
                  bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb \
                  cccccccc-cccc-cccc-cccc-cccccccccccc BOOT_VERIFIED
                printf payload-old >"$TEST_ROOT/root/system/etc/init/magisk/file"
                printf init-old >"$TEST_ROOT/root/system/etc/init/magisk.rc"
                printf runtime-old >"$TEST_ROOT/root/data/adb/magisk/file"
                test_prepare_secure_dir
                sm_snapshot_state_metadata
                sm_snapshot_all
                test_write_current_receipt STAGED

                # Model BusyBox fsync: unlike the normal harness stub, an
                # absent path is an error. /system/addon.d never existed.
                sm_fsync() {
                  local path
                  for path in "$@"; do
                    [ "$path" = /data/adb ] && continue
                    [ -e "$path" ] || return 1
                  done
                }
                sm_restore_snapshot
                test ! -e "$TEST_ROOT/root/system/addon.d"
                test "$(cat "$TEST_ROOT/root/system/etc/init/magisk/file")" = payload-old
                test "$(cat "$TEST_ROOT/root/data/adb/magisk/file")" = runtime-old
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_uninstall_keeps_rescue_until_the_prior_boot_tree_is_restored(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-uninstall-rescue-") as temp:
            result = run_harness(
                r"""
                SM_STATE_DIR="$TEST_ROOT/state"
                SM_ORIGINAL_FILE="$SM_STATE_DIR/originals.tsv"
                SM_OWNERSHIP_FILE="$SM_STATE_DIR/ownership.tsv"
                SM_MANIFEST_SHA256="$(printf '%064d' 0)"
                SM_SYSTEM_DIR=/system/etc/init/magisk
                SM_INIT_PATH=/system/etc/init/magisk.rc
                SM_POLICY_PATH=
                SM_POLICY_MUTATED=false
                sm_configure_rescue_paths
                sm_real_path() { printf '%s/root%s\n' "$TEST_ROOT" "$1"; }
                mkdir -p "$SM_STATE_DIR" "$TEST_ROOT/root$SM_SYSTEM_DIR" "$TEST_ROOT/root$SM_RESCUE_DIR"
                printf payload >"$TEST_ROOT/root$SM_SYSTEM_DIR/file"
                printf main-rc >"$TEST_ROOT/root$SM_INIT_PATH"
                printf rescue >"$TEST_ROOT/root$SM_RESCUE_DIR/busybox"
                printf rescue-rc >"$TEST_ROOT/root$SM_RESCUE_RC"
                : >"$SM_OWNERSHIP_FILE.new"
                sm_add_owned_file "$SM_SYSTEM_DIR/file" "$TEST_ROOT/root$SM_SYSTEM_DIR/file"
                sm_add_owned_file "$SM_INIT_PATH" "$TEST_ROOT/root$SM_INIT_PATH"
                sm_add_owned_file "$SM_RESCUE_DIR/busybox" "$TEST_ROOT/root$SM_RESCUE_DIR/busybox"
                sm_add_owned_file "$SM_RESCUE_RC" "$TEST_ROOT/root$SM_RESCUE_RC"
                mv "$SM_OWNERSHIP_FILE.new" "$SM_OWNERSHIP_FILE"
                SM_OWNERSHIP_SHA256="$(sm_sha256_file "$SM_OWNERSHIP_FILE")"
                {
                  printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/payload/data\n' "$SM_SYSTEM_DIR"
                  printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/init_rc/data\n' "$SM_INIT_PATH"
                  printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/rescue_rc/data\n' "$SM_RESCUE_RC"
                  printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/rescue_dir/data\n' "$SM_RESCUE_DIR"
                } >"$SM_ORIGINAL_FILE"

                sm_restore_originals ordinary
                test ! -e "$TEST_ROOT/root$SM_SYSTEM_DIR"
                test ! -e "$TEST_ROOT/root$SM_INIT_PATH"
                test -f "$TEST_ROOT/root$SM_RESCUE_RC"
                test -x "$TEST_ROOT/root$SM_RESCUE_DIR"/busybox || test -f "$TEST_ROOT/root$SM_RESCUE_DIR"/busybox

                sm_restore_originals rescue
                test ! -e "$TEST_ROOT/root$SM_RESCUE_RC"
                test ! -e "$TEST_ROOT/root$SM_RESCUE_DIR"
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_repeated_process_death_at_every_uninstall_boundary_is_idempotent(self) -> None:
        preserved = (
            ("/cache/magisk.log.bak", "magisk_log_bak", "file"),
            ("/cache/magisk.log", "magisk_log", "file"),
            ("/data/adb/sepolicy.rule", "preinit_rule", "file"),
            ("/data/adb/service.d", "service", "dir"),
            ("/data/adb/post-fs-data.d", "post_fs_data", "dir"),
            ("/data/adb/modules_update", "modules_update", "dir"),
            ("/data/adb/modules", "modules", "dir"),
            ("/data/adb/magisk.db-shm", "magisk_db_shm", "file"),
            ("/data/adb/magisk.db-wal", "magisk_db_wal", "file"),
            ("/data/adb/magisk.db", "magisk_db", "file"),
        )
        ordinary = (
            ("/system/addon.d/magisk", "addon_dir", "dir"),
            ("/system/addon.d/99-magisk.sh", "addon_script", "file"),
            ("/data/adb/magisk", "runtime", "dir"),
            ("/system/etc/selinux/plat_sepolicy.cil", "policy", "file"),
            ("/system/etc/init/magisk", "payload", "dir"),
            ("/system/etc/init/magisk.rc", "legacy_rc", "file"),
            ("/system/etc/init/hw/magisk.rc", "init_rc", "file"),
        )
        rescue = (
            ("/system/etc/init/hw/00-kitsune-magisk-rescue.rc", "rescue_rc", "file"),
            ("/system/etc/init/hw/.kitsune-system-mode-rescue", "rescue_dir", "dir"),
        )
        inventory = preserved + ordinary + rescue
        common = r"""
            SM_STATE_DIR="$TEST_ROOT/state"
            SM_ORIGINAL_FILE="$SM_STATE_DIR/originals.tsv"
            SM_SYSTEM_DIR=/system/etc/init/magisk
            SM_INIT_PATH=/system/etc/init/hw/magisk.rc
            SM_POLICY_PATH=/system/etc/selinux/plat_sepolicy.cil
            SM_POLICY_MUTATED=true
            sm_configure_rescue_paths
            sm_real_path() { printf '%s/root%s\n' "$TEST_ROOT" "$1"; }
            sm_destructive_target_safe() { return 0; }
            sm_assert_owned_restore_target() { return 0; }
        """
        setup_lines = [common, 'mkdir -p "$SM_STATE_DIR"']
        for path, label, kind in inventory:
            setup_lines.extend(
                (
                    f'path="{path}"',
                    'real="$(sm_real_path "$path")"',
                    (
                        'mkdir -p "$real"; printf live >"$real/owned"'
                        if kind == "dir"
                        else 'mkdir -p "$(dirname "$real")"; printf live >"$real"'
                    ),
                    (
                        f"printf '%s\\tfalse\\t-\\t0\\t-\\t-\\t-\\t-\\toriginal/{label}/data\\n' "
                        '"$path" >>"$SM_ORIGINAL_FILE"'
                    ),
                )
            )
        setup = "\n".join(setup_lines)

        def assert_rescue_present(root: Path) -> None:
            self.assertTrue(
                (root / "root/system/etc/init/hw/00-kitsune-magisk-rescue.rc").is_file()
            )
            self.assertTrue(
                (root / "root/system/etc/init/hw/.kitsune-system-mode-rescue/owned").is_file()
            )

        ordinary_verify = common + "\nsm_restore_originals ordinary\n"
        ordinary_verify += "\n".join(
            f'test ! -e "$(sm_real_path "{path}")"' for path, _, _ in ordinary
        )
        ordinary_verify += "\n" + "\n".join(
            f'test -e "$(sm_real_path "{path}")"' for path, _, _ in preserved
        )
        ordinary_verify += r"""
            test -f "$(sm_real_path "$SM_RESCUE_RC")"
            test -f "$(sm_real_path "$SM_RESCUE_DIR")/owned"
        """

        for index, (path, label, _) in enumerate(ordinary):
            with self.subTest(scope="ordinary", boundary=label):
                with tempfile.TemporaryDirectory(prefix=f"kitsune-uninstall-{label}-") as temp:
                    root = Path(temp)
                    prepared = run_harness(setup, root)
                    self.assertEqual(0, prepared.returncode, prepared.stderr)
                    first = run_harness(
                        common
                        + f'\nKITSUNE_SYSTEM_MODE_FAIL_AT="process-death:uninstall:{path}"\n'
                        + "sm_restore_originals ordinary\n",
                        root,
                    )
                    self.assertNotEqual(0, first.returncode)
                    assert_rescue_present(root)

                    second_path = ordinary[(index + 1) % len(ordinary)][0]
                    second = run_harness(
                        common
                        + f'\nKITSUNE_SYSTEM_MODE_FAIL_AT="process-death:uninstall:{second_path}"\n'
                        + "sm_restore_originals ordinary\n",
                        root,
                    )
                    self.assertNotEqual(0, second.returncode)
                    assert_rescue_present(root)
                    final = run_harness(ordinary_verify, root)
                    self.assertEqual(0, final.returncode, final.stderr)

        rescue_verify = common + r"""
            sm_restore_originals rescue
            test ! -e "$(sm_real_path "$SM_RESCUE_RC")"
            test ! -e "$(sm_real_path "$SM_RESCUE_DIR")"
            test -f "$(sm_real_path "$SM_INIT_PATH")"
        """
        for index, (path, label, _) in enumerate(rescue):
            with self.subTest(scope="rescue", boundary=label):
                with tempfile.TemporaryDirectory(prefix=f"kitsune-rescue-remove-{label}-") as temp:
                    root = Path(temp)
                    prepared = run_harness(setup, root)
                    self.assertEqual(0, prepared.returncode, prepared.stderr)
                    first = run_harness(
                        common
                        + f'\nKITSUNE_SYSTEM_MODE_FAIL_AT="process-death:uninstall:{path}"\n'
                        + "sm_restore_originals rescue\n",
                        root,
                    )
                    self.assertNotEqual(0, first.returncode)
                    self.assertTrue(
                        (root / "root/system/etc/init/hw/magisk.rc").is_file()
                    )
                    second_path = rescue[(index + 1) % len(rescue)][0]
                    second = run_harness(
                        common
                        + f'\nKITSUNE_SYSTEM_MODE_FAIL_AT="process-death:uninstall:{second_path}"\n'
                        + "sm_restore_originals rescue\n",
                        root,
                    )
                    self.assertNotEqual(0, second.returncode)
                    self.assertTrue(
                        (root / "root/system/etc/init/hw/magisk.rc").is_file()
                    )
                    final = run_harness(rescue_verify, root)
                    self.assertEqual(0, final.returncode, final.stderr)

    def test_repeated_process_death_at_every_rollback_boundary_preserves_a_boot_path(self) -> None:
        labels = (
            "magisk_log_bak",
            "magisk_log",
            "preinit_rule",
            "service",
            "post_fs_data",
            "modules_update",
            "modules",
            "magisk_db_shm",
            "magisk_db_wal",
            "magisk_db",
            "addon_dir",
            "addon_script",
            "runtime",
            "bootanim_gz",
            "bootanim",
            "payload",
            "init_rc",
            "rescue_rc",
            "rescue_dir",
        )
        common = r"""
            SM_STATE_DIR="$TEST_ROOT/state"
            SM_TRANSACTION_FILE="$SM_STATE_DIR/transaction.env"
            SM_MANIFEST_COPY="$SM_STATE_DIR/install-manifest.json"
            SM_OWNERSHIP_FILE="$SM_STATE_DIR/ownership.tsv"
            SM_ORIGINAL_FILE="$SM_STATE_DIR/originals.tsv"
            SM_JOURNAL_FILE="$SM_STATE_DIR/journal.tsv"
            SM_BOOT_PROOF="$SM_STATE_DIR/boot-verified.env"
            SM_SYSTEM_DIR=/system/etc/init/magisk
            SM_INIT_PATH=/system/etc/init/magisk.rc
            SM_POLICY_PATH=
            SM_TRANSACTION_ID=12345678-1234-1234-1234-123456789abc
            SM_STAGING_PATH=/system/etc/init/.magisk.kitsune-stage-test
            SM_ROLLBACK_DIR="$SM_STATE_DIR/rollback/$SM_TRANSACTION_ID"
            SM_PRIOR_STATE=BOOT_VERIFIED
            sm_configure_rescue_paths
            SM_RESCUE_PAYLOAD="$SM_RESCUE_PAYLOAD_PREFIX/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
            sm_real_path() { printf '%s/root%s\n' "$TEST_ROOT" "$1"; }
            sm_update_state() { test_update_state "$1"; }
            # Aggregate snapshot stability has a dedicated behavioral test;
            # this matrix isolates reverse-publication death boundaries.
            sm_verify_snapshot_sources() { return 0; }
        """
        setup = common + r"""
            mkdir -p "$SM_STATE_DIR/original" "$TEST_ROOT/root$SM_SYSTEM_DIR" \
              "$TEST_ROOT/root/data/adb/magisk" "$TEST_ROOT/root/system/addon.d/magisk" \
              "$TEST_ROOT/root$SM_RESCUE_PAYLOAD"
            test_write_receipt "$SM_TRANSACTION_FILE" \
              bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb \
              cccccccc-cccc-cccc-cccc-cccccccccccc BOOT_VERIFIED
            printf prior-manifest >"$SM_MANIFEST_COPY"
            printf prior-ownership >"$SM_OWNERSHIP_FILE"
            printf prior-originals >"$SM_ORIGINAL_FILE"
            printf prior-journal >"$SM_JOURNAL_FILE"
            printf prior-proof >"$SM_BOOT_PROOF"
            printf prior-backup >"$SM_STATE_DIR/original/data"
            printf old-payload >"$TEST_ROOT/root$SM_SYSTEM_DIR/file"
            printf old-init >"$TEST_ROOT/root$SM_INIT_PATH"
            printf old-bootanim >"$TEST_ROOT/root/system/etc/init/bootanim.rc"
            printf old-bootanim-gz >"$TEST_ROOT/root/system/etc/init/bootanim.rc.gz"
            printf old-runtime >"$TEST_ROOT/root/data/adb/magisk/file"
            printf old-addon >"$TEST_ROOT/root/system/addon.d/99-magisk.sh"
            printf old-addon-dir >"$TEST_ROOT/root/system/addon.d/magisk/file"
            printf old-rescue >"$TEST_ROOT/root$SM_RESCUE_PAYLOAD/busybox"
            printf old-rescue-rc >"$TEST_ROOT/root$SM_RESCUE_RC"
            test_prepare_secure_dir
            sm_snapshot_state_metadata
            sm_snapshot_all
            test_write_current_receipt STAGED
            printf new-payload >"$TEST_ROOT/root$SM_SYSTEM_DIR/file"
            printf new-init >"$TEST_ROOT/root$SM_INIT_PATH"
            printf new-bootanim >"$TEST_ROOT/root/system/etc/init/bootanim.rc"
            printf new-bootanim-gz >"$TEST_ROOT/root/system/etc/init/bootanim.rc.gz"
            printf new-runtime >"$TEST_ROOT/root/data/adb/magisk/file"
            printf new-addon >"$TEST_ROOT/root/system/addon.d/99-magisk.sh"
            printf new-addon-dir >"$TEST_ROOT/root/system/addon.d/magisk/file"
            printf new-rescue >"$TEST_ROOT/root$SM_RESCUE_PAYLOAD/busybox"
            printf new-rescue-rc >"$TEST_ROOT/root$SM_RESCUE_RC"
        """
        verify = common + r"""
            sm_restore_snapshot
            test "$(cat "$TEST_ROOT/root$SM_SYSTEM_DIR/file")" = old-payload
            test "$(cat "$TEST_ROOT/root$SM_INIT_PATH")" = old-init
            test "$(cat "$TEST_ROOT/root/data/adb/magisk/file")" = old-runtime
            test "$(cat "$TEST_ROOT/root$SM_RESCUE_PAYLOAD/busybox")" = old-rescue
            test "$(cat "$TEST_ROOT/root$SM_RESCUE_RC")" = old-rescue-rc
            test "$(sm_get TRANSACTION_ID "$SM_TRANSACTION_FILE")" = \
              cccccccc-cccc-cccc-cccc-cccccccccccc
            test "$(sm_get STATE "$SM_TRANSACTION_FILE")" = BOOT_VERIFIED
            test ! -e "$SM_ROLLBACK_DIR"
        """

        for index, label in enumerate(labels):
            with self.subTest(boundary=label):
                with tempfile.TemporaryDirectory(prefix=f"kitsune-power-loss-{label}-") as temp:
                    root = Path(temp)
                    first = run_harness(
                        setup
                        + f'\nKITSUNE_SYSTEM_MODE_FAIL_AT="process-death:rollback:{label}"\n'
                        + "sm_restore_snapshot\n",
                        root,
                    )
                    self.assertNotEqual(0, first.returncode)
                    main_ok = (
                        (root / "root/system/etc/init/magisk/file").is_file()
                        and (root / "root/system/etc/init/magisk.rc").is_file()
                    )
                    rescue_ok = (
                        (root / "root/system/etc/init/.kitsune-system-mode-rescue/versions/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/busybox").is_file()
                        and (root / "root/system/etc/init/00-kitsune-magisk-rescue.rc").is_file()
                    )
                    self.assertTrue(main_ok or rescue_ok)

                    second_label = labels[(index + 1) % len(labels)]
                    second = run_harness(
                        common
                        + f'\nKITSUNE_SYSTEM_MODE_FAIL_AT="process-death:rollback:{second_label}"\n'
                        + "sm_restore_snapshot\n",
                        root,
                    )
                    self.assertNotEqual(0, second.returncode)
                    final = run_harness(verify, root)
                    self.assertEqual(0, final.returncode, final.stderr)

    def test_final_journal_covers_creates_replacements_removals_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-journal-") as temp:
            result = run_harness(
                r"""
                SM_STATE_DIR="$TEST_ROOT/state"
                SM_OWNERSHIP_FILE="$SM_STATE_DIR/ownership.tsv"
                SM_JOURNAL_FILE="$SM_STATE_DIR/journal.tsv"
                SM_SYSTEM_DIR=/system/etc/init/magisk
                SM_INIT_PATH=/system/etc/init/magisk.rc
                SM_POLICY_PATH=
                SM_POLICY_MUTATED=false
                SM_LEGACY_MIGRATION=false
                SM_ACTIVE_PAYLOAD=/system/etc/init/magisk/versions/12345678-1234-1234-1234-123456789abc
                SM_ROLLBACK_DIR="$SM_STATE_DIR/rollback/transaction"
                sm_real_path() { printf '%s/root%s\n' "$TEST_ROOT" "$1"; }

                mkdir -p \
                  "$SM_ROLLBACK_DIR/payload/data/versions/old" \
                  "$SM_ROLLBACK_DIR/runtime/data" \
                  "$SM_ROLLBACK_DIR/init_rc" \
                  "$TEST_ROOT/root$SM_ACTIVE_PAYLOAD" \
                  "$TEST_ROOT/root/data/adb/magisk" \
                  "$TEST_ROOT/root/system/etc/init"
                printf old-payload >"$SM_ROLLBACK_DIR/payload/data/versions/old/file"
                printf old-manifest >"$SM_ROLLBACK_DIR/payload/data/install-manifest.json"
                printf old-runtime >"$SM_ROLLBACK_DIR/runtime/data/replaced"
                printf removed-runtime >"$SM_ROLLBACK_DIR/runtime/data/removed"
                printf old-init >"$SM_ROLLBACK_DIR/init_rc/data"
                printf new-payload >"$TEST_ROOT/root$SM_ACTIVE_PAYLOAD/file"
                printf new-runtime >"$TEST_ROOT/root/data/adb/magisk/replaced"
                printf created-runtime >"$TEST_ROOT/root/data/adb/magisk/created"
                printf new-init >"$TEST_ROOT/root$SM_INIT_PATH"

                mkdir -p "$SM_STATE_DIR"
                : >"$SM_OWNERSHIP_FILE.new"
                sm_add_owned_file "$SM_ACTIVE_PAYLOAD/file" "$TEST_ROOT/root$SM_ACTIVE_PAYLOAD/file"
                sm_add_owned_file /data/adb/magisk/replaced "$TEST_ROOT/root/data/adb/magisk/replaced"
                sm_add_owned_file /data/adb/magisk/created "$TEST_ROOT/root/data/adb/magisk/created"
                sm_add_owned_file "$SM_INIT_PATH" "$TEST_ROOT/root$SM_INIT_PATH"
                mv "$SM_OWNERSHIP_FILE.new" "$SM_OWNERSHIP_FILE"

                sm_generate_journal
                awk -F '\t' -v path="$SM_ACTIVE_PAYLOAD/file" \
                  '$3 == "create" && $4 == path && $5 == "-" && $7 == "true" { found=1 } END { exit !found }' "$SM_JOURNAL_FILE"
                awk -F '\t' '$2 == "init-published" && $3 == "replace" && $4 == "/system/etc/init/magisk.rc" && $5 != "-" && $6 != "-" { found=1 } END { exit !found }' "$SM_JOURNAL_FILE"
                awk -F '\t' '$2 == "runtime-published" && $3 == "remove" && $4 == "/data/adb/magisk/removed" && $5 != "-" && $6 == "-" { found=1 } END { exit !found }' "$SM_JOURNAL_FILE"
                awk -F '\t' '$2 == "superseded-payload-removed" && $3 == "remove" && $4 == "/system/etc/init/magisk/versions/old/file" { found=1 } END { exit !found }' "$SM_JOURNAL_FILE"
                awk -F '\t' '$2 == "manifest-published" && $3 == "replace" && $4 == "/system/etc/init/magisk/install-manifest.json" && $5 != "-" && $6 == "-" { found=1 } END { exit !found }' "$SM_JOURNAL_FILE"
                awk -F '\t' '$1 != NR || seen[$4]++ { exit 1 }' "$SM_JOURNAL_FILE"
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_failure_journal_is_durable_before_mutation_and_marks_boundaries(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-journal-plan-") as temp:
            result = run_harness(
                r"""
                SM_STATE_DIR="$TEST_ROOT/state"
                SM_JOURNAL_FILE="$SM_STATE_DIR/journal.tsv"
                SM_SYSTEM_DIR=/system/etc/init/magisk
                SM_INIT_PATH=/system/etc/init/magisk.rc
                SM_POLICY_PATH=
                SM_POLICY_MUTATED=false
                SM_LEGACY_MIGRATION=false
                SM_ACTIVE_PAYLOAD=/system/etc/init/magisk/versions/12345678-1234-1234-1234-123456789abc
                SM_ROLLBACK_DIR="$SM_STATE_DIR/rollback/transaction"
                sm_real_path() { printf '%s/root%s\n' "$TEST_ROOT" "$1"; }
                mkdir -p "$SM_STATE_DIR" "$SM_ROLLBACK_DIR" "$TEST_ROOT/root/system/etc/init/magisk/versions"

                sm_initialize_journal
                awk -F '\t' -v path="$SM_ACTIVE_PAYLOAD" \
                  '$2 == "version-published" && $4 == path && $7 == "false" { found=1 } END { exit !found }' "$SM_JOURNAL_FILE"
                test "$(awk -F '\t' '$7 == "true" { count++ } END { print count + 0 }' "$SM_JOURNAL_FILE")" = 0

                mkdir -p "$TEST_ROOT/root$SM_ACTIVE_PAYLOAD"
                printf payload >"$TEST_ROOT/root$SM_ACTIVE_PAYLOAD/file"
                sm_journal_mark version-published "$SM_ACTIVE_PAYLOAD"
                awk -F '\t' -v path="$SM_ACTIVE_PAYLOAD" \
                  '$2 == "version-published" && $4 == path && $6 != "-" && $7 == "true" { found=1 } END { exit !found }' "$SM_JOURNAL_FILE"
                awk -F '\t' '$2 == "init-published" && $7 == "false" { found=1 } END { exit !found }' "$SM_JOURNAL_FILE"
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_exact_ownership_rejects_empty_directories_and_special_nodes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-ownership-") as temp:
            result = run_harness(
                r"""
                SM_SYSTEM_DIR=/system/etc/init/magisk
                SM_OWNERSHIP_FILE="$TEST_ROOT/ownership.tsv"
                root="$TEST_ROOT/root$SM_SYSTEM_DIR"
                sm_real_path() { printf '%s/root%s\n' "$TEST_ROOT" "$1"; }
                mkdir -p "$root/owned" "$root/unowned-empty"
                printf payload >"$root/owned/file"
                : >"$SM_OWNERSHIP_FILE.new"
                sm_add_owned_file "$SM_SYSTEM_DIR/owned/file" "$root/owned/file"
                mv "$SM_OWNERSHIP_FILE.new" "$SM_OWNERSHIP_FILE"

                ! sm_assert_no_unowned_files
                rmdir "$root/unowned-empty"
                sm_assert_no_unowned_files

                mkfifo "$root/unowned-fifo"
                ! sm_assert_no_unowned_files
                unlink "$root/unowned-fifo"
                sm_assert_no_unowned_files
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_ownership_inventory_is_digest_bound_structural_and_link_safe(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-ownership-digest-") as temp:
            result = run_harness(
                r"""
                SM_SYSTEM_DIR=/system/etc/init/magisk
                SM_INIT_PATH=/system/etc/init/magisk.rc
                SM_POLICY_PATH=
                SM_POLICY_MUTATED=false
                sm_configure_rescue_paths
                SM_OWNERSHIP_FILE="$TEST_ROOT/ownership.tsv"
                sm_real_path() { printf '%s/root%s\n' "$TEST_ROOT" "$1"; }
                payload="$TEST_ROOT/root$SM_SYSTEM_DIR/payload"
                mkdir -p "$(dirname "$payload")"
                printf payload >"$payload"
                : >"$SM_OWNERSHIP_FILE.new"
                sm_add_owned_file "$SM_SYSTEM_DIR/payload" "$payload"
                mv "$SM_OWNERSHIP_FILE.new" "$SM_OWNERSHIP_FILE"
                chmod 0600 "$SM_OWNERSHIP_FILE"
                SM_OWNERSHIP_SHA256="$(sm_sha256_file "$SM_OWNERSHIP_FILE")"
                sm_validate_ownership_inventory
                sm_verify_owned
                original_row="$(cat "$SM_OWNERSHIP_FILE")"

                # The receipt digest detects a byte edit before row parsing.
                printf '%s\n%s\n' "$original_row" "$original_row" >"$SM_OWNERSHIP_FILE"
                ! sm_validate_ownership_inventory

                # Even a recomputed digest cannot authorize duplicates,
                # injected paths, malformed field counts, or unknown kinds.
                SM_OWNERSHIP_SHA256="$(sm_sha256_file "$SM_OWNERSHIP_FILE")"
                ! sm_validate_ownership_inventory
                remainder="${original_row#*"$SM_TAB"}"
                printf '/etc/passwd\t%s\n' "$remainder" >"$SM_OWNERSHIP_FILE"
                SM_OWNERSHIP_SHA256="$(sm_sha256_file "$SM_OWNERSHIP_FILE")"
                ! sm_validate_ownership_inventory
                printf '%s\textra\n' "$original_row" >"$SM_OWNERSHIP_FILE"
                SM_OWNERSHIP_SHA256="$(sm_sha256_file "$SM_OWNERSHIP_FILE")"
                ! sm_validate_ownership_inventory
                printf '%s\n' "$original_row" | awk -F '\t' 'BEGIN { OFS="\t" } {$8="directory"; print}' \
                  >"$SM_OWNERSHIP_FILE"
                SM_OWNERSHIP_SHA256="$(sm_sha256_file "$SM_OWNERSHIP_FILE")"
                ! sm_validate_ownership_inventory

                # A regular-file row never accepts a symlink substitution,
                # even when the target currently contains identical bytes.
                printf '%s\n' "$original_row" >"$SM_OWNERSHIP_FILE"
                SM_OWNERSHIP_SHA256="$(sm_sha256_file "$SM_OWNERSHIP_FILE")"
                mv "$payload" "$payload.real"
                ln -s payload.real "$payload"
                ! sm_verify_owned
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_pr5b_receipt_is_canonically_validated_and_migrated_once(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-pr5b-migration-") as temp:
            result = run_harness(
                r"""
                test_use_host_lock_storage
                SM_LOCK_HELD=true
                SM_LOCK_ACTION=installer-install
                SM_STATE_DIR="$TEST_ROOT/state"
                SM_TRANSACTION_FILE="$SM_STATE_DIR/transaction.env"
                SM_MANIFEST_COPY="$SM_STATE_DIR/install-manifest.json"
                SM_OWNERSHIP_FILE="$SM_STATE_DIR/ownership.tsv"
                SM_ORIGINAL_FILE="$SM_STATE_DIR/originals.tsv"
                SM_JOURNAL_FILE="$SM_STATE_DIR/journal.tsv"
                SM_BOOT_PROOF="$SM_STATE_DIR/boot-verified.env"
                SM_SECURE_DIR_METADATA="$SM_STATE_DIR/secure-dir.env"
                SM_SYSTEM_DIR=/system/etc/init/magisk
                SM_INIT_PATH=/system/etc/init/magisk.rc
                SM_MIRROR=/
                sm_real_path() { printf '%s/root%s\n' "$TEST_ROOT" "$1"; }
                sm_validate_state_storage() { return 0; }
                sm_validate_secure_dir_base() { return 0; }
                sm_prepare_secure_dir_metadata() {
                  {
                    printf 'SCHEMA_VERSION=1\nPATH=/data/adb\nUID=0\nGID=0\nMODE=700\n'
                    printf 'CONTEXT_B64=LQ==\n'
                  } >"$SM_SECURE_DIR_METADATA"
                  chmod 0600 "$SM_SECURE_DIR_METADATA"
                  SM_SECURE_DIR_SHA256="$(sm_sha256_file "$SM_SECURE_DIR_METADATA")"
                }
                getprop() {
                  case "$1" in
                    ro.build.fingerprint) printf 'pr5b/fingerprint\n' ;;
                    ro.build.version.sdk) printf '32\n' ;;
                    ro.product.cpu.abilist) printf 'arm64-v8a\n' ;;
                    ro.product.cpu.abi) printf 'arm64-v8a\n' ;;
                  esac
                }
                mkdir -p "$SM_STATE_DIR/original" "$TEST_ROOT/root$SM_SYSTEM_DIR"
                printf payload >"$TEST_ROOT/root$SM_SYSTEM_DIR/magisk"
                chmod 0755 "$TEST_ROOT/root$SM_SYSTEM_DIR/magisk"
                fingerprint="$(printf 'pr5b/fingerprint' | bb sha256sum | awk '{print $1}')"
                digest="$(sm_sha256_file "$TEST_ROOT/root$SM_SYSTEM_DIR/magisk")"
                mode="0$(bb stat -c %a "$TEST_ROOT/root$SM_SYSTEM_DIR/magisk")"
                uid="$(bb stat -c %u "$TEST_ROOT/root$SM_SYSTEM_DIR/magisk")"
                gid="$(bb stat -c %g "$TEST_ROOT/root$SM_SYSTEM_DIR/magisk")"
                printf '%s\t%s\t7\t%s\t%s\t%s\t-\tfile\n' \
                  "$SM_SYSTEM_DIR/magisk" "$digest" "$mode" "$uid" "$gid" >"$SM_OWNERSHIP_FILE"
                chmod 0600 "$SM_OWNERSHIP_FILE"
                for spec in \
                  "$SM_SYSTEM_DIR:payload" "$SM_INIT_PATH:init_rc" \
                  "/system/etc/init/bootanim.rc:bootanim" "/data/adb/magisk:runtime" \
                  "/system/addon.d/99-magisk.sh:addon_script" "/system/addon.d/magisk:addon_dir"; do
                  path="${spec%%:*}"; label="${spec##*:}"
                  mkdir -p "$SM_STATE_DIR/original/$label"
                  printf absent >"$SM_STATE_DIR/original/$label/absent"
                  printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/%s/data\n' \
                    "$path" "$label" >>"$SM_ORIGINAL_FILE"
                done
                chmod 0600 "$SM_ORIGINAL_FILE"
                printf '1\tpublish\tcreate\t%s\t-\t%s\ttrue\n' \
                  "$SM_SYSTEM_DIR/magisk" "$digest" >"$SM_JOURNAL_FILE"
                chmod 0600 "$SM_JOURNAL_FILE"
                {
                  printf 'SCHEMA_VERSION=1\n'
                  printf 'INSTALL_ID=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa\n'
                  printf 'TRANSACTION_ID=bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb\n'
                  printf 'STATE=BOOT_VERIFIED\nPRIOR_STATE=UNINSTALLED\n'
                  printf 'FINGERPRINT_SHA256=%s\nTARGET_API=32\n' "$fingerprint"
                  printf 'REPORT_SHA256=%064d\nBACKUP_SHA256=%064d\n' 1 2
                  printf 'ADAPTER_ID_B64=bXVtdQ==\nTARGET_ABIS_B64=YXJtNjQtdjhh\n'
                  printf 'SNAPSHOT_ID_B64=c25hcHNob3Q=\nBACKUP_LOCATION_B64=L2JhY2t1cA==\n'
                  printf 'RESTORE_COMMAND_B64=L2Jpbi9yZXN0b3Jl\n'
                  printf 'INIT_PATH=%s\nPOLICY_PATH=\nPOLICY_SOURCE=\nRUNTIME_PATH=/sbin\n' "$SM_INIT_PATH"
                  printf 'SELINUX_STRATEGY=disabled\nSOURCE_COMMIT=%040d\nUPSTREAM_BASE=%040d\n' 3 4
                  printf 'ARTIFACT_SHA256=%064d\nPRODUCT_VERSION_B64=cHI1Yg==\n' 5
                  printf 'COMMIT_BOOT_ID=11111111-1111-4111-8111-111111111111\n'
                  printf 'ROLLBACK_DIR=%s/rollback/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb\n' "$SM_STATE_DIR"
                  printf 'STAGING_PATH=/system/etc/init/.magisk.kitsune-stage-bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb\n'
                } >"$SM_TRANSACTION_FILE"
                chmod 0600 "$SM_TRANSACTION_FILE"

                sm_load_transaction
                test "$SM_PR5B_RECEIPT:$SM_PR5B_MIGRATED" = true:false
                projection="$TEST_ROOT/pr5b-manifest.json"
                sm_generate_pr5b_manifest_projection "$projection"
                cp "$projection" "$TEST_ROOT/root$SM_SYSTEM_DIR/install-manifest.json"
                cp "$projection" "$SM_MANIFEST_COPY"
                chmod 0600 "$SM_MANIFEST_COPY"
                sm_validate_pr5b_installed_state

                printf tamper >>"$SM_OWNERSHIP_FILE"
                ! sm_validate_pr5b_installed_state
                sed '$d' "$SM_OWNERSHIP_FILE" >"$SM_OWNERSHIP_FILE.restored"
                mv "$SM_OWNERSHIP_FILE.restored" "$SM_OWNERSHIP_FILE"
                sm_validate_pr5b_installed_state
                printf tamper >>"$SM_MANIFEST_COPY"
                ! sm_validate_pr5b_installed_state
                cp "$projection" "$SM_MANIFEST_COPY"
                sm_validate_pr5b_installed_state

                sm_migrate_pr5b_receipt
                grep -qx 'MIGRATED_FROM_PR5B=true' "$SM_TRANSACTION_FILE"
                grep -Eq '^MANIFEST_SHA256=[a-f0-9]{64}$' "$SM_TRANSACTION_FILE"
                grep -Eq '^OWNERSHIP_SHA256=[a-f0-9]{64}$' "$SM_TRANSACTION_FILE"
                sm_load_transaction
                test "$SM_PR5B_RECEIPT:$SM_PR5B_MIGRATED" = false:true
                sm_validate_installed_state
                sm_quiesce_magisk() { return 0; }
                sm_assert_mutable_namespaces_idle() { return 0; }
                sm_assert_magisk_quiesced() { return 0; }
                sm_extend_pr5b_originals
                test "$SM_PR5B_MIGRATED" = false
                sm_validate_originals
                grep -q 'original/magisk_db/data$' "$SM_ORIGINAL_FILE"
                grep -q 'original/rescue_dir/data$' "$SM_ORIGINAL_FILE"
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_snapshot_and_restore_reject_special_nodes_and_nested_mounts(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-mount-guard-") as temp:
            result = run_harness(
                r"""
                SM_SYSTEM_DIR=/system/etc/init/magisk
                SM_INIT_PATH=/system/etc/init/magisk.rc
                SM_POLICY_PATH=
                sm_configure_rescue_paths
                sm_real_path() { printf '%s/root%s\n' "$TEST_ROOT" "$1"; }
                runtime="$TEST_ROOT/root/data/adb/magisk"
                mkdir -p "$runtime/module"
                printf payload >"$runtime/module/file"

                printf '40 30 0:40 / %s/module rw - tmpfs tmpfs rw\n' "$runtime" >"$SM_MOUNTINFO_FILE"
                ! sm_snapshot_source_safe runtime "$runtime"
                ! sm_destructive_target_safe runtime "$runtime"

                : >"$SM_MOUNTINFO_FILE"
                mkfifo "$runtime/module/unsafe-fifo"
                ! sm_snapshot_source_safe runtime "$runtime"
                ! sm_destructive_target_safe runtime "$runtime"
                unlink "$runtime/module/unsafe-fifo"

                ln -s module "$TEST_ROOT/root/data/adb/runtime-link"
                ! sm_snapshot_source_safe runtime "$TEST_ROOT/root/data/adb/runtime-link"
                ! sm_destructive_target_safe runtime "$TEST_ROOT/root/data/adb/runtime-link"
                sm_snapshot_source_safe runtime "$runtime"
                sm_destructive_target_safe runtime "$runtime"
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_terminal_rollback_cleanup_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-terminal-cleanup-") as temp:
            result = run_harness(
                r"""
                SM_STATE_DIR="$TEST_ROOT/state"
                SM_TRANSACTION_ID=12345678-1234-1234-1234-123456789abc
                SM_ROLLBACK_DIR="$SM_STATE_DIR/rollback/$SM_TRANSACTION_ID"
                mkdir -p "$SM_ROLLBACK_DIR"
                printf retained >"$SM_ROLLBACK_DIR/data"

                SM_STATE=BOOT_VERIFIED
                sm_cleanup_terminal_rollback
                test ! -e "$SM_ROLLBACK_DIR"
                sm_cleanup_terminal_rollback

                mkdir -p "$SM_ROLLBACK_DIR"
                SM_STATE=UNINSTALLED
                sm_cleanup_terminal_rollback
                test ! -e "$SM_ROLLBACK_DIR"

                mkdir -p "$SM_ROLLBACK_DIR"
                SM_STATE=COMMITTED
                ! sm_cleanup_terminal_rollback
                test -e "$SM_ROLLBACK_DIR"
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_clean_uninstall_removes_the_terminal_tombstone(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-uninstalled-tombstone-") as temp:
            result = run_harness(
                r"""
                SM_STATE_DIR="$TEST_ROOT/data/adb/kitsune/system-mode"
                SM_TRANSACTION_FILE="$SM_STATE_DIR/transaction.env"
                SM_TRANSACTION_ID=12345678-1234-1234-1234-123456789abc
                SM_ROLLBACK_DIR="$SM_STATE_DIR/rollback/$SM_TRANSACTION_ID"
                SM_STATE=UNINSTALLED
                mkdir -p "$SM_STATE_DIR"
                test_write_current_receipt UNINSTALLED

                sm_finalize_uninstalled_state
                test ! -e "$SM_TRANSACTION_FILE"
                test ! -e "$SM_STATE_DIR"
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_two_deaths_during_setup_marker_recovery_are_idempotent(self) -> None:
        common = r"""
            SM_STATE_DIR="$TEST_ROOT/data/adb/kitsune/system-mode"
            SM_TRANSACTION_FILE="$SM_STATE_DIR/transaction.env"
            SM_TRANSACTION_ID=12345678-1234-1234-1234-123456789abc
            SM_ROLLBACK_DIR="$SM_STATE_DIR/rollback/$SM_TRANSACTION_ID"
        """
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-setup-deaths-") as temp:
            root = Path(temp)
            first = run_harness(
                common
                + r"""
                mkdir -p "$SM_ROLLBACK_DIR"
                KITSUNE_SYSTEM_MODE_FAIL_AT=process-death:setup-marker-published
                sm_publish_setup_marker
                """,
                root,
            )
            self.assertNotEqual(0, first.returncode)
            self.assertTrue((root / "data/adb/.kitsune-system-mode-setup-v1.env").is_file())

            second = run_harness(
                common
                + r"""
                KITSUNE_SYSTEM_MODE_FAIL_AT=process-death:setup-rollback-removed
                sm_recover_setup_marker
                """,
                root,
            )
            self.assertNotEqual(0, second.returncode)

            final = run_harness(
                common
                + r"""
                sm_recover_setup_marker
                test ! -e "$SM_SETUP_MARKER"
                test ! -e "$SM_ROLLBACK_DIR"
                """,
                root,
            )
            self.assertEqual(0, final.returncode, final.stderr)

    def test_two_deaths_during_terminal_handoff_are_idempotent(self) -> None:
        common = r"""
            SM_STATE_DIR="$TEST_ROOT/data/adb/kitsune/system-mode"
            SM_TRANSACTION_FILE="$SM_STATE_DIR/transaction.env"
            SM_TRANSACTION_ID=12345678-1234-1234-1234-123456789abc
            SM_ROLLBACK_DIR="$SM_STATE_DIR/rollback/$SM_TRANSACTION_ID"
        """
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-terminal-deaths-") as temp:
            root = Path(temp)
            first = run_harness(
                common
                + r"""
                mkdir -p "$SM_ROLLBACK_DIR/state/transaction"
                printf absent >"$SM_ROLLBACK_DIR/state/transaction/absent"
                test_write_current_receipt ROLLING_BACK
                KITSUNE_SYSTEM_MODE_FAIL_AT=process-death:rollback-terminal-published
                sm_publish_rollback_terminal
                """,
                root,
            )
            self.assertNotEqual(0, first.returncode)
            self.assertTrue((root / "data/adb/.kitsune-system-mode-rollback-v1.env").is_file())

            second = run_harness(
                common
                + r"""
                KITSUNE_SYSTEM_MODE_FAIL_AT=process-death:rollback-storage-removed
                sm_complete_rollback_terminal
                """,
                root,
            )
            self.assertNotEqual(0, second.returncode)

            final = run_harness(
                common
                + r"""
                sm_complete_rollback_terminal
                test ! -e "$SM_ROLLBACK_TERMINAL"
                test ! -e "$SM_TRANSACTION_FILE"
                test ! -e "$SM_ROLLBACK_DIR"
                """,
                root,
            )
            self.assertEqual(0, final.returncode, final.stderr)

    def test_process_death_during_each_snapshot_capture_keeps_live_bytes(self) -> None:
        labels = (
            "payload", "legacy_rc", "init_rc", "policy", "policy_gz", "bootanim",
            "bootanim_gz", "runtime", "magisk_db", "magisk_db_wal", "magisk_db_shm",
            "modules", "modules_update", "post_fs_data", "service", "preinit_rule",
            "magisk_log", "magisk_log_bak", "addon_script", "addon_dir", "rescue_dir",
            "rescue_rc",
        )
        common = r"""
            SM_STATE_DIR="$TEST_ROOT/state"
            SM_SYSTEM_DIR=/system/etc/init/magisk
            SM_INIT_PATH=/system/etc/init/hw/magisk.rc
            SM_POLICY_PATH=/vendor/etc/selinux/precompiled_sepolicy
            SM_POLICY_MUTATED=true
            SM_TRANSACTION_ID=12345678-1234-1234-1234-123456789abc
            SM_ROLLBACK_DIR="$SM_STATE_DIR/rollback/$SM_TRANSACTION_ID"
            sm_configure_rescue_paths
            sm_real_path() { printf '%s/root%s\n' "$TEST_ROOT" "$1"; }
        """
        for label in labels:
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory(prefix=f"kitsune-snapshot-death-{label}-") as temp:
                    root = Path(temp)
                    first = run_harness(
                        common
                        + f"\nlabel={label}\n"
                        + r"""
                        canonical="$(sm_label_path "$label")"
                        real="$(sm_real_path "$canonical")"
                        mkdir -p "$(dirname "$real")" "$SM_ROLLBACK_DIR"
                        printf original >"$real"
                        KITSUNE_SYSTEM_MODE_FAIL_AT="process-death:snapshot:$label"
                        sm_snapshot_one "$label"
                        """,
                        root,
                    )
                    self.assertNotEqual(0, first.returncode)
                    final = run_harness(
                        common
                        + f"\nlabel={label}\n"
                        + r"""
                        canonical="$(sm_label_path "$label")"
                        real="$(sm_real_path "$canonical")"
                        sm_validate_snapshot_entry "$label"
                        test "$(cat "$real")" = original
                        sm_remove_tree_safe "Test rollback cleanup" "$SM_ROLLBACK_DIR" "$SM_ROLLBACK_DIR"
                        """,
                        root,
                    )
                    self.assertEqual(0, final.returncode, final.stderr)

    def test_process_death_during_each_state_snapshot_recovers_setup(self) -> None:
        labels = (
            "transaction", "manifest_copy", "ownership", "originals",
            "journal", "boot_proof", "secure_dir", "original_dir",
        )
        common = r"""
            SM_STATE_DIR="$TEST_ROOT/data/adb/kitsune/system-mode"
            SM_TRANSACTION_FILE="$SM_STATE_DIR/transaction.env"
            SM_MANIFEST_COPY="$SM_STATE_DIR/install-manifest.json"
            SM_OWNERSHIP_FILE="$SM_STATE_DIR/ownership.tsv"
            SM_ORIGINAL_FILE="$SM_STATE_DIR/originals.tsv"
            SM_JOURNAL_FILE="$SM_STATE_DIR/journal.tsv"
            SM_BOOT_PROOF="$SM_STATE_DIR/boot-verified.env"
            SM_SECURE_DIR_METADATA="$SM_STATE_DIR/secure-dir.env"
            SM_TRANSACTION_ID=12345678-1234-1234-1234-123456789abc
            SM_ROLLBACK_DIR="$SM_STATE_DIR/rollback/$SM_TRANSACTION_ID"
        """
        for label in labels:
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory(prefix=f"kitsune-state-death-{label}-") as temp:
                    root = Path(temp)
                    first = run_harness(
                        common
                        + f'\nKITSUNE_SYSTEM_MODE_FAIL_AT="process-death:snapshot-state:{label}"\n'
                        + r"""
                        mkdir -p "$SM_STATE_DIR/original"
                        test_write_receipt "$SM_TRANSACTION_FILE" \
                          bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb \
                          cccccccc-cccc-cccc-cccc-cccccccccccc BOOT_VERIFIED
                        printf manifest >"$SM_MANIFEST_COPY"
                        printf ownership >"$SM_OWNERSHIP_FILE"
                        printf originals >"$SM_ORIGINAL_FILE"
                        printf journal >"$SM_JOURNAL_FILE"
                        printf proof >"$SM_BOOT_PROOF"
                        printf secure >"$SM_SECURE_DIR_METADATA"
                        printf original >"$SM_STATE_DIR/original/data"
                        sm_publish_setup_marker
                        mkdir -p "$SM_ROLLBACK_DIR"
                        sm_snapshot_state_metadata
                        """,
                        root,
                    )
                    self.assertNotEqual(0, first.returncode)
                    final = run_harness(
                        common
                        + r"""
                        sm_recover_setup_marker
                        test "$(sm_get TRANSACTION_ID "$SM_TRANSACTION_FILE")" = \
                          cccccccc-cccc-cccc-cccc-cccccccccccc
                        test ! -e "$SM_SETUP_MARKER"
                        test ! -e "$SM_ROLLBACK_DIR"
                        """,
                        root,
                    )
                    self.assertEqual(0, final.returncode, final.stderr)

    def test_exact_uninstall_refuses_unowned_fixed_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-unowned-fixed-") as temp:
            result = run_harness(
                r"""
                SM_SYSTEM_DIR=/system/etc/init/magisk
                SM_INIT_PATH=/system/etc/init/magisk.rc
                SM_OWNERSHIP_FILE="$TEST_ROOT/ownership.tsv"
                SM_MANIFEST_SHA256="$(printf '%064d' 0)"
                fixed="$TEST_ROOT/root$SM_INIT_PATH"
                mutable="$TEST_ROOT/root/data/adb/modules"
                mkdir -p "$(dirname "$fixed")" "$mutable"
                printf expected >"$fixed"
                printf module >"$mutable/user-module"
                : >"$SM_OWNERSHIP_FILE"

                ! sm_assert_owned_restore_target "$SM_INIT_PATH" "$fixed"
                : >"$SM_OWNERSHIP_FILE.new"
                sm_add_owned_file "$SM_INIT_PATH" "$fixed"
                mv "$SM_OWNERSHIP_FILE.new" "$SM_OWNERSHIP_FILE"
                SM_OWNERSHIP_SHA256="$(sm_sha256_file "$SM_OWNERSHIP_FILE")"
                sm_assert_owned_restore_target "$SM_INIT_PATH" "$fixed"
                printf changed >"$fixed"
                ! sm_assert_owned_restore_target "$SM_INIT_PATH" "$fixed"

                sm_assert_owned_restore_target /data/adb/modules "$mutable"
                rm -f "$fixed"
                sm_assert_owned_restore_target "$SM_INIT_PATH" "$fixed"
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_uninstall_preserves_user_state_after_install_and_upgrade(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-uninstall-user-state-") as temp:
            result = run_harness(
                r"""
                SM_STATE_DIR="$TEST_ROOT/state"
                SM_ORIGINAL_FILE="$SM_STATE_DIR/originals.tsv"
                SM_SYSTEM_DIR=/system/etc/init/magisk
                SM_INIT_PATH=/system/etc/init/magisk.rc
                SM_POLICY_PATH=
                SM_POLICY_MUTATED=false
                sm_configure_rescue_paths
                sm_real_path() { printf '%s/root%s\n' "$TEST_ROOT" "$1"; }
                sm_destructive_target_safe() { return 0; }
                sm_assert_owned_restore_target() { return 0; }
                mkdir -p "$SM_STATE_DIR"
                : >"$SM_ORIGINAL_FILE"
                for path in /data/adb/magisk.db /data/adb/magisk.db-wal \
                    /data/adb/magisk.db-shm /data/adb/modules /data/adb/modules_update \
                    /data/adb/post-fs-data.d /data/adb/service.d /data/adb/sepolicy.rule \
                    /cache/magisk.log /cache/magisk.log.bak; do
                    real="$(sm_real_path "$path")"
                    label="$(basename "$path")"
                    mkdir -p "$(dirname "$real")"
                    case "$path" in
                        *.d|*/modules|*/modules_update)
                            mkdir -p "$real"
                            printf user-created-after-install >"$real/user-file"
                            ;;
                        *) printf user-changed-after-install >"$real" ;;
                    esac
                    printf '%s\tfalse\t-\t0\t-\t-\t-\t-\toriginal/%s/data\n' \
                        "$path" "$label" >>"$SM_ORIGINAL_FILE"
                done
                before="$(sm_digest_path "$TEST_ROOT/root")"
                sm_restore_originals ordinary
                test "$(sm_digest_path "$TEST_ROOT/root")" = "$before"

                # A prior PR5B upgrade may retain an older original snapshot.
                # Successful uninstall must neither overwrite current policy
                # nor resurrect user data deleted after that snapshot.
                mkdir -p "$SM_STATE_DIR/original/modules/data"
                printf old-module >"$SM_STATE_DIR/original/modules/data/removed-module"
                digest="$(sm_digest_path "$SM_STATE_DIR/original/modules/data")"
                printf '/data/adb/modules\ttrue\t%s\t0\t0700\t0\t0\t-\toriginal/modules/data\n' \
                    "$digest" >"$SM_ORIGINAL_FILE"
                sm_restore_originals ordinary
                test "$(sm_digest_path "$TEST_ROOT/root")" = "$before"
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_uninstall_retains_ownership_until_rescue_cleanup_finishes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-uninstall-metadata-") as temp:
            result = run_harness(
                r"""
                SM_STATE_DIR="$TEST_ROOT/state"
                SM_MANIFEST_COPY="$SM_STATE_DIR/install-manifest.json"
                SM_OWNERSHIP_FILE="$SM_STATE_DIR/ownership.tsv"
                SM_ORIGINAL_FILE="$SM_STATE_DIR/originals.tsv"
                SM_JOURNAL_FILE="$SM_STATE_DIR/journal.tsv"
                SM_BOOT_PROOF="$SM_STATE_DIR/boot-verified.env"
                SM_SECURE_DIR_METADATA="$SM_STATE_DIR/secure-dir.env"
                mkdir -p "$SM_STATE_DIR/original"
                for file in "$SM_MANIFEST_COPY" "$SM_OWNERSHIP_FILE" "$SM_ORIGINAL_FILE" \
                  "$SM_JOURNAL_FILE" "$SM_BOOT_PROOF" "$SM_SECURE_DIR_METADATA"; do
                  printf retained >"$file"
                done
                printf backup >"$SM_STATE_DIR/original/data"

                sm_cleanup_uninstalled_metadata false
                test ! -e "$SM_MANIFEST_COPY"
                test ! -e "$SM_BOOT_PROOF"
                test -e "$SM_OWNERSHIP_FILE"
                test -e "$SM_JOURNAL_FILE"
                test -e "$SM_ORIGINAL_FILE"

                sm_cleanup_uninstalled_metadata true
                test ! -e "$SM_OWNERSHIP_FILE"
                test ! -e "$SM_JOURNAL_FILE"
                test ! -e "$SM_ORIGINAL_FILE"
                test ! -e "$SM_STATE_DIR/original"
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_lock_serializes_authorization_and_recovers_after_holder_death(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-lock-") as temp:
            result = run_harness(
                r"""
                test_use_host_lock_storage
                test_accept_host_authorization
                SM_AUTHORIZATION_FILE="$TEST_ROOT/authorization.env"
                SM_AUTHORIZATION_CLAIM="$TEST_ROOT/authorization.claimed"
                mutation="$TEST_ROOT/mutations"
                : >"$mutation"
                printf 'single-use-authorization\n' >"$SM_AUTHORIZATION_FILE"
                chmod 0600 "$SM_AUTHORIZATION_FILE"
                SM_AUTH_FILE_SHA256="$(sm_sha256_file "$SM_AUTHORIZATION_FILE")"

                contender() {
                  name="$1"
                  : >"$TEST_ROOT/ready-$name"
                  while [ ! -f "$TEST_ROOT/start" ]; do sleep 0.01; done
                  if sm_acquire_lock "contender-$name"; then
                    if sm_consume_authorization; then
                      printf '%s\n' "$name" >>"$mutation"
                      sleep 0.2 9>&-
                      sm_release_lock || exit 33
                      exit 0
                    fi
                    sm_release_lock || exit 34
                    exit 31
                  fi
                  exit 20
                }

                contender one & first=$!
                contender two & second=$!
                attempts=0
                while [ ! -f "$TEST_ROOT/ready-one" ] || [ ! -f "$TEST_ROOT/ready-two" ]; do
                  attempts=$((attempts + 1))
                  [ "$attempts" -lt 500 ] || exit 34
                  sleep 0.01
                done
                : >"$TEST_ROOT/start"
                set +e
                wait "$first"; first_status=$?
                wait "$second"; second_status=$?
                set -e
                # The second process waits for the kernel lock, then observes
                # that the authorization was atomically consumed. It cannot
                # enter a mutation even though it began concurrently.
                case "$first_status:$second_status" in 0:31|31:0) ;; *) exit 35 ;; esac
                test "$(wc -l <"$mutation" | tr -d ' ')" = 1
                test ! -e "$SM_AUTHORIZATION_FILE"
                test ! -e "$SM_AUTHORIZATION_CLAIM"

                # A safe stale claim is not removable without the kernel lock,
                # but a fresh locked flow may discard it before atomically
                # claiming and consuming a newly issued authorization.
                printf stale >"$SM_AUTHORIZATION_CLAIM"
                chmod 0600 "$SM_AUTHORIZATION_CLAIM"
                printf fresh >"$SM_AUTHORIZATION_FILE"
                chmod 0600 "$SM_AUTHORIZATION_FILE"
                SM_AUTH_FILE_SHA256="$(sm_sha256_file "$SM_AUTHORIZATION_FILE")"
                ! sm_cleanup_authorization_claim
                test -f "$SM_AUTHORIZATION_CLAIM"
                sm_acquire_lock stale-claim-cleanup
                sm_consume_authorization
                sm_release_lock
                test ! -e "$SM_AUTHORIZATION_FILE"
                test ! -e "$SM_AUTHORIZATION_CLAIM"

                # Unsafe stale state fails closed and leaves both pieces for
                # inspection instead of deleting attacker-redirected bytes.
                printf unsafe >"$SM_AUTHORIZATION_CLAIM"
                chmod 0644 "$SM_AUTHORIZATION_CLAIM"
                printf retry >"$SM_AUTHORIZATION_FILE"
                chmod 0600 "$SM_AUTHORIZATION_FILE"
                SM_AUTH_FILE_SHA256="$(sm_sha256_file "$SM_AUTHORIZATION_FILE")"
                sm_acquire_lock unsafe-claim
                ! sm_consume_authorization
                sm_release_lock
                test -f "$SM_AUTHORIZATION_FILE"
                test -f "$SM_AUTHORIZATION_CLAIM"
                rm -f "$SM_AUTHORIZATION_FILE" "$SM_AUTHORIZATION_CLAIM"

                # The descriptor is the owner token. Killing its shell releases
                # exclusion even though the diagnostic inode deliberately stays.
                (
                  sm_acquire_lock crash-holder || exit 41
                  : >"$TEST_ROOT/holder-ready"
                  while :; do :; done
                ) & holder=$!
                attempts=0
                while [ ! -f "$TEST_ROOT/holder-ready" ]; do
                  attempts=$((attempts + 1))
                  [ "$attempts" -lt 500 ] || exit 42
                  sleep 0.01
                done
                kill -9 "$holder"
                set +e
                wait "$holder"
                set -e
                sm_acquire_lock after-crash
                sm_release_lock
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_remount_journal_recovers_death_before_and_after_rw(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-remount-death-") as temp:
            result = run_harness(
                r"""
                test_use_host_lock_storage
                SM_LOCK_HELD=true
                SM_LOCK_ACTION=test
                SM_PERSISTENT_REMOUNTED=
                TEST_REMOUNT_EVENTS="$TEST_ROOT/remount-events"
                : >"$TEST_REMOUNT_EVENTS"
                sm_remount() {
                  # The journal must be durable before an rw request is issued.
                  [ "$1" != rw ] || test -f "$SM_REMOUNT_FILE"
                  printf '%s:%s\n' "$1" "$2" >>"$TEST_REMOUNT_EVENTS"
                  TEST_MOUNT_MODE="$1"
                }
                sm_mount_mode_is() { [ "$1" = "$TEST_MOUNT_MODE" ]; }

                # Simulate death after the journal publication but before the
                # rw remount. A new shell reloads it and safely retries ro.
                sm_record_persistent_remount /system
                test -f "$SM_REMOUNT_FILE"
                SM_PERSISTENT_REMOUNTED=
                TEST_MOUNT_MODE=ro
                sm_restore_persistent_mounts
                grep -qx 'ro:/system' "$TEST_REMOUNT_EVENTS"
                test ! -e "$SM_REMOUNT_FILE"

                # Exercise the real prepare ordering, then erase all in-memory
                # state to model death immediately after the rw remount.
                : >"$TEST_REMOUNT_EVENTS"
                SM_MIRROR=/
                SM_SYSTEM_DIR=/system/etc/init/magisk
                SM_INIT_PATH=/system/etc/init/magisk.rc
                SM_POLICY_PATH=
                SM_POLICY_MUTATED=false
                sm_configure_rescue_paths
                SM_MOUNTS_FILE="$TEST_ROOT/mounts"
                printf '/dev/block/system / ext4 ro,relatime 0 0\n' >"$SM_MOUNTS_FILE"
                TEST_MOUNT_MODE=ro
                sm_prepare_persistent_mounts
                grep -qx 'rw:/' "$TEST_REMOUNT_EVENTS"
                test -f "$SM_REMOUNT_FILE"
                SM_PERSISTENT_REMOUNTED=
                TEST_MOUNT_MODE=rw
                sm_restore_persistent_mounts
                grep -qx 'ro:/' "$TEST_REMOUNT_EVENTS"
                test ! -e "$SM_REMOUNT_FILE"
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_partial_remount_prepare_unwinds_and_retains_unrestored_entries(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-remount-unwind-") as temp:
            result = run_harness(
                r"""
                test_use_host_lock_storage
                SM_LOCK_HELD=true
                SM_LOCK_ACTION=test
                SM_MIRROR=/
                SM_SYSTEM_DIR=/system/etc/init/magisk
                SM_INIT_PATH=/system/etc/init/magisk.rc
                SM_POLICY_PATH=
                SM_POLICY_MUTATED=false
                SM_MOUNTS_FILE="$TEST_ROOT/mounts"
                printf '/dev/system /system ext4 ro 0 0\n/dev/data /data ext4 ro 0 0\n/dev/cache /cache ext4 rw 0 0\n' \
                  >"$SM_MOUNTS_FILE"
                system_mode=ro
                data_mode=ro
                events="$TEST_ROOT/remount-events"
                : >"$events"
                sm_mountpoint_for() {
                  case "$1" in /data/*) printf '/data\n' ;; /cache/*) printf '/cache\n' ;; *) printf '/system\n' ;; esac
                }
                sm_mount_mode_is() {
                  case "$2" in
                    /system) test "$1" = "$system_mode" ;;
                    /data) test "$1" = "$data_mode" ;;
                    /cache) test "$1" = rw ;;
                    *) return 1 ;;
                  esac
                }
                sm_remount() {
                  printf '%s:%s\n' "$1" "$2" >>"$events"
                  case "$1:$2" in
                    rw:/system) system_mode=rw ;;
                    rw:/data) return 1 ;;
                    ro:/system) system_mode=ro ;;
                    ro:/data) data_mode=ro ;;
                    *) return 1 ;;
                  esac
                }

                ! sm_prepare_persistent_mounts
                grep -qx 'rw:/system' "$events"
                grep -qx 'rw:/data' "$events"
                grep -qx 'ro:/system' "$events"
                grep -qx 'ro:/data' "$events"
                test "$system_mode:$data_mode" = ro:ro
                test ! -e "$SM_REMOUNT_FILE"

                # If an unwind remount itself fails, its journal row survives
                # and is reloaded by a fresh process before any new mutation.
                : >"$events"
                SM_PERSISTENT_REMOUNTED=/system
                sm_write_remount_journal
                system_mode=rw
                sm_remount() {
                  printf '%s:%s\n' "$1" "$2" >>"$events"
                  return 1
                }
                ! sm_restore_persistent_mounts
                SM_PERSISTENT_REMOUNTED=
                sm_load_remount_journal
                test "$SM_PERSISTENT_REMOUNTED" = /system
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_remount_journal_retains_failures_and_rejects_unsafe_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-remount-guard-") as temp:
            result = run_harness(
                r"""
                test_use_host_lock_storage
                SM_LOCK_HELD=true
                SM_LOCK_ACTION=test
                SM_PERSISTENT_REMOUNTED=
                sm_record_persistent_remount /system
                sm_record_persistent_remount /vendor
                TEST_FAIL_VENDOR=true
                sm_remount() {
                  [ "$TEST_FAIL_VENDOR:$1:$2" != true:ro:/vendor ]
                }
                sm_mount_mode_is() { return 0; }
                ! sm_restore_persistent_mounts
                SM_PERSISTENT_REMOUNTED=
                sm_load_remount_journal
                test "$SM_PERSISTENT_REMOUNTED" = /vendor
                TEST_FAIL_VENDOR=false
                sm_restore_persistent_mounts
                test ! -e "$SM_REMOUNT_FILE"

                # A journal from another boot is retained and rejected; it is
                # never assumed safe merely because /dev contains a filename.
                SM_PERSISTENT_REMOUNTED=/system
                sm_write_remount_journal
                TEST_BOOT_ID=22222222-2222-4222-8222-222222222222
                SM_PERSISTENT_REMOUNTED=
                ! sm_load_remount_journal
                test -f "$SM_REMOUNT_FILE"
                TEST_BOOT_ID=11111111-1111-4111-8111-111111111111
                sm_load_remount_journal

                chmod 0644 "$SM_REMOUNT_FILE"
                SM_PERSISTENT_REMOUNTED=
                ! sm_load_remount_journal
                chmod 0600 "$SM_REMOUNT_FILE"
                rm -f "$SM_REMOUNT_FILE"
                printf sentinel >"$TEST_ROOT/sentinel"
                ln -s "$TEST_ROOT/sentinel" "$SM_REMOUNT_FILE"
                ! sm_load_remount_journal
                test "$(cat "$TEST_ROOT/sentinel")" = sentinel
                rm -f "$SM_REMOUNT_FILE"

                # An interrupted journal publication is deleted only when its
                # inode and mode are safe. Redirected or permissive staging is
                # retained and blocks recovery for inspection.
                printf staged >"$SM_REMOUNT_FILE.new"
                chmod 0644 "$SM_REMOUNT_FILE.new"
                ! sm_load_remount_journal
                test -f "$SM_REMOUNT_FILE.new"
                chmod 0600 "$SM_REMOUNT_FILE.new"
                sm_load_remount_journal
                test ! -e "$SM_REMOUNT_FILE.new"
                ln -s "$TEST_ROOT/sentinel" "$SM_REMOUNT_FILE.new"
                ! sm_load_remount_journal
                test "$(cat "$TEST_ROOT/sentinel")" = sentinel
                rm -f "$SM_REMOUNT_FILE.new"

                # Syntactically plausible corruption remains fail-closed.
                encoded="$(printf '/system /system' | base64 | tr -d '\n')"
                {
                  printf 'SCHEMA_VERSION=1\n'
                  printf 'BOOT_ID=%s\n' "$TEST_BOOT_ID"
                  printf 'MOUNTPOINTS_B64=%s\n' "$encoded"
                } >"$SM_REMOUNT_FILE"
                chmod 0600 "$SM_REMOUNT_FILE"
                ! sm_load_remount_journal
                test -f "$SM_REMOUNT_FILE"
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_lock_acquisition_restores_terminal_remount_without_receipt(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-terminal-remount-") as temp:
            result = run_harness(
                r"""
                test_use_host_lock_storage
                SM_TRANSACTION_FILE="$TEST_ROOT/missing-transaction.env"
                SM_LOCK_HELD=true
                SM_LOCK_ACTION=seed
                SM_PERSISTENT_REMOUNTED=/system
                sm_write_remount_journal
                SM_LOCK_HELD=false
                SM_LOCK_ACTION=
                SM_PERSISTENT_REMOUNTED=
                TEST_REMOUNT_EVENT=
                sm_remount() { TEST_REMOUNT_EVENT="$1:$2"; }
                sm_mount_mode_is() { [ "$1" = ro ]; }

                sm_acquire_lock terminal-cleanup
                test "$TEST_REMOUNT_EVENT" = ro:/system
                test ! -e "$SM_REMOUNT_FILE"
                test ! -e "$SM_TRANSACTION_FILE"
                sm_release_lock
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_generated_manifest_validates_against_checked_in_schema(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-manifest-") as temp:
            root = Path(temp)
            result = run_harness(
                r"""
                SM_STATE_DIR="$TEST_ROOT/state"
                SM_OWNERSHIP_FILE="$SM_STATE_DIR/ownership.tsv"
                SM_ORIGINAL_FILE="$SM_STATE_DIR/originals.tsv"
                SM_JOURNAL_FILE="$SM_STATE_DIR/journal.tsv"
                SM_INSTALL_ID=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa
                SM_PRODUCT_VERSION=31.0-kitsune
                SM_VERSION_CODE=31000
                SM_SOURCE_COMMIT="$(printf '%040d' 1)"
                SM_UPSTREAM_BASE="$(printf '%040d' 2)"
                SM_ARTIFACT_SHA256="$(printf '%064d' 3)"
                SM_ADAPTER_ID=generic-in-guest
                SM_AUTHORIZATION_ID=bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb
                SM_TARGET_CONTRACT_SHA256="$(printf '%064d' 4)"
                SM_AUTH_BOOT_ID_SHA256="$(printf '%064d' 5)"
                SM_PROBE_SHA256="$(printf '%064d' 6)"
                SM_QUALIFICATION_SHA256="$(printf '%064d' 4)"
                SM_INSTANCE_IDENTITY_SHA256="$(printf '%064d' 5)"
                SM_SERIAL_SHA256="$(printf '%064d' 7)"
                SM_FINGERPRINT_SHA256="$(printf '%064d' 8)"
                SM_TARGET_ABIS=arm64-v8a
                SM_INIT_PATH=/system/etc/init/hw/magisk.rc
                SM_SELINUX_STRATEGY=split
                SM_POLICY_SOURCE=
                SM_POLICY_MUTATED=false
                SM_LEGACY_MIGRATION=false
                SM_RUNTIME_PATH=/debug_ramdisk
                SM_PREINIT_DEVICE=
                SM_PREINIT_DIR=
                SM_ACTIVE_PAYLOAD=/system/etc/init/magisk/versions/cccccccc-cccc-cccc-cccc-cccccccccccc
                SM_RESCUE_PAYLOAD=/system/etc/init/hw/.kitsune-system-mode-rescue/versions/cccccccc-cccc-cccc-cccc-cccccccccccc
                SM_OWNERSHIP_SHA256="$(printf '%064d' 6)"
                SM_ORIGINALS_SHA256="$(printf '%064d' 9)"
                SM_SECURE_DIR_SHA256="$(printf '%064d' 1)"
                SM_BACKUP_LOCATION=/external/backup
                SM_BACKUP_SHA256="$(printf '%064d' 2)"
                SM_RESTORE_COMMAND='restore exact backup'
                mkdir -p "$SM_STATE_DIR"
                printf '/system/etc/init/magisk.rc\t%064d\t4\t0755\t0\t0\t-\tlink\n' 3 >"$SM_OWNERSHIP_FILE"
                printf '/system/addon.d/99-magisk.sh\tfalse\t-\t0\t-\t-\t-\toriginal/addon_script/data\n' >"$SM_ORIGINAL_FILE"
                printf '1\tpublish\tcreate\t/system/etc/init/magisk.rc\t-\t%064d\ttrue\n' 3 >"$SM_JOURNAL_FILE"
                getprop() { [ "$1" = ro.build.version.sdk ] && printf '32\n'; }

                sm_generate_manifest
                """,
                root,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            manifest = json.loads((root / "state/.install-manifest.json.new").read_text())
            schema = json.loads(MANIFEST_SCHEMA.read_text())
            validate_schema_instance(manifest, schema)
            self.assertEqual("link", manifest["payload"][0]["kind"])

    def test_snapshot_inventory_is_digest_bound_and_globally_stable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-snapshot-integrity-") as temp:
            result = run_harness(
                r"""
                SM_STATE_DIR="$TEST_ROOT/state"
                SM_SYSTEM_DIR=/system/etc/init/magisk
                SM_INIT_PATH=/system/etc/init/magisk.rc
                SM_POLICY_PATH=
                SM_POLICY_MUTATED=false
                SM_TRANSACTION_ID=12345678-1234-1234-1234-123456789abc
                SM_ROLLBACK_DIR="$SM_STATE_DIR/rollback/$SM_TRANSACTION_ID"
                sm_configure_rescue_paths
                sm_real_path() { printf '%s/root%s\n' "$TEST_ROOT" "$1"; }
                mkdir -p "$TEST_ROOT/root$SM_SYSTEM_DIR" "$TEST_ROOT/root/data/adb/magisk"
                printf payload >"$TEST_ROOT/root$SM_SYSTEM_DIR/file"
                printf init >"$TEST_ROOT/root$SM_INIT_PATH"
                printf runtime >"$TEST_ROOT/root/data/adb/magisk/file"

                sm_snapshot_all
                sm_verify_snapshot_sources
                printf changed >"$TEST_ROOT/root/data/adb/magisk/file"
                ! sm_verify_snapshot_sources
                printf runtime >"$TEST_ROOT/root/data/adb/magisk/file"
                sm_verify_snapshot_sources

                printf corrupt >"$SM_ROLLBACK_DIR/runtime/data/file"
                ! sm_validate_snapshot_entry runtime
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_snapshot_refuses_processes_using_mutable_namespaces(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-process-quiesce-") as temp:
            result = run_harness(
                r"""
                SM_PROC_ROOT="$TEST_ROOT/proc"
                mkdir -p "$SM_PROC_ROOT/123/fd" "$SM_PROC_ROOT/123/fdinfo" \
                  "$SM_PROC_ROOT/456/fd" "$SM_PROC_ROOT/456/fdinfo"
                : >"$SM_PROC_ROOT/123/maps"
                : >"$SM_PROC_ROOT/456/maps"
                ln -s / "$SM_PROC_ROOT/456/cwd"
                ln -s /data/adb/modules/example "$SM_PROC_ROOT/123/cwd"
                ! sm_assert_mutable_namespaces_idle
                rm "$SM_PROC_ROOT/123/cwd"
                ln -s / "$SM_PROC_ROOT/123/cwd"
                ln -s /data/adb/modules/example/state "$SM_PROC_ROOT/123/fd/9"
                printf 'flags:\t0100002\n' >"$SM_PROC_ROOT/123/fdinfo/9"
                ! sm_assert_mutable_namespaces_idle
                printf 'flags:\t0100000\n' >"$SM_PROC_ROOT/123/fdinfo/9"
                sm_assert_mutable_namespaces_idle

                # Private as well as shared writable mappings are treated as
                # active references; an unreadable or malformed stable proc
                # entry also fails closed.
                printf '1000-2000 rw-p 00000000 00:00 0 /data/adb/magisk/module\n' \
                  >"$SM_PROC_ROOT/123/maps"
                ! sm_assert_mutable_namespaces_idle
                printf '1000-2000 r--p 00000000 00:00 0 /data/adb/magisk/module\n' \
                  >"$SM_PROC_ROOT/123/maps"
                sm_assert_mutable_namespaces_idle
                printf 'malformed maps row\n' >"$SM_PROC_ROOT/123/maps"
                ! sm_assert_mutable_namespaces_idle
                : >"$SM_PROC_ROOT/123/maps"

                rm "$SM_PROC_ROOT/456/cwd"
                ! sm_assert_mutable_namespaces_idle
                ln -s / "$SM_PROC_ROOT/456/cwd"
                chmod 000 "$SM_PROC_ROOT/456/maps"
                ! sm_assert_mutable_namespaces_idle
                chmod 0600 "$SM_PROC_ROOT/456/maps"
                chmod 000 "$SM_PROC_ROOT/456/fdinfo"
                ! sm_assert_mutable_namespaces_idle
                chmod 0700 "$SM_PROC_ROOT/456/fdinfo"
                sm_assert_mutable_namespaces_idle
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_daemon_scan_rejects_stable_unreadable_process_entries(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-daemon-proc-") as temp:
            result = run_harness(
                r"""
                SM_PROC_ROOT="$TEST_ROOT/proc"
                mkdir -p "$SM_PROC_ROOT/123" "$SM_PROC_ROOT/456"
                printf ordinary >"$SM_PROC_ROOT/123/comm"
                printf magiskd >"$SM_PROC_ROOT/456/comm"
                sm_magisk_daemon_running
                printf ordinary >"$SM_PROC_ROOT/456/comm"
                set +e
                sm_magisk_daemon_running
                status=$?
                set -e
                test "$status" = 1
                chmod 000 "$SM_PROC_ROOT/456/comm"
                set +e
                sm_magisk_daemon_running
                status=$?
                set -e
                test "$status" = 2
                chmod 0600 "$SM_PROC_ROOT/456/comm"
                rm "$SM_PROC_ROOT/456/comm"
                set +e
                sm_magisk_daemon_running
                status=$?
                set -e
                test "$status" = 2
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_snapshot_ignores_exited_processes_but_not_unreadable_live_ones(self) -> None:
        with tempfile.TemporaryDirectory(prefix="kitsune-transaction-zombie-") as temp:
            result = run_harness(
                r"""
                SM_PROC_ROOT="$TEST_ROOT/proc"
                process="$SM_PROC_ROOT/123"
                mkdir -p "$process/fd" "$process/fdinfo"
                : >"$process/maps"
                # MuMu leaves the stopped daemon's shell as a zombie. Its
                # cwd and descriptors are gone although /proc/PID still exists.
                printf '123 (sh) Z 1 123 123 0 -1 4227076 0\n' >"$process/stat"
                sm_assert_mutable_namespaces_idle
                printf '123 (sh) X 1 123 123 0 -1 4227076 0\n' >"$process/stat"
                sm_assert_mutable_namespaces_idle

                # Parse after the final closing parenthesis: a process name
                # may contain spaces and apparent state fields.
                printf '123 (fake) Z name) S 1 123 123 0 -1 0 0\n' >"$process/stat"
                ! sm_assert_mutable_namespaces_idle
                printf '123 (fake) S name) Z 1 123 123 0 -1 0 0\n' >"$process/stat"
                sm_assert_mutable_namespaces_idle
                printf '456 (sh) Z 1 123 123 0 -1 0 0\n' >"$process/stat"
                ! sm_assert_mutable_namespaces_idle
                rm "$process/stat"
                ! sm_assert_mutable_namespaces_idle
                printf '123 (sh) Z 1 123 123 0 -1 0 0\n' >"$TEST_ROOT/redirect"
                ln -s "$TEST_ROOT/redirect" "$process/stat"
                ! sm_assert_mutable_namespaces_idle
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
