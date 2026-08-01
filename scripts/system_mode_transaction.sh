#!/system/bin/sh

# Persistent transaction support shared by app, recovery, addon.d, boot
# verification, and uninstall entry points. This file is sourced; it never
# mutates a target merely by being loaded.
# shellcheck disable=SC2016

SM_SCHEMA_VERSION=1
SM_AUTHORIZATION_FILE=/data/local/tmp/kitsune-system-mode-recovery-v1.env
SM_STATE_DIR=/data/adb/kitsune/system-mode
SM_TRANSACTION_FILE=$SM_STATE_DIR/transaction.env
SM_MANIFEST_COPY=$SM_STATE_DIR/install-manifest.json
SM_OWNERSHIP_FILE=$SM_STATE_DIR/ownership.tsv
SM_ORIGINAL_FILE=$SM_STATE_DIR/originals.tsv
SM_JOURNAL_FILE=$SM_STATE_DIR/journal.tsv
SM_BOOT_PROOF=$SM_STATE_DIR/boot-verified.env
SM_TAB="$(printf '\t')"

sm_log() {
  if command -v ui_print >/dev/null 2>&1; then
    ui_print "$1"
  else
    echo "$1"
  fi
}

sm_configure() {
  SM_INSTALL_DIR="$1"
  SM_MIRROR="${2:-/}"
  SM_SYSTEM_DIR="${3:-/system/etc/init/magisk}"
  SM_BB="${4:-$SM_INSTALL_DIR/busybox}"
  [ -x "$SM_BB" ] || SM_BB=/data/adb/magisk/busybox
  [ -x "$SM_BB" ] || {
    sm_log "! System Mode transaction runtime is unavailable"
    return 1
  }
  case "$SM_MIRROR" in
    /|/proc/*/attr) ;;
    *) sm_log "! Invalid System Mode transaction mirror"; return 1 ;;
  esac
  return 0
}

sm_get() {
  local key="$1" file="$2"
  [ -f "$file" ] || return 1
  "$SM_BB" sed -n "s/^${key}=//p" "$file" | "$SM_BB" head -n 1
}

sm_sha256_file() {
  "$SM_BB" sha256sum "$1" 2>/dev/null | "$SM_BB" awk 'NR == 1 { print $1 }'
}

sm_path_present() {
  [ -e "$1" ] || [ -L "$1" ]
}

sm_real_path() {
  case "$1" in
    /data|/data/*) printf '%s\n' "$1" ;;
    /*)
      if [ "$SM_MIRROR" = / ]; then
        printf '%s\n' "$1"
      else
        printf '%s%s\n' "$SM_MIRROR" "$1"
      fi
      ;;
    *) return 1 ;;
  esac
}

sm_parent() {
  "$SM_BB" dirname "$1"
}

sm_fsync() {
  [ "$#" -gt 0 ] || return 1
  "$SM_BB" fsync "$@"
}

sm_fsync_existing_parent() {
  local parent
  parent="$(sm_parent "$1")" || return 1
  # An absent leaf cannot have changed a namespace whose parent is also
  # absent. Do not turn an exact rollback/uninstall into a false failure just
  # because an optional tree such as /system/addon.d never existed.
  [ ! -d "$parent" ] || sm_fsync "$parent"
}

sm_probe_writable_directory() {
  local directory="$1" probe="$1/.kitsune-system-mode-write-probe.$$"
  [ -d "$directory" ] || return 1
  sm_path_present "$probe" && return 1
  if ! (set -C; : >"$probe") 2>/dev/null; then
    return 1
  fi
  if ! sm_fsync "$probe" "$directory"; then
    "$SM_BB" rm -f "$probe"
    sm_fsync "$directory" 2>/dev/null || true
    return 1
  fi
  "$SM_BB" rm -f "$probe" || return 1
  sm_fsync "$directory"
}

sm_mountpoint_for() {
  local path="$1"
  "$SM_BB" awk -v target="$path" '
    $2 == "/" || target == $2 || index(target, $2 "/") == 1 {
      if (length($2) > length(best)) best=$2
    }
    END { if (best != "") print best; else exit 1 }
  ' /proc/mounts
}

sm_remount() {
  local mode="$1" mountpoint="$2"
  if [ -x /system/bin/mount ]; then
    /system/bin/mount -o "$mode,remount" "$mountpoint"
  else
    "$SM_BB" mount -o "$mode,remount" "$mountpoint"
  fi
}

sm_prepare_persistent_mounts() {
  local canonical real mountpoint options seen=
  SM_PERSISTENT_REMOUNTED=
  for canonical in "$SM_SYSTEM_DIR" "$SM_SYSTEM_DIR.rc" "$SM_INIT_PATH" \
    "$SM_POLICY_PATH" /system/etc/init/bootanim.rc \
    /system/addon.d/99-magisk.sh /system/addon.d/magisk /data/adb/magisk; do
    [ -n "$canonical" ] || continue
    real="$(sm_real_path "$canonical")" || return 1
    mountpoint="$(sm_mountpoint_for "$real")" || return 1
    case "|$seen|" in *"|$mountpoint|"*) continue ;; esac
    seen="${seen:+$seen|}$mountpoint"
    options="$("$SM_BB" awk -v mountpoint="$mountpoint" '$2 == mountpoint { print $4; exit }' /proc/mounts)" || return 1
    case ",$options," in *,rw,*) continue ;; esac
    sm_log "- Remounting System Mode filesystem read-write: $mountpoint"
    sm_remount rw "$mountpoint" || {
      sm_log "! Unable to remount System Mode filesystem: $mountpoint"
      return 1
    }
    SM_PERSISTENT_REMOUNTED="${SM_PERSISTENT_REMOUNTED:+$SM_PERSISTENT_REMOUNTED }$mountpoint"
  done
  return 0
}

sm_restore_persistent_mounts() {
  local mountpoint failed=0
  # The managed Android mountpoints are fixed paths without whitespace.
  # shellcheck disable=SC2086
  for mountpoint in $SM_PERSISTENT_REMOUNTED; do
    sm_remount ro "$mountpoint" || failed=1
  done
  SM_PERSISTENT_REMOUNTED=
  [ "$failed" = 0 ]
}

sm_fsync_tree() {
  local root="$1" path
  if [ -f "$root" ]; then
    sm_fsync "$root" || return 1
  elif [ -d "$root" ]; then
    "$SM_BB" find "$root" -type f -print | while IFS= read -r path; do
      sm_fsync "$path" || exit 1
    done || return 1
    "$SM_BB" find "$root" -type d -print | "$SM_BB" sort -r | while IFS= read -r path; do
      sm_fsync "$path" || exit 1
    done || return 1
  fi
  return 0
}

sm_context() {
  # Disabled/permissive kernels do not enforce labels, and some writable
  # emulator filesystems present the same xattr as "unlabeled" after a cold
  # start. Keep strict context ownership where SELinux actually enforces it.
  if [ ! -r /sys/fs/selinux/enforce ] || [ "$(cat /sys/fs/selinux/enforce 2>/dev/null)" != 1 ]; then
    printf '%s\n' -
    return 0
  fi
  "$SM_BB" ls -Zd "$1" 2>/dev/null | "$SM_BB" awk '
    NR == 1 {
      for (i = 1; i <= NF; i++) {
        if ($i ~ /^u:[^:]+:[^:]+:s[0-9]/) {
          print $i
          exit
        }
      }
    }
  '
}

sm_atomic_publish() {
  local staged="$1" destination="$2" boundary="${3:-atomic-publish}" parent expected actual size keep short
  parent="$(sm_parent "$destination")" || return 1
  case "${KITSUNE_SYSTEM_MODE_FAIL_AT:-}" in
    "enospc:$boundary"|"erofs:$boundary")
      sm_log "! Injected System Mode I/O failure at $boundary"
      return 98
      ;;
    "short-write:$boundary")
      [ -f "$staged" ] || return 98
      expected="$(sm_sha256_file "$staged")" || return 98
      size="$($SM_BB stat -c %s "$staged")" || return 98
      keep=$((size > 0 ? size - 1 : 0))
      short="$staged.kitsune-short"
      "$SM_BB" dd if="$staged" of="$short" bs=1 count="$keep" 2>/dev/null || return 98
      "$SM_BB" mv -f "$short" "$staged" || return 98
      actual="$(sm_sha256_file "$staged")" || return 98
      [ "$actual" != "$expected" ] || return 98
      sm_log "! Detected injected System Mode short write at $boundary"
      return 98
      ;;
  esac
  sm_fsync_tree "$staged" || return 1
  case "${KITSUNE_SYSTEM_MODE_FAIL_AT:-}" in
    "fsync-file:$boundary") sm_log "! Injected System Mode file fsync failure at $boundary"; return 98 ;;
    "rename:$boundary") sm_log "! Injected System Mode rename failure at $boundary"; return 98 ;;
  esac
  "$SM_BB" mv -f "$staged" "$destination" || return 1
  case "${KITSUNE_SYSTEM_MODE_FAIL_AT:-}" in
    "fsync-parent:$boundary") sm_log "! Injected System Mode parent fsync failure at $boundary"; return 98 ;;
  esac
  sm_fsync "$destination" "$parent" || return 1
  sm_failpoint "$boundary"
}

sm_failpoint() {
  local boundary="$1"
  case "${KITSUNE_SYSTEM_MODE_FAIL_AT:-}" in
    "$boundary")
      sm_log "! Injected System Mode failure at $boundary"
      return 97
      ;;
    "process-death:$boundary")
      sm_log "! Injected System Mode process death at $boundary"
      kill -9 $$
      ;;
    "reboot:$boundary")
      sm_log "! Injected System Mode reboot at $boundary"
      /system/bin/reboot 2>/dev/null
      kill -9 $$
      ;;
  esac
  return 0
}

sm_digest_path() {
  local path="$1" item rel kind digest mode uid gid target
  if [ -f "$path" ]; then
    sm_sha256_file "$path"
  elif [ -L "$path" ]; then
    target="$($SM_BB readlink "$path")" || return 1
    printf 'link:%s' "$target" | "$SM_BB" sha256sum | "$SM_BB" awk '{ print $1 }'
  elif [ -d "$path" ]; then
    (
      cd "$path" || exit 1
      "$SM_BB" find . -mindepth 1 -print | "$SM_BB" sort | while IFS= read -r item; do
        rel="${item#./}"
        mode="$($SM_BB stat -c %a "$item")" || exit 1
        uid="$($SM_BB stat -c %u "$item")" || exit 1
        gid="$($SM_BB stat -c %g "$item")" || exit 1
        if [ -f "$item" ]; then
          kind="file"
          digest="$(sm_sha256_file "$item")" || exit 1
        elif [ -L "$item" ]; then
          kind="link"
          digest="$(printf 'link:%s' "$($SM_BB readlink "$item")" | "$SM_BB" sha256sum | "$SM_BB" awk '{ print $1 }')" || exit 1
        elif [ -d "$item" ]; then
          kind=directory
          digest=-
        else
          kind=other
          digest=-
        fi
        printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$rel" "$kind" "$digest" "$mode" "$uid" "$gid"
      done
    ) | "$SM_BB" sha256sum | "$SM_BB" awk '{ print $1 }'
  else
    printf '%s\n' -
  fi
}

sm_json_escape() {
  printf '%s' "$1" | "$SM_BB" sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/\\t/g'
}

sm_decode() {
  printf '%s' "$1" | "$SM_BB" base64 -d 2>/dev/null
}

sm_valid_single_line() {
  [ -n "$1" ] || return 1
  [ "$(printf '%s' "$1" | "$SM_BB" tr -d '\r\n')" = "$1" ] || return 1
  ! printf '%s' "$1" | LC_ALL=C "$SM_BB" grep -q '[[:cntrl:]]'
}

sm_valid_hex() {
  local value="$1" length="$2"
  [ "${#value}" -eq "$length" ] || return 1
  case "$value" in *[!a-f0-9]*|'') return 1 ;; esac
  return 0
}

sm_valid_uuid() {
  printf '%s\n' "$1" | LC_ALL=C "$SM_BB" grep -Eq \
    '^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$'
}

sm_live_abis() {
  local abis
  abis="$(getprop ro.product.cpu.abilist)"
  [ -n "$abis" ] || abis="$(getprop ro.product.cpu.abi)"
  printf '%s\n' "$abis"
}

sm_validate_live_target() {
  local live_fingerprint live_api live_abis
  live_fingerprint="$(printf '%s' "$(getprop ro.build.fingerprint)" | "$SM_BB" sha256sum | "$SM_BB" awk '{ print $1 }')"
  live_api="$(getprop ro.build.version.sdk)"
  live_abis="$(sm_live_abis)"
  [ "$live_fingerprint" = "$SM_FINGERPRINT_SHA256" ] || {
    sm_log "! System Mode transaction belongs to a different target"
    return 1
  }
  [ "$live_api" = "$SM_TARGET_API" ] || {
    sm_log "! System Mode transaction API does not match the live target"
    return 1
  }
  [ "$live_abis" = "$SM_TARGET_ABIS" ] || {
    sm_log "! System Mode transaction ABI list does not match the live target"
    return 1
  }
  return 0
}

sm_validate_authorization() {
  local auth="$SM_AUTHORIZATION_FILE" schema fingerprint api
  local adapter_b64 init_b64 selinux_b64 snapshot_b64 location_b64 restore_b64
  [ -f "$auth" ] || {
    sm_log "! Run 'kitsune system-mode authorize' with a verified doctor report first"
    return 1
  }
  schema="$(sm_get SCHEMA_VERSION "$auth")"
  [ "$schema" = "$SM_SCHEMA_VERSION" ] || { sm_log "! Unsupported recovery authorization"; return 1; }
  SM_REPORT_SHA256="$(sm_get REPORT_SHA256 "$auth")"
  fingerprint="$(sm_get FINGERPRINT_SHA256 "$auth")"
  SM_BACKUP_SHA256="$(sm_get BACKUP_SHA256 "$auth")"
  case "$SM_REPORT_SHA256:$fingerprint:$SM_BACKUP_SHA256" in
    *[!a-f0-9:]*|*::*|:*|*:) sm_log "! Invalid recovery authorization digest"; return 1 ;;
  esac
  [ "${#SM_REPORT_SHA256}" -eq 64 ] && [ "${#fingerprint}" -eq 64 ] &&
    [ "${#SM_BACKUP_SHA256}" -eq 64 ] || {
      sm_log "! Invalid recovery authorization digest length"
      return 1
    }
  api="$(sm_get TARGET_API "$auth")"
  case "$api" in *[!0-9]*|'') sm_log "! Invalid recovery authorization API"; return 1 ;; esac
  adapter_b64="$(sm_get ADAPTER_ID_B64 "$auth")"
  init_b64="$(sm_get INIT_DIRECTORY_B64 "$auth")"
  selinux_b64="$(sm_get SELINUX_STRATEGY_B64 "$auth")"
  snapshot_b64="$(sm_get SNAPSHOT_ID_B64 "$auth")"
  location_b64="$(sm_get BACKUP_LOCATION_B64 "$auth")"
  restore_b64="$(sm_get RESTORE_COMMAND_B64 "$auth")"
  SM_TARGET_ABIS="$(sm_decode "$(sm_get TARGET_ABIS_B64 "$auth")")" || return 1
  SM_ADAPTER_ID="$(sm_decode "$adapter_b64")" || return 1
  SM_AUTH_INIT_DIRECTORY="$(sm_decode "$init_b64")" || return 1
  SM_SELINUX_STRATEGY="$(sm_decode "$selinux_b64")" || return 1
  SM_SNAPSHOT_ID="$(sm_decode "$snapshot_b64")" || return 1
  SM_BACKUP_LOCATION="$(sm_decode "$location_b64")" || return 1
  SM_RESTORE_COMMAND="$(sm_decode "$restore_b64")" || return 1
  if ! sm_valid_single_line "$SM_TARGET_ABIS" || ! sm_valid_single_line "$SM_ADAPTER_ID" ||
     ! sm_valid_single_line "$SM_AUTH_INIT_DIRECTORY" || ! sm_valid_single_line "$SM_SELINUX_STRATEGY" ||
     ! sm_valid_single_line "$SM_SNAPSHOT_ID" || ! sm_valid_single_line "$SM_BACKUP_LOCATION" ||
     ! sm_valid_single_line "$SM_RESTORE_COMMAND"; then
    sm_log "! Recovery authorization contains invalid text"
    return 1
  fi
  case "$SM_ADAPTER_ID" in *[!A-Za-z0-9._-]*) sm_log "! Invalid adapter identifier"; return 1 ;; esac
  case "$SM_AUTH_INIT_DIRECTORY" in
    /system/etc/init|/system/etc/init/hw|/vendor/etc/init|/odm/etc/init|/product/etc/init|/system_ext/etc/init) ;;
    *) sm_log "! Unauthorized init directory"; return 1 ;;
  esac
  SM_FINGERPRINT_SHA256="$fingerprint"
  SM_TARGET_API="$api"
  sm_validate_live_target || return 1
  return 0
}

sm_select_strategies() {
  local candidate real
  SM_RUNTIME_PATH="${KITSUNE_SYSTEM_RUNTIME:-}"
  if [ -n "$SM_RUNTIME_PATH" ]; then
    case "$SM_RUNTIME_PATH" in /sbin|/debug_ramdisk) ;; *) sm_log "! Invalid adapter runtime path"; return 1 ;; esac
  elif [ -d /debug_ramdisk ] && [ -w /debug_ramdisk ]; then
    SM_RUNTIME_PATH=/debug_ramdisk
  else
    SM_RUNTIME_PATH=/sbin
  fi
  SM_INIT_PATH="$SM_AUTH_INIT_DIRECTORY/magisk.rc"
  real="$(sm_real_path "$SM_AUTH_INIT_DIRECTORY")" || return 1
  sm_probe_writable_directory "$real" || { sm_log "! Authorized init directory is unavailable"; return 1; }

  SM_POLICY_PATH=
  for candidate in \
    /vendor/etc/selinux/precompiled_sepolicy \
    /odm/etc/selinux/precompiled_sepolicy \
    /system/etc/selinux/precompiled_sepolicy \
    /system_root/sepolicy \
    /system_root/sepolicy_debug \
    /system_root/sepolicy.unlocked; do
    real="$(sm_real_path "$candidate")" || return 1
    if [ -f "$real" ]; then
      SM_POLICY_PATH="$candidate"
      break
    fi
  done
  if [ -d /sys/fs/selinux ] && [ -z "$SM_POLICY_PATH" ]; then
    sm_log "! Cannot identify the SELinux policy used by the next boot"
    return 1
  fi
  case "$SM_SELINUX_STRATEGY" in
    precompiled) case "$SM_POLICY_PATH" in */precompiled_sepolicy) ;; *) sm_log "! Doctor and installer policy strategies disagree"; return 1 ;; esac ;;
    monolithic|split) ;;
    disabled) SM_POLICY_PATH= ;;
    *) sm_log "! Unsupported SELinux strategy"; return 1 ;;
  esac
  SM_POLICY_SOURCE="$SM_POLICY_PATH"
  if command -v is_rootfs >/dev/null 2>&1 && is_rootfs; then
    SM_POLICY_PATH=
    SM_SELINUX_STRATEGY="live+$SM_SELINUX_STRATEGY"
  fi
  return 0
}

