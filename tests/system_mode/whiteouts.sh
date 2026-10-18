#!/system/bin/sh
# Run with the packaged BusyBox ash on a rooted test device.
set -eu
. "$1"
SM_BB="$2"
TEST_ROOT="$("$SM_BB" mktemp -d /data/local/tmp/kitsune-whiteouts.XXXXXX)"
trap '"$SM_BB" rm -rf "$TEST_ROOT"' EXIT
SM_STATE_DIR="$TEST_ROOT/state"
SM_TRANSACTION_ID=11111111-1111-4111-8111-111111111111
SM_ROLLBACK_DIR="$SM_STATE_DIR/rollback/$SM_TRANSACTION_ID"
sm_real_path() { printf '%s%s\n' "$TEST_ROOT" "$1"; }

for label in modules modules_update; do
  root="$TEST_ROOT/data/adb/$label"
  node="$root/mount_test/system/bin/removed"
  "$SM_BB" mkdir -p "${node%/*}"
  "$SM_BB" mknod "$node" c 0 0
  "$SM_BB" ln -s /unavailable "$node.link"
  sm_snapshot_one "$label"
  sm_validate_snapshot_entry "$label"
  copy="$SM_ROLLBACK_DIR/$label/data/mount_test/system/bin/removed"
  [ "$("$SM_BB" stat -c '%t:%T' "$copy")" = 0:0 ]
  [ "$("$SM_BB" readlink "$copy.link")" = /unavailable ]
  original="$(sm_digest_path "$root")"
  "$SM_BB" rm "$copy"
  "$SM_BB" touch "$copy"
  if sm_validate_snapshot_entry "$label"; then exit 1; fi
  "$SM_BB" rm "$copy"
  "$SM_BB" mknod "$copy" c 0 0
  sm_validate_snapshot_entry "$label"
  sm_remove_tree_safe fixture "/data/adb/$label" "$root"
  "$SM_BB" cp -a "$SM_ROLLBACK_DIR/$label/data" "$root"
  [ "$original" = "$(sm_digest_path "$root")" ]
  for spec in 'c 1 3' 'b 0 0' p; do
    # Deliberate word splitting supplies the mknod type and device numbers.
    "$SM_BB" mknod "$node.unsafe" $spec
    if sm_snapshot_source_safe "$label" "$root"; then exit 1; fi
    if sm_destructive_target_safe "$label" "$root"; then exit 1; fi
    "$SM_BB" rm "$node.unsafe"
  done
done
runtime="$TEST_ROOT/data/adb/magisk"
"$SM_BB" mkdir -p "$runtime"
"$SM_BB" mknod "$runtime/unsafe" c 0 0
if sm_snapshot_source_safe runtime "$runtime"; then exit 1; fi
SM_STATE=BOOT_VERIFIED
sm_cleanup_terminal_rollback
[ ! -e "$SM_ROLLBACK_DIR" ]
echo 'Module whiteout snapshot, restore, and rejection checks passed'
