#!/system/bin/sh
# shellcheck shell=busybox
# shellcheck disable=SC3043

# Boot-complete verifier and recovery entry point for a versioned PR7 payload.

SMV_PAYLOAD=${0%/*}
SMV_SYSTEM_DIR=${SMV_PAYLOAD%/versions/*}
SMV_SOURCE=$SMV_PAYLOAD/system_mode_transaction.sh
SMV_SOURCE_BB=$SMV_PAYLOAD/busybox
SMV_TMP=/dev/.kitsune-system-mode-verify.$$
SMV_BB=$SMV_TMP/busybox

smv_log() {
  log -t KitsuneSystemMode -- "$1" 2>/dev/null || echo "$1"
}

smv_exit() {
  local result="$1"
  if command -v sm_release_lock >/dev/null 2>&1 && ! sm_release_lock; then
    smv_log "System Mode transaction lock release failed"
    result=1
  fi
  [ ! -x "$SMV_BB" ] || "$SMV_BB" rm -rf "$SMV_TMP"
  exit "$result"
}

smv_reboot() {
  /system/bin/setprop sys.powerctl reboot 2>/dev/null
  sm_release_lock || smv_log "System Mode transaction lock release failed before reboot"
  # The reverse transaction can remove both the active payload and its
  # BusyBox. The /dev copy is the only executable guaranteed to survive.
  "$SMV_BB" sleep 2
  "$SMV_BB" reboot -f
  smv_exit 1
}

[ -f "$SMV_SOURCE" ] && [ -x "$SMV_SOURCE_BB" ] || exit 1
"$SMV_SOURCE_BB" mkdir "$SMV_TMP" || exit 1
"$SMV_SOURCE_BB" chmod 0700 "$SMV_TMP" || exit 1
"$SMV_SOURCE_BB" cp "$SMV_SOURCE_BB" "$SMV_BB" || exit 1
"$SMV_SOURCE_BB" chmod 0700 "$SMV_BB" || exit 1
export ASH_STANDALONE=1
set -o standalone || exit 1
# shellcheck disable=SC1090
. "$SMV_SOURCE" || smv_exit 1
sm_log() { smv_log "$1"; }
sm_configure "$SMV_PAYLOAD" / "$SMV_SYSTEM_DIR" "$SMV_BB" || smv_exit 1
sm_acquire_lock verify || smv_exit 1
sm_load_transaction || smv_exit 1
[ "$SM_ACTIVE_PAYLOAD" = "$SMV_PAYLOAD" ] || smv_exit 1

case "$SM_STATE" in
  COMMITTED|BOOT_VERIFIED)
    if sm_verify_boot; then
      smv_log "System Mode boot verification passed"
      smv_exit 0
    fi
    sm_load_transaction || smv_exit 1
    ;;
  UNINSTALLED) smv_exit 0 ;;
  FAILED)
    smv_log "System Mode requires the verified external restore"
    smv_exit 1
    ;;
esac

case "$SM_STATE" in
  PREFLIGHTED|STAGED|ROLLBACK_REQUIRED|ROLLING_BACK) ;;
  *) smv_exit 1 ;;
esac

if [ -x "$SM_RUNTIME_PATH/magisk" ]; then
  "$SM_RUNTIME_PATH/magisk" --stop 9>&- 2>/dev/null || smv_log "Magisk daemon stop failed before rollback"
fi
if ! sm_prepare_persistent_mounts; then
  sm_restore_persistent_mounts || smv_log "Partial verification remount restoration failed"
  smv_exit 1
fi
if sm_abort_transaction; then
  sm_restore_persistent_mounts || smv_log "Rollback filesystem-mode restoration failed"
  "$SMV_BB" sync
  # /dev is boot-scoped, so deliberately retain SMV_TMP for the reboot path.
  smv_reboot
fi
sm_restore_persistent_mounts || true
smv_log "Automatic System Mode rollback failed; use the verified external restore"
smv_exit 1