sm_write_transaction() {
  local staged="$SM_STATE_DIR/.transaction.env.new"
  "$SM_BB" mkdir -p "$SM_STATE_DIR" || return 1
  "$SM_BB" chmod 0700 /data/adb/kitsune "$SM_STATE_DIR" 2>/dev/null || return 1
  {
    printf 'SCHEMA_VERSION=%s\n' "$SM_SCHEMA_VERSION"
    printf 'INSTALL_ID=%s\n' "$SM_INSTALL_ID"
    printf 'TRANSACTION_ID=%s\n' "$SM_TRANSACTION_ID"
    printf 'STATE=%s\n' "$SM_STATE"
    printf 'PRIOR_STATE=%s\n' "$SM_PRIOR_STATE"
    printf 'FINGERPRINT_SHA256=%s\n' "$SM_FINGERPRINT_SHA256"
    printf 'TARGET_API=%s\n' "$SM_TARGET_API"
    printf 'REPORT_SHA256=%s\n' "$SM_REPORT_SHA256"
    printf 'BACKUP_SHA256=%s\n' "$SM_BACKUP_SHA256"
    printf 'ADAPTER_ID_B64=%s\n' "$(printf '%s' "$SM_ADAPTER_ID" | "$SM_BB" base64 | "$SM_BB" tr -d '\n')"
    printf 'TARGET_ABIS_B64=%s\n' "$(printf '%s' "$SM_TARGET_ABIS" | "$SM_BB" base64 | "$SM_BB" tr -d '\n')"
    printf 'SNAPSHOT_ID_B64=%s\n' "$(printf '%s' "$SM_SNAPSHOT_ID" | "$SM_BB" base64 | "$SM_BB" tr -d '\n')"
    printf 'BACKUP_LOCATION_B64=%s\n' "$(printf '%s' "$SM_BACKUP_LOCATION" | "$SM_BB" base64 | "$SM_BB" tr -d '\n')"
    printf 'RESTORE_COMMAND_B64=%s\n' "$(printf '%s' "$SM_RESTORE_COMMAND" | "$SM_BB" base64 | "$SM_BB" tr -d '\n')"
    printf 'INIT_PATH=%s\n' "$SM_INIT_PATH"
    printf 'POLICY_PATH=%s\n' "$SM_POLICY_PATH"
    printf 'POLICY_SOURCE=%s\n' "$SM_POLICY_SOURCE"
    printf 'RUNTIME_PATH=%s\n' "$SM_RUNTIME_PATH"
    printf 'SELINUX_STRATEGY=%s\n' "$SM_SELINUX_STRATEGY"
    printf 'SOURCE_COMMIT=%s\n' "$SM_SOURCE_COMMIT"
    printf 'UPSTREAM_BASE=%s\n' "$SM_UPSTREAM_BASE"
    printf 'ARTIFACT_SHA256=%s\n' "$SM_ARTIFACT_SHA256"
    printf 'PRODUCT_VERSION_B64=%s\n' "$(printf '%s' "$SM_PRODUCT_VERSION" | "$SM_BB" base64 | "$SM_BB" tr -d '\n')"
    printf 'COMMIT_BOOT_ID=%s\n' "$SM_COMMIT_BOOT_ID"
    printf 'ROLLBACK_DIR=%s\n' "$SM_ROLLBACK_DIR"
    printf 'STAGING_PATH=%s\n' "$SM_STAGING_PATH"
  } >"$staged" || return 1
  "$SM_BB" chmod 0600 "$staged" || return 1
  sm_atomic_publish "$staged" "$SM_TRANSACTION_FILE" "state:$SM_STATE"
}

