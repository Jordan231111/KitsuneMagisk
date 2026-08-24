#!/system/bin/sh
# shellcheck shell=busybox
# shellcheck disable=SC3043

# Invariant boot-time recovery hook for interrupted System Mode mutations. The
# fixed rescue RC always points at one complete versioned rescue payload. Every
# boot-critical mutation starts only after that RC has reached persistent
# storage, and rollback/uninstall removes it only after the prior boot path is
# already restored.

KSR_PAYLOAD=${0%/*}
KSR_BB_SOURCE=$KSR_PAYLOAD/busybox
KSR_TRANSACTION=$KSR_PAYLOAD/system_mode_transaction.sh
KSR_TMP=/dev/.kitsune-system-mode-rescue.$$
KSR_BB=$KSR_TMP/busybox

ksr_log() {
  log -t KitsuneSystemMode -- "$1" 2>/dev/null || echo "$1"
}

ksr_exit() {
  local result="$1"
  if command -v sm_release_lock >/dev/null 2>&1 && ! sm_release_lock; then
    ksr_log "System Mode transaction lock release failed"
    result=1
  fi
  [ ! -x "$KSR_BB" ] || "$KSR_BB" rm -rf "$KSR_TMP"
  exit "$result"
}

ksr_reboot() {
  /system/bin/setprop sys.powerctl reboot 2>/dev/null
  sm_release_lock || ksr_log "System Mode transaction lock release failed before reboot"
  "$KSR_BB" sleep 2
  "$KSR_BB" reboot -f
  ksr_exit 1
}

[ -x "$KSR_BB_SOURCE" ] && [ -f "$KSR_TRANSACTION" ] || exit 1
"$KSR_BB_SOURCE" mkdir "$KSR_TMP" || exit 1
"$KSR_BB_SOURCE" chmod 0700 "$KSR_TMP" || exit 1
"$KSR_BB_SOURCE" cp "$KSR_BB_SOURCE" "$KSR_BB" || exit 1
"$KSR_BB_SOURCE" chmod 0700 "$KSR_BB" || exit 1
export ASH_STANDALONE=1
# shellcheck disable=SC1090
. "$KSR_TRANSACTION" || ksr_exit 1
sm_configure "$KSR_PAYLOAD" / /system/etc/init/magisk "$KSR_BB" || ksr_exit 1
sm_acquire_lock rescue || ksr_exit 1

# A terminal marker intentionally outlives rollback storage and, for a fresh
# failed install, transaction.env itself. Finish those idempotent handoffs
# before treating an absent canonical receipt as a safe stock system.
sm_recover_setup_marker || {
  ksr_log "Invalid System Mode setup marker; verified external recovery is required"
  ksr_exit 1
}
sm_complete_rollback_terminal || {
  ksr_log "Incomplete System Mode rollback terminal; verified external recovery is required"
  ksr_exit 1
}

# An absent transaction now means the stock system is already safe. An invalid
# transaction is never guessed at; its externally verified recovery remains
# the only authorized fallback.
[ -f "$SM_TRANSACTION_FILE" ] || ksr_exit 0
sm_load_transaction || {
  ksr_log "Invalid System Mode state; verified external recovery is required"
  ksr_exit 1
}

case "$SM_STATE" in
  PREFLIGHTED|STAGED|ROLLBACK_REQUIRED|ROLLING_BACK)
    ksr_log "Recovering an interrupted System Mode mutation from the invariant rescue hook"
    ;;
  COMMITTED)
    sm_register_boot_attempt
    boot_result=$?
    case "$boot_result" in
      0) ;;
      2) ksr_log "A prior System Mode boot attempt did not verify" ;;
      *)
        ksr_log "System Mode boot-attempt registration failed"
        sm_update_state ROLLBACK_REQUIRED || ksr_exit 1
        ;;
    esac
    sm_load_transaction || ksr_exit 1
    active_launcher="$(sm_real_path "$SM_ACTIVE_PAYLOAD/kitsune_system_launcher.sh")" || ksr_exit 1
    active_init="$(sm_real_path "$SM_INIT_PATH")" || ksr_exit 1
    if [ "$SM_STATE" = COMMITTED ] && [ -f "$active_launcher" ] && \
       [ -x "$active_launcher" ] && [ -f "$active_init" ]; then
      ksr_exit 0
    fi
    ksr_log "Committed System Mode boot path is incomplete; restoring the prior transaction"
    ;;
  BOOT_VERIFIED|UNINSTALLED)
    ksr_exit 0
    ;;
  FAILED)
    ksr_log "System Mode recovery is failed; use the verified external restore"
    ksr_exit 1
    ;;
  *)
    ksr_exit 1
    ;;
esac

if ! sm_prepare_persistent_mounts; then
  sm_restore_persistent_mounts || true
  ksr_log "Invariant rescue could not prepare persistent filesystems"
  ksr_exit 1
fi
if ! sm_abort_transaction; then
  sm_restore_persistent_mounts || true
  ksr_log "Invariant rescue rollback failed; use the verified external restore"
  ksr_exit 1
fi
sm_restore_persistent_mounts || {
  ksr_log "Invariant rescue could not restore filesystem mount modes"
  ksr_exit 1
}
"$KSR_BB" sync
# Rollback can remove the persistent rescue version. Keep the /dev BusyBox
# alive until init accepts the reboot or the forced fallback executes.
ksr_reboot
