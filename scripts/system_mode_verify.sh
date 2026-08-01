#!/system/bin/sh
# shellcheck disable=SC1090,SC2034,SC2093

# MuMu can leave an init-launched shell blocked when it remounts read-only the
# same filesystem from which that shell is still reading its script. Relocate
# this small verifier to boot tmpfs before it can change a persistent mount.
if [ "${KITSUNE_SYSTEM_MODE_VERIFY_TMPFS:-}" != 1 ]; then
  KITSUNE_SYSTEM_MODE_VERIFY_DIR="/dev/.kitsune-system-mode-verify.$$"
  KITSUNE_SYSTEM_MODE_VERIFY_COPY="$KITSUNE_SYSTEM_MODE_VERIFY_DIR/system_mode_verify.sh"
  mkdir "$KITSUNE_SYSTEM_MODE_VERIFY_DIR" || exit 1
  chmod 0700 "$KITSUNE_SYSTEM_MODE_VERIFY_DIR" || { rm -rf "$KITSUNE_SYSTEM_MODE_VERIFY_DIR"; exit 1; }
  cp "$0" "$KITSUNE_SYSTEM_MODE_VERIFY_COPY" || { rm -rf "$KITSUNE_SYSTEM_MODE_VERIFY_DIR"; exit 1; }
  chmod 0700 "$KITSUNE_SYSTEM_MODE_VERIFY_COPY" || { rm -rf "$KITSUNE_SYSTEM_MODE_VERIFY_DIR"; exit 1; }
  export KITSUNE_SYSTEM_MODE_VERIFY_TMPFS=1 KITSUNE_SYSTEM_MODE_VERIFY_DIR
  exec /system/bin/sh "$KITSUNE_SYSTEM_MODE_VERIFY_COPY"
  rm -rf "$KITSUNE_SYSTEM_MODE_VERIFY_DIR"
  exit 1
fi

verify_exit() {
  local result="$1"
  rm -rf "$KITSUNE_SYSTEM_MODE_VERIFY_DIR"
  exit "$result"
}

SYSTEM_PAYLOAD=/system/etc/init/magisk
TRANSACTION="$SYSTEM_PAYLOAD/system_mode_transaction.sh"
BUSYBOX=/data/adb/magisk/busybox

[ -f "$TRANSACTION" ] && [ -x "$BUSYBOX" ] || verify_exit 1
ui_print() { log -t KitsuneSystemMode -- "$1"; }
. "$TRANSACTION" || verify_exit 1
sm_configure "$SYSTEM_PAYLOAD" / "$SYSTEM_PAYLOAD" "$BUSYBOX" || verify_exit 1
sm_load_transaction || verify_exit 1
case "$SM_STATE" in
  COMMITTED)
    if sm_verify_boot; then
      verify_exit 0
    fi
    # Verification failures transition to ROLLBACK_REQUIRED. Recover during
    # this same completed boot instead of depending on another reboot.
    sm_load_transaction || verify_exit 1
    [ "$SM_STATE" = ROLLBACK_REQUIRED ] || verify_exit 1
    ;;
  BOOT_VERIFIED)
    sm_verify_boot
    verify_exit $?
    ;;
  PREFLIGHTED|STAGED|ROLLBACK_REQUIRED|ROLLING_BACK)
    ;;
  UNINSTALLED)
    verify_exit 0
    ;;
  FAILED)
    ui_print "! System Mode recovery requires the verified external restore"
    verify_exit 1
    ;;
esac

# Rollback can remove /data/adb/magisk. Keep the recovery applet on the boot
# tmpfs, with its required BusyBox basename, until every reverse operation has
# completed.
RECOVERY_DIR="/dev/.kitsune-system-mode.$$"
RECOVERY_BB="$RECOVERY_DIR/busybox"
mkdir "$RECOVERY_DIR" || verify_exit 1
chmod 0700 "$RECOVERY_DIR" || { rm -rf "$RECOVERY_DIR"; verify_exit 1; }
cp "$BUSYBOX" "$RECOVERY_BB" || { rm -rf "$RECOVERY_DIR"; verify_exit 1; }
chmod 0700 "$RECOVERY_BB" || { rm -rf "$RECOVERY_DIR"; verify_exit 1; }
SM_BB="$RECOVERY_BB"
# The daemon can update /data/adb/magisk during boot-complete handling. Stop it
# before restoring that exact tree; this verifier remains root and immediately
# reboots after recovery.
if [ -x "$SM_RUNTIME_PATH/magisk" ]; then
  "$SM_RUNTIME_PATH/magisk" --stop || ui_print "W: Magisk daemon stop failed before rollback"
fi
if ! sm_prepare_persistent_mounts; then
  sm_restore_persistent_mounts || true
  "$RECOVERY_BB" rm -rf "$RECOVERY_DIR"
  verify_exit 1
fi
if sm_recover_pending; then
  sm_restore_persistent_mounts || ui_print "W: Rollback filesystem mode restoration failed"
  "$RECOVERY_BB" sync
  "$RECOVERY_BB" rm -rf "$KITSUNE_SYSTEM_MODE_VERIFY_DIR"
  /system/bin/setprop sys.powerctl reboot 2>/dev/null
  "$RECOVERY_BB" sleep 2
  "$RECOVERY_BB" reboot -f
fi
sm_restore_persistent_mounts || ui_print "W: Rollback filesystem mode restoration failed"
"$RECOVERY_BB" rm -rf "$RECOVERY_DIR"
verify_exit 1