sm_load_transaction() {
  [ -f "$SM_TRANSACTION_FILE" ] || return 1
  [ "$(sm_get SCHEMA_VERSION "$SM_TRANSACTION_FILE")" = "$SM_SCHEMA_VERSION" ] || return 1
  SM_INSTALL_ID="$(sm_get INSTALL_ID "$SM_TRANSACTION_FILE")"
  SM_TRANSACTION_ID="$(sm_get TRANSACTION_ID "$SM_TRANSACTION_FILE")"
  SM_STATE="$(sm_get STATE "$SM_TRANSACTION_FILE")"
  SM_PRIOR_STATE="$(sm_get PRIOR_STATE "$SM_TRANSACTION_FILE")"
  SM_FINGERPRINT_SHA256="$(sm_get FINGERPRINT_SHA256 "$SM_TRANSACTION_FILE")"
  SM_TARGET_API="$(sm_get TARGET_API "$SM_TRANSACTION_FILE")"
  SM_REPORT_SHA256="$(sm_get REPORT_SHA256 "$SM_TRANSACTION_FILE")"
  SM_BACKUP_SHA256="$(sm_get BACKUP_SHA256 "$SM_TRANSACTION_FILE")"
  SM_ADAPTER_ID="$(sm_decode "$(sm_get ADAPTER_ID_B64 "$SM_TRANSACTION_FILE")")"
  SM_TARGET_ABIS="$(sm_decode "$(sm_get TARGET_ABIS_B64 "$SM_TRANSACTION_FILE")")"
  SM_SNAPSHOT_ID="$(sm_decode "$(sm_get SNAPSHOT_ID_B64 "$SM_TRANSACTION_FILE")")"
  SM_BACKUP_LOCATION="$(sm_decode "$(sm_get BACKUP_LOCATION_B64 "$SM_TRANSACTION_FILE")")"
  SM_RESTORE_COMMAND="$(sm_decode "$(sm_get RESTORE_COMMAND_B64 "$SM_TRANSACTION_FILE")")"
  SM_INIT_PATH="$(sm_get INIT_PATH "$SM_TRANSACTION_FILE")"
  SM_POLICY_PATH="$(sm_get POLICY_PATH "$SM_TRANSACTION_FILE")"
  SM_POLICY_SOURCE="$(sm_get POLICY_SOURCE "$SM_TRANSACTION_FILE")"
  SM_RUNTIME_PATH="$(sm_get RUNTIME_PATH "$SM_TRANSACTION_FILE")"
  SM_SELINUX_STRATEGY="$(sm_get SELINUX_STRATEGY "$SM_TRANSACTION_FILE")"
  SM_SOURCE_COMMIT="$(sm_get SOURCE_COMMIT "$SM_TRANSACTION_FILE")"
  SM_UPSTREAM_BASE="$(sm_get UPSTREAM_BASE "$SM_TRANSACTION_FILE")"
  SM_ARTIFACT_SHA256="$(sm_get ARTIFACT_SHA256 "$SM_TRANSACTION_FILE")"
  SM_PRODUCT_VERSION="$(sm_decode "$(sm_get PRODUCT_VERSION_B64 "$SM_TRANSACTION_FILE")")"
  SM_COMMIT_BOOT_ID="$(sm_get COMMIT_BOOT_ID "$SM_TRANSACTION_FILE")"
  SM_ROLLBACK_DIR="$(sm_get ROLLBACK_DIR "$SM_TRANSACTION_FILE")"
  SM_STAGING_PATH="$(sm_get STAGING_PATH "$SM_TRANSACTION_FILE")"
  sm_valid_uuid "$SM_INSTALL_ID" && sm_valid_uuid "$SM_TRANSACTION_ID" || return 1
  case "$SM_STATE" in UNINSTALLED|PREFLIGHTED|STAGED|COMMITTED|BOOT_VERIFIED|ROLLBACK_REQUIRED|ROLLING_BACK|FAILED) ;; *) return 1 ;; esac
  case "$SM_PRIOR_STATE" in UNINSTALLED|BOOT_VERIFIED) ;; *) return 1 ;; esac
  case "$SM_INIT_PATH" in /system/etc/init/magisk.rc|/system/etc/init/hw/magisk.rc|/vendor/etc/init/magisk.rc|/odm/etc/init/magisk.rc|/product/etc/init/magisk.rc|/system_ext/etc/init/magisk.rc) ;; *) return 1 ;; esac
  case "$SM_RUNTIME_PATH" in /sbin|/debug_ramdisk) ;; *) return 1 ;; esac
  case "$SM_POLICY_PATH" in ""|/vendor/etc/selinux/precompiled_sepolicy|/odm/etc/selinux/precompiled_sepolicy|/system/etc/selinux/precompiled_sepolicy|/system_root/sepolicy|/system_root/sepolicy_debug|/system_root/sepolicy.unlocked) ;; *) return 1 ;; esac
  case "$SM_POLICY_SOURCE" in ""|/vendor/etc/selinux/precompiled_sepolicy|/odm/etc/selinux/precompiled_sepolicy|/system/etc/selinux/precompiled_sepolicy|/system_root/sepolicy|/system_root/sepolicy_debug|/system_root/sepolicy.unlocked) ;; *) return 1 ;; esac
  [ "$SM_ROLLBACK_DIR" = "$SM_STATE_DIR/rollback/$SM_TRANSACTION_ID" ] || return 1
  [ "$SM_STAGING_PATH" = "$(sm_parent "$SM_SYSTEM_DIR")/.magisk.kitsune-stage-$SM_TRANSACTION_ID" ] || return 1
  sm_valid_hex "$SM_FINGERPRINT_SHA256" 64 && sm_valid_hex "$SM_REPORT_SHA256" 64 &&
    sm_valid_hex "$SM_BACKUP_SHA256" 64 && sm_valid_hex "$SM_SOURCE_COMMIT" 40 &&
    sm_valid_hex "$SM_UPSTREAM_BASE" 40 && sm_valid_hex "$SM_ARTIFACT_SHA256" 64 || return 1
  case "$SM_TARGET_API" in *[!0-9]*|'') return 1 ;; esac
  case "$SM_ADAPTER_ID" in *[!A-Za-z0-9._-]*|'') return 1 ;; esac
  case "$SM_TARGET_ABIS" in *[!A-Za-z0-9,._-]*|''|,*|*,|*,,*) return 1 ;; esac
  case "$SM_SELINUX_STRATEGY" in precompiled|monolithic|split|disabled|live+precompiled|live+monolithic|live+split|live+disabled) ;; *) return 1 ;; esac
  sm_valid_single_line "$SM_SNAPSHOT_ID" && sm_valid_single_line "$SM_BACKUP_LOCATION" &&
    sm_valid_single_line "$SM_RESTORE_COMMAND" && sm_valid_single_line "$SM_PRODUCT_VERSION" || return 1
  [ -z "$SM_COMMIT_BOOT_ID" ] || sm_valid_uuid "$SM_COMMIT_BOOT_ID" || return 1
  sm_validate_live_target
}

