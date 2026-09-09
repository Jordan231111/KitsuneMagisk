#!/system/bin/sh
# shellcheck shell=busybox
# shellcheck disable=SC2016,SC3043

# Boot-time System Mode launcher. The bootstrap mirrors official v30.7
# scripts/live_setup.sh, but reads immutable payloads from the version selected
# by the manifest-owned transaction.

KSL_PAYLOAD=${0%/*}
KSL_SYSTEM_DIR=${KSL_PAYLOAD%/versions/*}
KSL_BB=$KSL_PAYLOAD/busybox
KSL_TRANSACTION=$KSL_PAYLOAD/system_mode_transaction.sh
KSL_RUNTIME_MOUNT_CREATED=false
KSL_RUNTIME_MOUNT_OWNED=false
KSL_RUNTIME_DIR_CREATED=false
KSL_RUNTIME_MOUNT_TARGET=
KSL_RUNTIME_MOUNT_LIST=/dev/.kitsune-system-mode-runtime-mounts.$$
KSL_SBIN_BACKING=
KSL_SBIN_SYSROOT=
KSL_SBIN_SYSROOT_MOUNTED=false
KSL_RECOVERY_DIR=
KSL_RECOVERY_BB=

ksl_log() {
  log -t KitsuneSystemMode -- "$1" 2>/dev/null || echo "$1"
}

ksl_exit() {
  local result="$1"
  if command -v sm_release_lock >/dev/null 2>&1 && ! sm_release_lock; then
    ksl_log "System Mode transaction lock release failed"
    result=1
  fi
  if [ -n "$KSL_RECOVERY_BB" ] && [ -x "$KSL_RECOVERY_BB" ]; then
    "$KSL_RECOVERY_BB" rm -rf "$KSL_RECOVERY_DIR" || result=1
  fi
  exit "$result"
}

[ -x "$KSL_BB" ] && [ -f "$KSL_TRANSACTION" ] || exit 1
export ASH_STANDALONE=1
# shellcheck disable=SC1090
. "$KSL_TRANSACTION" || exit 1
sm_configure "$KSL_PAYLOAD" / "$KSL_SYSTEM_DIR" "$KSL_BB" || ksl_exit 1
sm_acquire_lock "launcher-${1:-invalid}" || ksl_exit 1
sm_load_transaction || { ksl_log "Invalid System Mode transaction"; ksl_exit 1; }

ksl_reboot() {
  local reboot_bb="${1:-$KSL_BB}"
  /system/bin/setprop sys.powerctl reboot 2>/dev/null
  sm_release_lock || ksl_log "System Mode transaction lock release failed before reboot"
  "$reboot_bb" sleep 2
  "$reboot_bb" reboot -f
  ksl_exit 1
}

ksl_recover_pending() {
  local recovery_dir=/dev/.kitsune-system-mode-launcher.$$ recovery_bb
  recovery_bb=$recovery_dir/busybox
  KSL_RECOVERY_DIR="$recovery_dir"
  KSL_RECOVERY_BB="$recovery_bb"
  "$KSL_BB" mkdir "$recovery_dir" || return 1
  "$KSL_BB" chmod 0700 "$recovery_dir" || return 1
  "$KSL_BB" cp "$KSL_BB" "$recovery_bb" || return 1
  "$KSL_BB" chmod 0700 "$recovery_bb" || return 1
  # shellcheck disable=SC2034 # Used by the sourced transaction functions.
  SM_BB=$recovery_bb
  if ! sm_prepare_persistent_mounts; then
    sm_restore_persistent_mounts || ksl_log "Partial recovery remount restoration failed"
    return 1
  fi
  if ! sm_abort_transaction; then
    sm_restore_persistent_mounts || ksl_log "Failed-recovery remount restoration failed"
    return 1
  fi
  sm_restore_persistent_mounts || ksl_log "Rollback mount-mode restoration failed"
  "$recovery_bb" sync
  # Rollback can remove KSL_PAYLOAD and KSL_BB. Keep the /dev recovery copy
  # alive until init accepts the reboot (or it performs the forced fallback).
  ksl_reboot "$recovery_bb"
}

if [ "$SM_ACTIVE_PAYLOAD" != "$KSL_PAYLOAD" ]; then
  case "$SM_STATE" in
    PREFLIGHTED|STAGED|COMMITTED|ROLLBACK_REQUIRED|ROLLING_BACK)
      # During an upgrade the prior verified RC can run between publishing the
      # new version and switching init to it. Its known-good BusyBox and
      # transaction engine must recover that pending transaction rather than
      # merely rejecting the active-payload mismatch and stranding STAGED.
      ksl_log "Recovering a pending transaction from the prior launcher"
      ksl_recover_pending
      ksl_log "Prior-launcher transaction recovery failed"
      ksl_exit 1
      ;;
    *)
      ksl_log "Refusing a superseded System Mode launcher"
      ksl_exit 1
      ;;
  esac
fi

case "$SM_STATE" in
  COMMITTED)
    sm_register_boot_attempt
    case $? in
      0) ;;
      2)
        ksl_log "Recovering after an unverified System Mode boot attempt"
        ksl_recover_pending
        ksl_exit 1
        ;;
      *)
        ksl_log "Boot-attempt registration failed; rolling back instead of retrying indefinitely"
        ksl_recover_pending
        ksl_exit 1
        ;;
    esac
    ;;
  BOOT_VERIFIED) ;;
  PREFLIGHTED|STAGED|ROLLBACK_REQUIRED|ROLLING_BACK)
    ksl_log "Recovering an interrupted System Mode transaction before Magisk starts"
    ksl_recover_pending
    ksl_exit 1
    ;;
  FAILED)
    ksl_log "System Mode requires the verified external restore"
    ksl_exit 1
    ;;
  *) ksl_exit 1 ;;
esac

ksl_mount_tmpfs() {
  local target="$1"
  [ ! -L "$target" ] || return 1
  if "$KSL_BB" awk -v target="$target" '$2 == target && $3 == "tmpfs" { found=1 } END { exit !found }' /proc/mounts; then
    ksl_runtime_mount_owned "$target" || return 1
    KSL_RUNTIME_MOUNT_OWNED=true
    KSL_RUNTIME_MOUNT_TARGET="$target"
    return 0
  fi
  mount -t tmpfs -o mode=0755 magisk "$target" || return 1
  KSL_RUNTIME_MOUNT_CREATED=true
  KSL_RUNTIME_MOUNT_OWNED=true
  KSL_RUNTIME_MOUNT_TARGET="$target"
  sm_failpoint runtime-tmpfs-mounted
}

ksl_runtime_mount_owned() {
  local marker="$1/.kitsune-system-mode-$SM_INSTALL_ID" uid
  [ -f "$marker" ] && [ ! -L "$marker" ] || return 1
  uid="$($KSL_BB stat -c %u "$marker" 2>/dev/null)" || return 1
  [ "$uid" = 0 ]
}

ksl_runtime_mount_present() {
  "$KSL_BB" awk -v target="$1" '$2 == target { found=1 } END { exit !found }' /proc/mounts
}

ksl_record_runtime_mount() {
  local target="$1" uid mode
  case "$target" in
    "$KSL_RUNTIME_MOUNT_TARGET"/*) ;;
    *) return 1 ;;
  esac
  sm_valid_single_line "$target" || return 1
  if [ ! -e "$KSL_RUNTIME_MOUNT_LIST" ] && [ ! -L "$KSL_RUNTIME_MOUNT_LIST" ]; then
    (set -C; : >"$KSL_RUNTIME_MOUNT_LIST") 2>/dev/null || return 1
    "$KSL_BB" chmod 0600 "$KSL_RUNTIME_MOUNT_LIST" || return 1
  fi
  [ -f "$KSL_RUNTIME_MOUNT_LIST" ] && [ ! -L "$KSL_RUNTIME_MOUNT_LIST" ] || return 1
  uid="$($KSL_BB stat -c %u "$KSL_RUNTIME_MOUNT_LIST" 2>/dev/null)" || return 1
  mode="$($KSL_BB stat -c %a "$KSL_RUNTIME_MOUNT_LIST" 2>/dev/null)" || return 1
  [ "$uid:$mode" = 0:600 ] || return 1
  printf '%s\n' "$target" >>"$KSL_RUNTIME_MOUNT_LIST" || return 1
}

ksl_unmount_one() {
  local target="$1"
  ksl_runtime_mount_present "$target" || return 0
  "$KSL_BB" umount "$target" 2>/dev/null ||
    "$KSL_BB" umount -l "$target" 2>/dev/null || return 1
  ! ksl_runtime_mount_present "$target"
}

ksl_unwind_runtime() {
  local target="${KSL_RUNTIME_MOUNT_TARGET:-$SM_RUNTIME_PATH}" mount_path failed=0
  local owned="$KSL_RUNTIME_MOUNT_OWNED"
  if [ "$owned" != true ] && ksl_runtime_mount_owned "$target"; then
    owned=true
  fi

  if [ "$KSL_RUNTIME_MOUNT_CREATED" = true ] || [ "$owned" = true ]; then
    # Worker tmpfs and legacy /sbin bind mounts must be detached before the
    # runtime tmpfs. The list contains only children created by this launcher.
    if [ -f "$KSL_RUNTIME_MOUNT_LIST" ] && [ ! -L "$KSL_RUNTIME_MOUNT_LIST" ]; then
      while IFS= read -r mount_path; do
        [ -n "$mount_path" ] || continue
        case "$mount_path" in
          "$target"/*) ksl_unmount_one "$mount_path" || failed=1 ;;
          *) failed=1 ;;
        esac
      done <"$KSL_RUNTIME_MOUNT_LIST"
    fi
    # A prior launcher process can die after mounting the worker but before it
    # records the ready property. This exact owned child is safe to retry.
    ksl_unmount_one "$target/.magisk/worker" || failed=1
    ksl_unmount_one "$target" || failed=1
  fi

  if [ "$KSL_SBIN_SYSROOT_MOUNTED" = true ]; then
    if ksl_unmount_one "$KSL_SBIN_SYSROOT"; then
      KSL_SBIN_SYSROOT_MOUNTED=false
    else
      failed=1
    fi
  fi

  "$KSL_BB" rm -f "$KSL_RUNTIME_MOUNT_LIST" || failed=1
  if [ -n "$KSL_SBIN_SYSROOT" ]; then
    sm_remove_tree_safe "Boot sysroot cleanup target" "$KSL_SBIN_SYSROOT" \
      "$KSL_SBIN_SYSROOT" || failed=1
  fi
  if [ -n "$KSL_SBIN_BACKING" ]; then
    sm_remove_tree_safe "Boot backing cleanup target" "$KSL_SBIN_BACKING" \
      "$KSL_SBIN_BACKING" || failed=1
  fi
  if [ "$KSL_RUNTIME_DIR_CREATED" = true ] && [ -d "$target" ] && [ ! -L "$target" ]; then
    "$KSL_BB" rmdir "$target" 2>/dev/null || failed=1
  fi
  KSL_RUNTIME_MOUNT_CREATED=false
  KSL_RUNTIME_MOUNT_OWNED=false
  [ "$failed" = 0 ]
}

ksl_root_block() {
  local source resolved device_number device_name candidate
  source="$("$KSL_BB" awk '
    $5 == "/" {
      for (field = 7; field <= NF; field++) {
        if ($field == "-") {
          print $(field + 2)
          exit
        }
      }
    }
  ' /proc/self/mountinfo)" || return 1
  case "$source" in
    /dev/root)
      resolved="$("$KSL_BB" readlink -f /dev/root 2>/dev/null)" || resolved=
      if [ -n "$resolved" ] && [ -b "$resolved" ]; then
        printf '%s\n' "$resolved"
        return 0
      fi
      ;;
    /dev/*)
      if [ -b "$source" ]; then
        printf '%s\n' "$source"
        return 0
      fi
      ;;
  esac

  device_number="$("$KSL_BB" awk '$5 == "/" { print $3; exit }' /proc/self/mountinfo)" || return 1
  case "$device_number" in *[!0-9:]|*:*:*|:*|*:) return 1 ;; esac
  [ -r "/sys/dev/block/$device_number/uevent" ] || return 1
  device_name="$("$KSL_BB" awk -F= '$1 == "DEVNAME" { print substr($0, 9); exit }' "/sys/dev/block/$device_number/uevent")" || return 1
  case "$device_name" in ''|/*|*..*|*[!A-Za-z0-9._/-]*) return 1 ;; esac
  for candidate in "/dev/block/$device_name" "/dev/$device_name"; do
    if [ -b "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}

ksl_mount_sbin() {
  local block file sfile result=0
  local target="${1:-/sbin}"
  local backing="/dev/.kitsune-system-mode-sbin-$SM_INSTALL_ID"
  local sysroot="/dev/.kitsune-system-mode-sysroot-$SM_INSTALL_ID"
  [ "$target" = /sbin ] || return 1
  [ ! -L /sbin ] || return 1
  KSL_SBIN_BACKING="$backing"
  KSL_SBIN_SYSROOT="$sysroot"
  if "$KSL_BB" awk '$2 == "/" && ($3 == "rootfs" || $3 == "tmpfs") { found=1 } END { exit !found }' /proc/mounts; then
    # Preserve RAM-backed /sbin on boot-scoped /dev. Avoid rewriting /root
    # or changing the root mount mode merely to create a backing directory.
    sm_remove_tree_safe "Boot backing cleanup target" "$backing" "$backing" || return 1
    "$KSL_BB" mkdir -p "$backing" || return 1
    "$KSL_BB" chmod 0750 "$backing" || return 1
    "$KSL_BB" cp -a /sbin/. "$backing/" || return 1
    ksl_mount_tmpfs "$target" || return 1
    for file in "$backing"/*; do
      [ -e "$file" ] || break
      "$KSL_BB" ln -s "$file" "$target/${file##*/}" || return 1
    done
  elif [ -e /sbin ]; then
    ksl_mount_tmpfs "$target" || return 1
    sm_remove_tree_safe "Boot sysroot cleanup target" "$sysroot" "$sysroot" || return 1
    "$KSL_BB" mkdir -p "$sysroot" || return 1
    block="$(ksl_root_block)" || {
      sm_remove_tree_safe "Boot sysroot cleanup target" "$sysroot" "$sysroot"
      return 1
    }
    mount -o ro "$block" "$sysroot" || {
      sm_remove_tree_safe "Boot sysroot cleanup target" "$sysroot" "$sysroot"
      return 1
    }
    KSL_SBIN_SYSROOT_MOUNTED=true
    for file in "$sysroot"/sbin/*; do
      [ -e "$file" ] || break
      if [ -L "$file" ]; then
        "$KSL_BB" cp -a "$file" "$target/" || { result=1; break; }
      else
        sfile="$target/${file##*/}"
        : >"$sfile" || { result=1; break; }
        mount -o bind "$file" "$sfile" || { result=1; break; }
        if ! ksl_record_runtime_mount "$sfile"; then
          ksl_unmount_one "$sfile" || true
          result=1
          break
        fi
      fi
    done
    if ksl_unmount_one "$sysroot"; then
      KSL_SBIN_SYSROOT_MOUNTED=false
    else
      result=1
    fi
    sm_remove_tree_safe "Boot sysroot cleanup target" "$sysroot" "$sysroot" || result=1
    [ "$result" = 0 ] || return 1
  else
    return 1
  fi
}

