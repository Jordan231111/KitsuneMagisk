from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]
TRANSACTION = ROOT / "scripts" / "system_mode_transaction.sh"


SHELL_PREAMBLE = r"""
set -eu
. "$1"
TEST_ROOT="$2"

bb() {
  applet="$1"
  shift
  case "$applet" in
    sha256sum) shasum -a 256 "$@" ;;
    stat)
      if [ "$1" = -c ]; then
        format="$2"
        path="$3"
        case "$format" in
          %s) /usr/bin/stat -f %z "$path" ;;
          %a) /usr/bin/stat -f %Lp "$path" ;;
          %u) /usr/bin/stat -f %u "$path" ;;
          %g) /usr/bin/stat -f %g "$path" ;;
          *) return 2 ;;
        esac
      else
        command stat "$@"
      fi
      ;;
    *) command "$applet" "$@" ;;
  esac
}

SM_BB=bb
sm_fsync() { return 0; }
sm_fsync_tree() { return 0; }
sm_log() { :; }
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
                fingerprint="$(printf '%s' exact/fingerprint | shasum -a 256 | awk '{ print $1 }')"
                mkdir -p "$SM_STATE_DIR"
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
                SM_ROLLBACK_DIR="$SM_STATE_DIR/rollback/new"
                mkdir -p "$SM_STATE_DIR/original"
                printf prior-transaction >"$SM_TRANSACTION_FILE"
                printf prior-manifest >"$SM_MANIFEST_COPY"
                printf prior-ownership >"$SM_OWNERSHIP_FILE"
                printf prior-originals >"$SM_ORIGINAL_FILE"
                printf prior-journal >"$SM_JOURNAL_FILE"
                printf prior-proof >"$SM_BOOT_PROOF"
                printf prior-backup >"$SM_STATE_DIR/original/data"

                sm_snapshot_state_metadata
                printf new-transaction >"$SM_TRANSACTION_FILE"
                printf new-manifest >"$SM_MANIFEST_COPY"
                rm -f "$SM_OWNERSHIP_FILE" "$SM_BOOT_PROOF"
                printf new-backup >"$SM_STATE_DIR/original/data"
                sm_restore_state_metadata

                test "$(cat "$SM_TRANSACTION_FILE")" = prior-transaction
                test "$(cat "$SM_MANIFEST_COPY")" = prior-manifest
                test "$(cat "$SM_OWNERSHIP_FILE")" = prior-ownership
                test "$(cat "$SM_ORIGINAL_FILE")" = prior-originals
                test "$(cat "$SM_JOURNAL_FILE")" = prior-journal
                test "$(cat "$SM_BOOT_PROOF")" = prior-proof
                test "$(cat "$SM_STATE_DIR/original/data")" = prior-backup
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
                SM_STAGING_PATH=/system/etc/init/.magisk.kitsune-stage-test
                SM_ROLLBACK_DIR="$SM_STATE_DIR/rollback/new"
                SM_PRIOR_STATE=BOOT_VERIFIED
                sm_real_path() { printf '%s%s\n' "$TEST_ROOT/root" "$1"; }
                sm_update_state() { SM_STATE="$1"; }

                mkdir -p "$SM_STATE_DIR/original" "$TEST_ROOT/root/system/etc/init/magisk" \
                  "$TEST_ROOT/root/data/adb/magisk" "$TEST_ROOT/root/system/addon.d/magisk"
                printf prior-transaction >"$SM_TRANSACTION_FILE"
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

                sm_snapshot_state_metadata
                sm_snapshot_all
                printf new-transaction >"$SM_TRANSACTION_FILE"
                printf payload-new >"$TEST_ROOT/root/system/etc/init/magisk/file"
                printf legacy-new >"$TEST_ROOT/root/system/etc/init/magisk.rc"
                printf bootanim-new >"$TEST_ROOT/root/system/etc/init/bootanim.rc"
                printf runtime-new >"$TEST_ROOT/root/data/adb/magisk/file"
                printf addon-new >"$TEST_ROOT/root/system/addon.d/99-magisk.sh"
                printf addon-dir-new >"$TEST_ROOT/root/system/addon.d/magisk/file"

                sm_restore_snapshot
                test "$(cat "$TEST_ROOT/root/system/etc/init/magisk/file")" = payload-old
                test "$(cat "$TEST_ROOT/root/system/etc/init/magisk.rc")" = legacy-old
                test "$(cat "$TEST_ROOT/root/system/etc/init/bootanim.rc")" = bootanim-old
                test "$(cat "$TEST_ROOT/root/system/etc/init/bootanim.rc.gz")" = bootanim-gz-old
                test "$(cat "$TEST_ROOT/root/data/adb/magisk/file")" = runtime-old
                test "$(cat "$TEST_ROOT/root/system/addon.d/99-magisk.sh")" = addon-old
                test "$(cat "$TEST_ROOT/root/system/addon.d/magisk/file")" = addon-dir-old
                test "$(cat "$SM_TRANSACTION_FILE")" = prior-transaction
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
                SM_STAGING_PATH=/system/etc/init/.magisk.kitsune-stage-test
                SM_ROLLBACK_DIR="$SM_STATE_DIR/rollback/new"
                sm_real_path() { printf '%s%s\n' "$TEST_ROOT/root" "$1"; }
                sm_update_state() { SM_STATE="$1"; }

                mkdir -p "$SM_STATE_DIR/original" \
                  "$TEST_ROOT/root/system/etc/init/magisk" \
                  "$TEST_ROOT/root/data/adb/magisk"
                printf prior-transaction >"$SM_TRANSACTION_FILE"
                printf payload-old >"$TEST_ROOT/root/system/etc/init/magisk/file"
                printf init-old >"$TEST_ROOT/root/system/etc/init/magisk.rc"
                printf runtime-old >"$TEST_ROOT/root/data/adb/magisk/file"
                sm_snapshot_state_metadata
                sm_snapshot_all

                # Model BusyBox fsync: unlike the normal harness stub, an
                # absent path is an error. /system/addon.d never existed.
                sm_fsync() {
                  local path
                  for path in "$@"; do [ -e "$path" ] || return 1; done
                }
                sm_restore_snapshot
                test ! -e "$TEST_ROOT/root/system/addon.d"
                test "$(cat "$TEST_ROOT/root/system/etc/init/magisk/file")" = payload-old
                test "$(cat "$TEST_ROOT/root/data/adb/magisk/file")" = runtime-old
                """,
                Path(temp),
            )
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