sm_update_state() {
  SM_STATE="$1"
  sm_write_transaction
}

sm_label_path() {
  case "$1" in
    payload) printf '%s\n' "$SM_SYSTEM_DIR" ;;
    legacy_rc) printf '%s.rc\n' "$SM_SYSTEM_DIR" ;;
    init_rc) printf '%s\n' "$SM_INIT_PATH" ;;
    policy) printf '%s\n' "$SM_POLICY_PATH" ;;
    policy_gz) [ -n "$SM_POLICY_PATH" ] && printf '%s.gz\n' "$SM_POLICY_PATH" ;;
    bootanim) printf '%s\n' /system/etc/init/bootanim.rc ;;
    bootanim_gz) printf '%s\n' /system/etc/init/bootanim.rc.gz ;;
    runtime) printf '%s\n' /data/adb/magisk ;;
    addon_script) printf '%s\n' /system/addon.d/99-magisk.sh ;;
    addon_dir) printf '%s\n' /system/addon.d/magisk ;;
    *) return 1 ;;
  esac
}

sm_state_path() {
  case "$1" in
    transaction) printf '%s\n' "$SM_TRANSACTION_FILE" ;;
    manifest_copy) printf '%s\n' "$SM_MANIFEST_COPY" ;;
    ownership) printf '%s\n' "$SM_OWNERSHIP_FILE" ;;
    originals) printf '%s\n' "$SM_ORIGINAL_FILE" ;;
    journal) printf '%s\n' "$SM_JOURNAL_FILE" ;;
    boot_proof) printf '%s\n' "$SM_BOOT_PROOF" ;;
    original_dir) printf '%s\n' "$SM_STATE_DIR/original" ;;
    *) return 1 ;;
  esac
}

sm_snapshot_state_metadata() {
  local label source destination
  "$SM_BB" mkdir -p "$SM_ROLLBACK_DIR/state" || return 1
  for label in transaction manifest_copy ownership originals journal boot_proof original_dir; do
    source="$(sm_state_path "$label")" || return 1
    destination="$SM_ROLLBACK_DIR/state/$label"
    "$SM_BB" mkdir -p "$destination" || return 1
    if sm_path_present "$source"; then
      "$SM_BB" cp -a "$source" "$destination/data" || return 1
      sm_fsync_tree "$destination/data" || return 1
      printf 'present\n' >"$destination/present" || return 1
    else
      printf 'absent\n' >"$destination/absent" || return 1
    fi
    sm_fsync_tree "$destination" || return 1
    sm_fsync "$SM_ROLLBACK_DIR/state" || return 1
    sm_failpoint "snapshot-state:$label" || return 1
  done
  sm_fsync "$SM_ROLLBACK_DIR" "$SM_STATE_DIR/rollback" "$SM_STATE_DIR" || return 1
}

sm_restore_state_metadata() {
  local label destination source failed=0
  # Restore the transaction record last. Until then, any interrupted recovery
  # remains visibly tied to this rollback directory and can be retried.
  for label in original_dir boot_proof journal originals ownership manifest_copy transaction; do
    destination="$(sm_state_path "$label")" || return 1
    source="$SM_ROLLBACK_DIR/state/$label"
    [ -d "$source" ] || { failed=1; break; }
    "$SM_BB" rm -rf "$destination" || { failed=1; break; }
    if [ -f "$source/present" ]; then
      "$SM_BB" mkdir -p "$(sm_parent "$destination")" || { failed=1; break; }
      "$SM_BB" cp -a "$source/data" "$destination" || { failed=1; break; }
      sm_fsync_tree "$destination" || { failed=1; break; }
    elif [ ! -f "$source/absent" ]; then
      failed=1
      break
    fi
    sm_fsync "$(sm_parent "$destination")" || { failed=1; break; }
    sm_failpoint "rollback-state:$label" || { failed=1; break; }
  done
  [ "$failed" = 0 ]
}

sm_cleanup_staging() {
  local path real failed=0
  for path in \
    "$SM_STAGING_PATH" \
    "$SM_INIT_PATH.kitsune-new" \
    /system/etc/init/bootanim.rc.kitsune-stock-new \
    /system/addon.d/.99-magisk.sh.kitsune-new \
    "$SM_SYSTEM_DIR/.install-manifest.json.new" \
    "$SM_SYSTEM_DIR/.install-manifest.state-new"; do
    [ -n "$path" ] || continue
    real="$(sm_real_path "$path")" || { failed=1; continue; }
    "$SM_BB" rm -rf "$real" "$real.kitsune-short" || failed=1
    sm_fsync_existing_parent "$real" 2>/dev/null || failed=1
  done
  for path in \
    "$SM_STATE_DIR/.transaction.env.new" \
    "$SM_STATE_DIR/.install-manifest.json.new" \
    "$SM_STATE_DIR/.install-manifest.copy.new" \
    "$SM_STATE_DIR/.install-manifest.state-new" \
    "$SM_STATE_DIR/.boot-verified.env.new" \
    "$SM_ORIGINAL_FILE.new" "$SM_OWNERSHIP_FILE.new" "$SM_JOURNAL_FILE.new"; do
    "$SM_BB" rm -f "$path" "$path.kitsune-short" || failed=1
  done
  sm_fsync "$SM_STATE_DIR" 2>/dev/null || failed=1
  [ "$failed" = 0 ]
}

sm_restore_preflight() {
  local rollback="$SM_ROLLBACK_DIR"
  sm_cleanup_staging || {
    sm_update_state FAILED
    return 1
  }
  if ! sm_restore_state_metadata; then
    sm_update_state FAILED
    sm_log "! Transaction metadata recovery failed; use the verified external restore"
    return 1
  fi
  "$SM_BB" rm -rf "$rollback" || return 1
  sm_fsync "$SM_STATE_DIR/rollback" "$SM_STATE_DIR" || return 1
  return 0
}

sm_snapshot_one() {
  local label="$1" canonical real destination
  canonical="$(sm_label_path "$label")" || return 1
  [ -n "$canonical" ] || return 0
  real="$(sm_real_path "$canonical")" || return 1
  destination="$SM_ROLLBACK_DIR/$label"
  "$SM_BB" mkdir -p "$destination" || return 1
  if sm_path_present "$real"; then
    "$SM_BB" cp -a "$real" "$destination/data" || return 1
    sm_fsync_tree "$destination/data" || return 1
    printf 'present\n' >"$destination/present" || return 1
  else
    printf 'absent\n' >"$destination/absent" || return 1
  fi
  sm_fsync_tree "$destination" || return 1
  sm_fsync "$SM_ROLLBACK_DIR" || return 1
  sm_failpoint "snapshot:$label"
}

sm_snapshot_all() {
  local label
  "$SM_BB" mkdir -p "$SM_ROLLBACK_DIR" || return 1
  for label in payload legacy_rc init_rc policy policy_gz bootanim bootanim_gz runtime addon_script addon_dir; do
    [ -n "$SM_POLICY_PATH" ] || case "$label" in policy|policy_gz) continue ;; esac
    [ "$label" != legacy_rc ] || [ "$SM_SYSTEM_DIR.rc" != "$SM_INIT_PATH" ] || continue
    sm_snapshot_one "$label" || return 1
  done
  return 0
}

sm_original_record() {
  local canonical="$1" label="$2" source="$3" existed="$4" metadata="${5:-$3}"
  local destination="$SM_STATE_DIR/original/$label" digest=- size=0 mode=- uid=- gid=- context=-
  "$SM_BB" mkdir -p "$destination" || return 1
  if [ "$existed" = true ]; then
    sm_path_present "$source" || return 1
    "$SM_BB" cp -a "$source" "$destination/data" || return 1
    sm_fsync_tree "$destination/data" || return 1
    digest="$(sm_digest_path "$destination/data")" || return 1
    if [ -f "$destination/data" ]; then size="$($SM_BB stat -c %s "$destination/data")"; fi
    mode="0$($SM_BB stat -c %a "$metadata")"
    uid="$($SM_BB stat -c %u "$metadata")"
    gid="$($SM_BB stat -c %g "$metadata")"
    context="$(sm_context "$metadata")"
    [ -n "$context" ] || context=-
  else
    printf 'absent\n' >"$destination/absent" || return 1
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$canonical" "$existed" "$digest" "$size" "$mode" "$uid" "$gid" "$context" "original/$label/data" >>"$SM_ORIGINAL_FILE.new" || return 1
  sm_fsync_tree "$destination" || return 1
}

