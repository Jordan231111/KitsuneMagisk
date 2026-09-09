#!/system/bin/sh
# shellcheck shell=busybox
# shellcheck disable=SC1091,SC2153,SC3043

# KitsuneMagisk Direct-System installer for the maintained Magisk base.
#
# This entry point is intentionally separate from Magisk's boot-image flows.
# It is executed in a private mount namespace by the manager and delegates all
# durable state, ownership, rollback, and exact uninstall work to the shared
# transaction engine.

KS_SYSTEM_DIR=/system/etc/init/magisk

ks_log() {
  if command -v ui_print >/dev/null 2>&1; then
    ui_print "$1"
  else
    echo "$1"
  fi
}

ks_fail() {
  ks_log "! $1"
  return 1
}

ks_require_file() {
  [ -f "$1" ] || ks_fail "Missing System Mode payload: ${1##*/}"
}

ks_source_environment() {
  KS_INSTALL_DIR="$1"
  KS_BB="$KS_INSTALL_DIR/busybox"
  [ -x "$KS_BB" ] || { ks_fail "BusyBox transaction runtime is unavailable"; return 1; }
  ks_require_file "$KS_INSTALL_DIR/util_functions.sh" || return 1
  ks_require_file "$KS_INSTALL_DIR/system_mode_transaction.sh" || return 1
  # shellcheck disable=SC1090
  . "$KS_INSTALL_DIR/util_functions.sh" || return 1
  # shellcheck disable=SC1090
  . "$KS_INSTALL_DIR/system_mode_transaction.sh" || return 1
  sm_configure "$KS_INSTALL_DIR" / "$KS_SYSTEM_DIR" "$KS_BB"
}

ks_validate_identity() {
  sm_valid_hex "${KITSUNE_SOURCE_COMMIT:-}" 40 || {
    ks_fail "The build is missing its full source identity"
    return 1
  }
  sm_valid_hex "${KITSUNE_UPSTREAM_BASE:-}" 40 || {
    ks_fail "The build is missing its upstream identity"
    return 1
  }
  [ "${KITSUNE_SOURCE_DIRTY:-true}" = false ] || {
    ks_fail "System Mode requires an artifact built from an exact clean commit"
    return 1
  }
  case "${MAGISK_VER_CODE:-}" in *[!0-9]*|'') ks_fail "The build is missing its version code"; return 1 ;; esac
  case "${MAGISK_VER:-}" in *kitsune*) ;; *) ks_fail "Refusing a non-Kitsune System Mode payload"; return 1 ;; esac
  return 0
}

ks_validate_debug_payload() {
  local identity
  [ -x "$KS_INSTALL_DIR/magisk" ] || { ks_fail "Magisk payload binary is unavailable"; return 1; }
  identity="$("$KS_INSTALL_DIR/magisk" -c 2>/dev/null)"
  [ "$identity" = "${MAGISK_VER}:MAGISK:D (${MAGISK_VER_CODE})" ] && return 0
  ks_fail "System Mode remains disabled in release builds until the release matrix passes"
}

ks_validate_target() {
  local api
  [ "$(id -u)" = 0 ] || { ks_fail "System Mode requires an authorized root shell"; return 1; }
  api="$(getprop ro.build.version.sdk)"
  case "$api" in *[!0-9]*|'') ks_fail "Unable to determine Android API level"; return 1 ;; esac
  [ "$api" -ge 25 ] || {
    ks_fail "System Mode is not qualified on Android 6/API 23-24; use normal Magisk installation"
    return 1
  }
  # Receive the daemon's module unmounts before freezing our private view.
  # A fully private copy here would retain stale, read-only module overlays.
  mount --make-rslave / || { ks_fail "Unable to isolate the installer mount namespace"; return 1; }
  SM_SLAVE_MOUNT_NAMESPACE=true
  return 0
}