ksl_validate_preinit_link() {
  local target="$1" resolved_preinit
  if [ "$SM_PREINIT_DIR" = /data/adb ]; then
    [ -L "$target/.magisk/preinit" ] || return 1
    resolved_preinit="$("$KSL_BB" readlink -f "$target/.magisk/preinit" 2>/dev/null)" || return 1
    [ "$resolved_preinit" = /data/adb ] || return 1
  else
    [ ! -e "$target/.magisk/preinit" ] && [ ! -L "$target/.magisk/preinit" ] || return 1
  fi
}

ksl_prepare_runtime() {
  local target="$SM_RUNTIME_PATH" file preinit_result
  sm_detect_preinit "$KSL_PAYLOAD/magisk" || return 1
  [ "$SM_DETECTED_PREINIT_DEVICE" = "$SM_PREINIT_DEVICE" ] &&
    [ "$SM_DETECTED_PREINIT_DIR" = "$SM_PREINIT_DIR" ] || {
      ksl_log "Pre-init storage changed after authorization"
      return 1
    }
  if "$KSL_BB" awk -v target="$target" '$2 == target && $3 == "tmpfs" { found=1 } END { exit !found }' /proc/mounts &&
     [ -f "$target/.kitsune-system-mode-$SM_INSTALL_ID" ] && [ -x "$target/magisk" ]; then
    { [ "$SM_VERSION_CODE" = 0 ] || [ "$("$target/magisk" -V 9>&- 2>/dev/null)" = "$SM_VERSION_CODE" ]; } &&
      ksl_validate_preinit_link "$target"
    return $?
  fi
  case "$target" in
    /sbin) ksl_mount_sbin "$target" || return 1 ;;
    /debug_ramdisk)
      [ ! -L "$target" ] || return 1
      if [ ! -d "$target" ]; then
        "$KSL_BB" mkdir "$target" || return 1
        KSL_RUNTIME_DIR_CREATED=true
      fi
      ksl_mount_tmpfs "$target" || return 1
      ;;
    *) return 1 ;;
  esac

  for file in magisk magisk32 magiskpolicy busybox stub.apk; do
    [ -f "$KSL_PAYLOAD/$file" ] || { [ "$file" = magisk32 ] && continue; return 1; }
    "$KSL_BB" cp -a "$KSL_PAYLOAD/$file" "$target/$file" || return 1
    "$KSL_BB" chmod 0755 "$target/$file" || return 1
    sm_failpoint "runtime-copy:$file" || return 1
  done
  "$KSL_BB" ln -sf ./magisk "$target/su" || return 1
  "$KSL_BB" ln -sf ./magisk "$target/resetprop" || return 1
  "$KSL_BB" ln -sf ./magiskpolicy "$target/supolicy" || return 1
  "$KSL_BB" mkdir -p "$target/.magisk/device" "$target/.magisk/worker" || return 1
  if ! "$KSL_BB" awk -v target="$target/.magisk/worker" '$2 == target { found=1 } END { exit !found }' /proc/mounts; then
    mount -t tmpfs -o mode=0755 magisk-worker "$target/.magisk/worker" || return 1
    if ! ksl_record_runtime_mount "$target/.magisk/worker"; then
      ksl_unmount_one "$target/.magisk/worker" || true
      return 1
    fi
    mount --make-private "$target/.magisk/worker" || return 1
    sm_failpoint runtime-worker-mounted || return 1
  fi
  : >"$target/.magisk/config" || return 1
  : >"$target/.kitsune-system-mode-$SM_INSTALL_ID" || return 1
  "$KSL_BB" chmod 0600 "$target/.kitsune-system-mode-$SM_INSTALL_ID" || return 1
  "$KSL_BB" chmod 0711 "$target" || return 1
  if [ -d /sys/fs/selinux ]; then
    chcon u:object_r:rootfs:s0 "$target" 2>/dev/null || "$KSL_BB" chmod 0711 "$target" || return 1
    chcon -R u:object_r:system_file:s0 "$target"/* "$target/.magisk" 2>/dev/null || return 1
  fi
  export MAGISKTMP="$target"
  MAKEDEV=1 "$target/magisk" --preinit-device 9>&- >/dev/null 2>&1
  preinit_result=$?
  # v30.7 returns 1 when no eligible pre-init partition exists; official
  # live_setup treats that as a supported layout and continues without rules.
  case "$preinit_result" in 0|1) ;; *) return 1 ;; esac
  ksl_validate_preinit_link "$target" || return 1
  return 0
}

ksl_apply_policy() {
  local rule="$SM_RUNTIME_PATH/.magisk/preinit/sepolicy.rule"
  [ -d /sys/fs/selinux ] || return 0
  # The persistent source path is retained as install evidence and, for a
  # legacy migration, exact-uninstall material. Boot always updates the policy
  # that the kernel actually loaded; a precompiled sidecar can be stale while
  # Android is running a split policy assembled from CIL.
  if [ -f "$rule" ]; then
    "$KSL_PAYLOAD/magiskpolicy" --live --magisk --apply "$rule" 9>&-
  else
    "$KSL_PAYLOAD/magiskpolicy" --live --magisk 9>&-
  fi
}

ksl_set_stage_ready() {
  /system/bin/setprop "$1" "$SM_INSTALL_ID"
}

ksl_prepare() {
  [ "$(getprop kitsune.system_mode.ready)" != "$SM_INSTALL_ID" ] || return 0
  ksl_prepare_runtime || {
    ksl_log "Unable to prepare $SM_RUNTIME_PATH from the committed payload"
    return 1
  }
  ksl_apply_policy || {
    ksl_log "Unable to apply Magisk and module policy rules"
    return 1
  }
  sm_failpoint runtime-policy-applied || return 1
  ksl_set_stage_ready kitsune.system_mode.ready || return 1
  "$KSL_BB" rm -f "$KSL_RUNTIME_MOUNT_LIST" || return 1
  ksl_log "Prepared System Mode runtime $SM_RUNTIME_PATH"
}

ksl_run_prepare() {
  local unwind_failed=false
  ksl_prepare && return 0
  ksl_log "System Mode boot preparation failed; restoring this boot before recovery"
  ksl_unwind_runtime || {
    unwind_failed=true
    ksl_log "Runtime mount cleanup was incomplete; a reboot is required"
  }
  case "$SM_STATE" in
    COMMITTED|PREFLIGHTED|STAGED|ROLLBACK_REQUIRED|ROLLING_BACK)
      ksl_log "Entering managed rollback after boot preparation failure"
      if ksl_recover_pending; then
        return 0
      fi
      ksl_log "Managed rollback failed; verified external recovery is required"
      # Clear a partially detached boot tmpfs even when persistent rollback
      # itself cannot complete. A normal cleanup failure leaves this boot safe
      # enough to expose diagnostics instead of creating an automatic loop.
      [ "$unwind_failed" != true ] || ksl_reboot "${SM_BB:-$KSL_BB}"
      return 1
      ;;
    BOOT_VERIFIED)
      # The transaction rollback snapshot is intentionally gone after verified
      # boot. Fail closed once and reboot to clear live policy/runtime state;
      # the next boot sees FAILED and does not recurse into this path.
      if sm_update_state FAILED; then
        ksl_log "Previously verified System Mode failed to initialize; external recovery is required"
        ksl_reboot "$KSL_BB"
      fi
      ksl_log "Unable to record the failed verified runtime; refusing an automatic reboot loop"
      return 1
      ;;
    *) return 1 ;;
  esac
}

ksl_post_fs_data() {
  [ "$(getprop kitsune.system_mode.ready)" = "$SM_INSTALL_ID" ] || return 1
  [ "$(getprop kitsune.system_mode.post_fs_data.ready)" != "$SM_INSTALL_ID" ] || return 0
  "$KSL_BB" timeout 120 "$SM_RUNTIME_PATH/magisk" --post-fs-data 9>&- || {
    ksl_log "Magisk post-fs-data stage failed"
    return 1
  }
  ksl_set_stage_ready kitsune.system_mode.post_fs_data.ready || return 1
  ksl_log "Completed Magisk post-fs-data stage"
}

ksl_service() {
  [ "$(getprop kitsune.system_mode.post_fs_data.ready)" = "$SM_INSTALL_ID" ] || return 1
  [ "$(getprop kitsune.system_mode.service.ready)" != "$SM_INSTALL_ID" ] || return 0
  "$KSL_BB" timeout 120 "$SM_RUNTIME_PATH/magisk" --service 9>&- || {
    ksl_log "Magisk service stage failed"
    return 1
  }
  ksl_set_stage_ready kitsune.system_mode.service.ready || return 1
  ksl_log "Completed Magisk service stage"
}

ksl_boot_complete() {
  [ "$(getprop kitsune.system_mode.service.ready)" = "$SM_INSTALL_ID" ] || return 1
  [ "$(getprop kitsune.system_mode.boot_complete.ready)" != "$SM_INSTALL_ID" ] || return 0
  "$KSL_BB" timeout 120 "$SM_RUNTIME_PATH/magisk" --boot-complete 9>&- || {
    ksl_log "Magisk boot-complete stage failed"
    return 1
  }
  ksl_set_stage_ready kitsune.system_mode.boot_complete.ready || return 1
  ksl_log "Completed Magisk boot-complete stage"
}

ksl_result=0
case "${1:-}" in
  prepare) ksl_run_prepare || ksl_result=$? ;;
  post-fs-data) ksl_post_fs_data || ksl_result=$? ;;
  service) ksl_service || ksl_result=$? ;;
  boot-complete) ksl_boot_complete || ksl_result=$? ;;
  *) ksl_result=2 ;;
esac
ksl_exit "$ksl_result"