sm_decompress_original() {
  local compressed="$1" destination="$2"
  "$SM_BB" gzip -cdf "$compressed" >"$destination" || return 1
  "$SM_BB" chmod --reference="${compressed%.gz}" "$destination" 2>/dev/null || "$SM_BB" chmod 0644 "$destination"
  "$SM_BB" chown --reference="${compressed%.gz}" "$destination" 2>/dev/null || "$SM_BB" chown 0:0 "$destination"
}

sm_prepare_originals() {
  local config_real init_real policy_real policy_gz bootanim_real bootanim_gz temp legacy=false conflict
  if [ -f "$SM_ORIGINAL_FILE" ]; then
    [ -f "$SM_MANIFEST_COPY" ] || { sm_log "! Original backups exist without an install manifest"; return 1; }
    return 0
  fi
  config_real="$(sm_real_path "$SM_SYSTEM_DIR/config")" || return 1
  [ "$("$SM_BB" sed -n 's/^SYSTEMMODE=//p' "$config_real" 2>/dev/null | "$SM_BB" head -n 1)" = true ] && legacy=true
  if sm_path_present "$(sm_real_path "$SM_SYSTEM_DIR")" && [ "$legacy" != true ]; then
    sm_log "! Refusing to replace an unowned System Mode payload path"
    return 1
  fi
  if [ "$legacy" != true ]; then
    for conflict in /data/adb/magisk /system/addon.d/99-magisk.sh /system/addon.d/magisk \
      "$SM_SYSTEM_DIR.rc" "$SM_INIT_PATH"; do
      if sm_path_present "$(sm_real_path "$conflict")"; then
        sm_log "! Refusing to replace unowned path $conflict"
        return 1
      fi
    done
  fi
  "$SM_BB" mkdir -p "$SM_STATE_DIR/original" || return 1
  : >"$SM_ORIGINAL_FILE.new" || return 1
  sm_original_record "$SM_SYSTEM_DIR" payload /dev/null false || return 1
  if [ "$SM_SYSTEM_DIR.rc" != "$SM_INIT_PATH" ]; then
    sm_original_record "$SM_SYSTEM_DIR.rc" legacy_rc /dev/null false || return 1
  fi
  init_real="$(sm_real_path "$SM_INIT_PATH")" || return 1
  if [ "$legacy" = true ] && [ "$SM_SYSTEM_DIR.rc" != "$SM_INIT_PATH" ] && sm_path_present "$init_real"; then
    sm_original_record "$SM_INIT_PATH" init_rc "$init_real" true || return 1
  else
    sm_original_record "$SM_INIT_PATH" init_rc /dev/null false || return 1
  fi
  sm_original_record /data/adb/magisk runtime /dev/null false || return 1
  sm_original_record /system/addon.d/99-magisk.sh addon_script /dev/null false || return 1
  sm_original_record /system/addon.d/magisk addon_dir /dev/null false || return 1

  if [ -n "$SM_POLICY_PATH" ]; then
    policy_real="$(sm_real_path "$SM_POLICY_PATH")" || return 1
    policy_gz="$policy_real.gz"
    if [ "$legacy" = true ] && [ -f "$policy_gz" ]; then
      temp="$SM_STATE_DIR/.policy.original"
      sm_decompress_original "$policy_gz" "$temp" || return 1
      sm_original_record "$SM_POLICY_PATH" policy "$temp" true "$policy_real" || return 1
      "$SM_BB" rm -f "$temp"
    else
      sm_original_record "$SM_POLICY_PATH" policy "$policy_real" true || return 1
    fi
  fi
  bootanim_real="$(sm_real_path /system/etc/init/bootanim.rc)" || return 1
  bootanim_gz="$bootanim_real.gz"
  if [ "$legacy" = true ] && [ -f "$bootanim_gz" ]; then
    temp="$SM_STATE_DIR/.bootanim.original"
    sm_decompress_original "$bootanim_gz" "$temp" || return 1
    sm_original_record /system/etc/init/bootanim.rc bootanim "$temp" true "$bootanim_real" || return 1
    "$SM_BB" rm -f "$temp"
  elif sm_path_present "$bootanim_real"; then
    sm_original_record /system/etc/init/bootanim.rc bootanim "$bootanim_real" true || return 1
  else
    sm_original_record /system/etc/init/bootanim.rc bootanim /dev/null false || return 1
  fi
  sm_atomic_publish "$SM_ORIGINAL_FILE.new" "$SM_ORIGINAL_FILE" original-inventory || return 1
  sm_fsync_tree "$SM_STATE_DIR/original" || return 1
  return 0
}

sm_restore_snapshot() {
  local label canonical real source failed=0 rollback="$SM_ROLLBACK_DIR"
  sm_update_state ROLLING_BACK || return 1
  for label in addon_dir addon_script runtime bootanim_gz bootanim policy_gz policy init_rc legacy_rc payload; do
    [ -n "$SM_POLICY_PATH" ] || case "$label" in policy|policy_gz) continue ;; esac
    [ "$label" != legacy_rc ] || [ "$SM_SYSTEM_DIR.rc" != "$SM_INIT_PATH" ] || continue
    canonical="$(sm_label_path "$label")" || return 1
    real="$(sm_real_path "$canonical")" || return 1
    source="$SM_ROLLBACK_DIR/$label"
    [ -d "$source" ] || { failed=1; break; }
    "$SM_BB" rm -rf "$real" || { failed=1; break; }
    if [ -f "$source/present" ]; then
      "$SM_BB" mkdir -p "$(sm_parent "$real")" || { failed=1; break; }
      "$SM_BB" cp -a "$source/data" "$real" || { failed=1; break; }
      sm_fsync_tree "$real" || { failed=1; break; }
    elif [ ! -f "$source/absent" ]; then
      failed=1
      break
    fi
    sm_fsync_existing_parent "$real" || { failed=1; break; }
    sm_failpoint "rollback:$label" || { failed=1; break; }
  done
  if [ "$failed" != 0 ]; then
    sm_update_state FAILED
    sm_log "! Automatic rollback failed at ${canonical:-transaction setup}; use the verified external restore"
    return 1
  fi
  if ! sm_cleanup_staging; then
    sm_update_state FAILED
    sm_log "! Transaction staging cleanup failed; use the verified external restore"
    return 1
  fi
  if ! sm_restore_state_metadata; then
    sm_update_state FAILED
    sm_log "! Transaction metadata recovery failed; use the verified external restore"
    return 1
  fi
  "$SM_BB" rm -rf "$rollback" || return 1
  sm_fsync "$SM_STATE_DIR/rollback" "$SM_STATE_DIR" || return 1
  return 0
}

sm_abort_transaction() {
  [ -f "$SM_TRANSACTION_FILE" ] || return 0
  sm_load_transaction || return 1
  case "$SM_STATE" in
    PREFLIGHTED) sm_restore_preflight ;;
    STAGED|COMMITTED|ROLLBACK_REQUIRED|ROLLING_BACK)
      [ "$SM_STATE" = ROLLING_BACK ] || sm_update_state ROLLBACK_REQUIRED || return 1
      sm_restore_snapshot
      ;;
    BOOT_VERIFIED|UNINSTALLED) return 0 ;;
    FAILED) return 1 ;;
  esac
}

sm_verify_owned() {
  local path digest size mode uid gid context kind real actual actual_size actual_mode actual_uid actual_gid actual_context
  [ -f "$SM_OWNERSHIP_FILE" ] || { sm_log "! System Mode ownership inventory is missing"; return 1; }
  while IFS="$SM_TAB" read -r path digest size mode uid gid context kind; do
    [ -n "$path" ] || continue
    real="$(sm_real_path "$path")" || return 1
    case "$kind" in
      file) [ -f "$real" ] || { sm_log "! Owned file is missing: $path"; return 1; }; actual="$(sm_sha256_file "$real")" ;;
      link) [ -L "$real" ] || { sm_log "! Owned link is missing: $path"; return 1; }; actual="$(sm_digest_path "$real")" ;;
      *) sm_log "! Invalid ownership record for $path"; return 1 ;;
    esac
    [ "$actual" = "$digest" ] || { sm_log "! Owned path digest changed: $path"; return 1; }
    actual_size="$($SM_BB stat -c %s "$real" 2>/dev/null)" || actual_size=0
    actual_mode="0$($SM_BB stat -c %a "$real")" || return 1
    actual_uid="$($SM_BB stat -c %u "$real")" || return 1
    actual_gid="$($SM_BB stat -c %g "$real")" || return 1
    actual_context="$(sm_context "$real")"
    [ -n "$actual_context" ] || actual_context=-
    [ "$actual_size" = "$size" ] && [ "$actual_mode" = "$mode" ] &&
      [ "$actual_uid" = "$uid" ] && [ "$actual_gid" = "$gid" ] &&
      [ "$actual_context" = "$context" ] || {
        sm_log "! Owned path metadata changed: $path"
        return 1
      }
  done <"$SM_OWNERSHIP_FILE"
  return 0
}