ks_validate_policy() {
  local strategy="$SM_SELINUX_STRATEGY" policy="$SM_POLICY_SOURCE"
  local output="$KS_INSTALL_DIR/.kitsune-sepolicy-$SM_TRANSACTION_ID"
  [ -d /sys/fs/selinux ] || return 0
  strategy="${strategy#live+}"
  ks_log "- Validating SELinux strategy: $strategy"
  case "$strategy" in
    disabled)
      return 0
      ;;
    precompiled|monolithic)
      [ -n "$policy" ] || { ks_fail "The authorized SELinux source is missing"; return 1; }
      [ -f "$policy" ] || { ks_fail "The authorized SELinux source is unavailable"; return 1; }
      "$KS_INSTALL_DIR/magiskpolicy" --load "$policy" --save "$output" --magisk || {
        ks_fail "The next-boot SELinux policy cannot be parsed with the included magiskpolicy"
        return 1
      }
      ;;
    split)
      "$KS_INSTALL_DIR/magiskpolicy" --load-split --save "$output" --magisk || {
        ks_fail "The split SELinux policy cannot be compiled with the included magiskpolicy"
        return 1
      }
      ;;
    *)
      ks_fail "Unsupported SELinux strategy: $strategy"
      return 1
      ;;
  esac
  [ -s "$output" ] || { ks_fail "SELinux validation produced an empty policy"; return 1; }
  sm_fsync "$output" || return 1
  "$SM_BB" rm -f "$output" || return 1
  sm_fsync "$KS_INSTALL_DIR" 2>/dev/null || true

  # The persistent and running policies are left byte-for-byte unchanged by
  # preflight. Parse the live policy with the same built-in rules; the boot
  # launcher performs the actual live load from init before entering the
  # magisk domain.
  "$KS_INSTALL_DIR/magiskpolicy" --save "$output.live" --magisk || {
    ks_fail "The running SELinux policy cannot be parsed with the built-in Magisk rules"
    return 1
  }
  [ -s "$output.live" ] || return 1
  "$SM_BB" rm -f "$output.live" || return 1
  return 0
}

ks_copy_payload() {
  local source="$1" destination="$2" name
  for name in \
    busybox magisk magiskboot magiskinit magiskpolicy stub.apk \
    util_functions.sh boot_patch.sh addon.d.sh \
    app_functions.sh uninstaller.sh module_installer.sh \
    kitsune_system_install.sh kitsune_system_launcher.sh kitsune_system_rescue.sh \
    system_mode_transaction.sh system_mode_verify.sh; do
    ks_require_file "$source/$name" || return 1
    # Manager extraction uses symlinks into nativeLibraryDir on a normal full
    # APK. Persistent System Mode payloads must own bytes, not references that
    # disappear when Android replaces or removes the manager package.
    "$SM_BB" cp -aL "$source/$name" "$destination/$name" || return 1
  done
  if [ -f "$source/magisk32" ]; then
    "$SM_BB" cp -aL "$source/magisk32" "$destination/magisk32" || return 1
  fi
  return 0
}