sm_recover_pending() {
  local current_boot
  [ -f "$SM_TRANSACTION_FILE" ] || return 0
  sm_load_transaction || { sm_log "! Invalid persistent System Mode transaction"; return 1; }
  case "$SM_STATE" in
    UNINSTALLED|BOOT_VERIFIED) return 0 ;;
    PREFLIGHTED) sm_restore_preflight ;;
    STAGED|ROLLBACK_REQUIRED|ROLLING_BACK)
      sm_log "- Recovering interrupted System Mode transaction"
      sm_restore_snapshot
      ;;
    COMMITTED)
      current_boot="$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"
      if [ "$current_boot" = "$SM_COMMIT_BOOT_ID" ]; then
        sm_log "! Reboot once to verify the committed System Mode installation"
        return 2
      fi
      if [ "$(getprop sys.boot_completed)" = 1 ]; then
        sm_log "! Committed System Mode payload did not pass boot verification; rolling back"
        sm_update_state ROLLBACK_REQUIRED || return 1
        sm_restore_snapshot
      else
        return 2
      fi
      ;;
    FAILED)
      sm_log "! System Mode transaction is failed; use the verified external restore"
      return 1
      ;;
  esac
}

sm_validate_installed_state() {
  local manifest_real
  manifest_real="$(sm_real_path "$SM_SYSTEM_DIR/install-manifest.json")" || return 1
  [ -f "$manifest_real" ] && [ -f "$SM_MANIFEST_COPY" ] || {
    sm_log "! Existing System Mode manifest is incomplete"
    return 1
  }
  [ "$(sm_sha256_file "$manifest_real")" = "$(sm_sha256_file "$SM_MANIFEST_COPY")" ] || {
    sm_log "! Existing System Mode manifest changed"
    return 1
  }
  sm_verify_owned || {
    sm_log "! Existing System Mode payload changed"
    return 1
  }
  sm_assert_no_unowned_files || {
    sm_log "! Existing System Mode roots contain unowned files"
    return 1
  }
  sm_validate_originals || {
    sm_log "! Existing System Mode original backup changed"
    return 1
  }
  return 0
}

sm_begin_transaction() {
  local upgrading=false prior_adapter prior_init prior_policy prior_policy_source prior_runtime prior_selinux
  local requested_source_commit="${KITSUNE_SOURCE_COMMIT:-}"
  local requested_upstream_base="${KITSUNE_UPSTREAM_BASE:-}"
  local requested_product_version="${MAGISK_VER:-unknown-kitsune}"
  SM_ARTIFACT_PATH="${1:-}"
  case "$requested_source_commit" in *[!a-f0-9]*|'') sm_log "! Missing full source identity"; return 1 ;; esac
  [ "${#requested_source_commit}" -eq 40 ] || { sm_log "! Invalid source identity"; return 1; }
  case "$requested_upstream_base" in *[!a-f0-9]*|'') sm_log "! Missing full upstream identity"; return 1 ;; esac
  [ "${#requested_upstream_base}" -eq 40 ] || { sm_log "! Invalid upstream identity"; return 1; }
  SM_STATE=
  sm_recover_pending
  case $? in 0) ;; 2) return 1 ;; *) return 1 ;; esac
  if [ -f "$SM_TRANSACTION_FILE" ]; then
    sm_load_transaction || return 1
  else
    SM_STATE=
  fi
  if [ "${SM_STATE:-}" = BOOT_VERIFIED ]; then
    sm_validate_installed_state || {
      sm_log "! Refusing to upgrade a modified System Mode installation"
      return 1
    }
    upgrading=true
    prior_adapter="$SM_ADAPTER_ID"
    prior_init="$SM_INIT_PATH"
    prior_policy="$SM_POLICY_PATH"
    prior_policy_source="$SM_POLICY_SOURCE"
    prior_runtime="$SM_RUNTIME_PATH"
    prior_selinux="$SM_SELINUX_STRATEGY"
  fi
  sm_validate_authorization || return 1
  sm_select_strategies || return 1
  if [ "$upgrading" = true ] &&
     { [ "$SM_ADAPTER_ID" != "$prior_adapter" ] || [ "$SM_INIT_PATH" != "$prior_init" ] ||
       [ "$SM_POLICY_PATH" != "$prior_policy" ] || [ "$SM_POLICY_SOURCE" != "$prior_policy_source" ] ||
       [ "$SM_RUNTIME_PATH" != "$prior_runtime" ] || [ "$SM_SELINUX_STRATEGY" != "$prior_selinux" ]; }; then
    sm_log "! Refusing to change System Mode adapter or boot strategy during upgrade"
    return 1
  fi
  # Recovery loads the prior receipt into these globals. Reapply the identity
  # of the artifact being installed only after the prior state is validated.
  SM_SOURCE_COMMIT="$requested_source_commit"
  SM_UPSTREAM_BASE="$requested_upstream_base"
  SM_PRODUCT_VERSION="$requested_product_version"
  [ -f "$SM_ARTIFACT_PATH" ] || { sm_log "! Exact install artifact is unavailable"; return 1; }
  SM_ARTIFACT_SHA256="$(sm_sha256_file "$SM_ARTIFACT_PATH")" || return 1
  case "$SM_ARTIFACT_SHA256" in *[!a-f0-9]*|'') sm_log "! Cannot hash exact install artifact"; return 1 ;; esac
  [ "${#SM_ARTIFACT_SHA256}" -eq 64 ] || { sm_log "! Invalid install artifact digest"; return 1; }
  if [ -f "$SM_TRANSACTION_FILE" ]; then
    SM_PRIOR_STATE="$(sm_get STATE "$SM_TRANSACTION_FILE")"
    SM_INSTALL_ID="$(sm_get INSTALL_ID "$SM_TRANSACTION_FILE")"
  else
    SM_PRIOR_STATE=UNINSTALLED
    SM_INSTALL_ID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null)"
  fi
  case "$SM_PRIOR_STATE" in BOOT_VERIFIED|UNINSTALLED) ;; *) SM_PRIOR_STATE=UNINSTALLED ;; esac
  sm_valid_uuid "$SM_INSTALL_ID" || { sm_log "! Unable to create install identity"; return 1; }
  SM_TRANSACTION_ID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null)"
  sm_valid_uuid "$SM_TRANSACTION_ID" || { sm_log "! Unable to create transaction identity"; return 1; }
  SM_ROLLBACK_DIR="$SM_STATE_DIR/rollback/$SM_TRANSACTION_ID"
  SM_STAGING_PATH="$(sm_parent "$SM_SYSTEM_DIR")/.magisk.kitsune-stage-$SM_TRANSACTION_ID"
  SM_COMMIT_BOOT_ID=
  "$SM_BB" rm -rf "$SM_STATE_DIR/rollback" || return 1
  "$SM_BB" mkdir -p "$SM_ROLLBACK_DIR" || return 1
  sm_snapshot_state_metadata || return 1
  SM_STATE=PREFLIGHTED
  sm_write_transaction || return 1
  sm_failpoint preflighted || return 1
  sm_prepare_originals || return 1
  sm_snapshot_all || return 1
  : >"$SM_JOURNAL_FILE" || return 1
  sm_fsync "$SM_JOURNAL_FILE" "$SM_STATE_DIR" || return 1
  sm_update_state STAGED || return 1
  sm_failpoint staged
}

sm_restore_legacy_bootanim() {
  local target compressed staged before after
  target="$(sm_real_path /system/etc/init/bootanim.rc)" || return 1
  compressed="$target.gz"
  [ -f "$compressed" ] || return 0
  before="$(sm_sha256_file "$target")"
  staged="$target.kitsune-stock-new"
  "$SM_BB" gzip -cdf "$compressed" >"$staged" || return 1
  "$SM_BB" chmod --reference="$target" "$staged" 2>/dev/null || "$SM_BB" chmod 0644 "$staged"
  "$SM_BB" chown --reference="$target" "$staged" 2>/dev/null || "$SM_BB" chown 0:0 "$staged"
  chcon --reference="$target" "$staged" 2>/dev/null || true
  sm_atomic_publish "$staged" "$target" legacy-init-restored || return 1
  after="$(sm_sha256_file "$target")"
  printf 'legacy-init\treplace\t/system/etc/init/bootanim.rc\t%s\t%s\ttrue\n' "$before" "$after" >>"$SM_JOURNAL_FILE" || return 1
  sm_fsync "$SM_JOURNAL_FILE" || return 1
}

sm_add_owned_file() {
  local canonical="$1" real="$2" digest size mode uid gid context kind=file
  if [ -L "$real" ]; then kind="link"; else [ -f "$real" ] || return 0; fi
  digest="$(sm_digest_path "$real")" || return 1
  size="$($SM_BB stat -c %s "$real" 2>/dev/null)" || size=0
  mode="0$($SM_BB stat -c %a "$real")" || return 1
  uid="$($SM_BB stat -c %u "$real")" || return 1
  gid="$($SM_BB stat -c %g "$real")" || return 1
  context="$(sm_context "$real")"
  [ -n "$context" ] || context=-
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$canonical" "$digest" "$size" "$mode" "$uid" "$gid" "$context" "$kind" >>"$SM_OWNERSHIP_FILE.new"
}

sm_collect_owned_tree() {
  local canonical_root="$1" real_root="$2" item relative
  [ -d "$real_root" ] || return 0
  (
    cd "$real_root" || exit 1
    "$SM_BB" find . \( -type f -o -type l \) -print | "$SM_BB" sort
  ) | while IFS= read -r item; do
    relative="${item#./}"
    [ "$canonical_root/$relative" = "$SM_SYSTEM_DIR/install-manifest.json" ] && continue
    sm_add_owned_file "$canonical_root/$relative" "$real_root/$relative" || exit 1
  done
}

sm_collect_ownership() {
  local payload_real runtime_real init_real policy_real addon_real
  : >"$SM_OWNERSHIP_FILE.new" || return 1
  payload_real="$(sm_real_path "$SM_SYSTEM_DIR")" || return 1
  runtime_real=/data/adb/magisk
  sm_collect_owned_tree "$SM_SYSTEM_DIR" "$payload_real" || return 1
  sm_collect_owned_tree /data/adb/magisk "$runtime_real" || return 1
  init_real="$(sm_real_path "$SM_INIT_PATH")" || return 1
  sm_add_owned_file "$SM_INIT_PATH" "$init_real" || return 1
  if [ -n "$SM_POLICY_PATH" ]; then
    policy_real="$(sm_real_path "$SM_POLICY_PATH")" || return 1
    sm_add_owned_file "$SM_POLICY_PATH" "$policy_real" || return 1
  fi
  addon_real="$(sm_real_path /system/addon.d/99-magisk.sh)" || return 1
  sm_add_owned_file /system/addon.d/99-magisk.sh "$addon_real" || return 1
  sm_atomic_publish "$SM_OWNERSHIP_FILE.new" "$SM_OWNERSHIP_FILE" ownership-inventory
}

sm_lookup_original() {
  local wanted="$1" path existed digest size mode uid gid context backup
  while IFS="$SM_TAB" read -r path existed digest size mode uid gid context backup; do
    [ "$path" = "$wanted" ] || continue
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$existed" "$digest" "$size" "$mode" "$uid" "$gid" "$context" "$backup"
    return 0
  done <"$SM_ORIGINAL_FILE"
  return 1
}

sm_generate_journal() {
  local path digest size mode uid gid context kind original before operation sequence=0
  : >"$SM_JOURNAL_FILE.new" || return 1
  while IFS="$SM_TAB" read -r path digest size mode uid gid context kind; do
    [ -n "$path" ] || continue
    original="$(sm_lookup_original "$path" 2>/dev/null)" || original=
    before="$(printf '%s' "$original" | "$SM_BB" cut -f2)"
    if [ -n "$before" ] && [ "$before" != - ]; then operation=replace; else operation=create; before=-; fi
    sequence=$((sequence + 1))
    printf '%s\tpublish\t%s\t%s\t%s\t%s\ttrue\n' "$sequence" "$operation" "$path" "$before" "$digest" >>"$SM_JOURNAL_FILE.new" || return 1
  done <"$SM_OWNERSHIP_FILE"
  sm_atomic_publish "$SM_JOURNAL_FILE.new" "$SM_JOURNAL_FILE" journal-published
}

sm_json_value_or_null() {
  if [ -n "$1" ] && [ "$1" != - ]; then printf '"%s"' "$(sm_json_escape "$1")"; else printf 'null'; fi
}

sm_generate_manifest() {
  local output="$SM_STATE_DIR/.install-manifest.json.new" first path digest size mode uid gid context kind
  local existed backup sequence boundary operation before after committed abi old_ifs
  {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "install_id": "%s",\n' "$(sm_json_escape "$SM_INSTALL_ID")"
    printf '  "state": "COMMITTED",\n'
    printf '  "product": {"name": "KitsuneMagisk", "version": "%s", "source_commit": "%s", "upstream_base": "%s", "artifact_sha256": "%s"},\n' \
      "$(sm_json_escape "$SM_PRODUCT_VERSION")" "$SM_SOURCE_COMMIT" "$SM_UPSTREAM_BASE" "$SM_ARTIFACT_SHA256"
    printf '  "target": {"adapter_id": "%s", "fingerprint_sha256": "%s", "api": %s, "abis": [' \
      "$(sm_json_escape "$SM_ADAPTER_ID")" "$SM_FINGERPRINT_SHA256" "$(getprop ro.build.version.sdk)"
    first=true
    old_ifs="$IFS"; IFS=,
    for abi in $SM_TARGET_ABIS; do
      [ "$first" = true ] || printf ', '
      printf '"%s"' "$(sm_json_escape "$abi")"
      first=false
    done
    IFS="$old_ifs"
    printf ']},\n'
    printf '  "strategies": {"init": "%s", "selinux": "%s:%s", "runtime_tmpfs": "%s"},\n' \
      "$(sm_json_escape "$SM_INIT_PATH")" "$(sm_json_escape "$SM_SELINUX_STRATEGY")" "$(sm_json_escape "$SM_POLICY_SOURCE")" "$(sm_json_escape "$SM_RUNTIME_PATH")"
    printf '  "payload": [\n'
    first=true
    while IFS="$SM_TAB" read -r path digest size mode uid gid context kind; do
      [ -n "$path" ] || continue
      [ "$first" = true ] || printf ',\n'
      printf '    {"path": "%s", "sha256": "%s", "size": %s, "mode": "%s", "uid": %s, "gid": %s, "selinux_context": ' \
        "$(sm_json_escape "$path")" "$digest" "$size" "$mode" "$uid" "$gid"
      sm_json_value_or_null "$context"
      printf '}'
      first=false
    done <"$SM_OWNERSHIP_FILE"
    printf '\n  ],\n'
    printf '  "originals": [\n'
    first=true
    while IFS="$SM_TAB" read -r path existed digest size mode uid gid context backup; do
      [ -n "$path" ] || continue
      [ "$first" = true ] || printf ',\n'
      printf '    {"path": "%s", "sha256": ' "$(sm_json_escape "$path")"
      sm_json_value_or_null "$digest"
      printf ', "size": %s, "mode": ' "$size"
      sm_json_value_or_null "$mode"
      printf ', "uid": '
      if [ "$uid" = - ]; then printf 'null'; else printf '%s' "$uid"; fi
      printf ', "gid": '
      if [ "$gid" = - ]; then printf 'null'; else printf '%s' "$gid"; fi
      printf ', "selinux_context": '
      sm_json_value_or_null "$context"
      printf ', "existed": %s, "backup_path": ' "$existed"
      if [ "$existed" = true ]; then printf '"%s/%s"' "$SM_STATE_DIR" "$(sm_json_escape "$backup")"; else printf 'null'; fi
      printf '}'
      first=false
    done <"$SM_ORIGINAL_FILE"
    printf '\n  ],\n'
    printf '  "backup": {"external": true, "location": "%s", "sha256": "%s", "restore_command": "%s"},\n' \
      "$(sm_json_escape "$SM_BACKUP_LOCATION")" "$SM_BACKUP_SHA256" "$(sm_json_escape "$SM_RESTORE_COMMAND")"
    printf '  "journal": [\n'
    first=true
    while IFS="$SM_TAB" read -r sequence boundary operation path before after committed; do
      [ -n "$sequence" ] || continue
      [ "$first" = true ] || printf ',\n'
      printf '    {"sequence": %s, "boundary": "%s", "operation": "%s", "target": "%s", "before_sha256": ' \
        "$sequence" "$(sm_json_escape "$boundary")" "$operation" "$(sm_json_escape "$path")"
      sm_json_value_or_null "$before"
      printf ', "after_sha256": '
      sm_json_value_or_null "$after"
      printf ', "committed": %s}' "$committed"
      first=false
    done <"$SM_JOURNAL_FILE"
    printf '\n  ]\n}\n'
  } >"$output" || return 1
  "$SM_BB" grep -q '"schema_version": 1' "$output" && "$SM_BB" grep -q '"state": "COMMITTED"' "$output" || return 1
  sm_fsync "$output" || return 1
}

sm_remove_legacy_sidecars() {
  local path real
  for path in /system/etc/init/bootanim.rc.gz "$SM_POLICY_PATH.gz"; do
    [ "$path" != .gz ] || continue
    real="$(sm_real_path "$path")" || return 1
    if sm_path_present "$real"; then
      "$SM_BB" rm -f "$real" || return 1
      sm_fsync "$(sm_parent "$real")" || return 1
      sm_failpoint "remove-sidecar:$path" || return 1
    fi
  done
}

sm_commit_transaction() {
  local manifest_real staged_manifest
  sm_load_transaction || return 1
  [ "$SM_STATE" = STAGED ] || return 1
  sm_remove_legacy_sidecars || return 1
  sm_collect_ownership || return 1
  sm_generate_journal || return 1
  sm_generate_manifest || return 1
  manifest_real="$(sm_real_path "$SM_SYSTEM_DIR/install-manifest.json")" || return 1
  staged_manifest="$(sm_parent "$manifest_real")/.install-manifest.json.new"
  "$SM_BB" cp -a "$SM_STATE_DIR/.install-manifest.json.new" "$staged_manifest" || return 1
  sm_atomic_publish "$staged_manifest" "$manifest_real" manifest-published || return 1
  "$SM_BB" cp -a "$manifest_real" "$SM_STATE_DIR/.install-manifest.copy.new" || return 1
  sm_atomic_publish "$SM_STATE_DIR/.install-manifest.copy.new" "$SM_MANIFEST_COPY" manifest-copy-published || return 1
  # Neither durable manifest depends on its construction copy after both
  # publications have reached disk. Remove all staging names before exposing
  # COMMITTED so a completed transaction is distinguishable from an interrupted
  # one without relying on a later install or boot to clean it up.
  sm_cleanup_staging || return 1
  SM_COMMIT_BOOT_ID="$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"
  sm_update_state COMMITTED || return 1
  sm_failpoint committed || return 1
  "$SM_BB" rm -f "$SM_AUTHORIZATION_FILE" || sm_log "W: Recovery authorization was not consumed"
  sm_fsync /data/local/tmp 2>/dev/null || sm_log "W: Authorization cleanup fsync failed"
  return 0
}