ks_stage_payload() {
  local stage real_parent
  stage="$(sm_real_path "$SM_STAGING_PATH")" || return 1
  sm_remove_tree_safe "Payload staging cleanup target" "$SM_STAGING_PATH" "$stage" || return 1
  "$SM_BB" mkdir -p "$stage" || return 1
  ks_copy_payload "$KS_INSTALL_DIR" "$stage" || return 1
  "$SM_BB" cp -aL "$KS_ARTIFACT" "$stage/magisk.apk" || return 1
  [ "$(sm_sha256_file "$stage/magisk.apk")" = "$SM_ARTIFACT_SHA256" ] || {
    ks_fail "The copied manager artifact does not match transaction identity"
    return 1
  }
  {
    printf 'SYSTEMMODE=true\n'
    printf 'SYSTEM_MODE_SCHEMA=1\n'
    printf 'INSTALL_ID=%s\n' "$SM_INSTALL_ID"
    printf 'TRANSACTION_ID=%s\n' "$SM_TRANSACTION_ID"
    printf 'SOURCE_COMMIT=%s\n' "$SM_SOURCE_COMMIT"
    printf 'UPSTREAM_BASE=%s\n' "$SM_UPSTREAM_BASE"
    printf 'VERSION_CODE=%s\n' "$SM_VERSION_CODE"
  } >"$stage/config" || return 1
  "$SM_BB" chmod 0755 "$stage" "$stage"/*.sh "$stage/busybox" "$stage/magisk" \
    "$stage/magiskboot" "$stage/magiskinit" "$stage/magiskpolicy" || return 1
  [ ! -f "$stage/magisk32" ] || "$SM_BB" chmod 0755 "$stage/magisk32" || return 1
  "$SM_BB" chmod 0644 "$stage/magisk.apk" "$stage/stub.apk" "$stage/config" || return 1
  "$SM_BB" chown -R 0:0 "$stage" || return 1
  if [ -d /sys/fs/selinux ]; then
    chcon -R u:object_r:system_file:s0 "$stage" || {
      ks_fail "Unable to label the staged System Mode payload"
      return 1
    }
  fi
  sm_fsync_tree "$stage" || return 1
  real_parent="$(sm_parent "$stage")" || return 1
  sm_fsync "$real_parent" || return 1
  sm_failpoint payload-staged
}

ks_publish_rescue_payload() {
  local stage root versions active
  stage="$(sm_real_path "$(sm_rescue_stage_path)")" || return 1
  root="$(sm_real_path "$SM_RESCUE_DIR")" || return 1
  versions="$root/versions"
  active="$(sm_real_path "$SM_RESCUE_PAYLOAD")" || return 1
  sm_remove_tree_safe "Rescue staging cleanup target" "$(sm_rescue_stage_path)" "$stage" || return 1
  if sm_path_present "$root"; then
    [ -d "$root" ] && [ ! -L "$root" ] || {
      ks_fail "The invariant rescue root is unsafe"
      return 1
    }
  else
    "$SM_BB" mkdir "$root" || return 1
  fi
  if sm_path_present "$versions"; then
    [ -d "$versions" ] && [ ! -L "$versions" ] || {
      ks_fail "The invariant rescue version root is unsafe"
      return 1
    }
  else
    "$SM_BB" mkdir "$versions" || return 1
  fi
  [ ! -e "$active" ] && [ ! -L "$active" ] || {
    ks_fail "The invariant rescue version already exists"
    return 1
  }
  "$SM_BB" mkdir "$stage" || return 1
  for name in busybox system_mode_transaction.sh kitsune_system_rescue.sh; do
    ks_require_file "$KS_INSTALL_DIR/$name" || return 1
    "$SM_BB" cp -aL "$KS_INSTALL_DIR/$name" "$stage/$name" || return 1
  done
  "$SM_BB" chmod 0700 "$stage" "$stage/busybox" "$stage/kitsune_system_rescue.sh" || return 1
  "$SM_BB" chmod 0600 "$stage/system_mode_transaction.sh" || return 1
  "$SM_BB" chown -R 0:0 "$stage" || return 1
  "$SM_BB" chown 0:0 "$root" "$versions" || return 1
  if [ -d /sys/fs/selinux ]; then
    chcon -R u:object_r:system_file:s0 "$stage" || {
      ks_fail "Unable to label the invariant rescue payload"
      return 1
    }
    chcon u:object_r:system_file:s0 "$root" "$versions" || return 1
  fi
  sm_fsync_tree "$stage" || return 1
  sm_fsync "$root" "$versions" "$(sm_parent "$root")" || return 1
  sm_failpoint rescue-payload-staged || return 1
  sm_atomic_publish "$stage" "$active" rescue-version-published
}

ks_write_rescue_rc() {
  local staged="$1"
  {
    printf '# KitsuneMagisk System Mode invariant rescue schema 1; generated, do not edit.\n'
    printf 'on post-fs-data\n'
    printf '    exec u:r:init:s0 0 0 -- %s/busybox sh %s/kitsune_system_rescue.sh\n' \
      "$SM_RESCUE_PAYLOAD" "$SM_RESCUE_PAYLOAD"
  } >"$staged" || return 1
  "$SM_BB" chmod 0644 "$staged" || return 1
  "$SM_BB" chown 0:0 "$staged" || return 1
  if [ -d /sys/fs/selinux ]; then
    chcon u:object_r:system_file:s0 "$staged" || return 1
  fi
  sm_fsync "$staged"
}

ks_publish_rescue_rc() {
  local real staged
  real="$(sm_real_path "$SM_RESCUE_RC")" || return 1
  staged="$real.kitsune-new"
  ks_write_rescue_rc "$staged" || return 1
  sm_atomic_publish "$staged" "$real" rescue-init-published
}

ks_remove_superseded_rescue() {
  local versions active item
  versions="$(sm_real_path "$SM_RESCUE_PAYLOAD_PREFIX")" || return 1
  active="$(sm_real_path "$SM_RESCUE_PAYLOAD")" || return 1
  "$SM_BB" find "$versions" -mindepth 1 -maxdepth 1 -print | while IFS= read -r item; do
    [ "$item" = "$active" ] && continue
    [ -d "$item" ] && [ ! -L "$item" ] || exit 1
    sm_remove_tree_safe "Superseded rescue target" "$item" "$item" || exit 1
  done || return 1
  sm_fsync "$versions" "$(sm_parent "$versions")" || return 1
  sm_failpoint rescue-superseded-removed
}

ks_publish_version() {
  local stage root versions active
  stage="$(sm_real_path "$SM_STAGING_PATH")" || return 1
  root="$(sm_real_path "$SM_SYSTEM_DIR")" || return 1
  versions="$root/versions"
  active="$(sm_real_path "$SM_ACTIVE_PAYLOAD")" || return 1
  "$SM_BB" mkdir -p "$versions" || return 1
  sm_fsync "$root" "$versions" "$(sm_parent "$root")" || return 1
  [ ! -e "$active" ] && [ ! -L "$active" ] || {
    ks_fail "The versioned System Mode destination already exists"
    return 1
  }
  sm_atomic_publish "$stage" "$active" version-published
}

ks_write_rc() {
  local staged="$1" ready="kitsune.system_mode.ready=$SM_INSTALL_ID"
  local post_ready="kitsune.system_mode.post_fs_data.ready=$SM_INSTALL_ID"
  local service_ready="kitsune.system_mode.service.ready=$SM_INSTALL_ID"
  local boot_ready="kitsune.system_mode.boot_complete.ready=$SM_INSTALL_ID"
  {
    printf '# KitsuneMagisk System Mode schema 1; generated, do not edit.\n'
    printf 'on post-fs-data\n'
    printf '    exec u:r:init:s0 0 0 -- %s/busybox sh %s/kitsune_system_launcher.sh prepare\n' \
      "$SM_ACTIVE_PAYLOAD" "$SM_ACTIVE_PAYLOAD"
    # Property actions can run after init has queued nonencrypted/boot and
    # started zygote. Finish post-fs-data here, before leaving this boot event.
    # The wrapper still refuses to run unless prepare published its success.
    printf '    exec u:r:magisk:s0 0 0 -- %s/busybox sh %s/kitsune_system_launcher.sh post-fs-data\n' \
      "$SM_ACTIVE_PAYLOAD" "$SM_ACTIVE_PAYLOAD"
    printf '\n'
    printf 'on property:vold.decrypt=trigger_restart_framework\n'
    printf '    setprop kitsune.system_mode.service.phase 1\n'
    printf '\n'
    printf 'on nonencrypted\n'
    printf '    setprop kitsune.system_mode.service.phase 1\n'
    printf '\n'
    printf 'on property:%s && property:kitsune.system_mode.service.phase=1\n' "$post_ready"
    printf '    exec u:r:magisk:s0 0 0 -- %s/busybox sh %s/kitsune_system_launcher.sh service\n' \
      "$SM_ACTIVE_PAYLOAD" "$SM_ACTIVE_PAYLOAD"
    printf '\n'
    printf 'on property:sys.boot_completed=1 && property:%s\n' "$service_ready"
    printf '    exec u:r:magisk:s0 0 0 -- %s/busybox sh %s/kitsune_system_launcher.sh boot-complete\n' \
      "$SM_ACTIVE_PAYLOAD" "$SM_ACTIVE_PAYLOAD"
    printf '\n'
    printf 'on property:%s\n' "$boot_ready"
    printf '    exec u:r:init:s0 0 0 -- %s/busybox sh %s/system_mode_verify.sh\n' \
      "$SM_ACTIVE_PAYLOAD" "$SM_ACTIVE_PAYLOAD"
    printf '\n'
    printf 'on property:init.svc.zygote=restarting && property:%s\n' "$ready"
    printf '    exec u:r:magisk:s0 0 0 -- %s/magisk --zygote-restart\n' "$SM_RUNTIME_PATH"
    printf '\n'
    printf 'on property:init.svc.zygote=stopped && property:%s\n' "$ready"
    printf '    exec u:r:magisk:s0 0 0 -- %s/magisk --zygote-restart\n' "$SM_RUNTIME_PATH"
  } >"$staged" || return 1
  "$SM_BB" chmod 0644 "$staged" || return 1
  "$SM_BB" chown 0:0 "$staged" || return 1
  if [ -d /sys/fs/selinux ]; then
    chcon u:object_r:system_file:s0 "$staged" || return 1
  fi
  sm_fsync "$staged"
}

ks_publish_rc() {
  local real staged
  real="$(sm_real_path "$SM_INIT_PATH")" || return 1
  staged="$real.kitsune-new"
  ks_write_rc "$staged" || return 1
  sm_atomic_publish "$staged" "$real" init-published
}

ks_remove_superseded_payload() {
  local root versions active item
  root="$(sm_real_path "$SM_SYSTEM_DIR")" || return 1
  versions="$root/versions"
  active="$(sm_real_path "$SM_ACTIVE_PAYLOAD")" || return 1
  "$SM_BB" find "$root" -mindepth 1 -maxdepth 1 -print | while IFS= read -r item; do
    [ "$item" = "$versions" ] && continue
    sm_remove_tree_safe "Superseded payload target" "$item" "$item" || exit 1
  done || return 1
  "$SM_BB" find "$versions" -mindepth 1 -maxdepth 1 -print | while IFS= read -r item; do
    [ "$item" = "$active" ] && continue
    sm_remove_tree_safe "Superseded version target" "$item" "$item" || exit 1
  done || return 1
  sm_fsync "$root" "$versions" || return 1
  sm_failpoint superseded-payload-removed
}

ks_publish_runtime() {
  local stage old=/data/adb/.magisk.kitsune-unused runtime=/data/adb/magisk
  stage="$(sm_runtime_stage_path)" || return 1
  old="$(sm_runtime_old_path)" || return 1
  sm_remove_tree_safe "Runtime staging cleanup target" "$stage" "$stage" || return 1
  sm_remove_tree_safe "Runtime prior-version cleanup target" "$old" "$old" || return 1
  "$SM_BB" mkdir -p "$stage" || return 1
  ks_copy_payload "$SM_ACTIVE_PAYLOAD" "$stage" || return 1
  "$SM_BB" cp -a "$SM_ACTIVE_PAYLOAD/magisk.apk" "$stage/magisk.apk" || return 1
  "$SM_BB" cp -a "$SM_ACTIVE_PAYLOAD/config" "$stage/config" || return 1
  "$SM_BB" chmod 0700 "$stage" || return 1
  "$SM_BB" chown -R 0:0 "$stage" || return 1
  sm_fsync_tree "$stage" || return 1
  sm_fsync /data/adb || return 1
  if [ -e "$runtime" ] || [ -L "$runtime" ]; then
    "$SM_BB" mv "$runtime" "$old" || return 1
    sm_fsync /data/adb || return 1
    sm_failpoint runtime-backed-up || return 1
  fi
  "$SM_BB" mv "$stage" "$runtime" || return 1
  sm_fsync_tree "$runtime" || return 1
  sm_fsync /data/adb || return 1
  sm_failpoint runtime-published || return 1
  "$runtime/magisk" --restorecon || {
    ks_fail "Unable to normalize Magisk runtime metadata"
    return 1
  }
  sm_fsync_tree "$runtime"
}

ks_rollback() {
  local result=0 has_transaction=false
  sm_cleanup_authorization_claim || result=1
  sm_recover_setup_marker || result=1
  sm_complete_rollback_terminal || result=1
  if [ "$result" = 0 ] && [ -f "$SM_TRANSACTION_FILE" ]; then
    has_transaction=true
    sm_load_transaction || result=1
  fi
  if [ "$result" = 0 ] && [ "$has_transaction" = true ]; then
    case "${SM_STATE:-}" in
      STAGED|COMMITTED|ROLLBACK_REQUIRED|ROLLING_BACK)
        # A failed initial prepare or a partial post-commit ro restoration can
        # leave a mixed mount set. Re-establish rw on every rollback target;
        # the transaction engine retains the complete original-ro set.
        sm_prepare_persistent_mounts || result=1
        ;;
    esac
  fi
  if [ "$result" = 0 ] && [ -f "$SM_TRANSACTION_FILE" ]; then
    sm_abort_transaction || result=1
  fi
  sm_restore_persistent_mounts || result=1
  return "$result"
}

ks_install() {
  KS_ARTIFACT="$2"
  ks_validate_identity || return 1
  ks_validate_debug_payload || return 1
  ks_validate_target || return 1
  [ -f "$KS_ARTIFACT" ] || { ks_fail "The exact manager APK is unavailable"; return 1; }
  sm_begin_transaction "$KS_ARTIFACT" || return 1
  sm_prepare_persistent_mounts || return 1
  ks_validate_policy || return 1
  ks_stage_payload || return 1
  ks_publish_rescue_payload || return 1
  sm_journal_mark rescue-version-published "$SM_RESCUE_PAYLOAD" || return 1
  ks_publish_rescue_rc || return 1
  sm_journal_mark rescue-init-published "$SM_RESCUE_RC" || return 1
  ks_remove_superseded_rescue || return 1
  sm_journal_mark rescue-superseded-removed "$SM_RESCUE_DIR" || return 1
  ks_publish_version || return 1
  sm_journal_mark version-published "$SM_ACTIVE_PAYLOAD" || return 1
  ks_publish_rc || return 1
  sm_journal_mark init-published "$SM_INIT_PATH" || return 1
  ks_remove_superseded_payload || return 1
  sm_journal_mark superseded-payload-removed "$SM_SYSTEM_DIR" || return 1
  ks_publish_runtime || return 1
  sm_journal_mark runtime-published /data/adb/magisk || return 1
  sm_commit_transaction || return 1
  sm_restore_persistent_mounts || {
    ks_fail "Installation committed, but filesystem modes could not be restored"
    return 1
  }
  ks_log "- System Mode committed; reboot once to verify this transaction"
  return 0
}

ks_recover() {
  local result=0
  ks_validate_target || return 1
  sm_cleanup_authorization_claim || return 1
  sm_validate_secure_dir_base || return 1
  sm_recover_setup_marker || return 1
  sm_complete_rollback_terminal || return 1
  if [ ! -f "$SM_TRANSACTION_FILE" ]; then
    ks_log "- Interrupted preflight was already recovered"
    return 0
  fi
  sm_load_transaction || { ks_fail "No valid System Mode transaction is available"; return 1; }
  if ! sm_prepare_persistent_mounts; then
    sm_restore_persistent_mounts || true
    return 1
  fi
  sm_abort_transaction || result=1
  sm_restore_persistent_mounts || result=1
  [ "$result" = 0 ] || return 1
  ks_log "- Interrupted System Mode transaction recovered"
}

ks_uninstall() {
  local result=0
  ks_validate_target || return 1
  sm_cleanup_authorization_claim || return 1
  sm_validate_secure_dir_base || return 1
  sm_load_transaction || { ks_fail "A valid System Mode receipt is required"; return 1; }
  if ! sm_prepare_persistent_mounts; then
    sm_restore_persistent_mounts || true
    return 1
  fi
  sm_uninstall || result=1
  sm_restore_persistent_mounts || result=1
  [ "$result" = 0 ] || return 1
  "$SM_BB" sync
  ks_log "- Exact System Mode uninstall completed; reboot to finish"
}

ks_main() {
  local action="${1:-}" install_dir="${2:-}" result release_result=0
  [ -n "$install_dir" ] || { ks_fail "Usage: kitsune_system_install.sh ACTION INSTALL_DIR [APK]"; return 2; }
  case "$action" in
    install) [ "$#" -eq 3 ] || { ks_fail "Install requires the exact manager APK"; return 2; } ;;
    recover|uninstall) ;;
    *) ks_fail "Unknown System Mode action: $action"; return 2 ;;
  esac
  ks_source_environment "$install_dir" || return 1
  sm_acquire_lock "installer-$action" || return 1
  case "$action" in
    install)
      ks_install "$install_dir" "$3"
      result=$?
      if [ "$result" != 0 ]; then
        ks_log "! System Mode installation failed; restoring the prior transaction"
        ks_rollback || ks_log "! Automatic rollback failed; use the verified external restore"
      fi
      ;;
    recover) ks_recover; result=$? ;;
    uninstall) ks_uninstall; result=$? ;;
  esac
  sm_release_lock || release_result=1
  if [ "$result" = 0 ] && [ "$release_result" != 0 ]; then
    ks_fail "System Mode transaction lock could not be released cleanly"
    result=1
  fi
  return "$result"
}

ks_main "$@"