sm_update_manifest_state() {
  local state="$1" manifest_real staged
  manifest_real="$(sm_real_path "$SM_SYSTEM_DIR/install-manifest.json")" || return 1
  [ -f "$manifest_real" ] || return 1
  staged="$(sm_parent "$manifest_real")/.install-manifest.state-new"
  "$SM_BB" sed "s/\"state\": \"COMMITTED\"/\"state\": \"$state\"/" "$manifest_real" >"$staged" || return 1
  "$SM_BB" grep -q "\"state\": \"$state\"" "$staged" || return 1
  sm_atomic_publish "$staged" "$manifest_real" "manifest-state:$state" || return 1
  "$SM_BB" cp -a "$manifest_real" "$SM_STATE_DIR/.install-manifest.copy.new" || return 1
  sm_atomic_publish "$SM_STATE_DIR/.install-manifest.copy.new" "$SM_MANIFEST_COPY" "manifest-copy-state:$state"
}

sm_verify_boot() {
  local boot_id staged
  sm_load_transaction || return 1
  [ "$SM_STATE" = COMMITTED ] || { [ "$SM_STATE" = BOOT_VERIFIED ]; return; }
  if ! sm_verify_owned; then
    sm_update_state ROLLBACK_REQUIRED
    sm_log "! System Mode boot verification failed"
    return 1
  fi
  [ -x "$SM_RUNTIME_PATH/magisk" ] || {
    sm_log "! System Mode runtime was not populated at $SM_RUNTIME_PATH"
    sm_update_state ROLLBACK_REQUIRED
    return 1
  }
  boot_id="$(cat /proc/sys/kernel/random/boot_id 2>/dev/null)"
  [ -n "$boot_id" ] || { sm_log "! Unable to read the boot identity"; return 1; }
  [ "$boot_id" != "$SM_COMMIT_BOOT_ID" ] || { sm_log "! A new boot is required before verification"; return 1; }
  staged="$SM_STATE_DIR/.boot-verified.env.new"
  {
    printf 'SCHEMA_VERSION=1\n'
    printf 'INSTALL_ID=%s\n' "$SM_INSTALL_ID"
    printf 'BOOT_ID=%s\n' "$boot_id"
    printf 'INIT_PATH=%s\n' "$SM_INIT_PATH"
    printf 'RUNTIME_PATH=%s\n' "$SM_RUNTIME_PATH"
  } >"$staged" || return 1
  sm_atomic_publish "$staged" "$SM_BOOT_PROOF" boot-proof-published || {
    sm_log "! Unable to persist System Mode boot proof"
    sm_update_state ROLLBACK_REQUIRED
    return 1
  }
  if ! sm_prepare_persistent_mounts; then
    sm_restore_persistent_mounts || true
    sm_log "! Unable to prepare persistent files for boot verification"
    sm_update_state ROLLBACK_REQUIRED
    return 1
  fi
  if ! sm_update_manifest_state BOOT_VERIFIED; then
    sm_restore_persistent_mounts || true
    sm_log "! Unable to publish the boot-verified manifest"
    sm_update_state ROLLBACK_REQUIRED
    return 1
  fi
  if ! sm_restore_persistent_mounts; then
    sm_log "! Unable to restore persistent filesystem modes after boot verification"
    sm_update_state ROLLBACK_REQUIRED
    return 1
  fi
  sm_update_state BOOT_VERIFIED || { sm_log "! Unable to publish the boot-verified state"; return 1; }
  "$SM_BB" rm -rf "$SM_ROLLBACK_DIR" || sm_log "W: Boot-verified rollback cleanup is incomplete"
  sm_fsync "$SM_STATE_DIR/rollback" "$SM_STATE_DIR" 2>/dev/null || true
  return 0
}

sm_restore_originals() {
  local path existed digest size mode uid gid context backup real source actual failed=0
  while IFS="$SM_TAB" read -r path existed digest size mode uid gid context backup; do
    [ -n "$path" ] || continue
    case "$path" in
      "$SM_SYSTEM_DIR"|"$SM_SYSTEM_DIR.rc"|"$SM_INIT_PATH"|"$SM_POLICY_PATH"|/system/etc/init/bootanim.rc|/data/adb/magisk|/system/addon.d/99-magisk.sh|/system/addon.d/magisk) ;;
      *) sm_log "! Refusing unrecognized original path $path"; return 1 ;;
    esac
    real="$(sm_real_path "$path")" || return 1
    if [ "$path" = /system/etc/init/bootanim.rc ]; then
      continue
    fi
    "$SM_BB" rm -rf "$real" || { failed=1; break; }
    if [ "$existed" = true ]; then
      source="$SM_STATE_DIR/$backup"
      sm_path_present "$source" || { failed=1; break; }
      actual="$(sm_digest_path "$source")"
      [ "$actual" = "$digest" ] || { failed=1; break; }
      "$SM_BB" mkdir -p "$(sm_parent "$real")" || { failed=1; break; }
      "$SM_BB" cp -a "$source" "$real" || { failed=1; break; }
      "$SM_BB" chmod "${mode#0}" "$real" || { failed=1; break; }
      "$SM_BB" chown "$uid:$gid" "$real" || { failed=1; break; }
      [ "$context" = - ] || chcon "$context" "$real" 2>/dev/null || { failed=1; break; }
      sm_fsync_tree "$real" || { failed=1; break; }
    fi
    sm_fsync_existing_parent "$real" || { failed=1; break; }
    sm_failpoint "uninstall:$path" || { failed=1; break; }
  done <"$SM_ORIGINAL_FILE"
  [ "$failed" = 0 ]
}

sm_validate_originals() {
  local path existed digest size mode uid gid context backup real source actual
  [ -f "$SM_ORIGINAL_FILE" ] || return 1
  while IFS="$SM_TAB" read -r path existed digest size mode uid gid context backup; do
    [ -n "$path" ] || continue
    if [ "$existed" = true ]; then
      source="$SM_STATE_DIR/$backup"
      sm_path_present "$source" || return 1
      [ "$(sm_digest_path "$source")" = "$digest" ] || return 1
    fi
    if [ "$path" = /system/etc/init/bootanim.rc ]; then
      real="$(sm_real_path "$path")" || return 1
      if [ "$existed" = true ]; then
        sm_path_present "$real" || return 1
        actual="$(sm_digest_path "$real")"
        [ "$actual" = "$digest" ] || return 1
      else
        ! sm_path_present "$real" || return 1
      fi
    fi
  done <"$SM_ORIGINAL_FILE"
  return 0
}

sm_assert_no_unowned_files() {
  local root real item canonical
  for root in "$SM_SYSTEM_DIR" /data/adb/magisk; do
    real="$(sm_real_path "$root")" || return 1
    [ -d "$real" ] || continue
    (
      cd "$real" || exit 1
      "$SM_BB" find . \( -type f -o -type l \) -print | "$SM_BB" sort
    ) | while IFS= read -r item; do
      canonical="$root/${item#./}"
      [ "$canonical" = "$SM_SYSTEM_DIR/install-manifest.json" ] && continue
      "$SM_BB" awk -F '\t' -v path="$canonical" '$1 == path { found=1 } END { exit !found }' "$SM_OWNERSHIP_FILE" || exit 1
    done || return 1
  done
  return 0
}

sm_uninstall() {
  sm_load_transaction || { sm_log "! A versioned System Mode manifest is required for uninstall"; return 1; }
  case "$SM_STATE" in
    PREFLIGHTED|STAGED|ROLLBACK_REQUIRED|ROLLING_BACK)
      sm_log "- Recovering interrupted System Mode uninstall before retry"
      sm_recover_pending || return 1
      sm_load_transaction || return 1
      ;;
  esac
  case "$SM_STATE" in BOOT_VERIFIED|COMMITTED) ;; *) sm_log "! System Mode is not in an uninstallable state"; return 1 ;; esac
  sm_validate_installed_state || { sm_log "! Exact System Mode uninstall refused"; return 1; }

  SM_PRIOR_STATE=BOOT_VERIFIED
  SM_TRANSACTION_ID="$(cat /proc/sys/kernel/random/uuid 2>/dev/null)"
  SM_ROLLBACK_DIR="$SM_STATE_DIR/rollback/$SM_TRANSACTION_ID"
  SM_STAGING_PATH="$(sm_parent "$SM_SYSTEM_DIR")/.magisk.kitsune-stage-$SM_TRANSACTION_ID"
  SM_COMMIT_BOOT_ID=
  "$SM_BB" rm -rf "$SM_STATE_DIR/rollback" || return 1
  "$SM_BB" mkdir -p "$SM_ROLLBACK_DIR" || return 1
  sm_snapshot_state_metadata || return 1
  sm_update_state PREFLIGHTED || return 1
  sm_snapshot_all || { sm_update_state FAILED; return 1; }
  sm_update_state STAGED || return 1
  if ! sm_restore_originals; then
    sm_update_state ROLLBACK_REQUIRED
    sm_restore_snapshot
    return 1
  fi
  if ! "$SM_BB" rm -f "$SM_MANIFEST_COPY" "$SM_OWNERSHIP_FILE" "$SM_ORIGINAL_FILE" \
       "$SM_JOURNAL_FILE" "$SM_BOOT_PROOF" ||
     ! "$SM_BB" rm -rf "$SM_STATE_DIR/original" ||
     ! sm_fsync "$SM_STATE_DIR"; then
    sm_update_state ROLLBACK_REQUIRED
    sm_restore_snapshot
    return 1
  fi
  SM_PRIOR_STATE=UNINSTALLED
  if ! sm_update_state UNINSTALLED; then
    sm_update_state ROLLBACK_REQUIRED
    sm_restore_snapshot
    return 1
  fi
  "$SM_BB" rm -rf "$SM_ROLLBACK_DIR" || sm_log "W: Uninstall rollback cleanup is incomplete"
  sm_fsync "$SM_STATE_DIR/rollback" "$SM_STATE_DIR" 2>/dev/null || true
  return 0
}
