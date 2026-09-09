#!/system/bin/sh
# shellcheck shell=busybox
# shellcheck disable=SC3040,SC3043

# Persistent transaction support shared by app, recovery, addon.d, boot
# verification, and uninstall entry points. This file is sourced; it never
# mutates a target merely by being loaded.
# shellcheck disable=SC2016

# All runtime entry points source this file from the packaged BusyBox ash. Do
# not allow a failed producer to be hidden by a successful final pipeline
# stage while calculating an ownership or rollback proof.
set -o pipefail || {
  echo "! System Mode requires a shell with pipefail support"
  return 1
}

SM_SCHEMA_VERSION=1
SM_AUTHORIZATION_FILE=/data/local/tmp/kitsune-system-mode-recovery-v1.env
SM_STATE_DIR=/data/adb/kitsune/system-mode
SM_TRANSACTION_FILE=$SM_STATE_DIR/transaction.env
SM_MANIFEST_COPY=$SM_STATE_DIR/install-manifest.json
SM_OWNERSHIP_FILE=$SM_STATE_DIR/ownership.tsv
SM_ORIGINAL_FILE=$SM_STATE_DIR/originals.tsv
SM_JOURNAL_FILE=$SM_STATE_DIR/journal.tsv
SM_BOOT_PROOF=$SM_STATE_DIR/boot-verified.env
SM_SECURE_DIR_METADATA=$SM_STATE_DIR/secure-dir.env
SM_SETUP_MARKER=/data/adb/.kitsune-system-mode-setup-v1.env
SM_ROLLBACK_TERMINAL=/data/adb/.kitsune-system-mode-rollback-v1.env
SM_PRIOR_TRANSACTION=/data/adb/.kitsune-system-mode-prior-v1.env
SM_LOCK_ROOT=/dev
SM_LOCK_FILE=$SM_LOCK_ROOT/.kitsune-system-mode-transaction-v1.lock
SM_REMOUNT_FILE=$SM_LOCK_ROOT/.kitsune-system-mode-remount-v1.env
SM_MOUNTS_FILE=/proc/mounts
SM_AUTHORIZATION_CLAIM=/data/local/tmp/.kitsune-system-mode-recovery-v1.claimed
SM_LOCK_HELD=false
SM_SLAVE_MOUNT_NAMESPACE=false
SM_LOCK_ACTION=
SM_LOCK_WAIT_ATTEMPTS=300
SM_LOCK_WAIT_INTERVAL=0.1
SM_PR5B_RECEIPT=false
SM_PR5B_MIGRATED=false
SM_PR5B_VALIDATED_RECEIPT_SHA256=
SM_PR5B_VALIDATED_MANIFEST_SHA256=
SM_PR5B_VALIDATED_OWNERSHIP_SHA256=
SM_PR5B_VALIDATED_ORIGINALS_SHA256=
SM_TAB="$(printf '\t')"
SM_SYSTEM_PAYLOAD_PREFIX=/system/etc/init/magisk/versions
SM_RESCUE_DIR=
SM_RESCUE_RC=
SM_RESCUE_PAYLOAD_PREFIX=

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

sm_lock_root_safe() {
  local uid mode
  [ -d "$SM_LOCK_ROOT" ] && [ ! -L "$SM_LOCK_ROOT" ] || return 1
  uid="$($SM_BB stat -c %u "$SM_LOCK_ROOT")" || return 1
  mode="$($SM_BB stat -c %a "$SM_LOCK_ROOT")" || return 1
  [ "$uid" = 0 ] && sm_reject_unsafe_mode "$mode"
}

sm_lock_file_safe() {
  local uid mode
  [ -f "$SM_LOCK_FILE" ] && [ ! -L "$SM_LOCK_FILE" ] || return 1
  uid="$($SM_BB stat -c %u "$SM_LOCK_FILE")" || return 1
  mode="$($SM_BB stat -c %a "$SM_LOCK_FILE")" || return 1
  [ "$uid" = 0 ] && sm_reject_unsafe_mode "$mode"
}

sm_release_lock() {
  local result=0
  [ "$SM_LOCK_HELD" = true ] || return 0
  "$SM_BB" flock -u 9 || result=1
  exec 9>&-
  SM_LOCK_HELD=false
  SM_LOCK_ACTION=
  [ "$result" = 0 ]
}

sm_acquire_lock() {
  local action="${1:-transaction}" boot attempt=0
  sm_valid_single_line "$action" || return 1
  case "$action" in *[!A-Za-z0-9._:-]*) return 1 ;; esac
  if [ "$SM_LOCK_HELD" = true ]; then
    [ "$SM_LOCK_ACTION" = "$action" ]
    return
  fi
  sm_lock_root_safe || {
    sm_log "! System Mode lock root is unsafe"
    return 1
  }
  if ! sm_path_present "$SM_LOCK_FILE"; then
    if ! (umask 077; set -C; : >"$SM_LOCK_FILE") 2>/dev/null; then
      sm_path_present "$SM_LOCK_FILE" || return 1
    fi
  fi
  sm_lock_file_safe || {
    sm_log "! System Mode transaction lock is unsafe"
    return 1
  }
  exec 9>>"$SM_LOCK_FILE" || return 1
  case "$SM_LOCK_WAIT_ATTEMPTS" in *[!0-9]*|'') exec 9>&-; return 1 ;; esac
  [ "$SM_LOCK_WAIT_ATTEMPTS" -gt 0 ] || { exec 9>&-; return 1; }
  # BusyBox flock has no portable timeout flag. Polling its nonblocking kernel
  # lock gives boot/rescue and app entry points a bounded handoff window while
  # retaining automatic release if the owning shell dies.
  while ! "$SM_BB" flock -n 9; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge "$SM_LOCK_WAIT_ATTEMPTS" ]; then
      exec 9>&-
      sm_log "! Another System Mode transaction did not release its lock"
      return 1
    fi
    "$SM_BB" sleep "$SM_LOCK_WAIT_INTERVAL" 9>&- || { exec 9>&-; return 1; }
  done
  SM_LOCK_HELD=true
  SM_LOCK_ACTION="$action"
  # The kernel lock, not this diagnostic record, owns exclusion. Keeping the
  # descriptor open makes process death release the lock automatically; /dev
  # removes the harmless inode at reboot without an unsafe unlink race.
  sm_lock_file_safe || { sm_release_lock; return 1; }
  boot="$(sm_current_boot_id)"
  sm_valid_uuid "$boot" || { sm_release_lock; return 1; }
  {
    printf 'SCHEMA_VERSION=1\n'
    printf 'ACTION=%s\n' "$action"
    printf 'BOOT_ID=%s\n' "$boot"
    printf 'PID=%s\n' "$$"
  } >"$SM_LOCK_FILE" || { sm_release_lock; return 1; }
  "$SM_BB" chmod 0600 "$SM_LOCK_FILE" || { sm_release_lock; return 1; }
  sm_fsync "$SM_LOCK_FILE" "$SM_LOCK_ROOT" || { sm_release_lock; return 1; }
  # Heal a prior holder that died after making an originally read-only mount
  # writable. This runs for every external entry point, including a terminal
  # uninstall whose receipt may already have been removed.
  if ! sm_restore_persistent_mounts; then
    sm_log "! Pending System Mode filesystem modes could not be restored"
    sm_release_lock
    return 1
  fi
  return 0
}

sm_require_lock() {
  [ "$SM_LOCK_HELD" = true ] || {
    sm_log "! System Mode transaction lock is not held"
    return 1
  }
}

sm_configure_rescue_paths() {
  local init_directory
  case "${SM_INIT_PATH:-}" in
    /system/etc/init/magisk.rc|/system/etc/init/hw/magisk.rc) ;;
    *) return 1 ;;
  esac
  init_directory="${SM_INIT_PATH%/*}"
  SM_RESCUE_DIR="$init_directory/.kitsune-system-mode-rescue"
  SM_RESCUE_RC="$init_directory/00-kitsune-magisk-rescue.rc"
  SM_RESCUE_PAYLOAD_PREFIX="$SM_RESCUE_DIR/versions"
}

sm_get() {
  local key="$1" file="$2"
  [ -f "$file" ] || return 1
  # Missing optional keys are represented by an empty value for compatibility
  # with pre-PR7 development receipts. Duplicate keys are never ambiguous:
  # accepting the first value would let a damaged receipt validate one value
  # and use another in a different parser.
  "$SM_BB" awk -v prefix="$key=" '
    index($0, prefix) == 1 {
      count++
      if (count == 1) value = substr($0, length(prefix) + 1)
    }
    END {
      if (count > 1) exit 1
      if (count == 1) print value
    }
  ' "$file"
}

sm_sha256_file() {
  local output digest
  output="$("$SM_BB" sha256sum "$1" 2>/dev/null)" || return 1
  digest="${output%%[[:space:]]*}"
  sm_valid_hex "$digest" 64 || return 1
  printf '%s\n' "$digest"
}

sm_path_present() {
  [ -e "$1" ] || [ -L "$1" ]
}

sm_reject_unsafe_mode() {
  # Reject group/world-writable state. The parent state directory is 0700, but
  # checking every retained component prevents a stale symlink or permissive
  # directory from redirecting a later root transaction.
  case "$1" in
    *[2367][0-7]|*[0-7][2367]) return 1 ;;
  esac
  return 0
}

sm_validate_state_storage() {
  local path uid mode
  for path in /data/adb/kitsune "$SM_STATE_DIR" "$SM_STATE_DIR/original" "$SM_STATE_DIR/rollback"; do
    sm_path_present "$path" || continue
    [ -d "$path" ] && [ ! -L "$path" ] || {
      sm_log "! Unsafe System Mode state directory: $path"
      return 1
    }
    uid="$($SM_BB stat -c %u "$path")" || return 1
    mode="$($SM_BB stat -c %a "$path")" || return 1
    if [ "$uid" != 0 ] || ! sm_reject_unsafe_mode "$mode"; then
      sm_log "! Unsafe System Mode state ownership: $path"
      return 1
    fi
  done
  for path in "$SM_TRANSACTION_FILE" "$SM_MANIFEST_COPY" "$SM_OWNERSHIP_FILE" \
    "$SM_ORIGINAL_FILE" "$SM_JOURNAL_FILE" "$SM_BOOT_PROOF" "$SM_SECURE_DIR_METADATA" \
    "$SM_SETUP_MARKER" "$SM_SETUP_MARKER.new" \
    "$SM_ROLLBACK_TERMINAL" "$SM_ROLLBACK_TERMINAL.new" \
    "$SM_PRIOR_TRANSACTION" "$SM_PRIOR_TRANSACTION.new" \
    "$SM_STATE_DIR/.pr5b-transaction.env.new"; do
    sm_path_present "$path" || continue
    [ -f "$path" ] && [ ! -L "$path" ] || {
      sm_log "! Unsafe System Mode state file: $path"
      return 1
    }
    uid="$($SM_BB stat -c %u "$path")" || return 1
    mode="$($SM_BB stat -c %a "$path")" || return 1
    if [ "$uid" != 0 ] || ! sm_reject_unsafe_mode "$mode"; then
      sm_log "! Unsafe System Mode state ownership: $path"
      return 1
    fi
  done
  return 0
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
  ' "$SM_MOUNTS_FILE"
}

sm_remount() {
  local mode="$1" mountpoint="$2"
  if [ -x /system/bin/mount ]; then
    /system/bin/mount -o "$mode,remount" "$mountpoint"
  else
    "$SM_BB" mount -o "$mode,remount" "$mountpoint"
  fi
}

sm_mount_mode_is() {
  local wanted="$1" mountpoint="$2" options
  options="$("$SM_BB" awk -v mountpoint="$mountpoint" \
    '$2 == mountpoint { print $4; found=1; exit } END { exit !found }' "$SM_MOUNTS_FILE")" || return 1
  case ",$options," in
    *",$wanted,"*) return 0 ;;
  esac
  return 1
}

sm_remount_file_safe() {
  local path="$1" uid mode
  [ -f "$path" ] && [ ! -L "$path" ] || return 1
  uid="$("$SM_BB" stat -c %u "$path")" || return 1
  mode="$("$SM_BB" stat -c %a "$path")" || return 1
  [ "$uid:$mode" = 0:600 ]
}

sm_valid_mountpoint() {
  local mountpoint="$1"
  sm_valid_single_line "$mountpoint" || return 1
  case "$mountpoint" in
    /*) ;;
    *) return 1 ;;
  esac
  case "$mountpoint" in
    *[!A-Za-z0-9._/-]*|*//*|*/./*|*/../*|*/.|*/..) return 1 ;;
  esac
  return 0
}

sm_load_remount_journal() {
  local staged="$SM_REMOUNT_FILE.new" schema boot encoded decoded mountpoint seen=''
  sm_require_lock || return 1
  if sm_path_present "$staged"; then
    sm_remount_file_safe "$staged" || {
      sm_log "! System Mode remount journal staging file is unsafe"
      return 1
    }
    "$SM_BB" rm -f "$staged" || return 1
    sm_fsync "$SM_LOCK_ROOT" || return 1
  fi
  sm_path_present "$SM_REMOUNT_FILE" || {
    SM_PERSISTENT_REMOUNTED=
    return 0
  }
  sm_remount_file_safe "$SM_REMOUNT_FILE" || {
    sm_log "! System Mode remount journal is unsafe"
    return 1
  }
  schema="$(sm_get SCHEMA_VERSION "$SM_REMOUNT_FILE")"
  boot="$(sm_get BOOT_ID "$SM_REMOUNT_FILE")"
  encoded="$(sm_get MOUNTPOINTS_B64 "$SM_REMOUNT_FILE")"
  [ "$schema" = 1 ] && sm_valid_uuid "$boot" && [ -n "$encoded" ] || return 1
  [ "$boot" = "$(sm_current_boot_id)" ] || {
    sm_log "! System Mode remount journal belongs to a different boot"
    return 1
  }
  decoded="$(sm_decode "$encoded")" || return 1
  sm_valid_single_line "$decoded" || return 1
  for mountpoint in $decoded; do
    sm_valid_mountpoint "$mountpoint" || return 1
    case " $seen " in *" $mountpoint "*) return 1 ;; esac
    seen="${seen:+$seen }$mountpoint"
  done
  [ -n "$seen" ] || return 1
  SM_PERSISTENT_REMOUNTED="$seen"
}

sm_write_remount_journal() {
  local staged="$SM_REMOUNT_FILE.new" mountpoint encoded boot
  sm_require_lock || return 1
  sm_lock_root_safe || return 1
  for mountpoint in $SM_PERSISTENT_REMOUNTED; do
    sm_valid_mountpoint "$mountpoint" || return 1
  done
  if [ -z "$SM_PERSISTENT_REMOUNTED" ]; then
    if sm_path_present "$SM_REMOUNT_FILE"; then
      sm_remount_file_safe "$SM_REMOUNT_FILE" || return 1
      "$SM_BB" rm -f "$SM_REMOUNT_FILE" || return 1
      ! sm_path_present "$SM_REMOUNT_FILE" || return 1
      sm_fsync "$SM_LOCK_ROOT" || return 1
    fi
    return 0
  fi
  if sm_path_present "$staged"; then
    sm_remount_file_safe "$staged" || return 1
    "$SM_BB" rm -f "$staged" || return 1
  fi
  encoded="$(printf '%s' "$SM_PERSISTENT_REMOUNTED" | "$SM_BB" base64 | "$SM_BB" tr -d '\n')" || return 1
  boot="$(sm_current_boot_id)"
  sm_valid_uuid "$boot" || return 1
  (
    umask 077
    {
      printf 'SCHEMA_VERSION=1\n'
      printf 'BOOT_ID=%s\n' "$boot"
      printf 'MOUNTPOINTS_B64=%s\n' "$encoded"
    } >"$staged"
  ) || return 1
  "$SM_BB" chmod 0600 "$staged" || return 1
  sm_fsync "$staged" || return 1
  "$SM_BB" mv -f "$staged" "$SM_REMOUNT_FILE" || return 1
  sm_fsync "$SM_REMOUNT_FILE" "$SM_LOCK_ROOT"
}

sm_record_persistent_remount() {
  local mountpoint="$1" prior="$SM_PERSISTENT_REMOUNTED"
  case " $prior " in *" $mountpoint "*) return 0 ;; esac
  SM_PERSISTENT_REMOUNTED="${prior:+$prior }$mountpoint"
  if ! sm_write_remount_journal; then
    SM_PERSISTENT_REMOUNTED="$prior"
    return 1
  fi
}

sm_prepare_persistent_mounts() {
  local canonical real mountpoint options seen='' persistent_policy='' failed=0
  sm_require_lock || return 1
  if [ "$SM_SLAVE_MOUNT_NAMESPACE" = true ]; then
    sm_quiesce_magisk || return 1
  fi
  sm_configure_rescue_paths || return 1
  # Load the boot-scoped journal first. It survives process death, while a cold
  # boot both resets mount modes and discards /dev, so it never misclassifies a
  # device's intentionally writable filesystem on a later boot.
  sm_load_remount_journal || return 1
  [ "${SM_POLICY_MUTATED:-false}" != true ] || persistent_policy="$SM_POLICY_PATH"
  for canonical in "$SM_SYSTEM_DIR" "$SM_SYSTEM_DIR.rc" "$SM_INIT_PATH" \
    "$SM_RESCUE_DIR" "$SM_RESCUE_RC" "$persistent_policy" /system/etc/init/bootanim.rc \
    /system/addon.d/99-magisk.sh /system/addon.d/magisk \
    /data/adb/magisk /data/adb/magisk.db /data/adb/magisk.db-wal /data/adb/magisk.db-shm \
    /data/adb/modules /data/adb/modules_update /data/adb/post-fs-data.d /data/adb/service.d \
    /data/adb/sepolicy.rule /cache/magisk.log /cache/magisk.log.bak; do
    [ -n "$canonical" ] || continue
    real="$(sm_real_path "$canonical")" || { failed=1; break; }
    mountpoint="$(sm_mountpoint_for "$real")" || { failed=1; break; }
    case "|$seen|" in *"|$mountpoint|"*) continue ;; esac
    seen="${seen:+$seen|}$mountpoint"
    options="$("$SM_BB" awk -v mountpoint="$mountpoint" '$2 == mountpoint { print $4; exit }' "$SM_MOUNTS_FILE")" || {
      failed=1
      break
    }
    case ",$options," in *,rw,*) continue ;; esac
    case ",$options," in
      *,ro,*) ;;
      *) sm_log "! Unknown mount mode for $mountpoint"; failed=1; break ;;
    esac
    # Durably record original-ro ownership before making the mount writable.
    # A process death at either side of the remount is therefore retry-safe.
    if ! sm_record_persistent_remount "$mountpoint"; then
      failed=1
      break
    fi
    sm_log "- Remounting System Mode filesystem read-write: $mountpoint"
    sm_remount rw "$mountpoint" || {
      sm_log "! Unable to remount System Mode filesystem: $mountpoint"
      failed=1
      break
    }
    sm_mount_mode_is rw "$mountpoint" || {
      sm_log "! System Mode filesystem did not become writable: $mountpoint"
      failed=1
      break
    }
  done
  if [ "$failed" != 0 ]; then
    # A prepare failure is itself transactionally unwound. Retain any entry
    # whose read-only restoration cannot be proven so the next lock holder
    # retries it before touching persistent state.
    sm_restore_persistent_mounts || \
      sm_log "! Partial System Mode remount preparation remains pending"
    return 1
  fi
  return 0
}

sm_runtime_stage_path() {
  printf '/data/adb/.magisk.kitsune-stage-%s\n' "${SM_TRANSACTION_ID:-cleanup}"
}

sm_runtime_old_path() {
  printf '/data/adb/.magisk.kitsune-old-%s\n' "${SM_TRANSACTION_ID:-cleanup}"
}

sm_rescue_stage_path() {
  sm_configure_rescue_paths || return 1
  printf '%s/.kitsune-system-mode-rescue-stage-%s\n' "${SM_INIT_PATH%/*}" "${SM_TRANSACTION_ID:-legacy}"
}

sm_restore_persistent_mounts() {
  local mountpoint failed=0 pending=''
  sm_require_lock || return 1
  sm_load_remount_journal || return 1
  # The managed Android mountpoints are fixed paths without whitespace.
  # shellcheck disable=SC2086
  for mountpoint in $SM_PERSISTENT_REMOUNTED; do
    if ! sm_remount ro "$mountpoint" || ! sm_mount_mode_is ro "$mountpoint"; then
      failed=1
      pending="${pending:+$pending }$mountpoint"
    fi
  done
  # Retain failed entries so a retry cannot silently report success while a
  # filesystem this transaction made writable is still writable.
  SM_PERSISTENT_REMOUNTED="$pending"
  sm_write_remount_journal || return 1
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
  local listing context
  # Disabled/permissive kernels do not enforce labels, and some writable
  # emulator filesystems present the same xattr as "unlabeled" after a cold
  # start. Keep strict context ownership where SELinux actually enforces it.
  if [ ! -r /sys/fs/selinux/enforce ] || [ "$(cat /sys/fs/selinux/enforce 2>/dev/null)" != 1 ]; then
    printf '%s\n' -
    return 0
  fi
  listing="$("$SM_BB" ls -Zd "$1" 2>/dev/null)" || return 1
  context="$(printf '%s\n' "$listing" | "$SM_BB" awk '
    NR == 1 {
      for (i = 1; i <= NF; i++) {
        if ($i ~ /^u:[^:]+:[^:]+:s[0-9]/) {
          print $i
          exit
        }
      }
    }
  ')" || return 1
  [ -n "$context" ] || return 1
  printf '%s\n' "$context"
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
  local path="$1" item rel kind digest mode uid gid target output
  local work='' base paths sorted metadata candidate
  if [ -L "$path" ]; then
    target="$($SM_BB readlink "$path")" || return 1
    output="$(printf 'link:%s' "$target" | "$SM_BB" sha256sum)" || return 1
    digest="${output%%[[:space:]]*}"
    sm_valid_hex "$digest" 64 || return 1
    printf '%s\n' "$digest"
  elif [ -f "$path" ]; then
    sm_sha256_file "$path"
  elif [ -d "$path" ]; then
    # Root's /dev tmpfs is preferred so a hard death cannot strand digest
    # scratch in persistent state and later block exact rollback/uninstall.
    for candidate in /dev "$SM_STATE_DIR" "${TMPDIR:-}" /data/local/tmp; do
      [ -n "$candidate" ] && [ -d "$candidate" ] && [ ! -L "$candidate" ] && [ -w "$candidate" ] || continue
      work="$candidate"
      break
    done
    [ -n "$work" ] || return 1
    base="$work/.kitsune-system-mode-digest.${SM_TRANSACTION_ID:-preflight}.$$"
    paths="$base.paths"
    sorted="$base.sorted"
    metadata="$base.metadata"
    ! sm_path_present "$paths" && ! sm_path_present "$sorted" && ! sm_path_present "$metadata" || return 1
    : >"$metadata" || return 1
    (
      cd "$path" || exit 1
      "$SM_BB" find . -mindepth 1 -print >"$paths" || exit 1
      "$SM_BB" sort "$paths" >"$sorted" || exit 1
      while IFS= read -r item; do
        rel="${item#./}"
        sm_valid_single_line "$rel" || exit 1
        mode="$($SM_BB stat -c %a "$item")" || exit 1
        uid="$($SM_BB stat -c %u "$item")" || exit 1
        gid="$($SM_BB stat -c %g "$item")" || exit 1
        if [ -L "$item" ]; then
          kind="link"
          target="$($SM_BB readlink "$item")" || exit 1
          output="$(printf 'link:%s' "$target" | "$SM_BB" sha256sum)" || exit 1
          digest="${output%%[[:space:]]*}"
          sm_valid_hex "$digest" 64 || exit 1
        elif [ -f "$item" ]; then
          kind="file"
          digest="$(sm_sha256_file "$item")" || exit 1
        elif [ -d "$item" ]; then
          kind=directory
          digest=-
        else
          kind=other
          digest=-
        fi
        printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
          "$rel" "$kind" "$digest" "$mode" "$uid" "$gid" >>"$metadata" || exit 1
      done <"$sorted"
    ) || {
      "$SM_BB" rm -f "$paths" "$sorted" "$metadata"
      return 1
    }
    digest="$(sm_sha256_file "$metadata")" || {
      "$SM_BB" rm -f "$paths" "$sorted" "$metadata"
      return 1
    }
    "$SM_BB" rm -f "$paths" "$sorted" "$metadata" || return 1
    printf '%s\n' "$digest"
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

sm_current_boot_id() {
  cat /proc/sys/kernel/random/boot_id 2>/dev/null
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

sm_authorization_file_safe() {
  local path="$1" uid mode
  [ -f "$path" ] && [ ! -L "$path" ] || return 1
  uid="$($SM_BB stat -c %u "$path")" || return 1
  mode="$($SM_BB stat -c %a "$path")" || return 1
  case "$uid:$mode" in 0:600|2000:600) return 0 ;; esac
  return 1
}

sm_validate_authorization() {
  local auth="$SM_AUTHORIZATION_FILE" schema serial fingerprint api
  local adapter_b64 init_b64 selinux_b64 snapshot_b64 location_b64 restore_b64
  local authorized_source authorized_artifact issued expires now live_boot boot_digest lease_port
  sm_authorization_file_safe "$auth" || {
    sm_log "! Run 'kitsune system-mode authorize' with a verified doctor report first"
    return 1
  }
  schema="$(sm_get SCHEMA_VERSION "$auth")"
  [ "$schema" = "$SM_SCHEMA_VERSION" ] || { sm_log "! Unsupported recovery authorization"; return 1; }
  SM_REPORT_SHA256="$(sm_get REPORT_SHA256 "$auth")"
  SM_AUTHORIZATION_ID="$(sm_get AUTHORIZATION_ID "$auth")"
  issued="$(sm_get ISSUED_AT_EPOCH "$auth")"
  expires="$(sm_get EXPIRES_AT_EPOCH "$auth")"
  SM_TARGET_CONTRACT_SHA256="$(sm_get TARGET_CONTRACT_SHA256 "$auth")"
  SM_AUTH_BOOT_ID_SHA256="$(sm_get BOOT_ID_SHA256 "$auth")"
  SM_PROBE_SHA256="$(sm_get PROBE_SHA256 "$auth")"
  authorized_source="$(sm_get SOURCE_COMMIT "$auth")"
  authorized_artifact="$(sm_get ARTIFACT_SHA256 "$auth")"
  SM_QUALIFICATION_SHA256="$(sm_get QUALIFICATION_SHA256 "$auth")"
  SM_INSTANCE_IDENTITY_SHA256="$(sm_get INSTANCE_IDENTITY_SHA256 "$auth")"
  lease_port="$(sm_get LEASE_PORT "$auth")"
  SM_LEASE_NONCE_SHA256="$(sm_get LEASE_NONCE_SHA256 "$auth")"
  serial="$(sm_get SERIAL_SHA256 "$auth")"
  fingerprint="$(sm_get FINGERPRINT_SHA256 "$auth")"
  SM_BACKUP_SHA256="$(sm_get BACKUP_SHA256 "$auth")"
  case "$SM_REPORT_SHA256:$serial:$fingerprint:$SM_BACKUP_SHA256" in
    *[!a-f0-9:]*|*::*|:*|*:) sm_log "! Invalid recovery authorization digest"; return 1 ;;
  esac
  [ "${#SM_REPORT_SHA256}" -eq 64 ] && [ "${#serial}" -eq 64 ] &&
    [ "${#fingerprint}" -eq 64 ] &&
    [ "${#SM_BACKUP_SHA256}" -eq 64 ] || {
      sm_log "! Invalid recovery authorization digest length"
      return 1
    }
  if ! sm_valid_hex "$authorized_source" 40 || ! sm_valid_hex "$authorized_artifact" 64 ||
     ! sm_valid_hex "$SM_QUALIFICATION_SHA256" 64 ||
     ! sm_valid_hex "$SM_INSTANCE_IDENTITY_SHA256" 64 ||
     ! sm_valid_hex "$SM_LEASE_NONCE_SHA256" 64; then
    sm_log "! Invalid authorized source or artifact identity"
    return 1
  fi
  sm_valid_uuid "$SM_AUTHORIZATION_ID" || { sm_log "! Invalid authorization identity"; return 1; }
  case "$issued:$expires" in *[!0-9:]*|*::*|:*|*:) sm_log "! Invalid authorization lifetime"; return 1 ;; esac
  now="$(date +%s 2>/dev/null)"
  case "$now" in *[!0-9]*|'') sm_log "! Target clock cannot validate authorization lifetime"; return 1 ;; esac
  [ "$now" -ge "$issued" ] && [ "$now" -lt "$expires" ] && [ $((expires - issued)) -le 300 ] || {
    sm_log "! Recovery authorization expired or has an invalid lifetime"
    return 1
  }
  case "$lease_port" in *[!0-9]*|'') sm_log "! Invalid host lease port"; return 1 ;; esac
  [ "$lease_port" -ge 1 ] && [ "$lease_port" -le 65535 ] || {
    sm_log "! Host lease port is outside the TCP range"
    return 1
  }
  SM_LEASE_PORT="$lease_port"
  SM_AUTH_EXPIRES_AT_EPOCH="$expires"
  if ! sm_valid_hex "$SM_TARGET_CONTRACT_SHA256" 64 ||
     ! sm_valid_hex "$SM_AUTH_BOOT_ID_SHA256" 64 || ! sm_valid_hex "$SM_PROBE_SHA256" 64; then
    sm_log "! Invalid authorization target contract"
    return 1
  fi
  live_boot="$(sm_current_boot_id)"
  sm_valid_uuid "$live_boot" || { sm_log "! Unable to bind authorization to this boot"; return 1; }
  boot_digest="$(printf '%s' "$live_boot" | "$SM_BB" sha256sum | "$SM_BB" awk '{ print $1 }')" || return 1
  [ "$boot_digest" = "$SM_AUTH_BOOT_ID_SHA256" ] || {
    sm_log "! Recovery authorization belongs to a different boot attempt"
    return 1
  }
  [ "$authorized_source" = "${KITSUNE_SOURCE_COMMIT:-}" ] || {
    sm_log "! Recovery authorization belongs to a different source commit"
    return 1
  }
  SM_AUTH_ARTIFACT_SHA256="$authorized_artifact"
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
  case "$SM_TARGET_ABIS" in *[!A-Za-z0-9,._-]*|''|,*|*,|*,,*) sm_log "! Invalid target ABI list"; return 1 ;; esac
  case "$SM_ADAPTER_ID" in *[!A-Za-z0-9._-]*) sm_log "! Invalid adapter identifier"; return 1 ;; esac
  case "$SM_RESTORE_COMMAND" in true|false|:|'/bin/true'|'/usr/bin/true'|'exit 0') sm_log "! Recovery command is a no-op"; return 1 ;; esac
  case "$SM_AUTH_INIT_DIRECTORY" in
    /system/etc/init|/system/etc/init/hw) ;;
    *) sm_log "! Unauthorized init directory"; return 1 ;;
  esac
  SM_SERIAL_SHA256="$serial"
  SM_FINGERPRINT_SHA256="$fingerprint"
  SM_TARGET_API="$api"
  SM_AUTH_FILE_SHA256="$(sm_sha256_file "$auth")" || return 1
  sm_validate_live_target || return 1
  return 0
}

sm_validate_host_lease() {
  local now timeout response digest
  now="$(date +%s 2>/dev/null)"
  case "$now" in *[!0-9]*|'') sm_log "! Target clock cannot validate host lease"; return 1 ;; esac
  [ "$now" -lt "$SM_AUTH_EXPIRES_AT_EPOCH" ] || {
    sm_log "! Recovery authorization expired before host handoff"
    return 1
  }
  timeout=$((SM_AUTH_EXPIRES_AT_EPOCH - now))
  response="$(
    "$SM_BB" timeout "$timeout" "$SM_BB" wget -qO- \
      "http://127.0.0.1:$SM_LEASE_PORT/$SM_AUTHORIZATION_ID" 2>/dev/null
  )" || {
    sm_log "! Exact live ADB host lease was not available"
    return 1
  }
  digest="$(printf '%s' "$response" | "$SM_BB" sha256sum | "$SM_BB" awk '{ print $1 }')" || return 1
  [ "$digest" = "$SM_LEASE_NONCE_SHA256" ] || {
    sm_log "! Host lease nonce did not match this authorization"
    return 1
  }
  now="$(date +%s 2>/dev/null)"
  case "$now" in *[!0-9]*|'') return 1 ;; esac
  [ "$now" -lt "$SM_AUTH_EXPIRES_AT_EPOCH" ] || {
    sm_log "! Recovery authorization expired during host verification"
    return 1
  }
  return 0
}

sm_cleanup_authorization_claim() {
  local claim="$SM_AUTHORIZATION_CLAIM"
  sm_require_lock || return 1
  sm_path_present "$claim" || return 0
  sm_authorization_file_safe "$claim" || {
    sm_log "! Stale recovery authorization claim is unsafe"
    return 1
  }
  "$SM_BB" rm -f "$claim" || return 1
  ! sm_path_present "$claim" || return 1
  sm_fsync /data/local/tmp
}

sm_consume_authorization() {
  local claim="$SM_AUTHORIZATION_CLAIM"
  sm_require_lock || return 1
  sm_cleanup_authorization_claim || return 1
  sm_authorization_file_safe "$SM_AUTHORIZATION_FILE" || return 1
  [ "$(sm_sha256_file "$SM_AUTHORIZATION_FILE")" = "$SM_AUTH_FILE_SHA256" ] || {
    sm_log "! Recovery authorization changed before one-time consumption"
    return 1
  }
  # Rename is the one-time claim. Unlike rm -f, it cannot let two consumers
  # both report success after validating the same pathname. A death after the
  # rename leaves no replayable canonical authorization; the next host handoff
  # must issue a fresh one and may discard this root/shell-owned tombstone.
  "$SM_BB" mv "$SM_AUTHORIZATION_FILE" "$claim" || return 1
  sm_fsync /data/local/tmp || return 1
  ! sm_path_present "$SM_AUTHORIZATION_FILE" || return 1
  sm_authorization_file_safe "$claim" || return 1
  [ "$(sm_sha256_file "$claim")" = "$SM_AUTH_FILE_SHA256" ] || return 1
  "$SM_BB" rm -f "$claim" || return 1
  ! sm_path_present "$claim" || return 1
  sm_fsync /data/local/tmp
}

sm_detect_preinit() {
  local binary="$1" device status matches count mount_point resolved
  [ -x "$binary" ] || return 1
  device="$("$SM_BB" env MAGISKTMP= "$binary" --preinit-device 9>&- 2>/dev/null)"
  status=$?
  case "$status" in
    0) [ -n "$device" ] || return 1 ;;
    1) [ -z "$device" ] || return 1 ;;
    *) return 1 ;;
  esac
  if [ "$status" = 1 ]; then
    SM_DETECTED_PREINIT_DEVICE=
    SM_DETECTED_PREINIT_DIR=
    return 0
  fi
  case "$device" in *[!A-Za-z0-9._-]*|.*|'') return 1 ;; esac
  matches="$("$SM_BB" awk -v wanted="$device" '
    function basename(path, fields) {
      fields = split(path, parts, "/")
      return parts[fields]
    }
    $4 == "/" {
      separator = 0
      for (field = 7; field <= NF; field++) {
        if ($field == "-") { separator = field; break }
      }
      if (!separator || basename($(separator + 2)) != wanted) next
      if (("," $6 ",") !~ /,rw,/) next
      print $5
    }
  ' /proc/self/mountinfo)" || return 1
  count="$(printf '%s\n' "$matches" | "$SM_BB" awk 'NF { count++ } END { print count + 0 }')" || return 1
  [ "$count" = 1 ] || {
    sm_log "! Pre-init device does not resolve to one writable root mount"
    return 1
  }
  mount_point="$(printf '%s\n' "$matches" | "$SM_BB" awk 'NF { print; exit }')"
  case "$mount_point" in /data|/cache|/klogdump|/metadata|/persist|/mnt/vendor/persist) ;; *) return 1 ;; esac
  if [ -e "$mount_point/unencrypted" ]; then
    resolved="$mount_point/unencrypted/magisk"
  elif [ -e "$mount_point/adb" ]; then
    resolved="$mount_point/adb"
  elif [ -e "$mount_point/watchdog" ]; then
    resolved="$mount_point/watchdog/magisk"
  else
    resolved="$mount_point/magisk"
  fi
  SM_DETECTED_PREINIT_DEVICE="$device"
  SM_DETECTED_PREINIT_DIR="$resolved"
  return 0
}

sm_select_preinit_strategy() {
  local binary="$1" uid mode
  sm_detect_preinit "$binary" || {
    sm_log "! Unable to resolve the exact v30.7 pre-init storage target"
    return 1
  }
  SM_PREINIT_DEVICE="$SM_DETECTED_PREINIT_DEVICE"
  SM_PREINIT_DIR="$SM_DETECTED_PREINIT_DIR"
  [ -n "$SM_PREINIT_DEVICE" ] || return 0
  [ "$SM_PREINIT_DIR" = /data/adb ] || {
    sm_log "! PR7 permits pre-init storage only in the transaction-owned /data/adb root"
    return 1
  }
  [ -d /data/adb ] && [ ! -L /data/adb ] || return 1
  uid="$($SM_BB stat -c %u /data/adb)" || return 1
  mode="$($SM_BB stat -c %a /data/adb)" || return 1
  [ "$uid" = 0 ] && sm_reject_unsafe_mode "$mode" || return 1
  return 0
}

sm_select_strategies() {
  local candidate real uid mode
  SM_RUNTIME_PATH="${KITSUNE_SYSTEM_RUNTIME:-}"
  if [ -n "$SM_RUNTIME_PATH" ]; then
    case "$SM_RUNTIME_PATH" in /sbin|/debug_ramdisk) ;; *) sm_log "! Invalid adapter runtime path"; return 1 ;; esac
  elif [ -d /debug_ramdisk ] && [ -w /debug_ramdisk ]; then
    SM_RUNTIME_PATH=/debug_ramdisk
  # Never mount through a /sbin symlink. On newer Android layouts that can
  # resolve into /system/bin and hide the platform binaries we still need to
  # finish booting. Only the legacy real-directory layout is eligible.
  elif [ ! -d /sbin ] || [ -L /sbin ]; then
    SM_RUNTIME_PATH=/debug_ramdisk
  else
    SM_RUNTIME_PATH=/sbin
  fi
  case "$SM_RUNTIME_PATH" in
    /debug_ramdisk)
      [ ! -L /debug_ramdisk ] || {
        sm_log "! Refusing a symbolic-link /debug_ramdisk runtime"
        return 1
      }
      ;;
    /sbin)
      [ ! -L /sbin ] || {
        sm_log "! Refusing a symbolic-link /sbin runtime"
        return 1
      }
      ;;
  esac
  SM_INIT_PATH="$SM_AUTH_INIT_DIRECTORY/magisk.rc"
  sm_configure_rescue_paths || return 1
  real="$(sm_real_path "$SM_AUTH_INIT_DIRECTORY")" || return 1
  [ -d "$real" ] && [ ! -L "$real" ] || { sm_log "! Authorized init directory is unavailable"; return 1; }
  uid="$($SM_BB stat -c %u "$real")" || return 1
  mode="$($SM_BB stat -c %a "$real")" || return 1
  if [ "$uid" != 0 ] || ! sm_reject_unsafe_mode "$mode"; then
    sm_log "! Authorized init directory is unsafe"
    return 1
  fi

  SM_POLICY_PATH=
  for candidate in \
    /sepolicy \
    /sepolicy_debug \
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
  case "$SM_SELINUX_STRATEGY" in
    precompiled) case "$SM_POLICY_PATH" in */precompiled_sepolicy) ;; *) sm_log "! Doctor and installer policy strategies disagree"; return 1 ;; esac ;;
    monolithic) case "$SM_POLICY_PATH" in /sepolicy|/sepolicy_debug|/system_root/sepolicy|/system_root/sepolicy_debug|/system_root/sepolicy.unlocked) ;; *) sm_log "! Doctor and installer policy strategies disagree"; return 1 ;; esac ;;
    split) SM_POLICY_PATH= ;;
    disabled) SM_POLICY_PATH= ;;
    *) sm_log "! Unsupported SELinux strategy"; return 1 ;;
  esac
  SM_POLICY_SOURCE="$SM_POLICY_PATH"
  if command -v is_rootfs >/dev/null 2>&1 && is_rootfs; then
    SM_POLICY_PATH=
    SM_SELINUX_STRATEGY="live+$SM_SELINUX_STRATEGY"
  fi
  sm_select_preinit_strategy "$SM_INSTALL_DIR/magisk" || return 1
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
    printf 'SERIAL_SHA256=%s\n' "$SM_SERIAL_SHA256"
    printf 'FINGERPRINT_SHA256=%s\n' "$SM_FINGERPRINT_SHA256"
    printf 'TARGET_API=%s\n' "$SM_TARGET_API"
    printf 'REPORT_SHA256=%s\n' "$SM_REPORT_SHA256"
    printf 'AUTHORIZATION_ID=%s\n' "$SM_AUTHORIZATION_ID"
    printf 'TARGET_CONTRACT_SHA256=%s\n' "$SM_TARGET_CONTRACT_SHA256"
    printf 'AUTH_BOOT_ID_SHA256=%s\n' "$SM_AUTH_BOOT_ID_SHA256"
    printf 'PROBE_SHA256=%s\n' "$SM_PROBE_SHA256"
    printf 'QUALIFICATION_SHA256=%s\n' "$SM_QUALIFICATION_SHA256"
    printf 'INSTANCE_IDENTITY_SHA256=%s\n' "$SM_INSTANCE_IDENTITY_SHA256"
    printf 'BACKUP_SHA256=%s\n' "$SM_BACKUP_SHA256"
    printf 'MANIFEST_SHA256=%s\n' "$SM_MANIFEST_SHA256"
    printf 'OWNERSHIP_SHA256=%s\n' "$SM_OWNERSHIP_SHA256"
    printf 'ORIGINALS_SHA256=%s\n' "$SM_ORIGINALS_SHA256"
    printf 'SECURE_DIR_SHA256=%s\n' "$SM_SECURE_DIR_SHA256"
    printf 'ADAPTER_ID_B64=%s\n' "$(printf '%s' "$SM_ADAPTER_ID" | "$SM_BB" base64 | "$SM_BB" tr -d '\n')"
    printf 'TARGET_ABIS_B64=%s\n' "$(printf '%s' "$SM_TARGET_ABIS" | "$SM_BB" base64 | "$SM_BB" tr -d '\n')"
    printf 'SNAPSHOT_ID_B64=%s\n' "$(printf '%s' "$SM_SNAPSHOT_ID" | "$SM_BB" base64 | "$SM_BB" tr -d '\n')"
    printf 'BACKUP_LOCATION_B64=%s\n' "$(printf '%s' "$SM_BACKUP_LOCATION" | "$SM_BB" base64 | "$SM_BB" tr -d '\n')"
    printf 'RESTORE_COMMAND_B64=%s\n' "$(printf '%s' "$SM_RESTORE_COMMAND" | "$SM_BB" base64 | "$SM_BB" tr -d '\n')"
    printf 'INIT_PATH=%s\n' "$SM_INIT_PATH"
    printf 'POLICY_PATH=%s\n' "$SM_POLICY_PATH"
    printf 'POLICY_SOURCE=%s\n' "$SM_POLICY_SOURCE"
    printf 'RUNTIME_PATH=%s\n' "$SM_RUNTIME_PATH"
    printf 'ACTIVE_PAYLOAD=%s\n' "$SM_ACTIVE_PAYLOAD"
    printf 'RESCUE_PAYLOAD=%s\n' "$SM_RESCUE_PAYLOAD"
    printf 'SELINUX_STRATEGY=%s\n' "$SM_SELINUX_STRATEGY"
    printf 'PREINIT_DEVICE_B64=%s\n' "$(printf '%s' "$SM_PREINIT_DEVICE" | "$SM_BB" base64 | "$SM_BB" tr -d '\n')"
    printf 'PREINIT_DIR_B64=%s\n' "$(printf '%s' "$SM_PREINIT_DIR" | "$SM_BB" base64 | "$SM_BB" tr -d '\n')"
    printf 'POLICY_MUTATED=%s\n' "$SM_POLICY_MUTATED"
    printf 'LEGACY_MIGRATION=%s\n' "$SM_LEGACY_MIGRATION"
    printf 'SOURCE_COMMIT=%s\n' "$SM_SOURCE_COMMIT"
    printf 'UPSTREAM_BASE=%s\n' "$SM_UPSTREAM_BASE"
    printf 'ARTIFACT_SHA256=%s\n' "$SM_ARTIFACT_SHA256"
    printf 'VERSION_CODE=%s\n' "$SM_VERSION_CODE"
    printf 'PRODUCT_VERSION_B64=%s\n' "$(printf '%s' "$SM_PRODUCT_VERSION" | "$SM_BB" base64 | "$SM_BB" tr -d '\n')"
    printf 'COMMIT_BOOT_ID=%s\n' "$SM_COMMIT_BOOT_ID"
    printf 'BOOT_ATTEMPT_ID=%s\n' "$SM_BOOT_ATTEMPT_ID"
    printf 'ROLLBACK_DIR=%s\n' "$SM_ROLLBACK_DIR"
    printf 'STAGING_PATH=%s\n' "$SM_STAGING_PATH"
  } >"$staged" || return 1
  "$SM_BB" chmod 0600 "$staged" || return 1
  sm_atomic_publish "$staged" "$SM_TRANSACTION_FILE" "state:$SM_STATE"
}

sm_load_transaction() {
  local migration_marker zero
  sm_validate_state_storage || return 1
  [ -f "$SM_TRANSACTION_FILE" ] || return 1
  [ "$(sm_get SCHEMA_VERSION "$SM_TRANSACTION_FILE")" = "$SM_SCHEMA_VERSION" ] || return 1
  SM_INSTALL_ID="$(sm_get INSTALL_ID "$SM_TRANSACTION_FILE")"
  SM_TRANSACTION_ID="$(sm_get TRANSACTION_ID "$SM_TRANSACTION_FILE")"
  SM_STATE="$(sm_get STATE "$SM_TRANSACTION_FILE")"
  SM_PRIOR_STATE="$(sm_get PRIOR_STATE "$SM_TRANSACTION_FILE")"
  SM_SERIAL_SHA256="$(sm_get SERIAL_SHA256 "$SM_TRANSACTION_FILE")"
  SM_FINGERPRINT_SHA256="$(sm_get FINGERPRINT_SHA256 "$SM_TRANSACTION_FILE")"
  SM_TARGET_API="$(sm_get TARGET_API "$SM_TRANSACTION_FILE")"
  SM_REPORT_SHA256="$(sm_get REPORT_SHA256 "$SM_TRANSACTION_FILE")"
  SM_AUTHORIZATION_ID="$(sm_get AUTHORIZATION_ID "$SM_TRANSACTION_FILE")"
  SM_TARGET_CONTRACT_SHA256="$(sm_get TARGET_CONTRACT_SHA256 "$SM_TRANSACTION_FILE")"
  SM_AUTH_BOOT_ID_SHA256="$(sm_get AUTH_BOOT_ID_SHA256 "$SM_TRANSACTION_FILE")"
  SM_PROBE_SHA256="$(sm_get PROBE_SHA256 "$SM_TRANSACTION_FILE")"
  SM_QUALIFICATION_SHA256="$(sm_get QUALIFICATION_SHA256 "$SM_TRANSACTION_FILE")"
  SM_INSTANCE_IDENTITY_SHA256="$(sm_get INSTANCE_IDENTITY_SHA256 "$SM_TRANSACTION_FILE")"
  SM_BACKUP_SHA256="$(sm_get BACKUP_SHA256 "$SM_TRANSACTION_FILE")"
  SM_MANIFEST_SHA256="$(sm_get MANIFEST_SHA256 "$SM_TRANSACTION_FILE")"
  SM_OWNERSHIP_SHA256="$(sm_get OWNERSHIP_SHA256 "$SM_TRANSACTION_FILE")"
  SM_ORIGINALS_SHA256="$(sm_get ORIGINALS_SHA256 "$SM_TRANSACTION_FILE")"
  SM_SECURE_DIR_SHA256="$(sm_get SECURE_DIR_SHA256 "$SM_TRANSACTION_FILE")"
  SM_ADAPTER_ID="$(sm_decode "$(sm_get ADAPTER_ID_B64 "$SM_TRANSACTION_FILE")")"
  SM_TARGET_ABIS="$(sm_decode "$(sm_get TARGET_ABIS_B64 "$SM_TRANSACTION_FILE")")"
  SM_SNAPSHOT_ID="$(sm_decode "$(sm_get SNAPSHOT_ID_B64 "$SM_TRANSACTION_FILE")")"
  SM_BACKUP_LOCATION="$(sm_decode "$(sm_get BACKUP_LOCATION_B64 "$SM_TRANSACTION_FILE")")"
  SM_RESTORE_COMMAND="$(sm_decode "$(sm_get RESTORE_COMMAND_B64 "$SM_TRANSACTION_FILE")")"
  SM_INIT_PATH="$(sm_get INIT_PATH "$SM_TRANSACTION_FILE")"
  sm_configure_rescue_paths || return 1
  SM_POLICY_PATH="$(sm_get POLICY_PATH "$SM_TRANSACTION_FILE")"
  SM_POLICY_SOURCE="$(sm_get POLICY_SOURCE "$SM_TRANSACTION_FILE")"
  SM_RUNTIME_PATH="$(sm_get RUNTIME_PATH "$SM_TRANSACTION_FILE")"
  SM_ACTIVE_PAYLOAD="$(sm_get ACTIVE_PAYLOAD "$SM_TRANSACTION_FILE")"
  SM_RESCUE_PAYLOAD="$(sm_get RESCUE_PAYLOAD "$SM_TRANSACTION_FILE")"
  SM_SELINUX_STRATEGY="$(sm_get SELINUX_STRATEGY "$SM_TRANSACTION_FILE")"
  SM_PREINIT_DEVICE="$(sm_decode "$(sm_get PREINIT_DEVICE_B64 "$SM_TRANSACTION_FILE")")"
  SM_PREINIT_DIR="$(sm_decode "$(sm_get PREINIT_DIR_B64 "$SM_TRANSACTION_FILE")")"
  SM_POLICY_MUTATED="$(sm_get POLICY_MUTATED "$SM_TRANSACTION_FILE")"
  SM_LEGACY_MIGRATION="$(sm_get LEGACY_MIGRATION "$SM_TRANSACTION_FILE")"
  SM_SOURCE_COMMIT="$(sm_get SOURCE_COMMIT "$SM_TRANSACTION_FILE")"
  SM_UPSTREAM_BASE="$(sm_get UPSTREAM_BASE "$SM_TRANSACTION_FILE")"
  SM_ARTIFACT_SHA256="$(sm_get ARTIFACT_SHA256 "$SM_TRANSACTION_FILE")"
  SM_VERSION_CODE="$(sm_get VERSION_CODE "$SM_TRANSACTION_FILE")"
  SM_PRODUCT_VERSION="$(sm_decode "$(sm_get PRODUCT_VERSION_B64 "$SM_TRANSACTION_FILE")")"
  SM_COMMIT_BOOT_ID="$(sm_get COMMIT_BOOT_ID "$SM_TRANSACTION_FILE")"
  SM_BOOT_ATTEMPT_ID="$(sm_get BOOT_ATTEMPT_ID "$SM_TRANSACTION_FILE")"
  SM_ROLLBACK_DIR="$(sm_get ROLLBACK_DIR "$SM_TRANSACTION_FILE")"
  SM_STAGING_PATH="$(sm_get STAGING_PATH "$SM_TRANSACTION_FILE")"
  migration_marker="$(sm_get MIGRATED_FROM_PR5B "$SM_TRANSACTION_FILE")"
  SM_PR5B_RECEIPT=false
  SM_PR5B_MIGRATED=false
  case "$migration_marker" in
    '')
      if [ -z "$SM_MANIFEST_SHA256" ] && [ -z "$SM_OWNERSHIP_SHA256" ] &&
         [ -z "$SM_SERIAL_SHA256" ] && [ -z "$SM_AUTHORIZATION_ID" ] &&
         [ -z "$SM_TARGET_CONTRACT_SHA256" ] && [ -z "$SM_AUTH_BOOT_ID_SHA256" ] &&
         [ -z "$SM_PROBE_SHA256" ] && [ -z "$SM_QUALIFICATION_SHA256" ] &&
         [ -z "$SM_INSTANCE_IDENTITY_SHA256" ] && [ -z "$SM_ACTIVE_PAYLOAD" ] &&
         [ -z "$SM_RESCUE_PAYLOAD" ] && [ -z "$SM_POLICY_MUTATED" ] &&
         [ -z "$SM_LEGACY_MIGRATION" ] && [ -z "$SM_VERSION_CODE" ]; then
        SM_PR5B_RECEIPT=true
      elif [ -z "$SM_MANIFEST_SHA256" ] || [ -z "$SM_OWNERSHIP_SHA256" ]; then
        sm_log "! System Mode receipt is missing one of its integrity digests"
        return 1
      fi
      ;;
    true)
      [ -n "$SM_MANIFEST_SHA256" ] && [ -n "$SM_OWNERSHIP_SHA256" ] &&
        [ -n "$SM_ORIGINALS_SHA256" ] && [ -n "$SM_SECURE_DIR_SHA256" ] &&
        [ -z "$SM_SERIAL_SHA256" ] && [ -z "$SM_AUTHORIZATION_ID" ] &&
        [ -z "$SM_TARGET_CONTRACT_SHA256" ] && [ -z "$SM_AUTH_BOOT_ID_SHA256" ] &&
        [ -z "$SM_PROBE_SHA256" ] && [ -z "$SM_QUALIFICATION_SHA256" ] &&
        [ -z "$SM_INSTANCE_IDENTITY_SHA256" ] && [ -z "$SM_ACTIVE_PAYLOAD" ] &&
        [ -z "$SM_RESCUE_PAYLOAD" ] && [ -z "$SM_POLICY_MUTATED" ] &&
        [ -z "$SM_LEGACY_MIGRATION" ] && [ -z "$SM_VERSION_CODE" ] || return 1
      SM_PR5B_MIGRATED=true
      ;;
    *) return 1 ;;
  esac
  zero="$(printf '%064d' 0)"
  # PR5B receipts predate versioned payloads and the live-policy-only flag.
  # They remain valid upgrade inputs; every new transaction writes all fields.
  [ -n "$SM_ACTIVE_PAYLOAD" ] || SM_ACTIVE_PAYLOAD="$SM_SYSTEM_DIR"
  [ -n "$SM_RESCUE_PAYLOAD" ] || SM_RESCUE_PAYLOAD="$SM_RESCUE_PAYLOAD_PREFIX/$SM_TRANSACTION_ID"
  [ -n "$SM_SERIAL_SHA256" ] || SM_SERIAL_SHA256="$zero"
  # Pre-PR7 development receipts did not bind ownership.tsv independently.
  # Load their shape so interrupted development transactions can recover, but
  # exact upgrade/uninstall validation rejects this zero sentinel. Every PR7
  # commit writes and validates a nonzero ownership digest.
  [ -n "$SM_MANIFEST_SHA256" ] || SM_MANIFEST_SHA256="$zero"
  [ -n "$SM_OWNERSHIP_SHA256" ] || SM_OWNERSHIP_SHA256="$zero"
  [ -n "$SM_ORIGINALS_SHA256" ] || SM_ORIGINALS_SHA256="$zero"
  [ -n "$SM_SECURE_DIR_SHA256" ] || SM_SECURE_DIR_SHA256="$zero"
  [ -n "$SM_POLICY_MUTATED" ] || {
    if [ -n "$SM_POLICY_PATH" ]; then SM_POLICY_MUTATED=true; else SM_POLICY_MUTATED=false; fi
  }
  [ -n "$SM_LEGACY_MIGRATION" ] || SM_LEGACY_MIGRATION=false
  [ -n "$SM_VERSION_CODE" ] || SM_VERSION_CODE=0
  [ -n "$SM_AUTHORIZATION_ID" ] || SM_AUTHORIZATION_ID="$SM_INSTALL_ID"
  [ -n "$SM_TARGET_CONTRACT_SHA256" ] || SM_TARGET_CONTRACT_SHA256="$SM_REPORT_SHA256"
  [ -n "$SM_AUTH_BOOT_ID_SHA256" ] || SM_AUTH_BOOT_ID_SHA256="$SM_FINGERPRINT_SHA256"
  [ -n "$SM_PROBE_SHA256" ] || SM_PROBE_SHA256="$SM_REPORT_SHA256"
  [ -n "$SM_QUALIFICATION_SHA256" ] || SM_QUALIFICATION_SHA256="$zero"
  [ -n "$SM_INSTANCE_IDENTITY_SHA256" ] || SM_INSTANCE_IDENTITY_SHA256="$zero"
  if [ "$SM_PR5B_RECEIPT" = true ] || [ "$SM_PR5B_MIGRATED" = true ]; then
    [ "$SM_STATE" = BOOT_VERIFIED ] || {
      sm_log "! Only a boot-verified PR5B receipt can be migrated safely"
      return 1
    }
  fi
  sm_valid_uuid "$SM_INSTALL_ID" && sm_valid_uuid "$SM_TRANSACTION_ID" || return 1
  sm_valid_uuid "$SM_AUTHORIZATION_ID" || return 1
  case "$SM_STATE" in UNINSTALLED|PREFLIGHTED|STAGED|COMMITTED|BOOT_VERIFIED|ROLLBACK_REQUIRED|ROLLING_BACK|FAILED) ;; *) return 1 ;; esac
  case "$SM_PRIOR_STATE" in UNINSTALLED|BOOT_VERIFIED) ;; *) return 1 ;; esac
  case "$SM_INIT_PATH" in /system/etc/init/magisk.rc|/system/etc/init/hw/magisk.rc) ;; *) return 1 ;; esac
  case "$SM_RUNTIME_PATH" in /sbin|/debug_ramdisk) ;; *) return 1 ;; esac
  case "$SM_ACTIVE_PAYLOAD" in
    "$SM_SYSTEM_DIR") ;;
    "$SM_SYSTEM_PAYLOAD_PREFIX"/*)
      sm_valid_uuid "${SM_ACTIVE_PAYLOAD##*/}" || return 1
      ;;
    *) return 1 ;;
  esac
  case "$SM_RESCUE_PAYLOAD" in
    "$SM_RESCUE_PAYLOAD_PREFIX"/*) sm_valid_uuid "${SM_RESCUE_PAYLOAD##*/}" || return 1 ;;
    *) return 1 ;;
  esac
  case "$SM_POLICY_PATH" in ""|/sepolicy|/sepolicy_debug|/vendor/etc/selinux/precompiled_sepolicy|/odm/etc/selinux/precompiled_sepolicy|/system/etc/selinux/precompiled_sepolicy|/system_root/sepolicy|/system_root/sepolicy_debug|/system_root/sepolicy.unlocked) ;; *) return 1 ;; esac
  case "$SM_POLICY_SOURCE" in ""|/sepolicy|/sepolicy_debug|/vendor/etc/selinux/precompiled_sepolicy|/odm/etc/selinux/precompiled_sepolicy|/system/etc/selinux/precompiled_sepolicy|/system_root/sepolicy|/system_root/sepolicy_debug|/system_root/sepolicy.unlocked) ;; *) return 1 ;; esac
  [ "$SM_ROLLBACK_DIR" = "$SM_STATE_DIR/rollback/$SM_TRANSACTION_ID" ] || return 1
  [ "$SM_STAGING_PATH" = "$(sm_parent "$SM_SYSTEM_DIR")/.magisk.kitsune-stage-$SM_TRANSACTION_ID" ] || return 1
  sm_valid_hex "$SM_SERIAL_SHA256" 64 && sm_valid_hex "$SM_FINGERPRINT_SHA256" 64 &&
    sm_valid_hex "$SM_REPORT_SHA256" 64 &&
    sm_valid_hex "$SM_TARGET_CONTRACT_SHA256" 64 && sm_valid_hex "$SM_AUTH_BOOT_ID_SHA256" 64 &&
    sm_valid_hex "$SM_PROBE_SHA256" 64 && sm_valid_hex "$SM_QUALIFICATION_SHA256" 64 &&
    sm_valid_hex "$SM_INSTANCE_IDENTITY_SHA256" 64 &&
    sm_valid_hex "$SM_BACKUP_SHA256" 64 && sm_valid_hex "$SM_MANIFEST_SHA256" 64 &&
    sm_valid_hex "$SM_OWNERSHIP_SHA256" 64 &&
    sm_valid_hex "$SM_ORIGINALS_SHA256" 64 &&
    sm_valid_hex "$SM_SECURE_DIR_SHA256" 64 &&
    sm_valid_hex "$SM_SOURCE_COMMIT" 40 &&
    sm_valid_hex "$SM_UPSTREAM_BASE" 40 && sm_valid_hex "$SM_ARTIFACT_SHA256" 64 || return 1
  case "$SM_TARGET_API" in *[!0-9]*|'') return 1 ;; esac
  case "$SM_ADAPTER_ID" in *[!A-Za-z0-9._-]*|'') return 1 ;; esac
  case "$SM_TARGET_ABIS" in *[!A-Za-z0-9,._-]*|''|,*|*,|*,,*) return 1 ;; esac
  case "$SM_SELINUX_STRATEGY" in precompiled|monolithic|split|disabled|live+precompiled|live+monolithic|live+split|live+disabled) ;; *) return 1 ;; esac
  case "$SM_PREINIT_DEVICE" in '') ;; *[!A-Za-z0-9._-]*) return 1 ;; esac
  case "$SM_PREINIT_DIR" in ''|/data/adb) ;; *) return 1 ;; esac
  if [ -n "$SM_PREINIT_DEVICE" ]; then [ "$SM_PREINIT_DIR" = /data/adb ] || return 1; else [ -z "$SM_PREINIT_DIR" ] || return 1; fi
  case "$SM_POLICY_MUTATED" in true|false) ;; *) return 1 ;; esac
  case "$SM_LEGACY_MIGRATION" in true|false) ;; *) return 1 ;; esac
  case "$SM_VERSION_CODE" in *[!0-9]*|'') return 1 ;; esac
  sm_valid_single_line "$SM_SNAPSHOT_ID" && sm_valid_single_line "$SM_BACKUP_LOCATION" &&
    sm_valid_single_line "$SM_RESTORE_COMMAND" && sm_valid_single_line "$SM_PRODUCT_VERSION" || return 1
  [ -z "$SM_COMMIT_BOOT_ID" ] || sm_valid_uuid "$SM_COMMIT_BOOT_ID" || return 1
  [ -z "$SM_BOOT_ATTEMPT_ID" ] || sm_valid_uuid "$SM_BOOT_ATTEMPT_ID" || return 1
  case "$SM_STATE" in
    COMMITTED|BOOT_VERIFIED|UNINSTALLED)
      [ "$SM_MANIFEST_SHA256" != "$zero" ] || [ "$SM_PR5B_RECEIPT" = true ] || return 1
      ;;
  esac
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
    magisk_db) printf '%s\n' /data/adb/magisk.db ;;
    magisk_db_wal) printf '%s\n' /data/adb/magisk.db-wal ;;
    magisk_db_shm) printf '%s\n' /data/adb/magisk.db-shm ;;
    modules) printf '%s\n' /data/adb/modules ;;
    modules_update) printf '%s\n' /data/adb/modules_update ;;
    post_fs_data) printf '%s\n' /data/adb/post-fs-data.d ;;
    service) printf '%s\n' /data/adb/service.d ;;
    preinit_rule) printf '%s\n' /data/adb/sepolicy.rule ;;
    magisk_log) printf '%s\n' /cache/magisk.log ;;
    magisk_log_bak) printf '%s\n' /cache/magisk.log.bak ;;
    addon_script) printf '%s\n' /system/addon.d/99-magisk.sh ;;
    addon_dir) printf '%s\n' /system/addon.d/magisk ;;
    rescue_rc) sm_configure_rescue_paths && printf '%s\n' "$SM_RESCUE_RC" ;;
    rescue_dir) sm_configure_rescue_paths && printf '%s\n' "$SM_RESCUE_DIR" ;;
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
    secure_dir) printf '%s\n' "$SM_SECURE_DIR_METADATA" ;;
    original_dir) printf '%s\n' "$SM_STATE_DIR/original" ;;
    *) return 1 ;;
  esac
}

sm_snapshot_state_metadata() {
  local label source destination source_digest snapshot_digest live_digest
  "$SM_BB" mkdir -p "$SM_ROLLBACK_DIR/state" || return 1
  for label in transaction manifest_copy ownership originals journal boot_proof secure_dir original_dir; do
    source="$(sm_state_path "$label")" || return 1
    destination="$SM_ROLLBACK_DIR/state/$label"
    "$SM_BB" mkdir -p "$destination" || return 1
    if sm_path_present "$source"; then
      sm_managed_path_safe "State snapshot source" "$source" "$source" true || return 1
      source_digest="$(sm_digest_path "$source")" || return 1
      "$SM_BB" cp -a "$source" "$destination/data" || return 1
      sm_fsync_tree "$destination/data" || return 1
      snapshot_digest="$(sm_digest_path "$destination/data")" || return 1
      sm_managed_path_safe "State snapshot source" "$source" "$source" true || return 1
      live_digest="$(sm_digest_path "$source")" || return 1
      [ "$source_digest" = "$snapshot_digest" ] && [ "$source_digest" = "$live_digest" ] || return 1
      printf '%s\n' "$source_digest" >"$destination/digest" || return 1
      printf 'present\n' >"$destination/present" || return 1
    else
      printf 'absent\n' >"$destination/absent" || return 1
      ! sm_path_present "$source" || return 1
    fi
    sm_fsync_tree "$destination" || return 1
    sm_fsync "$SM_ROLLBACK_DIR/state" || return 1
    sm_failpoint "snapshot-state:$label" || return 1
  done
  sm_fsync "$SM_ROLLBACK_DIR" "$SM_STATE_DIR/rollback" "$SM_STATE_DIR" || return 1
}

sm_restore_state_metadata() {
  local label destination source expected actual failed=0
  # Keep the current ROLLING_BACK receipt authoritative until a separately
  # durable terminal marker can publish the prior receipt after rollback
  # storage is gone. Replacing transaction.env from inside its own rollback
  # tree would create an unrecoverable death window.
  for label in original_dir secure_dir boot_proof journal originals ownership manifest_copy; do
    destination="$(sm_state_path "$label")" || return 1
    source="$SM_ROLLBACK_DIR/state/$label"
    [ -d "$source" ] || { failed=1; break; }
    if [ -f "$source/present" ] && [ ! -L "$source/present" ]; then
      if sm_path_present "$source/absent" ||
         [ ! -f "$source/digest" ] || [ -L "$source/digest" ] ||
         ! sm_path_present "$source/data"; then
        failed=1
        break
      fi
      expected="$($SM_BB cat "$source/digest")" || { failed=1; break; }
      sm_valid_hex "$expected" 64 || { failed=1; break; }
      sm_managed_path_safe "State rollback snapshot" "$destination" "$source/data" true || {
        failed=1
        break
      }
      actual="$(sm_digest_path "$source/data")" || { failed=1; break; }
      [ "$actual" = "$expected" ] || { failed=1; break; }
    elif [ -f "$source/absent" ] && [ ! -L "$source/absent" ]; then
      if sm_path_present "$source/present" || sm_path_present "$source/digest" ||
         sm_path_present "$source/data"; then
        failed=1
        break
      fi
    else
      failed=1
      break
    fi
    sm_managed_path_safe "State restore target" "$destination" "$destination" true || {
      failed=1
      break
    }
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

sm_marker_file_safe() {
  local path="$1" uid mode
  [ -f "$path" ] && [ ! -L "$path" ] || return 1
  uid="$($SM_BB stat -c %u "$path")" || return 1
  mode="$($SM_BB stat -c %a "$path")" || return 1
  [ "$uid" = 0 ] && sm_reject_unsafe_mode "$mode"
}

sm_remove_marker_stage() {
  local staged="$1"
  sm_path_present "$staged" || return 0
  sm_marker_file_safe "$staged" || return 1
  "$SM_BB" rm -f "$staged" || return 1
  sm_fsync "$(sm_parent "$staged")"
}

sm_publish_setup_marker() {
  local staged="$SM_SETUP_MARKER.new"
  ! sm_path_present "$SM_SETUP_MARKER" && ! sm_path_present "$SM_ROLLBACK_TERMINAL" || return 1
  sm_remove_marker_stage "$staged" || return 1
  {
    printf 'SCHEMA_VERSION=1\n'
    printf 'TRANSACTION_ID=%s\n' "$SM_TRANSACTION_ID"
    printf 'ROLLBACK_DIR=%s\n' "$SM_ROLLBACK_DIR"
  } >"$staged" || return 1
  "$SM_BB" chmod 0600 "$staged" || return 1
  sm_atomic_publish "$staged" "$SM_SETUP_MARKER" setup-marker-published
}

sm_recover_setup_marker() {
  local transaction rollback current fresh=false
  sm_remove_marker_stage "$SM_SETUP_MARKER.new" || return 1
  sm_path_present "$SM_SETUP_MARKER" || return 0
  sm_marker_file_safe "$SM_SETUP_MARKER" || return 1
  [ "$(sm_get SCHEMA_VERSION "$SM_SETUP_MARKER")" = 1 ] || return 1
  transaction="$(sm_get TRANSACTION_ID "$SM_SETUP_MARKER")"
  rollback="$(sm_get ROLLBACK_DIR "$SM_SETUP_MARKER")"
  sm_valid_uuid "$transaction" || return 1
  [ "$rollback" = "$SM_STATE_DIR/rollback/$transaction" ] || return 1
  current="$(sm_get TRANSACTION_ID "$SM_TRANSACTION_FILE" 2>/dev/null)" || current=
  if [ "$current" = "$transaction" ]; then
    # PREFLIGHTED publication won the race; normal receipt recovery owns the
    # complete rollback snapshot from this point onward.
    "$SM_BB" rm -f "$SM_SETUP_MARKER" || return 1
    sm_fsync /data/adb || return 1
    return 0
  fi
  [ ! -f "$SM_TRANSACTION_FILE" ] || fresh=false
  [ -f "$SM_TRANSACTION_FILE" ] || fresh=true
  sm_remove_tree_safe "Setup rollback cleanup target" "$rollback" "$rollback" || return 1
  sm_failpoint setup-rollback-removed || return 1
  "$SM_BB" rmdir "$SM_STATE_DIR/rollback" 2>/dev/null || true
  if [ "$fresh" = true ]; then
    "$SM_BB" rmdir "$SM_STATE_DIR" 2>/dev/null || true
    "$SM_BB" rmdir /data/adb/kitsune 2>/dev/null || true
  fi
  "$SM_BB" rm -f "$SM_SETUP_MARKER" || return 1
  sm_fsync /data/adb || return 1
  sm_failpoint setup-marker-cleaned || return 1
  return 0
}

sm_publish_rollback_terminal() {
  local transaction_snapshot="$SM_ROLLBACK_DIR/state/transaction" staged="$SM_ROLLBACK_TERMINAL.new"
  local prior=false prior_install='' prior_transaction='' prior_sha256=''
  ! sm_path_present "$SM_ROLLBACK_TERMINAL" || return 0
  sm_remove_marker_stage "$SM_ROLLBACK_TERMINAL.new" || return 1
  sm_remove_marker_stage "$SM_PRIOR_TRANSACTION.new" || return 1
  [ -d "$transaction_snapshot" ] && [ ! -L "$transaction_snapshot" ] || return 1
  if [ -f "$transaction_snapshot/present" ]; then
    [ ! -f "$transaction_snapshot/absent" ] && [ -f "$transaction_snapshot/data" ] || return 1
    "$SM_BB" cp -a "$transaction_snapshot/data" "$SM_PRIOR_TRANSACTION.new" || return 1
    "$SM_BB" chmod 0600 "$SM_PRIOR_TRANSACTION.new" || return 1
    sm_atomic_publish "$SM_PRIOR_TRANSACTION.new" "$SM_PRIOR_TRANSACTION" rollback-prior-published || return 1
    prior_install="$(sm_get INSTALL_ID "$SM_PRIOR_TRANSACTION")"
    prior_transaction="$(sm_get TRANSACTION_ID "$SM_PRIOR_TRANSACTION")"
    sm_valid_uuid "$prior_install" && sm_valid_uuid "$prior_transaction" || return 1
    [ "$(sm_get SCHEMA_VERSION "$SM_PRIOR_TRANSACTION")" = "$SM_SCHEMA_VERSION" ] || return 1
    case "$(sm_get STATE "$SM_PRIOR_TRANSACTION")" in BOOT_VERIFIED|UNINSTALLED) ;; *) return 1 ;; esac
    prior_sha256="$(sm_sha256_file "$SM_PRIOR_TRANSACTION")" || return 1
    prior=true
  elif [ -f "$transaction_snapshot/absent" ]; then
    [ ! -f "$transaction_snapshot/present" ] && ! sm_path_present "$SM_PRIOR_TRANSACTION" || return 1
  else
    return 1
  fi
  {
    printf 'SCHEMA_VERSION=1\n'
    printf 'TRANSACTION_ID=%s\n' "$SM_TRANSACTION_ID"
    printf 'ROLLBACK_DIR=%s\n' "$SM_ROLLBACK_DIR"
    printf 'PRIOR_PRESENT=%s\n' "$prior"
    printf 'PRIOR_INSTALL_ID=%s\n' "$prior_install"
    printf 'PRIOR_TRANSACTION_ID=%s\n' "$prior_transaction"
    printf 'PRIOR_SHA256=%s\n' "$prior_sha256"
  } >"$staged" || return 1
  "$SM_BB" chmod 0600 "$staged" || return 1
  sm_atomic_publish "$staged" "$SM_ROLLBACK_TERMINAL" rollback-terminal-published
}

sm_complete_rollback_terminal() {
  local transaction rollback prior prior_install prior_transaction prior_sha256 canonical_transaction
  sm_remove_marker_stage "$SM_ROLLBACK_TERMINAL.new" || return 1
  sm_remove_marker_stage "$SM_PRIOR_TRANSACTION.new" || return 1
  sm_path_present "$SM_ROLLBACK_TERMINAL" || return 0
  sm_marker_file_safe "$SM_ROLLBACK_TERMINAL" || return 1
  [ "$(sm_get SCHEMA_VERSION "$SM_ROLLBACK_TERMINAL")" = 1 ] || return 1
  transaction="$(sm_get TRANSACTION_ID "$SM_ROLLBACK_TERMINAL")"
  rollback="$(sm_get ROLLBACK_DIR "$SM_ROLLBACK_TERMINAL")"
  prior="$(sm_get PRIOR_PRESENT "$SM_ROLLBACK_TERMINAL")"
  prior_install="$(sm_get PRIOR_INSTALL_ID "$SM_ROLLBACK_TERMINAL")"
  prior_transaction="$(sm_get PRIOR_TRANSACTION_ID "$SM_ROLLBACK_TERMINAL")"
  prior_sha256="$(sm_get PRIOR_SHA256 "$SM_ROLLBACK_TERMINAL")"
  sm_valid_uuid "$transaction" || return 1
  [ "$rollback" = "$SM_STATE_DIR/rollback/$transaction" ] || return 1
  case "$prior" in true|false) ;; *) return 1 ;; esac
  if [ "$prior" = true ]; then
    sm_valid_uuid "$prior_install" && sm_valid_uuid "$prior_transaction" || return 1
    sm_valid_hex "$prior_sha256" 64 || return 1
    sm_marker_file_safe "$SM_PRIOR_TRANSACTION" || return 1
    [ "$(sm_sha256_file "$SM_PRIOR_TRANSACTION")" = "$prior_sha256" ] || return 1
    [ "$(sm_get SCHEMA_VERSION "$SM_PRIOR_TRANSACTION")" = "$SM_SCHEMA_VERSION" ] || return 1
    [ "$(sm_get INSTALL_ID "$SM_PRIOR_TRANSACTION")" = "$prior_install" ] &&
      [ "$(sm_get TRANSACTION_ID "$SM_PRIOR_TRANSACTION")" = "$prior_transaction" ] || return 1
    case "$(sm_get STATE "$SM_PRIOR_TRANSACTION")" in BOOT_VERIFIED|UNINSTALLED) ;; *) return 1 ;; esac
  else
    [ -z "$prior_install$prior_transaction$prior_sha256" ] &&
      ! sm_path_present "$SM_PRIOR_TRANSACTION" || return 1
  fi

  canonical_transaction="$(sm_get TRANSACTION_ID "$SM_TRANSACTION_FILE" 2>/dev/null)" || canonical_transaction=
  if sm_path_present "$rollback"; then
    if [ "$prior" = true ]; then
      case "$canonical_transaction" in "$transaction"|"$prior_transaction") ;; *) return 1 ;; esac
    else
      [ "$canonical_transaction" = "$transaction" ] || return 1
    fi
  fi

  if sm_path_present "$rollback"; then
    [ -d "$rollback" ] && [ ! -L "$rollback" ] || return 1
    sm_remove_tree_safe "Terminal rollback cleanup target" "$rollback" "$rollback" || return 1
    sm_fsync "$SM_STATE_DIR/rollback" "$SM_STATE_DIR" || return 1
    sm_failpoint rollback-storage-removed || return 1
  fi

  if [ "$prior" = true ]; then
    if [ "$canonical_transaction" != "$prior_transaction" ]; then
      [ "$canonical_transaction" = "$transaction" ] && sm_marker_file_safe "$SM_PRIOR_TRANSACTION" || return 1
      "$SM_BB" cp -a "$SM_PRIOR_TRANSACTION" "$SM_STATE_DIR/.transaction.env.new" || return 1
      "$SM_BB" chmod 0600 "$SM_STATE_DIR/.transaction.env.new" || return 1
      sm_atomic_publish "$SM_STATE_DIR/.transaction.env.new" "$SM_TRANSACTION_FILE" rollback-prior-restored || return 1
    fi
  else
    case "$canonical_transaction" in ''|"$transaction") ;; *) return 1 ;; esac
    "$SM_BB" rm -f "$SM_TRANSACTION_FILE" || return 1
    [ ! -d "$SM_STATE_DIR" ] || sm_fsync "$SM_STATE_DIR" || return 1
    sm_failpoint rollback-fresh-receipt-removed || return 1
    [ ! -d "$SM_STATE_DIR/rollback" ] || "$SM_BB" rmdir "$SM_STATE_DIR/rollback" 2>/dev/null || return 1
    [ ! -d "$SM_STATE_DIR" ] || "$SM_BB" rmdir "$SM_STATE_DIR" 2>/dev/null || return 1
    "$SM_BB" rmdir /data/adb/kitsune 2>/dev/null || true
  fi

  "$SM_BB" rm -f "$SM_PRIOR_TRANSACTION" "$SM_ROLLBACK_TERMINAL" || return 1
  sm_fsync /data/adb || return 1
  sm_failpoint rollback-terminal-cleaned
}

sm_finish_rollback_storage() {
  sm_publish_rollback_terminal || return 1
  sm_complete_rollback_terminal
}

sm_cleanup_staging() {
  local path real failed=0 policy_stage=''
  [ -z "${SM_POLICY_PATH:-}" ] || policy_stage="$SM_POLICY_PATH.kitsune-original-new"
  for path in \
    "$SM_STAGING_PATH" \
    "$SM_INIT_PATH.kitsune-new" \
    "$(sm_rescue_stage_path)" \
    "$SM_RESCUE_RC.kitsune-new" \
    /system/etc/init/bootanim.rc.kitsune-stock-new \
    /system/addon.d/.99-magisk.sh.kitsune-new \
    "$policy_stage" \
    "$SM_SYSTEM_DIR/.install-manifest.json.new" \
    "$SM_SYSTEM_DIR/.install-manifest.state-new"; do
    [ -n "$path" ] || continue
    real="$(sm_real_path "$path")" || { failed=1; continue; }
    sm_remove_tree_safe "Staging cleanup target" "$path" "$real" || failed=1
    sm_remove_tree_safe "Short-write cleanup target" "$path.kitsune-short" \
      "$real.kitsune-short" || failed=1
    sm_fsync_existing_parent "$real" 2>/dev/null || failed=1
  done
  for path in "$(sm_runtime_stage_path)" "$(sm_runtime_old_path)"; do
    sm_remove_tree_safe "Runtime staging cleanup target" "$path" "$path" || failed=1
    sm_remove_tree_safe "Runtime short-write cleanup target" "$path.kitsune-short" \
      "$path.kitsune-short" || failed=1
    sm_fsync_existing_parent "$path" 2>/dev/null || failed=1
  done
  for path in \
    "$SM_STATE_DIR/.transaction.env.new" \
    "$SM_STATE_DIR/.install-manifest.json.new" \
    "$SM_STATE_DIR/.install-manifest.copy.new" \
    "$SM_STATE_DIR/.install-manifest.state-new" \
    "$SM_STATE_DIR/.boot-verified.env.new" \
    "$SM_STATE_DIR/.secure-dir.env.new" \
    "$SM_STATE_DIR/.policy.original" \
    "$SM_STATE_DIR/.bootanim.original" \
    "$SM_STATE_DIR/.pr5b-transaction.env.new" \
    "$SM_ORIGINAL_FILE.new" "$SM_OWNERSHIP_FILE.new" "$SM_JOURNAL_FILE.new" \
    "$SM_STATE_DIR/.journal-removals.new"; do
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
  sm_finish_rollback_storage || return 1
  return 0
}

sm_snapshot_one() {
  local label="$1" canonical real destination source_digest snapshot_digest live_digest
  canonical="$(sm_label_path "$label")" || return 1
  [ -n "$canonical" ] || return 0
  real="$(sm_real_path "$canonical")" || return 1
  destination="$SM_ROLLBACK_DIR/$label"
  "$SM_BB" mkdir -p "$destination" || return 1
  if sm_path_present "$real"; then
    sm_snapshot_source_safe "$label" "$real" || return 1
    source_digest="$(sm_digest_path "$real")" || return 1
    "$SM_BB" cp -a "$real" "$destination/data" || return 1
    sm_fsync_tree "$destination/data" || return 1
    snapshot_digest="$(sm_digest_path "$destination/data")" || return 1
    sm_snapshot_source_safe "$label" "$real" || return 1
    live_digest="$(sm_digest_path "$real")" || return 1
    [ "$source_digest" = "$snapshot_digest" ] && [ "$source_digest" = "$live_digest" ] || {
      sm_log "! Source changed while snapshotting $canonical"
      return 1
    }
    printf '%s\n' "$source_digest" >"$destination/digest" || return 1
    printf 'present\n' >"$destination/present" || return 1
  else
    printf 'absent\n' >"$destination/absent" || return 1
    ! sm_path_present "$real" || {
      sm_log "! Source appeared while snapshotting $canonical"
      return 1
    }
  fi
  sm_fsync_tree "$destination" || return 1
  sm_fsync "$SM_ROLLBACK_DIR" || return 1
  sm_failpoint "snapshot:$label"
}

sm_validate_snapshot_entry() {
  local label="$1" source="$SM_ROLLBACK_DIR/$1" expected actual
  [ -d "$source" ] && [ ! -L "$source" ] || return 1
  if [ -f "$source/present" ] && [ ! -L "$source/present" ]; then
    [ ! -f "$source/absent" ] && [ ! -L "$source/absent" ] &&
      [ -f "$source/digest" ] && [ ! -L "$source/digest" ] &&
      sm_path_present "$source/data" || return 1
    expected="$($SM_BB cat "$source/digest")" || return 1
    sm_valid_hex "$expected" 64 || return 1
    sm_snapshot_source_safe "$label" "$source/data" || return 1
    actual="$(sm_digest_path "$source/data")" || return 1
    [ "$actual" = "$expected" ]
  elif [ -f "$source/absent" ] && [ ! -L "$source/absent" ]; then
    ! sm_path_present "$source/present" && ! sm_path_present "$source/digest" &&
      ! sm_path_present "$source/data"
  else
    return 1
  fi
}

sm_verify_snapshot_sources() {
  local label canonical real source expected actual
  for label in payload legacy_rc init_rc policy policy_gz bootanim bootanim_gz \
    runtime magisk_db magisk_db_wal magisk_db_shm modules modules_update post_fs_data service \
    preinit_rule magisk_log magisk_log_bak addon_script addon_dir rescue_dir rescue_rc; do
    if [ -z "$SM_POLICY_PATH" ] || [ "$SM_POLICY_MUTATED" != true ]; then
      case "$label" in policy|policy_gz) continue ;; esac
    fi
    [ "$label" != legacy_rc ] || [ "$SM_SYSTEM_DIR.rc" != "$SM_INIT_PATH" ] || continue
    sm_validate_snapshot_entry "$label" || return 1
    canonical="$(sm_label_path "$label")" || return 1
    real="$(sm_real_path "$canonical")" || return 1
    source="$SM_ROLLBACK_DIR/$label"
    if [ -f "$source/present" ]; then
      sm_snapshot_source_safe "$label" "$real" || return 1
      expected="$($SM_BB cat "$source/digest")" || return 1
      actual="$(sm_digest_path "$real")" || return 1
      [ "$actual" = "$expected" ] || {
        sm_log "! Snapshot source changed after capture: $canonical"
        return 1
      }
    else
      ! sm_path_present "$real" || {
        sm_log "! Snapshot source appeared after capture: $canonical"
        return 1
      }
    fi
  done
  return 0
}

sm_managed_path_safe() {
  local purpose="$1" canonical="$2" path="$3" reject_link="${4:-false}"
  local unexpected mounted mountinfo="${SM_MOUNTINFO_FILE:-/proc/self/mountinfo}"
  sm_path_present "$path" || return 0
  if [ -L "$path" ]; then
    [ "$reject_link" != true ] || {
      sm_log "! $purpose is a symbolic link: $canonical"
      return 1
    }
    return 0
  fi
  [ -f "$path" ] || [ -d "$path" ] || {
    sm_log "! $purpose has an unsupported node type: $canonical"
    return 1
  }
  [ ! -d "$path" ] || {
    unexpected="$({
      cd "$path" || exit 1
      "$SM_BB" find . -mindepth 1 ! -type f ! -type d ! -type l -print
    } | "$SM_BB" head -n 1)" || return 1
    [ -z "$unexpected" ] || {
      sm_log "! $purpose contains a special node: $canonical/${unexpected#./}"
      return 1
    }
  }
  # All managed canonical paths and permitted mirrors are whitespace-free, so
  # their mountinfo representation is byte-identical. Reject an exact bind or
  # any nested mount before cp -a or rm -rf can cross a filesystem boundary.
  [ -r "$mountinfo" ] || {
    sm_log "! Mount topology is unavailable while checking $canonical"
    return 1
  }
  mounted="$($SM_BB awk -v target="$path" '
    $5 == target || index($5, target "/") == 1 { print $5; exit }
  ' "$mountinfo")" || return 1
  [ -z "$mounted" ] || {
    sm_log "! $purpose contains a mounted view: $canonical ($mounted)"
    return 1
  }
  return 0
}

sm_snapshot_source_safe() {
  local label="$1" path="$2" canonical reject_link=false
  canonical="$(sm_label_path "$label")" || return 1
  case "$label" in
    runtime|magisk_db|magisk_db_wal|magisk_db_shm|modules|modules_update|post_fs_data|service|preinit_rule|magisk_log|magisk_log_bak)
      reject_link=true
      ;;
  esac
  sm_managed_path_safe "Snapshot source" "$canonical" "$path" "$reject_link"
}

sm_destructive_target_safe() {
  local label="$1" path="$2" canonical reject_link=false
  canonical="$(sm_label_path "$label")" || return 1
  case "$label" in
    runtime|magisk_db|magisk_db_wal|magisk_db_shm|modules|modules_update|post_fs_data|service|preinit_rule|magisk_log|magisk_log_bak)
      reject_link=true
      ;;
  esac
  sm_managed_path_safe "Restore target" "$canonical" "$path" "$reject_link"
}

sm_remove_tree_safe() {
  local purpose="$1" canonical="$2" path="$3"
  sm_managed_path_safe "$purpose" "$canonical" "$path" true || return 1
  "$SM_BB" rm -rf "$path"
}

sm_magisk_daemon_running() {
  local process comm name proc_root="${SM_PROC_ROOT:-/proc}"
  [ -d "$proc_root" ] && [ ! -L "$proc_root" ] || return 2
  for process in "$proc_root"/[0-9]*; do
    [ -d "$process" ] || continue
    [ ! -L "$process" ] || {
      sm_log "! Process entry ${process##*/} is redirected"
      return 2
    }
    comm="$process/comm"
    if [ ! -f "$comm" ] || [ -L "$comm" ] || [ ! -r "$comm" ]; then
      [ ! -d "$process" ] && continue
      sm_log "! Cannot inspect process ${process##*/} while quiescing Magisk"
      return 2
    fi
    name="$($SM_BB cat "$comm" 2>/dev/null)" || {
      [ ! -d "$process" ] && continue
      sm_log "! Cannot read process ${process##*/} while quiescing Magisk"
      return 2
    }
    [ "$name" != magiskd ] || return 0
  done
  return 1
}

sm_isolate_installer_mounts() {
  local target count=0
  [ "$SM_SLAVE_MOUNT_NAMESPACE" = true ] || return 0
  "$SM_BB" mount --make-rprivate / || return 1
  # Earlier PR7 payloads named the worker tmpfs differently from upstream.
  # Their daemon cannot recognize those module views during --stop. Detach
  # them only in this installer namespace before reading persistent files.
  while :; do
    target="$("$SM_BB" awk '$1 == "magisk-worker" { print $2; exit }' "$SM_MOUNTS_FILE")" || return 1
    [ -n "$target" ] || break
    sm_valid_mountpoint "$target" || return 1
    case "$target" in /|*'\'*) return 1 ;; esac
    count=$((count + 1))
    [ "$count" -le 256 ] || return 1
    "$SM_BB" umount -l "$target" || return 1
  done
  SM_SLAVE_MOUNT_NAMESPACE=false
}

sm_quiesce_magisk() {
  local candidate uid mode stopped=false attempt=0 status
  if sm_magisk_daemon_running; then status=0; else status=$?; fi
  case "$status" in
    0) ;;
    1) sm_isolate_installer_mounts; return $? ;;
    *) sm_log "! Magisk daemon state cannot be proven"; return 1 ;;
  esac
  sm_log "- Quiescing Magisk before the exact mutable-state snapshot"
  for candidate in \
    "${SM_TRUSTED_STOP_CLIENT:-}" \
    "${SM_ACTIVE_PAYLOAD:+$SM_ACTIVE_PAYLOAD/magisk}" \
    "$SM_SYSTEM_DIR/magisk" "$SM_SYSTEM_DIR/magisk64" "$SM_SYSTEM_DIR/magisk32"; do
    [ -n "$candidate" ] || continue
    [ -f "$candidate" ] && [ ! -L "$candidate" ] && [ -x "$candidate" ] || continue
    uid="$($SM_BB stat -c %u "$candidate")" || continue
    mode="$($SM_BB stat -c %a "$candidate")" || continue
    if [ "$uid" != 0 ] || ! sm_reject_unsafe_mode "$mode"; then
      continue
    fi
    if "$candidate" --stop 9>&- >/dev/null 2>&1; then
      stopped=true
      break
    fi
  done
  [ "$stopped" = true ] || {
    sm_log "! No trusted Magisk client could stop the running daemon"
    return 1
  }
  while :; do
    if sm_magisk_daemon_running; then status=0; else status=$?; fi
    case "$status" in
      0)
        attempt=$((attempt + 1))
        [ "$attempt" -lt 10 ] || {
          sm_log "! Magisk daemon remained alive after the stop request"
          return 1
        }
        "$SM_BB" sleep 1
        ;;
      1) break ;;
      *) sm_log "! Magisk daemon state became unreadable after the stop request"; return 1 ;;
    esac
  done
  sm_failpoint daemon-quiesced || return 1
  sm_isolate_installer_mounts
}

sm_assert_magisk_quiesced() {
  local status
  if sm_magisk_daemon_running; then status=0; else status=$?; fi
  case "$status" in
    0) sm_log "! Magisk daemon restarted during the mutable-state snapshot"; return 1 ;;
    1) return 0 ;;
    *) sm_log "! Magisk daemon state cannot be proven after the snapshot"; return 1 ;;
  esac
}

sm_mutable_path() {
  case "$1" in
    /data/adb/magisk|/data/adb/magisk/*|\
    /data/adb/magisk.db|/data/adb/magisk.db-wal|/data/adb/magisk.db-shm|\
    /data/adb/modules|/data/adb/modules/*|\
    /data/adb/modules_update|/data/adb/modules_update/*|\
    /data/adb/post-fs-data.d|/data/adb/post-fs-data.d/*|\
    /data/adb/service.d|/data/adb/service.d/*|\
    /data/adb/sepolicy.rule|/cache/magisk.log|/cache/magisk.log.bak) return 0 ;;
  esac
  return 1
}

sm_process_exited() {
  local process="$1" record
  [ -f "$process/stat" ] && [ ! -L "$process/stat" ] || return 1
  record="$($SM_BB cat "$process/stat" 2>/dev/null)" || return 1
  case "$record" in "${process##*/} ("*") "*) ;; *) return 1 ;; esac
  # The comm field can contain spaces and closing parentheses. Only the state
  # after its final delimiter is kernel evidence that file references are gone.
  record="${record##*) }"
  case "$record" in 'Z '*|'X '*|'x '*) return 0 ;; esac
  return 1
}

sm_assert_mutable_namespaces_idle() {
  local proc_root="${SM_PROC_ROOT:-/proc}" process pid link target fd flags access mapped maps
  [ -d "$proc_root" ] && [ ! -L "$proc_root" ] || return 1
  for process in "$proc_root"/[0-9]*; do
    [ -d "$process" ] || continue
    pid="${process##*/}"
    [ "$pid" != "$$" ] || continue
    [ ! -L "$process" ] || {
      sm_log "! Process entry $pid is redirected"
      return 1
    }
    link="$process/cwd"
    [ -L "$link" ] || {
      if [ ! -d "$process" ] || sm_process_exited "$process"; then continue; fi
      sm_log "! Cannot inspect process $pid working directory"
      return 1
    }
    target="$($SM_BB readlink "$link" 2>/dev/null)" || {
      if [ ! -d "$process" ] || sm_process_exited "$process"; then continue; fi
      sm_log "! Cannot inspect process $pid working directory"
      return 1
    }
    target="${target% (deleted)}"
    if sm_mutable_path "$target"; then
      sm_log "! Process $pid still references mutable Magisk state: $target"
      return 1
    fi
    if [ ! -d "$process/fd" ] || [ -L "$process/fd" ] ||
       [ ! -r "$process/fd" ] || [ ! -x "$process/fd" ]; then
      if [ ! -d "$process" ] || sm_process_exited "$process"; then continue; fi
      sm_log "! Cannot inspect process $pid file descriptors"
      return 1
    fi
    if [ ! -d "$process/fdinfo" ] || [ -L "$process/fdinfo" ] ||
       [ ! -r "$process/fdinfo" ] || [ ! -x "$process/fdinfo" ]; then
      if [ ! -d "$process" ] || sm_process_exited "$process"; then continue; fi
      sm_log "! Cannot inspect process $pid descriptor metadata"
      return 1
    fi
    for link in "$process"/fd/*; do
      if ! sm_path_present "$link"; then continue; fi
      [ -L "$link" ] || {
        [ ! -d "$process" ] && continue
        sm_log "! Process $pid has an uninspectable descriptor"
        return 1
      }
      target="$($SM_BB readlink "$link" 2>/dev/null)" || {
        if [ ! -d "$process" ] || ! sm_path_present "$link"; then continue; fi
        sm_log "! Cannot inspect process $pid descriptor ${link##*/}"
        return 1
      }
      target="${target% (deleted)}"
      sm_mutable_path "$target" || continue
      fd="${link##*/}"
      flags="$($SM_BB awk '$1 == "flags:" { print $2; found=1; exit } END { exit !found }' \
        "$process/fdinfo/$fd" 2>/dev/null)" || {
        if [ ! -d "$process" ] || ! sm_path_present "$link"; then continue; fi
        sm_log "! Cannot inspect process $pid descriptor flags"
        return 1
      }
      case "$flags" in ''|*[!0-7]*) sm_log "! Process $pid has invalid descriptor flags"; return 1 ;; esac
      access="${flags#"${flags%?}"}"
      case "$access" in 1|2|3)
        sm_log "! Process $pid has mutable Magisk state open for writing: $target"
        return 1
        ;;
      esac
    done
    maps="$process/maps"
    if [ ! -f "$maps" ] || [ -L "$maps" ] || [ ! -r "$maps" ]; then
      if [ ! -d "$process" ] || sm_process_exited "$process"; then continue; fi
      sm_log "! Cannot inspect process $pid memory mappings"
      return 1
    fi
    mapped="$($SM_BB awk '
      NF && (NF < 5 || $1 !~ /^[0-9a-f]+-[0-9a-f]+$/ || $2 !~ /^[r-][w-][x-][ps]$/) {
        exit 2
      }
      substr($2, 2, 1) == "w" {
        path=$6
        for (field=7; field<=NF; field++) path=path " " $field
        sub(/ \(deleted\)$/, "", path)
        if (path == "/data/adb/magisk" || index(path, "/data/adb/magisk/") == 1 ||
            path == "/data/adb/magisk.db" || path == "/data/adb/magisk.db-wal" ||
            path == "/data/adb/magisk.db-shm" || path == "/data/adb/modules" ||
            index(path, "/data/adb/modules/") == 1 || path == "/data/adb/modules_update" ||
            index(path, "/data/adb/modules_update/") == 1 || path == "/data/adb/post-fs-data.d" ||
            index(path, "/data/adb/post-fs-data.d/") == 1 || path == "/data/adb/service.d" ||
            index(path, "/data/adb/service.d/") == 1 || path == "/data/adb/sepolicy.rule" ||
            path == "/cache/magisk.log" || path == "/cache/magisk.log.bak") {
          print path
          exit
        }
      }
    ' "$maps" 2>/dev/null)" || {
      if [ ! -d "$process" ] || sm_process_exited "$process"; then continue; fi
      sm_log "! Cannot parse process $pid memory mappings"
      return 1
    }
    [ -z "$mapped" ] || {
      sm_log "! Process $pid has a writable mapping of mutable Magisk state: $mapped"
      return 1
    }
  done
  return 0
}

sm_cleanup_terminal_rollback() {
  case "$SM_STATE" in BOOT_VERIFIED|UNINSTALLED) ;; *) return 1 ;; esac
  [ "$SM_ROLLBACK_DIR" = "$SM_STATE_DIR/rollback/$SM_TRANSACTION_ID" ] || return 1
  if sm_path_present "$SM_ROLLBACK_DIR"; then
    [ -d "$SM_ROLLBACK_DIR" ] && [ ! -L "$SM_ROLLBACK_DIR" ] || return 1
    sm_remove_tree_safe "Terminal transaction cleanup target" \
      "$SM_ROLLBACK_DIR" "$SM_ROLLBACK_DIR" || return 1
  fi
  if [ -d "$SM_STATE_DIR/rollback" ]; then
    sm_fsync "$SM_STATE_DIR/rollback" "$SM_STATE_DIR" || return 1
    "$SM_BB" rmdir "$SM_STATE_DIR/rollback" 2>/dev/null || true
    sm_fsync "$SM_STATE_DIR" || return 1
  fi
  return 0
}

sm_snapshot_all() {
  local label
  "$SM_BB" mkdir -p "$SM_ROLLBACK_DIR" || return 1
  for label in payload legacy_rc init_rc policy policy_gz bootanim bootanim_gz \
    runtime magisk_db magisk_db_wal magisk_db_shm modules modules_update post_fs_data service \
    preinit_rule magisk_log magisk_log_bak addon_script addon_dir rescue_dir rescue_rc; do
    if [ -z "$SM_POLICY_PATH" ] || [ "$SM_POLICY_MUTATED" != true ]; then
      case "$label" in policy|policy_gz) continue ;; esac
    fi
    [ "$label" != legacy_rc ] || [ "$SM_SYSTEM_DIR.rc" != "$SM_INIT_PATH" ] || continue
    sm_snapshot_one "$label" || return 1
  done
  # Two complete sweeps catch workers that mutate an earlier namespace while
  # a later one is being copied. The daemon must remain stopped throughout.
  sm_verify_snapshot_sources || return 1
  "$SM_BB" sleep "${SM_SNAPSHOT_SETTLE_SECONDS:-1}" || return 1
  sm_verify_snapshot_sources || return 1
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

sm_prepare_secure_dir_metadata() {
  local staged="$SM_STATE_DIR/.secure-dir.env.new" uid gid mode context secure
  if [ -f "$SM_SECURE_DIR_METADATA" ]; then
    sm_validate_secure_dir_metadata
    return
  fi
  secure="$(sm_real_path /data/adb)" || return 1
  [ -d "$secure" ] && [ ! -L "$secure" ] || {
    sm_log "! /data/adb is not a safe persistent directory"
    return 1
  }
  uid="$($SM_BB stat -c %u "$secure")" || return 1
  gid="$($SM_BB stat -c %g "$secure")" || return 1
  mode="$($SM_BB stat -c %a "$secure")" || return 1
  case "$uid:$gid:$mode" in *[!0-9:]*) return 1 ;; esac
  sm_reject_unsafe_mode "$mode" || {
    sm_log "! /data/adb has unsafe group/world-write permissions"
    return 1
  }
  context="$(sm_context "$secure")"
  [ -n "$context" ] || context=-
  {
    printf 'SCHEMA_VERSION=1\n'
    printf 'PATH=/data/adb\n'
    printf 'UID=%s\n' "$uid"
    printf 'GID=%s\n' "$gid"
    printf 'MODE=%s\n' "$mode"
    printf 'CONTEXT_B64=%s\n' "$(printf '%s' "$context" | "$SM_BB" base64 | "$SM_BB" tr -d '\n')"
  } >"$staged" || return 1
  "$SM_BB" chmod 0600 "$staged" || return 1
  sm_atomic_publish "$staged" "$SM_SECURE_DIR_METADATA" secure-dir-metadata || return 1
  SM_SECURE_DIR_SHA256="$(sm_sha256_file "$SM_SECURE_DIR_METADATA")" || return 1
}

sm_validate_secure_dir_base() {
  local secure uid gid mode mounted mountinfo="${SM_MOUNTINFO_FILE:-/proc/self/mountinfo}"
  secure="$(sm_real_path /data/adb)" || return 1
  [ -d "$secure" ] && [ ! -L "$secure" ] || {
    sm_log "! /data/adb is not a safe persistent directory"
    return 1
  }
  uid="$($SM_BB stat -c %u "$secure")" || return 1
  gid="$($SM_BB stat -c %g "$secure")" || return 1
  mode="$($SM_BB stat -c %a "$secure")" || return 1
  case "$uid:$gid:$mode" in *[!0-9:]*) return 1 ;; esac
  if [ "$uid" != 0 ] || ! sm_reject_unsafe_mode "$mode"; then
    sm_log "! /data/adb must be root-owned without group/world write access"
    return 1
  fi
  [ -r "$mountinfo" ] || return 1
  mounted="$($SM_BB awk -v target="$secure" '$5 == target { print $5; exit }' "$mountinfo")" || return 1
  [ -z "$mounted" ] || {
    sm_log "! /data/adb is an unexpected exact mountpoint"
    return 1
  }
  return 0
}

sm_validate_secure_dir_metadata() {
  local uid gid mode context
  [ -f "$SM_SECURE_DIR_METADATA" ] && [ ! -L "$SM_SECURE_DIR_METADATA" ] || return 1
  [ "$(sm_get SCHEMA_VERSION "$SM_SECURE_DIR_METADATA")" = 1 ] || return 1
  [ "$(sm_get PATH "$SM_SECURE_DIR_METADATA")" = /data/adb ] || return 1
  uid="$(sm_get UID "$SM_SECURE_DIR_METADATA")"
  gid="$(sm_get GID "$SM_SECURE_DIR_METADATA")"
  mode="$(sm_get MODE "$SM_SECURE_DIR_METADATA")"
  context="$(sm_decode "$(sm_get CONTEXT_B64 "$SM_SECURE_DIR_METADATA")")" || return 1
  case "$uid:$gid" in *[!0-9:]*) return 1 ;; esac
  case "$mode" in [0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) ;; *) return 1 ;; esac
  sm_reject_unsafe_mode "$mode" || return 1
  [ "$context" = - ] || sm_valid_single_line "$context" || return 1
  if [ "$SM_SECURE_DIR_SHA256" != "$(printf '%064d' 0)" ]; then
    [ "$(sm_sha256_file "$SM_SECURE_DIR_METADATA")" = "$SM_SECURE_DIR_SHA256" ] || return 1
  fi
  return 0
}

sm_restore_secure_dir_metadata() {
  local uid gid mode context secure
  sm_validate_secure_dir_metadata || return 1
  secure="$(sm_real_path /data/adb)" || return 1
  [ -d "$secure" ] && [ ! -L "$secure" ] || return 1
  uid="$(sm_get UID "$SM_SECURE_DIR_METADATA")"
  gid="$(sm_get GID "$SM_SECURE_DIR_METADATA")"
  mode="$(sm_get MODE "$SM_SECURE_DIR_METADATA")"
  context="$(sm_decode "$(sm_get CONTEXT_B64 "$SM_SECURE_DIR_METADATA")")" || return 1
  "$SM_BB" chown "$uid:$gid" "$secure" || return 1
  "$SM_BB" chmod "$mode" "$secure" || return 1
  [ "$context" = - ] || chcon "$context" "$secure" 2>/dev/null || return 1
  sm_fsync "$secure"
}

sm_legacy_exact_setting() {
  local file="$1" key="$2" expected="$3"
  [ -f "$file" ] && [ ! -L "$file" ] || return 1
  "$SM_BB" awk -v prefix="$key=" -v expected="$expected" '
    index($0, prefix) == 1 {
      count++
      value = substr($0, length(prefix) + 1)
    }
    END { exit !(count == 1 && value == expected) }
  ' "$file"
}

sm_validate_legacy_directory() {
  local path="$1" uid mode unsupported
  [ -d "$path" ] && [ ! -L "$path" ] || return 1
  uid="$($SM_BB stat -c %u "$path")" || return 1
  mode="$($SM_BB stat -c %a "$path")" || return 1
  [ "$uid" = 0 ] && sm_reject_unsafe_mode "$mode" || return 1
  unsupported="$(
    cd "$path" || exit 1
    "$SM_BB" find . -mindepth 1 ! -type f ! -type d -print | "$SM_BB" head -n 1
  )" || return 1
  [ -z "$unsupported" ]
}

sm_legacy_regular_file() {
  local path="$1" uid mode
  [ -f "$path" ] && [ ! -L "$path" ] || return 1
  uid="$($SM_BB stat -c %u "$path")" || return 1
  mode="$($SM_BB stat -c %a "$path")" || return 1
  [ "$uid" = 0 ] && sm_reject_unsafe_mode "$mode"
}

sm_legacy_rc_signature() {
  local path="$1"
  sm_legacy_regular_file "$path" || return 1
  "$SM_BB" grep -Fq '/system/etc/init/magisk' "$path" &&
    "$SM_BB" grep -Fq -- '--setup-sbin' "$path" &&
    "$SM_BB" grep -Fq -- '--post-fs-data' "$path"
}

sm_legacy_live_policy_rc() {
  local file="$1"
  sm_legacy_regular_file "$file" || return 1
  # This historical launcher changes only the running policy. It never had a
  # persistent-policy gzip original; require its complete command vocabulary
  # before treating an absent sidecar as intentional.
  "$SM_BB" awk '
    {
      line=$0
      gsub(/[[:space:]]+/, " ", line)
      sub(/^ /, "", line); sub(/ $/, "", line)
      if (line == "" || line ~ /^#/) next
      if (line ~ /^on (post-fs-data|nonencrypted|property:vold.decrypt=trigger_restart_framework|property:sys.boot_completed=1|property:init.svc.zygote=(restarting|stopped))$/) next
      if (line == "start logd" || line == "mkdir /data/adb/magisk 755") next
      if (line !~ /^exec u:r:(su|magisk|update_engine|init):s0 (root|0) (root|0) -- /) { bad=1; exit }
      sub(/^exec [^ ]+ [^ ]+ [^ ]+ -- /, "", line)
      if (line == "/system/etc/init/magisk/magiskpolicy --live --magisk") { policy=1; next }
      if (line ~ /^\/system\/etc\/init\/magisk\/magisk(32|64)? --auto-selinux --setup-sbin \/system\/etc\/init\/magisk \/(sbin|debug_ramdisk)$/) { setup=1; next }
      if (line ~ /^\/(sbin|debug_ramdisk)\/magisk --auto-selinux --(post-fs-data|service|boot-complete|zygote-restart)$/) {
        if (line ~ / --post-fs-data$/) post=1
        next
      }
      bad=1; exit
    }
    END { exit bad || !policy || !setup || !post }
  ' "$file"
}

sm_find_legacy_policy_sidecar() {
  local canonical real count=0
  SM_LEGACY_POLICY_PATH=
  # Historical Kitsune selected from this exact ordered set before writing a
  # gzip stock backup beside the persistently patched policy. Do not infer the
  # legacy target from the current kernel's preferred policy source: both can
  # exist, and restoring the wrong file would corrupt exact uninstall.
  for canonical in \
    /vendor/etc/selinux/precompiled_sepolicy \
    /odm/etc/selinux/precompiled_sepolicy \
    /system/etc/selinux/precompiled_sepolicy \
    /system_root/sepolicy \
    /system_root/sepolicy_debug \
    /system_root/sepolicy.unlocked; do
    real="$(sm_real_path "$canonical")" || return 1
    sm_path_present "$real.gz" || continue
    count=$((count + 1))
    [ "$count" = 1 ] || {
      sm_log "! Legacy System Mode has ambiguous policy backup sidecars"
      return 1
    }
    if ! sm_legacy_regular_file "$real" || ! sm_legacy_regular_file "$real.gz" ||
       ! "$SM_BB" gzip -t "$real.gz"; then
      sm_log "! Legacy System Mode policy backup is unsafe or corrupt"
      return 1
    fi
    SM_LEGACY_POLICY_PATH="$canonical"
  done
  [ "$count" = 1 ] && return 0
  if command -v is_rootfs >/dev/null 2>&1 && is_rootfs; then
    return 0
  fi
  if sm_legacy_live_policy_rc "$(sm_real_path "$SM_SYSTEM_DIR.rc")"; then
    return 0
  fi
  sm_log "! Legacy System Mode has no exact restorable policy backup"
  return 1
}

sm_validate_legacy_footprint() {
  local payload config legacy_rc selected_init bootanim bootanim_gz runtime addon_script addon_dir
  local payload_policy payload_init runtime_policy runtime_init candidate found_binary=false
  payload="$(sm_real_path "$SM_SYSTEM_DIR")" || return 1
  config="$payload/config"
  legacy_rc="$(sm_real_path "$SM_SYSTEM_DIR.rc")" || return 1
  selected_init="$(sm_real_path "$SM_INIT_PATH")" || return 1
  bootanim="$(sm_real_path /system/etc/init/bootanim.rc)" || return 1
  bootanim_gz="$bootanim.gz"
  runtime=/data/adb/magisk
  addon_script="$(sm_real_path /system/addon.d/99-magisk.sh)" || return 1
  addon_dir="$(sm_real_path /system/addon.d/magisk)" || return 1

  sm_validate_legacy_directory "$payload" || {
    sm_log "! Legacy System Mode payload is not a safe owned directory"
    return 1
  }
  sm_legacy_exact_setting "$config" SYSTEMMODE true || {
    sm_log "! Legacy System Mode config is missing one exact SYSTEMMODE=true marker"
    return 1
  }
  sm_legacy_exact_setting "$config" RECOVERYMODE false || {
    sm_log "! Legacy System Mode config is missing one exact RECOVERYMODE=false marker"
    return 1
  }
  payload_policy="$payload/magiskpolicy"
  payload_init="$payload/magiskinit"
  if ! sm_legacy_regular_file "$payload_policy" || ! sm_legacy_regular_file "$payload_init"; then
    sm_log "! Legacy System Mode payload is missing its policy or init binary"
    return 1
  fi
  if [ -n "${SM_LEGACY_POLICY_PATH:-}" ]; then
    [ "$SM_POLICY_PATH" = "$SM_LEGACY_POLICY_PATH" ] || return 1
  fi
  for candidate in magisk magisk32 magisk64; do
    if sm_legacy_regular_file "$payload/$candidate"; then
      found_binary=true
      break
    fi
  done
  [ "$found_binary" = true ] || {
    sm_log "! Legacy System Mode payload has no recognizable Magisk binary"
    return 1
  }

  if sm_path_present "$legacy_rc"; then
    sm_legacy_rc_signature "$legacy_rc" || {
      sm_log "! Refusing an unrecognized legacy System Mode init RC"
      return 1
    }
  elif sm_legacy_rc_signature "$bootanim"; then
    if ! sm_legacy_regular_file "$bootanim_gz" || ! "$SM_BB" gzip -t "$bootanim_gz"; then
      sm_log "! Legacy bootanim injection has no valid stock gzip sidecar"
      return 1
    fi
  else
    sm_log "! Legacy System Mode has no recognizable init launcher"
    return 1
  fi
  if [ -f "$bootanim_gz" ] || [ -L "$bootanim_gz" ]; then
    if ! sm_legacy_regular_file "$bootanim_gz" || ! "$SM_BB" gzip -t "$bootanim_gz"; then
      sm_log "! Legacy bootanim sidecar is unsafe or corrupt"
      return 1
    fi
  fi
  if [ "$SM_SYSTEM_DIR.rc" != "$SM_INIT_PATH" ] && sm_path_present "$selected_init"; then
    sm_log "! Refusing to absorb an unrelated selected init RC"
    return 1
  fi

  if sm_path_present "$runtime"; then
    sm_validate_legacy_directory "$runtime" || {
      sm_log "! Legacy Magisk runtime is not a safe owned directory"
      return 1
    }
    runtime_policy="$runtime/magiskpolicy"
    runtime_init="$runtime/magiskinit"
    if ! sm_legacy_regular_file "$runtime_policy" || ! sm_legacy_regular_file "$runtime_init"; then
      sm_log "! Legacy Magisk runtime is incomplete"
      return 1
    fi
    [ "$(sm_sha256_file "$runtime_policy")" = "$(sm_sha256_file "$payload_policy")" ] &&
      [ "$(sm_sha256_file "$runtime_init")" = "$(sm_sha256_file "$payload_init")" ] || {
        sm_log "! Legacy runtime does not match the persistent System Mode payload"
        return 1
      }
  fi

  if sm_path_present "$addon_script"; then
    if ! sm_legacy_regular_file "$addon_script" ||
       ! sm_legacy_exact_setting "$addon_script" SYSTEMINSTALL true ||
       ! "$SM_BB" grep -Fq '/system/etc/init/magisk' "$addon_script"; then
      sm_log "! Refusing an unrecognized legacy addon.d script"
      return 1
    fi
  fi
  if sm_path_present "$addon_dir"; then
    sm_log "! Refusing an unowned legacy addon.d payload directory"
    return 1
  fi
  return 0
}

sm_append_current_original() {
  local canonical="$1" label="$2" real
  real="$(sm_real_path "$canonical")" || return 1
  if sm_path_present "$real"; then
    sm_original_record "$canonical" "$label" "$real" true
  else
    sm_original_record "$canonical" "$label" /dev/null false
  fi
}

sm_extend_pr5b_originals() {
  local label container
  [ "${SM_PR5B_MIGRATED:-false}" = true ] || return 1
  sm_validate_pr5b_originals || return 1
  sm_quiesce_magisk || return 1
  sm_assert_mutable_namespaces_idle || return 1
  "$SM_BB" cp -a "$SM_ORIGINAL_FILE" "$SM_ORIGINAL_FILE.new" || return 1
  for label in magisk_db magisk_db_wal magisk_db_shm modules modules_update \
    post_fs_data service preinit_rule magisk_log magisk_log_bak rescue_rc rescue_dir; do
    container="$SM_STATE_DIR/original/$label"
    if sm_path_present "$container"; then
      [ -d "$container" ] && [ ! -L "$container" ] || return 1
      "$SM_BB" rm -rf "$container" || return 1
    fi
  done
  sm_append_current_original /data/adb/magisk.db magisk_db || return 1
  sm_append_current_original /data/adb/magisk.db-wal magisk_db_wal || return 1
  sm_append_current_original /data/adb/magisk.db-shm magisk_db_shm || return 1
  sm_append_current_original /data/adb/modules modules || return 1
  sm_append_current_original /data/adb/modules_update modules_update || return 1
  sm_append_current_original /data/adb/post-fs-data.d post_fs_data || return 1
  sm_append_current_original /data/adb/service.d service || return 1
  sm_append_current_original /data/adb/sepolicy.rule preinit_rule || return 1
  sm_append_current_original /cache/magisk.log magisk_log || return 1
  sm_append_current_original /cache/magisk.log.bak magisk_log_bak || return 1
  for container in "$SM_RESCUE_RC" "$SM_RESCUE_DIR"; do
    ! sm_path_present "$(sm_real_path "$container")" || {
      sm_log "! Refusing to absorb a pre-existing PR7 rescue path during PR5B migration"
      return 1
    }
  done
  sm_original_record "$SM_RESCUE_RC" rescue_rc /dev/null false || return 1
  sm_original_record "$SM_RESCUE_DIR" rescue_dir /dev/null false || return 1
  sm_assert_magisk_quiesced || return 1
  sm_assert_mutable_namespaces_idle || return 1
  sm_atomic_publish "$SM_ORIGINAL_FILE.new" "$SM_ORIGINAL_FILE" pr5b-originals-extended || return 1
  sm_fsync_tree "$SM_STATE_DIR/original" || return 1
  SM_ORIGINALS_SHA256="$(sm_sha256_file "$SM_ORIGINAL_FILE")" || return 1
  SM_PR5B_MIGRATED=false
  sm_validate_originals
}

sm_prepare_originals() {
  local config_real init_real policy_real policy_gz bootanim_real bootanim_gz temp legacy=false conflict
  sm_configure_rescue_paths || return 1
  if [ -f "$SM_ORIGINAL_FILE" ]; then
    [ -f "$SM_MANIFEST_COPY" ] || { sm_log "! Original backups exist without an install manifest"; return 1; }
    if [ "${SM_PR5B_MIGRATED:-false}" = true ]; then
      sm_extend_pr5b_originals
      return
    fi
    sm_validate_originals && sm_validate_secure_dir_metadata && return 0
    sm_log "! Existing development receipt predates the complete mutable-state contract; restore externally before upgrading"
    return 1
  fi
  config_real="$(sm_real_path "$SM_SYSTEM_DIR/config")" || return 1
  sm_legacy_exact_setting "$config_real" SYSTEMMODE true && legacy=true
  for conflict in "$SM_RESCUE_RC" "$SM_RESCUE_DIR"; do
    if sm_path_present "$(sm_real_path "$conflict")"; then
      sm_log "! Refusing to absorb unowned rescue path $conflict"
      return 1
    fi
  done
  if sm_path_present "$(sm_real_path "$SM_SYSTEM_DIR")" && [ "$legacy" != true ]; then
    sm_log "! Refusing to replace an unowned System Mode payload path"
    return 1
  fi
  if [ "$legacy" = true ]; then
    sm_validate_legacy_footprint || return 1
  fi
  if [ "$legacy" != true ]; then
    for conflict in /data/adb/magisk /data/adb/magisk.db /data/adb/magisk.db-wal \
      /data/adb/magisk.db-shm /data/adb/modules /data/adb/modules_update \
      /data/adb/post-fs-data.d /data/adb/service.d /data/adb/sepolicy.rule \
      /cache/magisk.log /cache/magisk.log.bak \
      /system/addon.d/99-magisk.sh /system/addon.d/magisk \
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
  sm_original_record "$SM_INIT_PATH" init_rc /dev/null false || return 1
  sm_original_record /data/adb/magisk runtime /dev/null false || return 1
  sm_original_record /data/adb/magisk.db magisk_db /dev/null false || return 1
  sm_original_record /data/adb/magisk.db-wal magisk_db_wal /dev/null false || return 1
  sm_original_record /data/adb/magisk.db-shm magisk_db_shm /dev/null false || return 1
  sm_original_record /data/adb/modules modules /dev/null false || return 1
  sm_original_record /data/adb/modules_update modules_update /dev/null false || return 1
  sm_original_record /data/adb/post-fs-data.d post_fs_data /dev/null false || return 1
  sm_original_record /data/adb/service.d service /dev/null false || return 1
  sm_original_record /data/adb/sepolicy.rule preinit_rule /dev/null false || return 1
  sm_original_record /cache/magisk.log magisk_log /dev/null false || return 1
  sm_original_record /cache/magisk.log.bak magisk_log_bak /dev/null false || return 1
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
  # Rescue is intentionally last in the original inventory. Exact uninstall
  # restores every ordinary boot path first, removes the rescue RC next, and
  # removes the now-inert rescue bytes last.
  sm_original_record "$SM_RESCUE_RC" rescue_rc /dev/null false || return 1
  sm_original_record "$SM_RESCUE_DIR" rescue_dir /dev/null false || return 1
  sm_atomic_publish "$SM_ORIGINAL_FILE.new" "$SM_ORIGINAL_FILE" original-inventory || return 1
  sm_fsync_tree "$SM_STATE_DIR/original" || return 1
  return 0
}

sm_restore_snapshot() {
  local label canonical real source failed=0 rollback="$SM_ROLLBACK_DIR"
  sm_quiesce_magisk && sm_assert_mutable_namespaces_idle || {
    sm_log "! Mutable Magisk state is still in use; refusing unsafe rollback"
    return 1
  }
  sm_update_state ROLLING_BACK || return 1
  for label in magisk_log_bak magisk_log preinit_rule service post_fs_data modules_update modules \
    magisk_db_shm magisk_db_wal magisk_db runtime addon_dir addon_script \
    bootanim_gz bootanim policy_gz policy payload legacy_rc init_rc rescue_rc rescue_dir; do
    if [ -z "$SM_POLICY_PATH" ] || [ "$SM_POLICY_MUTATED" != true ]; then
      case "$label" in policy|policy_gz) continue ;; esac
    fi
    [ "$label" != legacy_rc ] || [ "$SM_SYSTEM_DIR.rc" != "$SM_INIT_PATH" ] || continue
    canonical="$(sm_label_path "$label")" || return 1
    real="$(sm_real_path "$canonical")" || return 1
    source="$SM_ROLLBACK_DIR/$label"
    if [ ! -d "$source" ]; then
      case "$label" in
        rescue_dir|rescue_rc)
          # Pre-rescue development receipts have neither a rescue snapshot nor
          # a live rescue path. They remain valid one-time upgrade inputs.
          if ! sm_path_present "$real"; then continue; fi
          ;;
      esac
      failed=1
      break
    fi
    sm_validate_snapshot_entry "$label" || { failed=1; break; }
    sm_destructive_target_safe "$label" "$real" || { failed=1; break; }
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
  if ! sm_restore_secure_dir_metadata; then
    sm_update_state FAILED
    sm_log "! /data/adb metadata recovery failed; use the verified external restore"
    return 1
  fi
  if ! sm_restore_state_metadata; then
    sm_update_state FAILED
    sm_log "! Transaction metadata recovery failed; use the verified external restore"
    return 1
  fi
  sm_finish_rollback_storage || return 1
  return 0
}

sm_abort_transaction() {
  sm_recover_setup_marker || return 1
  sm_complete_rollback_terminal || return 1
  [ -f "$SM_TRANSACTION_FILE" ] || return 0
  sm_load_transaction || return 1
  case "$SM_STATE" in
    PREFLIGHTED) sm_restore_preflight ;;
    STAGED|COMMITTED|ROLLBACK_REQUIRED|ROLLING_BACK)
      [ "$SM_STATE" = ROLLING_BACK ] || sm_update_state ROLLBACK_REQUIRED || return 1
      sm_restore_snapshot
      ;;
    BOOT_VERIFIED|UNINSTALLED) sm_cleanup_terminal_rollback ;;
    FAILED) return 1 ;;
  esac
}

sm_owned_path_allowed() {
  local path="$1" system="${SM_SYSTEM_DIR:-}" init="${SM_INIT_PATH:-}"
  local rescue_dir="${SM_RESCUE_DIR:-}" rescue_rc="${SM_RESCUE_RC:-}"
  local policy="${SM_POLICY_PATH:-}"
  sm_valid_single_line "$path" || return 1
  case "$path" in
    *//*|*/./*|*/../*|*/.|*/..) return 1 ;;
  esac
  [ -z "$init" ] || [ "$path" != "$init" ] || return 0
  [ -z "$rescue_rc" ] || [ "$path" != "$rescue_rc" ] || return 0
  case "$path" in
    /system/addon.d/99-magisk.sh|/data/adb/magisk/*) return 0 ;;
  esac
  if [ -n "$system" ]; then case "$path" in "$system"/*) return 0 ;; esac; fi
  if [ -n "$rescue_dir" ]; then case "$path" in "$rescue_dir"/*) return 0 ;; esac; fi
  if [ -n "$policy" ] && [ "${SM_POLICY_MUTATED:-false}" = true ] && [ "$path" = "$policy" ]; then
    return 0
  fi
  return 1
}

sm_validate_ownership_rows() {
  local path digest size mode uid gid context kind extra rows=0
  [ -f "$SM_OWNERSHIP_FILE" ] && [ ! -L "$SM_OWNERSHIP_FILE" ] || {
    sm_log "! System Mode ownership inventory is missing or redirected"
    return 1
  }
  "$SM_BB" awk -F '\t' '
    NF != 8 || seen[$1]++ { invalid=1 }
    END { exit invalid }
  ' "$SM_OWNERSHIP_FILE" || {
    sm_log "! System Mode ownership inventory has duplicate or malformed rows"
    return 1
  }
  while IFS="$SM_TAB" read -r path digest size mode uid gid context kind extra; do
    [ -z "$extra" ] || return 1
    sm_owned_path_allowed "$path" || {
      sm_log "! System Mode ownership inventory contains an unrecognized path: $path"
      return 1
    }
    sm_valid_hex "$digest" 64 || return 1
    case "$size" in *[!0-9]*|'') return 1 ;; esac
    case "$uid" in *[!0-9]*|'') return 1 ;; esac
    case "$gid" in *[!0-9]*|'') return 1 ;; esac
    case "$mode" in 0[0-7][0-7][0-7]) ;; *) return 1 ;; esac
    [ "$context" = - ] || sm_valid_single_line "$context" || return 1
    case "$kind" in file|link) ;; *) return 1 ;; esac
    rows=$((rows + 1))
  done <"$SM_OWNERSHIP_FILE"
  [ "$rows" -gt 0 ]
}

sm_validate_ownership_inventory() {
  local actual zero expected="${SM_OWNERSHIP_SHA256:-}"
  zero="$(printf '%064d' 0)"
  [ -n "$expected" ] || expected="$zero"
  sm_valid_hex "$expected" 64 || return 1
  [ "$expected" != "$zero" ] || {
    sm_log "! System Mode ownership inventory is not receipt-bound"
    return 1
  }
  actual="$(sm_sha256_file "$SM_OWNERSHIP_FILE")" || return 1
  [ "$actual" = "$expected" ] || {
    sm_log "! System Mode ownership inventory digest changed"
    return 1
  }
  sm_validate_ownership_rows
}

sm_verify_owned_entries() {
  local path digest size mode uid gid context kind real actual actual_size actual_mode actual_uid actual_gid actual_context
  while IFS="$SM_TAB" read -r path digest size mode uid gid context kind; do
    [ -n "$path" ] || continue
    real="$(sm_real_path "$path")" || return 1
    case "$kind" in
      file) [ -f "$real" ] && [ ! -L "$real" ] || { sm_log "! Owned file is missing or redirected: $path"; return 1; }; actual="$(sm_sha256_file "$real")" ;;
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

sm_verify_owned() {
  sm_validate_ownership_inventory || return 1
  sm_verify_owned_entries
}

sm_verify_owned_entry() {
  local canonical="$1" real="$2" record path digest size mode uid gid context kind
  local actual actual_size actual_mode actual_uid actual_gid actual_context
  [ -f "$SM_OWNERSHIP_FILE" ] && [ ! -L "$SM_OWNERSHIP_FILE" ] || return 1
  record="$($SM_BB awk -F '\t' -v target="$canonical" '
    $1 == target { count++; row=$0 }
    END { if (count == 1) print row; else exit 1 }
  ' "$SM_OWNERSHIP_FILE")" || return 1
  IFS="$SM_TAB" read -r path digest size mode uid gid context kind <<EOF
$record
EOF
  [ "$path" = "$canonical" ] || return 1
  case "$kind" in
    file) [ -f "$real" ] && [ ! -L "$real" ] || return 1 ;;
    link) [ -L "$real" ] || return 1 ;;
    *) return 1 ;;
  esac
  actual="$(sm_digest_path "$real")" || return 1
  actual_size="$($SM_BB stat -c %s "$real" 2>/dev/null)" || actual_size=0
  actual_mode="0$($SM_BB stat -c %a "$real")" || return 1
  actual_uid="$($SM_BB stat -c %u "$real")" || return 1
  actual_gid="$($SM_BB stat -c %g "$real")" || return 1
  actual_context="$(sm_context "$real")"
  [ -n "$actual_context" ] || actual_context=-
  [ "$actual" = "$digest" ] && [ "$actual_size" = "$size" ] &&
    [ "$actual_mode" = "$mode" ] && [ "$actual_uid" = "$uid" ] &&
    [ "$actual_gid" = "$gid" ] && [ "$actual_context" = "$context" ]
}

sm_assert_owned_restore_target() {
  local canonical="$1" real="$2" item relative child directory prefix manifest
  sm_validate_ownership_inventory || return 1
  sm_path_present "$real" || return 0
  case "$canonical" in
    /data/adb/magisk|/data/adb/magisk.db|/data/adb/magisk.db-wal|/data/adb/magisk.db-shm|/data/adb/modules|/data/adb/modules_update|/data/adb/post-fs-data.d|/data/adb/service.d|/data/adb/sepolicy.rule|/cache/magisk.log|/cache/magisk.log.bak)
      # These namespaces are explicitly mutable in the receipt. Their exact
      # pre-install bytes were snapshotted after the daemon was quiesced.
      return 0
      ;;
  esac
  if [ -f "$real" ] || [ -L "$real" ]; then
    sm_verify_owned_entry "$canonical" "$real" || {
      sm_log "! Refusing to remove an unowned fixed path: $canonical"
      return 1
    }
    return 0
  fi
  [ -d "$real" ] && [ ! -L "$real" ] || return 1
  manifest="$SM_SYSTEM_DIR/install-manifest.json"
  (
    cd "$real" || exit 1
    "$SM_BB" find . \( -type f -o -type l \) -print | "$SM_BB" sort
  ) | while IFS= read -r item; do
    relative="${item#./}"
    child="$canonical/$relative"
    if [ "$child" = "$manifest" ]; then
      [ -f "$real/$relative" ] && [ ! -L "$real/$relative" ] &&
        [ "$(sm_sha256_file "$real/$relative")" = "$SM_MANIFEST_SHA256" ] || exit 1
      continue
    fi
    sm_verify_owned_entry "$child" "$real/$relative" || {
      sm_log "! Refusing to remove an unowned fixed path: $child"
      exit 1
    }
  done || return 1
  # Ownership records contain leaves, not directory rows. Every non-root
  # directory must therefore contain at least one recorded descendant (or the
  # separately receipt-bound manifest) so an injected empty tree is retained.
  (
    cd "$real" || exit 1
    "$SM_BB" find . -mindepth 1 -type d -print | "$SM_BB" sort
  ) | while IFS= read -r directory; do
    relative="${directory#./}"
    prefix="$canonical/$relative/"
    if ! "$SM_BB" awk -F '\t' -v wanted="$prefix" \
      'index($1, wanted) == 1 { found=1 } END { exit !found }' "$SM_OWNERSHIP_FILE"; then
      case "$manifest" in "$prefix"*) ;; *)
        sm_log "! Refusing to remove an unowned empty directory: ${prefix%/}"
        exit 1
        ;;
      esac
    fi
  done || return 1
  # Reject a non-empty root whose only leaves somehow escaped both loops.
  "$SM_BB" awk -F '\t' -v root="$canonical" \
    '$1 == root || index($1, root "/") == 1 { found=1 } END { exit !found }' \
    "$SM_OWNERSHIP_FILE" || {
      [ "$canonical" = "$SM_SYSTEM_DIR" ] && [ -f "$real/install-manifest.json" ] || {
        sm_log "! Refusing to remove an unowned fixed directory: $canonical"
        return 1
      }
    }
  return 0
}

sm_recover_pending() {
  local current_boot
  sm_recover_setup_marker || return 1
  sm_complete_rollback_terminal || return 1
  [ -f "$SM_TRANSACTION_FILE" ] || return 0
  sm_load_transaction || { sm_log "! Invalid persistent System Mode transaction"; return 1; }
  case "$SM_STATE" in
    UNINSTALLED|BOOT_VERIFIED) sm_cleanup_terminal_rollback ;;
    PREFLIGHTED) sm_restore_preflight ;;
    STAGED|ROLLBACK_REQUIRED|ROLLING_BACK)
      sm_log "- Recovering interrupted System Mode transaction"
      sm_restore_snapshot
      ;;
    COMMITTED)
      current_boot="$(sm_current_boot_id)"
      if [ "$current_boot" = "$SM_COMMIT_BOOT_ID" ]; then
        sm_log "! Reboot once to verify the committed System Mode installation"
        return 2
      fi
      if { [ -n "$SM_BOOT_ATTEMPT_ID" ] && [ "$current_boot" != "$SM_BOOT_ATTEMPT_ID" ]; } ||
         [ "$(getprop sys.boot_completed)" = 1 ]; then
        sm_log "! Committed System Mode payload missed its boot-verification window; rolling back"
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

sm_validate_pr5b_originals() {
  local path existed digest size mode uid gid context backup label source container actual real
  local seen='|'
  [ -f "$SM_ORIGINAL_FILE" ] && [ ! -L "$SM_ORIGINAL_FILE" ] || return 1
  "$SM_BB" awk -F '\t' 'NF != 9 || seen[$1]++ { invalid=1 } END { exit invalid || NR == 0 }' \
    "$SM_ORIGINAL_FILE" || return 1
  while IFS="$SM_TAB" read -r path existed digest size mode uid gid context backup; do
    [ -n "$path" ] || continue
    if [ "$path" = "$SM_SYSTEM_DIR" ]; then label=payload
    elif [ "$path" = "$SM_INIT_PATH" ]; then label=init_rc
    elif [ "$path" = "$SM_SYSTEM_DIR.rc" ]; then label=legacy_rc
    elif [ -n "$SM_POLICY_PATH" ] && [ "$path" = "$SM_POLICY_PATH" ]; then label=policy
    elif [ "$path" = /system/etc/init/bootanim.rc ]; then label=bootanim
    elif [ "$path" = /data/adb/magisk ]; then label=runtime
    elif [ "$path" = /system/addon.d/99-magisk.sh ]; then label=addon_script
    elif [ "$path" = /system/addon.d/magisk ]; then label=addon_dir
    else return 1
    fi
    case "$seen" in *"|$label|"*) return 1 ;; esac
    seen="$seen$label|"
    [ "$backup" = "original/$label/data" ] || return 1
    [ "$context" = - ] || sm_valid_single_line "$context" || return 1
    source="$SM_STATE_DIR/$backup"
    container="${source%/data}"
    [ -d "$container" ] && [ ! -L "$container" ] || return 1
    case "$existed" in
      true)
        sm_valid_hex "$digest" 64 || return 1
        case "$size" in *[!0-9]*|'') return 1 ;; esac
        case "$mode" in 0[0-7][0-7][0-7]|0[0-7][0-7][0-7][0-7]) ;; *) return 1 ;; esac
        case "$uid:$gid" in *[!0-9:]*) return 1 ;; esac
        [ ! -f "$container/absent" ] && [ ! -L "$container/absent" ] || return 1
        sm_path_present "$source" && [ "$(sm_digest_path "$source")" = "$digest" ] || return 1
        ;;
      false)
        [ "$digest:$size:$mode:$uid:$gid:$context" = '-:0:-:-:-:-' ] || return 1
        [ -f "$container/absent" ] && [ ! -L "$container/absent" ] &&
          [ "$("$SM_BB" cat "$container/absent")" = absent ] || return 1
        ! sm_path_present "$source" || return 1
        ;;
      *) return 1 ;;
    esac
    if [ "$path" = /system/etc/init/bootanim.rc ]; then
      real="$(sm_real_path "$path")" || return 1
      if [ "$existed" = true ]; then
        sm_path_present "$real" && [ "$(sm_digest_path "$real")" = "$digest" ] || return 1
      else
        ! sm_path_present "$real" || return 1
      fi
    fi
  done <"$SM_ORIGINAL_FILE"
  for label in payload init_rc bootanim runtime addon_script addon_dir; do
    case "$seen" in *"|$label|"*) ;; *) return 1 ;; esac
  done
  if [ "$SM_SYSTEM_DIR.rc" != "$SM_INIT_PATH" ]; then
    case "$seen" in *'|legacy_rc|'*) ;; *) return 1 ;; esac
  fi
  if [ -n "$SM_POLICY_PATH" ]; then
    case "$seen" in *'|policy|'*) ;; *) return 1 ;; esac
  fi
  return 0
}

sm_validate_pr5b_journal() {
  [ -f "$SM_JOURNAL_FILE" ] && [ ! -L "$SM_JOURNAL_FILE" ] || return 1
  "$SM_BB" awk -F '\t' '
    function hex64(value) { return value ~ /^[a-f0-9]+$/ && length(value) == 64 }
    NF != 7 || $1 != NR || $2 != "publish" ||
      ($3 != "create" && $3 != "replace") ||
      ($5 != "-" && !hex64($5)) || !hex64($6) || $7 != "true" || seen[$4]++ {
      invalid=1
    }
    END { exit invalid || NR == 0 }
  ' "$SM_JOURNAL_FILE" || return 1
  while IFS="$SM_TAB" read -r sequence boundary operation path before after committed; do
    sm_owned_path_allowed "$path" || return 1
    "$SM_BB" awk -F '\t' -v path="$path" -v digest="$after" '
      $1 == path && $2 == digest { count++ }
      END { exit count != 1 }
    ' "$SM_OWNERSHIP_FILE" || return 1
  done <"$SM_JOURNAL_FILE"
  "$SM_BB" awk -F '\t' '
    NR == FNR { journal[$4]++; next }
    !journal[$1] { missing=1 }
    END { exit missing }
  ' "$SM_JOURNAL_FILE" "$SM_OWNERSHIP_FILE"
}

sm_generate_pr5b_manifest_projection() {
  local output="$1" first path digest size mode uid gid context kind
  local existed backup sequence boundary operation before after committed abi old_ifs
  (umask 077; set -C; {
    printf '{\n'
    printf '  "schema_version": 1,\n'
    printf '  "install_id": "%s",\n' "$(sm_json_escape "$SM_INSTALL_ID")"
    printf '  "state": "%s",\n' "$SM_STATE"
    printf '  "product": {"name": "KitsuneMagisk", "version": "%s", "source_commit": "%s", "upstream_base": "%s", "artifact_sha256": "%s"},\n' \
      "$(sm_json_escape "$SM_PRODUCT_VERSION")" "$SM_SOURCE_COMMIT" "$SM_UPSTREAM_BASE" "$SM_ARTIFACT_SHA256"
    printf '  "target": {"adapter_id": "%s", "fingerprint_sha256": "%s", "api": %s, "abis": [' \
      "$(sm_json_escape "$SM_ADAPTER_ID")" "$SM_FINGERPRINT_SHA256" "$SM_TARGET_API"
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
      "$(sm_json_escape "$SM_INIT_PATH")" "$(sm_json_escape "$SM_SELINUX_STRATEGY")" \
      "$(sm_json_escape "$SM_POLICY_SOURCE")" "$(sm_json_escape "$SM_RUNTIME_PATH")"
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
      if [ "$existed" = true ]; then
        printf '"%s/%s"' "$SM_STATE_DIR" "$(sm_json_escape "$backup")"
      else
        printf 'null'
      fi
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
  } >"$output")
}

sm_validate_pr5b_installed_state() {
  local manifest_real projection expected
  sm_require_lock || return 1
  [ "$SM_PR5B_RECEIPT" = true ] && [ "$SM_STATE" = BOOT_VERIFIED ] || return 1
  manifest_real="$(sm_real_path "$SM_SYSTEM_DIR/install-manifest.json")" || return 1
  [ -f "$manifest_real" ] && [ ! -L "$manifest_real" ] &&
    [ -f "$SM_MANIFEST_COPY" ] && [ ! -L "$SM_MANIFEST_COPY" ] || return 1
  sm_validate_ownership_rows || return 1
  sm_verify_owned_entries || return 1
  sm_assert_no_unowned_files || return 1
  sm_validate_pr5b_originals || return 1
  sm_validate_pr5b_journal || return 1
  sm_validate_secure_dir_base || return 1
  projection="$SM_LOCK_ROOT/.kitsune-pr5b-manifest-$SM_TRANSACTION_ID.$$.json"
  ! sm_path_present "$projection" || return 1
  sm_generate_pr5b_manifest_projection "$projection" || return 1
  expected="$(sm_sha256_file "$projection")" || { "$SM_BB" rm -f "$projection"; return 1; }
  if [ "$(sm_sha256_file "$manifest_real")" != "$expected" ] ||
     [ "$(sm_sha256_file "$SM_MANIFEST_COPY")" != "$expected" ]; then
    "$SM_BB" rm -f "$projection"
    sm_log "! PR5B manifest does not match its canonical receipt projection"
    return 1
  fi
  "$SM_BB" rm -f "$projection" || return 1
  SM_PR5B_VALIDATED_RECEIPT_SHA256="$(sm_sha256_file "$SM_TRANSACTION_FILE")" || return 1
  SM_PR5B_VALIDATED_MANIFEST_SHA256="$expected"
  SM_PR5B_VALIDATED_OWNERSHIP_SHA256="$(sm_sha256_file "$SM_OWNERSHIP_FILE")" || return 1
  SM_PR5B_VALIDATED_ORIGINALS_SHA256="$(sm_sha256_file "$SM_ORIGINAL_FILE")" || return 1
}

sm_migrate_pr5b_receipt() {
  local staged="$SM_STATE_DIR/.pr5b-transaction.env.new"
  sm_require_lock || return 1
  [ "$SM_PR5B_RECEIPT" = true ] || return 1
  [ "$(sm_sha256_file "$SM_TRANSACTION_FILE")" = "$SM_PR5B_VALIDATED_RECEIPT_SHA256" ] || return 1
  [ "$(sm_sha256_file "$SM_MANIFEST_COPY")" = "$SM_PR5B_VALIDATED_MANIFEST_SHA256" ] || return 1
  [ "$(sm_sha256_file "$(sm_real_path "$SM_SYSTEM_DIR/install-manifest.json")")" = "$SM_PR5B_VALIDATED_MANIFEST_SHA256" ] || return 1
  [ "$(sm_sha256_file "$SM_OWNERSHIP_FILE")" = "$SM_PR5B_VALIDATED_OWNERSHIP_SHA256" ] || return 1
  [ "$(sm_sha256_file "$SM_ORIGINAL_FILE")" = "$SM_PR5B_VALIDATED_ORIGINALS_SHA256" ] || return 1
  sm_validate_ownership_rows && sm_verify_owned_entries && sm_assert_no_unowned_files &&
    sm_validate_pr5b_originals && sm_validate_pr5b_journal || return 1
  sm_prepare_secure_dir_metadata || return 1
  SM_MANIFEST_SHA256="$SM_PR5B_VALIDATED_MANIFEST_SHA256"
  SM_OWNERSHIP_SHA256="$SM_PR5B_VALIDATED_OWNERSHIP_SHA256"
  SM_ORIGINALS_SHA256="$SM_PR5B_VALIDATED_ORIGINALS_SHA256"
  SM_SECURE_DIR_SHA256="$(sm_sha256_file "$SM_SECURE_DIR_METADATA")" || return 1
  if sm_path_present "$staged"; then
    sm_marker_file_safe "$staged" || return 1
    "$SM_BB" rm -f "$staged" || return 1
  fi
  (umask 077; {
    "$SM_BB" cat "$SM_TRANSACTION_FILE"
    printf 'MIGRATED_FROM_PR5B=true\n'
    printf 'MANIFEST_SHA256=%s\n' "$SM_MANIFEST_SHA256"
    printf 'OWNERSHIP_SHA256=%s\n' "$SM_OWNERSHIP_SHA256"
    printf 'ORIGINALS_SHA256=%s\n' "$SM_ORIGINALS_SHA256"
    printf 'SECURE_DIR_SHA256=%s\n' "$SM_SECURE_DIR_SHA256"
  } >"$staged") || return 1
  "$SM_BB" chmod 0600 "$staged" || return 1
  sm_atomic_publish "$staged" "$SM_TRANSACTION_FILE" pr5b-integrity-migrated || return 1
  SM_PR5B_RECEIPT=false
  SM_PR5B_MIGRATED=true
}

sm_register_boot_attempt() {
  local current_boot
  sm_load_transaction || return 1
  [ "$SM_STATE" = COMMITTED ] || { [ "$SM_STATE" = BOOT_VERIFIED ]; return; }
  current_boot="$(sm_current_boot_id)"
  sm_valid_uuid "$current_boot" || { sm_log "! Unable to identify the current boot attempt"; return 1; }
  [ "$current_boot" != "$SM_COMMIT_BOOT_ID" ] || {
    sm_log "! System Mode cannot be boot-verified before a reboot"
    return 1
  }
  if [ -z "$SM_BOOT_ATTEMPT_ID" ]; then
    SM_BOOT_ATTEMPT_ID="$current_boot"
    sm_write_transaction || return 1
    sm_log "- Registered the first System Mode boot attempt"
    return 0
  fi
  [ "$SM_BOOT_ATTEMPT_ID" = "$current_boot" ] && return 0
  sm_log "! A prior System Mode boot attempt did not verify; rollback is required"
  sm_update_state ROLLBACK_REQUIRED || return 1
  return 2
}

sm_validate_installed_state() {
  sm_verify_manifest_copies || return 1
  sm_verify_owned || {
    sm_log "! Existing System Mode payload changed"
    return 1
  }
  sm_assert_no_unowned_files || {
    sm_log "! Existing System Mode roots contain unowned files"
    return 1
  }
  if [ "${SM_PR5B_MIGRATED:-false}" = true ]; then
    sm_validate_pr5b_originals
  else
    sm_validate_originals
  fi || {
    sm_log "! Existing System Mode original backup changed"
    return 1
  }
  sm_validate_secure_dir_metadata || {
    sm_log "! Existing /data/adb metadata receipt changed"
    return 1
  }
  return 0
}

sm_verify_manifest_copies() {
  local manifest_real persistent_digest state_digest zero
  manifest_real="$(sm_real_path "$SM_SYSTEM_DIR/install-manifest.json")" || return 1
  [ -f "$manifest_real" ] && [ ! -L "$manifest_real" ] &&
    [ -f "$SM_MANIFEST_COPY" ] && [ ! -L "$SM_MANIFEST_COPY" ] || {
      sm_log "! Existing System Mode manifest is incomplete or redirected"
      return 1
    }
  persistent_digest="$(sm_sha256_file "$manifest_real")" || return 1
  state_digest="$(sm_sha256_file "$SM_MANIFEST_COPY")" || return 1
  [ "$persistent_digest" = "$SM_MANIFEST_SHA256" ] &&
    [ "$state_digest" = "$SM_MANIFEST_SHA256" ] || {
      sm_log "! Existing System Mode manifest does not match its receipt-bound digest"
      return 1
    }
  zero="$(printf '%064d' 0)"
  if [ "${SM_OWNERSHIP_SHA256:-$zero}" != "$zero" ]; then
    if [ "${SM_PR5B_MIGRATED:-false}" = true ]; then
      # The one-time migration authenticates the old canonical payload array
      # against ownership.tsv before binding both digests in the receipt.
      :
    elif ! "$SM_BB" grep -Fq \
         "\"ownership_inventory_sha256\": \"$SM_OWNERSHIP_SHA256\"" "$manifest_real" ||
       ! "$SM_BB" grep -Fq \
         "\"ownership_inventory_sha256\": \"$SM_OWNERSHIP_SHA256\"" "$SM_MANIFEST_COPY"; then
      sm_log "! System Mode manifest does not bind the ownership inventory"
      return 1
    fi
  fi
}

sm_begin_transaction() {
  local upgrading=false pr5b_upgrade=false legacy_migration=false legacy_policy_mutated=false config_real init_real
  local prior_adapter prior_init prior_policy prior_policy_source prior_runtime prior_selinux prior_policy_mutated
  local prior_preinit_device prior_preinit_dir prior_active_payload
  local requested_source_commit="${KITSUNE_SOURCE_COMMIT:-}"
  local requested_upstream_base="${KITSUNE_UPSTREAM_BASE:-}"
  local requested_product_version="${MAGISK_VER:-unknown-kitsune}"
  local requested_version_code="${MAGISK_VER_CODE:-}"
  SM_ARTIFACT_PATH="${1:-}"
  case "$requested_source_commit" in *[!a-f0-9]*|'') sm_log "! Missing full source identity"; return 1 ;; esac
  [ "${#requested_source_commit}" -eq 40 ] || { sm_log "! Invalid source identity"; return 1; }
  case "$requested_upstream_base" in *[!a-f0-9]*|'') sm_log "! Missing full upstream identity"; return 1 ;; esac
  [ "${#requested_upstream_base}" -eq 40 ] || { sm_log "! Invalid upstream identity"; return 1; }
  case "$requested_version_code" in *[!0-9]*|'') sm_log "! Missing product version code"; return 1 ;; esac
  SM_STATE=
  SM_LEGACY_POLICY_PATH=
  sm_recover_setup_marker || return 1
  sm_complete_rollback_terminal || return 1
  sm_validate_state_storage || return 1
  sm_recover_pending
  case $? in 0) ;; 2) return 1 ;; *) return 1 ;; esac
  if [ -f "$SM_TRANSACTION_FILE" ]; then
    sm_load_transaction || return 1
  else
    SM_STATE=
  fi
  if [ "${SM_STATE:-}" = BOOT_VERIFIED ]; then
    if [ "${SM_PR5B_RECEIPT:-false}" = true ]; then
      sm_validate_pr5b_installed_state || {
        sm_log "! PR5B receipt migration validation failed; use its verified external restore or uninstall with the PR5B manager"
        return 1
      }
      pr5b_upgrade=true
    elif ! sm_validate_installed_state; then
      sm_log "! Refusing to upgrade a modified System Mode installation"
      return 1
    fi
    upgrading=true
    prior_adapter="$SM_ADAPTER_ID"
    prior_init="$SM_INIT_PATH"
    prior_policy="$SM_POLICY_PATH"
    prior_policy_source="$SM_POLICY_SOURCE"
    prior_runtime="$SM_RUNTIME_PATH"
    prior_selinux="$SM_SELINUX_STRATEGY"
    prior_policy_mutated="$SM_POLICY_MUTATED"
    prior_preinit_device="$SM_PREINIT_DEVICE"
    prior_preinit_dir="$SM_PREINIT_DIR"
    prior_active_payload="$SM_ACTIVE_PAYLOAD"
  fi
  sm_validate_authorization || return 1
  sm_select_strategies || return 1
  if [ "$upgrading" = true ] && [ "$prior_policy_mutated" = true ]; then
    # The current policy source is still validated independently, while this
    # retained path identifies the historical persistent file exact uninstall
    # remains obliged to restore.
    SM_POLICY_PATH="$prior_policy"
  fi
  if [ "$upgrading" = true ] &&
     { [ "$SM_ADAPTER_ID" != "$prior_adapter" ] || [ "$SM_INIT_PATH" != "$prior_init" ] ||
       [ "$SM_POLICY_PATH" != "$prior_policy" ] || [ "$SM_POLICY_SOURCE" != "$prior_policy_source" ] ||
       [ "$SM_RUNTIME_PATH" != "$prior_runtime" ] || [ "$SM_SELINUX_STRATEGY" != "$prior_selinux" ] ||
       [ "$SM_PREINIT_DEVICE" != "$prior_preinit_device" ] || [ "$SM_PREINIT_DIR" != "$prior_preinit_dir" ]; }; then
    sm_log "! Refusing to change System Mode adapter or boot strategy during upgrade"
    return 1
  fi
  # Pre-transaction Kitsune stored the stock policy beside its rewritten
  # policy as *.gz. Recognize that exact migration marker so the new rollback
  # snapshot includes the currently installed policy before we restore stock.
  if [ "$upgrading" != true ]; then
    config_real="$(sm_real_path "$SM_SYSTEM_DIR/config")" || return 1
    if sm_legacy_exact_setting "$config_real" SYSTEMMODE true; then
      legacy_migration=true
      sm_find_legacy_policy_sidecar || return 1
      if [ -n "$SM_LEGACY_POLICY_PATH" ]; then
        SM_POLICY_PATH="$SM_LEGACY_POLICY_PATH"
        legacy_policy_mutated=true
      fi
    fi
  fi
  # Recovery loads the prior receipt into these globals. Reapply the identity
  # of the artifact being installed only after the prior state is validated.
  SM_SOURCE_COMMIT="$requested_source_commit"
  SM_UPSTREAM_BASE="$requested_upstream_base"
  SM_PRODUCT_VERSION="$requested_product_version"
  SM_VERSION_CODE="$requested_version_code"
  # The v30.7 launcher applies maintained Magisk rules to the live policy on
  # every boot. Persistent policy files are parsed and validated but are not
  # rewritten. A PR5B upgrade retains ownership of its already-patched policy
  # until exact uninstall restores the original.
  if { [ "$upgrading" = true ] && [ "$prior_policy_mutated" = true ]; } ||
     [ "$legacy_policy_mutated" = true ]; then
    SM_POLICY_MUTATED=true
  else
    SM_POLICY_MUTATED=false
  fi
  SM_LEGACY_MIGRATION="$legacy_migration"
  if [ "$upgrading" = true ]; then
    SM_TRUSTED_STOP_CLIENT="$prior_active_payload/magisk"
  elif [ "$legacy_migration" = true ]; then
    SM_TRUSTED_STOP_CLIENT=
    for config_real in "$SM_SYSTEM_DIR/magisk" "$SM_SYSTEM_DIR/magisk64" "$SM_SYSTEM_DIR/magisk32"; do
      if [ -f "$config_real" ] && [ ! -L "$config_real" ] && [ -x "$config_real" ]; then
        SM_TRUSTED_STOP_CLIENT="$config_real"
        break
      fi
    done
  else
    SM_TRUSTED_STOP_CLIENT=
  fi
  [ -f "$SM_ARTIFACT_PATH" ] || { sm_log "! Exact install artifact is unavailable"; return 1; }
  SM_ARTIFACT_SHA256="$(sm_sha256_file "$SM_ARTIFACT_PATH")" || return 1
  case "$SM_ARTIFACT_SHA256" in *[!a-f0-9]*|'') sm_log "! Cannot hash exact install artifact"; return 1 ;; esac
  [ "${#SM_ARTIFACT_SHA256}" -eq 64 ] || { sm_log "! Invalid install artifact digest"; return 1; }
  [ "$SM_ARTIFACT_SHA256" = "$SM_AUTH_ARTIFACT_SHA256" ] || {
    sm_log "! Recovery authorization belongs to different manager APK bytes"
    return 1
  }
  # Revalidate the external backup and exact host-instance identity through a
  # one-shot connection scoped to this live ADB transport. A copied guest
  # authorization cannot replay the host nonce. Everything above is read-only;
  # consume the authorization before the first write probe.
  sm_validate_host_lease || return 1
  sm_consume_authorization || return 1
  if [ "$pr5b_upgrade" = true ]; then
    sm_migrate_pr5b_receipt || {
      sm_log "! PR5B receipt integrity binding failed; no boot payload was changed"
      return 1
    }
    sm_validate_installed_state || {
      sm_log "! Migrated PR5B receipt did not pass current integrity validation"
      return 1
    }
  fi
  init_real="$(sm_real_path "$SM_AUTH_INIT_DIRECTORY")" || return 1
  sm_probe_writable_directory "$init_real" || {
    sm_log "! Authorized init directory is not durably writable"
    return 1
  }
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
  SM_ACTIVE_PAYLOAD="$SM_SYSTEM_PAYLOAD_PREFIX/$SM_TRANSACTION_ID"
  SM_RESCUE_PAYLOAD="$SM_RESCUE_PAYLOAD_PREFIX/$SM_TRANSACTION_ID"
  SM_COMMIT_BOOT_ID=
  SM_BOOT_ATTEMPT_ID=
  SM_MANIFEST_SHA256="$(printf '%064d' 0)"
  SM_OWNERSHIP_SHA256="$(printf '%064d' 0)"
  if [ -f "$SM_ORIGINAL_FILE" ]; then
    SM_ORIGINALS_SHA256="$(sm_sha256_file "$SM_ORIGINAL_FILE")" || return 1
  else
    SM_ORIGINALS_SHA256="$(printf '%064d' 0)"
  fi
  if [ -f "$SM_SECURE_DIR_METADATA" ]; then
    SM_SECURE_DIR_SHA256="$(sm_sha256_file "$SM_SECURE_DIR_METADATA")" || return 1
  else
    SM_SECURE_DIR_SHA256="$(printf '%064d' 0)"
  fi
  if sm_path_present "$SM_STATE_DIR/rollback"; then
    [ -d "$SM_STATE_DIR/rollback" ] && [ ! -L "$SM_STATE_DIR/rollback" ] || return 1
    [ -z "$("$SM_BB" find "$SM_STATE_DIR/rollback" -mindepth 1 -maxdepth 1 -print)" ] || {
      sm_log "! Unmarked rollback storage is not safe to replace"
      return 1
    }
    "$SM_BB" rmdir "$SM_STATE_DIR/rollback" || return 1
  fi
  # The setup marker is the first durable transaction byte outside the APK's
  # one-shot authorization. Validate its parent before publishing anything
  # into a security-sensitive root namespace.
  sm_validate_secure_dir_base || return 1
  sm_publish_setup_marker || return 1
  "$SM_BB" mkdir -p "$SM_ROLLBACK_DIR" || return 1
  sm_snapshot_state_metadata || return 1
  SM_STATE=PREFLIGHTED
  sm_write_transaction || return 1
  "$SM_BB" rm -f "$SM_SETUP_MARKER" || return 1
  sm_fsync /data/adb || return 1
  sm_failpoint preflighted || return 1
  sm_prepare_secure_dir_metadata || return 1
  sm_prepare_originals || return 1
  SM_ORIGINALS_SHA256="$(sm_sha256_file "$SM_ORIGINAL_FILE")" || return 1
  SM_SECURE_DIR_SHA256="$(sm_sha256_file "$SM_SECURE_DIR_METADATA")" || return 1
  # Bind a newly created or one-time migrated original inventory before any
  # boot-critical mutation. PREFLIGHT recovery remains able to restore the
  # prior metadata snapshot if power is lost on this publication boundary.
  sm_write_transaction || return 1
  sm_quiesce_magisk || return 1
  sm_assert_mutable_namespaces_idle || return 1
  sm_snapshot_all || return 1
  sm_assert_magisk_quiesced || return 1
  sm_assert_mutable_namespaces_idle || return 1
  sm_initialize_journal || return 1
  sm_update_state STAGED || return 1
  sm_failpoint staged
}

sm_restore_legacy_bootanim() {
  local target compressed staged
  [ "$SM_LEGACY_MIGRATION" = true ] || return 0
  target="$(sm_real_path /system/etc/init/bootanim.rc)" || return 1
  compressed="$target.gz"
  if [ ! -f "$compressed" ]; then
    if [ -f "$target" ] && "$SM_BB" grep -Eq 'magiskpolicy|--post-fs-data|--setup-sbin' "$target"; then
      sm_log "! Legacy bootanim.rc injection has no restorable stock sidecar"
      return 1
    fi
    return 0
  fi
  staged="$target.kitsune-stock-new"
  "$SM_BB" gzip -cdf "$compressed" >"$staged" || return 1
  "$SM_BB" chmod --reference="$target" "$staged" 2>/dev/null || "$SM_BB" chmod 0644 "$staged"
  "$SM_BB" chown --reference="$target" "$staged" 2>/dev/null || "$SM_BB" chown 0:0 "$staged"
  chcon --reference="$target" "$staged" 2>/dev/null || true
  sm_atomic_publish "$staged" "$target" legacy-init-restored || return 1
}

sm_restore_legacy_policy() {
  local path existed digest size mode uid gid context backup real source staged actual
  [ "$SM_POLICY_MUTATED" = true ] || return 0
  [ -n "$SM_POLICY_PATH" ] || return 0
  [ -f "$SM_ORIGINAL_FILE" ] || return 1
  while IFS="$SM_TAB" read -r path existed digest size mode uid gid context backup; do
    [ "$path" = "$SM_POLICY_PATH" ] || continue
    [ "$existed" = true ] || return 1
    source="$SM_STATE_DIR/$backup"
    sm_path_present "$source" || return 1
    [ "$(sm_digest_path "$source")" = "$digest" ] || return 1
    real="$(sm_real_path "$SM_POLICY_PATH")" || return 1
    staged="$real.kitsune-original-new"
    "$SM_BB" cp -a "$source" "$staged" || return 1
    "$SM_BB" chmod "${mode#0}" "$staged" || return 1
    "$SM_BB" chown "$uid:$gid" "$staged" || return 1
    [ "$context" = - ] || chcon "$context" "$staged" 2>/dev/null || return 1
    actual="$(sm_digest_path "$staged")"
    [ "$actual" = "$digest" ] || { "$SM_BB" rm -f "$staged"; return 1; }
    sm_atomic_publish "$staged" "$real" legacy-policy-restored || return 1
    sm_log "- Restored the recorded stock policy before live-policy activation"
    return 0
  done <"$SM_ORIGINAL_FILE"
  return 1
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
  local payload_real runtime_real init_real policy_real addon_real rescue_real rescue_rc_real
  : >"$SM_OWNERSHIP_FILE.new" || return 1
  payload_real="$(sm_real_path "$SM_SYSTEM_DIR")" || return 1
  runtime_real=/data/adb/magisk
  sm_collect_owned_tree "$SM_SYSTEM_DIR" "$payload_real" || return 1
  sm_collect_owned_tree /data/adb/magisk "$runtime_real" || return 1
  init_real="$(sm_real_path "$SM_INIT_PATH")" || return 1
  sm_add_owned_file "$SM_INIT_PATH" "$init_real" || return 1
  rescue_real="$(sm_real_path "$SM_RESCUE_DIR")" || return 1
  rescue_rc_real="$(sm_real_path "$SM_RESCUE_RC")" || return 1
  sm_collect_owned_tree "$SM_RESCUE_DIR" "$rescue_real" || return 1
  sm_add_owned_file "$SM_RESCUE_RC" "$rescue_rc_real" || return 1
  if [ -n "$SM_POLICY_PATH" ] && [ "$SM_POLICY_MUTATED" = true ]; then
    policy_real="$(sm_real_path "$SM_POLICY_PATH")" || return 1
    sm_add_owned_file "$SM_POLICY_PATH" "$policy_real" || return 1
  fi
  addon_real="$(sm_real_path /system/addon.d/99-magisk.sh)" || return 1
  sm_add_owned_file /system/addon.d/99-magisk.sh "$addon_real" || return 1
  sm_atomic_publish "$SM_OWNERSHIP_FILE.new" "$SM_OWNERSHIP_FILE" ownership-inventory || return 1
  SM_OWNERSHIP_SHA256="$(sm_sha256_file "$SM_OWNERSHIP_FILE")" || return 1
  sm_validate_ownership_inventory
}

sm_snapshot_path() {
  local wanted="$1" label suffix=
  case "$wanted" in
    "$SM_INIT_PATH") label=init_rc ;;
    "$SM_SYSTEM_DIR.rc") label=legacy_rc ;;
    "$SM_POLICY_PATH") [ -n "$SM_POLICY_PATH" ] || return 1; label=policy ;;
    "$SM_POLICY_PATH.gz") [ -n "$SM_POLICY_PATH" ] || return 1; label=policy_gz ;;
    /system/etc/init/bootanim.rc) label=bootanim ;;
    /system/etc/init/bootanim.rc.gz) label=bootanim_gz ;;
    /data/adb/magisk) label=runtime ;;
    /data/adb/magisk/*) label=runtime; suffix="${wanted#/data/adb/magisk}" ;;
    /system/addon.d/99-magisk.sh) label=addon_script ;;
    /system/addon.d/magisk) label=addon_dir ;;
    /system/addon.d/magisk/*) label=addon_dir; suffix="${wanted#/system/addon.d/magisk}" ;;
    "$SM_SYSTEM_DIR") label=payload ;;
    "$SM_SYSTEM_DIR"/*) label=payload; suffix="${wanted#"$SM_SYSTEM_DIR"}" ;;
    "$SM_RESCUE_RC") label=rescue_rc ;;
    "$SM_RESCUE_DIR") label=rescue_dir ;;
    "$SM_RESCUE_DIR"/*) label=rescue_dir; suffix="${wanted#"$SM_RESCUE_DIR"}" ;;
    *) return 1 ;;
  esac
  printf '%s/%s/data%s\n' "$SM_ROLLBACK_DIR" "$label" "$suffix"
}

sm_journal_has_target() {
  "$SM_BB" awk -F '\t' -v target="$1" '$4 == target { found=1 } END { exit !found }' "$SM_JOURNAL_FILE.new"
}

sm_journal_append() {
  local boundary="$1" operation="$2" path="$3" before="$4" after="$5" committed="${6:-true}" sequence
  sm_valid_single_line "$path" || return 1
  sm_journal_has_target "$path" && return 0
  sequence="$($SM_BB awk 'END { print NR + 1 }' "$SM_JOURNAL_FILE.new")" || return 1
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$sequence" "$boundary" "$operation" "$path" "$before" "$after" "$committed" >>"$SM_JOURNAL_FILE.new"
}

sm_journal_plan_path() {
  local boundary="$1" requested_operation="$2" path="$3" source before=- operation
  operation="$requested_operation"
  source="$(sm_snapshot_path "$path" 2>/dev/null)" || source=
  if [ -n "$source" ] && sm_path_present "$source"; then before="$(sm_digest_path "$source")" || return 1; fi
  if [ "$operation" = auto ]; then
    if [ "$before" = - ]; then operation=create; else operation=replace; fi
  elif [ "$operation" = remove ] && [ "$before" = - ]; then
    return 0
  fi
  sm_journal_append "$boundary" "$operation" "$path" "$before" - false
}

sm_initialize_journal() {
  : >"$SM_JOURNAL_FILE.new" || return 1
  [ "$SM_POLICY_MUTATED" != true ] || \
    sm_journal_plan_path legacy-policy-restored replace "$SM_POLICY_PATH" || return 1
  [ "$SM_LEGACY_MIGRATION" != true ] || \
    sm_journal_plan_path legacy-init-restored replace /system/etc/init/bootanim.rc || return 1
  if [ "$SM_LEGACY_MIGRATION" = true ] && [ "$SM_SYSTEM_DIR.rc" != "$SM_INIT_PATH" ]; then
    sm_journal_plan_path legacy-rc-removed remove "$SM_SYSTEM_DIR.rc" || return 1
  fi
  if [ -n "${SM_RESCUE_PAYLOAD:-}" ]; then
    sm_journal_plan_path rescue-version-published create "$SM_RESCUE_PAYLOAD" || return 1
    sm_journal_plan_path rescue-init-published auto "$SM_RESCUE_RC" || return 1
    sm_journal_plan_path rescue-superseded-removed auto "$SM_RESCUE_DIR" || return 1
  fi
  sm_journal_plan_path version-published create "$SM_ACTIVE_PAYLOAD" || return 1
  sm_journal_plan_path init-published auto "$SM_INIT_PATH" || return 1
  sm_journal_plan_path superseded-payload-removed auto "$SM_SYSTEM_DIR" || return 1
  sm_journal_plan_path runtime-published auto /data/adb/magisk || return 1
  sm_journal_plan_path legacy-addon-removed remove /system/addon.d/99-magisk.sh || return 1
  sm_journal_plan_path legacy-addon-removed remove /system/addon.d/magisk || return 1
  [ "$SM_LEGACY_MIGRATION" != true ] || \
    sm_journal_plan_path legacy-sidecar-removed remove /system/etc/init/bootanim.rc.gz || return 1
  if [ "$SM_LEGACY_MIGRATION" = true ] &&
     [ "$SM_POLICY_MUTATED" = true ] && [ -n "$SM_POLICY_PATH" ]; then
    sm_journal_plan_path legacy-sidecar-removed remove "$SM_POLICY_PATH.gz" || return 1
  fi
  sm_journal_plan_path manifest-published auto "$SM_SYSTEM_DIR/install-manifest.json" || return 1
  sm_atomic_publish "$SM_JOURNAL_FILE.new" "$SM_JOURNAL_FILE" journal-plan-published
}

sm_journal_mark() {
  local boundary="$1" path="$2" real after=- staged="$SM_JOURNAL_FILE.new" result
  [ -f "$SM_JOURNAL_FILE" ] || return 1
  "$SM_BB" awk -F '\t' -v boundary="$boundary" -v target="$path" \
    '$2 == boundary && $4 == target { found=1; exit } END { exit !found }' \
    "$SM_JOURNAL_FILE" || return 0
  real="$(sm_real_path "$path")" || return 1
  if sm_path_present "$real"; then after="$(sm_digest_path "$real")" || return 1; fi
  "$SM_BB" awk -F '\t' -v OFS='\t' -v boundary="$boundary" -v target="$path" -v after="$after" '
    $2 == boundary && $4 == target {
      $6=after
      $7="true"
      found=1
    }
    { print }
    END { if (!found) exit 2 }
  ' "$SM_JOURNAL_FILE" >"$staged"
  result=$?
  if [ "$result" = 2 ]; then
    "$SM_BB" rm -f "$staged"
    return 0
  fi
  [ "$result" = 0 ] || { "$SM_BB" rm -f "$staged"; return 1; }
  "$SM_BB" chmod 0600 "$staged" || return 1
  sm_fsync "$staged" || return 1
  "$SM_BB" mv -f "$staged" "$SM_JOURNAL_FILE" || return 1
  sm_fsync "$SM_JOURNAL_FILE" "$SM_STATE_DIR" || return 1
  return 0
}

sm_journal_owned() {
  local wanted_prefix="$1" boundary="$2" path digest size mode uid gid context kind source before operation
  while IFS="$SM_TAB" read -r path digest size mode uid gid context kind; do
    [ -n "$path" ] || continue
    case "$path" in "$wanted_prefix"|"$wanted_prefix"/*) ;; *) continue ;; esac
    source="$(sm_snapshot_path "$path" 2>/dev/null)" || source=
    if [ -n "$source" ] && sm_path_present "$source"; then
      before="$(sm_digest_path "$source")" || return 1
      operation=replace
    else
      before=-
      operation=create
    fi
    sm_journal_append "$boundary" "$operation" "$path" "$before" "$digest" || return 1
  done <"$SM_OWNERSHIP_FILE"
}

sm_journal_path_delta() {
  local boundary="$1" path="$2" source real before=- after=- operation
  source="$(sm_snapshot_path "$path" 2>/dev/null)" || source=
  real="$(sm_real_path "$path")" || return 1
  if [ -n "$source" ] && sm_path_present "$source"; then
    before="$(sm_digest_path "$source")" || return 1
  fi
  if sm_path_present "$real"; then
    after="$(sm_digest_path "$real")" || return 1
  fi
  if [ "$before" != - ] && [ "$after" != - ]; then
    operation=replace
  elif [ "$before" != - ]; then
    operation=remove
  elif [ "$after" != - ]; then
    operation=create
  else
    return 0
  fi
  sm_journal_append "$boundary" "$operation" "$path" "$before" "$after"
}

sm_journal_removed_tree() {
  local label="$1" canonical_root="$2" boundary="$3" source list item relative canonical before real after operation
  source="$SM_ROLLBACK_DIR/$label/data"
  [ -d "$source" ] || return 0
  list="$SM_STATE_DIR/.journal-removals.new"
  (
    cd "$source" || exit 1
    "$SM_BB" find . \( -type f -o -type l \) -print | "$SM_BB" sort
  ) >"$list" || return 1
  while IFS= read -r item; do
    relative="${item#./}"
    canonical="$canonical_root/$relative"
    [ "$canonical" != "$SM_SYSTEM_DIR/install-manifest.json" ] || continue
    sm_journal_has_target "$canonical" && continue
    before="$(sm_digest_path "$source/$relative")" || return 1
    real="$(sm_real_path "$canonical")" || return 1
    if sm_path_present "$real"; then
      after="$(sm_digest_path "$real")" || return 1
      operation=replace
    else
      after=-
      operation=remove
    fi
    sm_journal_append "$boundary" "$operation" "$canonical" "$before" "$after" || return 1
  done <"$list"
  "$SM_BB" rm -f "$list" || return 1
}

sm_generate_journal() {
  local path digest size mode uid gid context kind before source operation
  local manifest="$SM_SYSTEM_DIR/install-manifest.json"
  : >"$SM_JOURNAL_FILE.new" || return 1

  [ "$SM_POLICY_MUTATED" != true ] || sm_journal_path_delta legacy-policy-restored "$SM_POLICY_PATH" || return 1
  [ "$SM_LEGACY_MIGRATION" != true ] || sm_journal_path_delta legacy-init-restored /system/etc/init/bootanim.rc || return 1
  if [ "$SM_LEGACY_MIGRATION" = true ] && [ "$SM_SYSTEM_DIR.rc" != "$SM_INIT_PATH" ]; then
    sm_journal_path_delta legacy-rc-removed "$SM_SYSTEM_DIR.rc" || return 1
  fi
  if [ -n "${SM_RESCUE_PAYLOAD:-}" ]; then
    sm_journal_owned "$SM_RESCUE_PAYLOAD" rescue-version-published || return 1
    sm_journal_owned "$SM_RESCUE_RC" rescue-init-published || return 1
    sm_journal_removed_tree rescue_dir "$SM_RESCUE_DIR" rescue-superseded-removed || return 1
  fi
  sm_journal_owned "$SM_ACTIVE_PAYLOAD" version-published || return 1
  sm_journal_owned "$SM_INIT_PATH" init-published || return 1
  sm_journal_removed_tree payload "$SM_SYSTEM_DIR" superseded-payload-removed || return 1
  sm_journal_owned /data/adb/magisk runtime-published || return 1
  sm_journal_removed_tree runtime /data/adb/magisk runtime-published || return 1
  sm_journal_path_delta legacy-addon-removed /system/addon.d/99-magisk.sh || return 1
  sm_journal_path_delta legacy-addon-removed /system/addon.d/magisk || return 1
  [ "$SM_LEGACY_MIGRATION" != true ] || sm_journal_path_delta legacy-sidecar-removed /system/etc/init/bootanim.rc.gz || return 1
  if [ "$SM_LEGACY_MIGRATION" = true ] &&
     [ "$SM_POLICY_MUTATED" = true ] && [ -n "$SM_POLICY_PATH" ]; then
    sm_journal_path_delta legacy-sidecar-removed "$SM_POLICY_PATH.gz" || return 1
  fi

  # Any future owned path not covered by the ordered roots above must still be
  # represented instead of silently falling out of the manifest journal.
  while IFS="$SM_TAB" read -r path digest size mode uid gid context kind; do
    [ -n "$path" ] || continue
    sm_journal_has_target "$path" && continue
    before=-
    source="$(sm_snapshot_path "$path" 2>/dev/null)" || source=
    if [ -n "$source" ] && sm_path_present "$source"; then before="$(sm_digest_path "$source")" || return 1; fi
    if [ "$before" = - ]; then operation=create; else operation=replace; fi
    sm_journal_append publish "$operation" "$path" "$before" "$digest" || return 1
  done <"$SM_OWNERSHIP_FILE"

  source="$(sm_snapshot_path "$manifest" 2>/dev/null)" || source=
  if [ -n "$source" ] && sm_path_present "$source"; then
    before="$(sm_digest_path "$source")" || return 1
    operation=replace
  else
    before=-
    operation=create
  fi
  # A manifest cannot contain its own digest without a circular fixed point.
  # Its durable publication boundary and the byte-identical state copy provide
  # the commit proof, so after_sha256 is intentionally null for this one row.
  sm_journal_append manifest-published "$operation" "$manifest" "$before" - || return 1
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
    printf '  "product": {"name": "KitsuneMagisk", "version": "%s", "version_code": %s, "source_commit": "%s", "upstream_base": "%s", "artifact_sha256": "%s"},\n' \
      "$(sm_json_escape "$SM_PRODUCT_VERSION")" "$SM_VERSION_CODE" "$SM_SOURCE_COMMIT" "$SM_UPSTREAM_BASE" "$SM_ARTIFACT_SHA256"
    printf '  "target": {"adapter_id": "%s", "authorization_id": "%s", "target_contract_sha256": "%s", "authorization_boot_id_sha256": "%s", "probe_sha256": "%s", "qualification_sha256": "%s", "instance_identity_sha256": "%s", "serial_sha256": "%s", "fingerprint_sha256": "%s", "api": %s, "abis": [' \
      "$(sm_json_escape "$SM_ADAPTER_ID")" "$SM_AUTHORIZATION_ID" "$SM_TARGET_CONTRACT_SHA256" "$SM_AUTH_BOOT_ID_SHA256" "$SM_PROBE_SHA256" "$SM_QUALIFICATION_SHA256" "$SM_INSTANCE_IDENTITY_SHA256" "$SM_SERIAL_SHA256" "$SM_FINGERPRINT_SHA256" "$(getprop ro.build.version.sdk)"
    first=true
    old_ifs="$IFS"; IFS=,
    for abi in $SM_TARGET_ABIS; do
      [ "$first" = true ] || printf ', '
      printf '"%s"' "$(sm_json_escape "$abi")"
      first=false
    done
    IFS="$old_ifs"
    printf ']},\n'
    printf '  "strategies": {"init": "%s", "selinux": "%s:%s", "policy_mutated": %s, "legacy_migration": %s, "runtime_tmpfs": "%s", "preinit_device": ' \
      "$(sm_json_escape "$SM_INIT_PATH")" "$(sm_json_escape "$SM_SELINUX_STRATEGY")" "$(sm_json_escape "$SM_POLICY_SOURCE")" "$SM_POLICY_MUTATED" "$SM_LEGACY_MIGRATION" "$(sm_json_escape "$SM_RUNTIME_PATH")"
    sm_json_value_or_null "$SM_PREINIT_DEVICE"
    printf ', "preinit_directory": '
    sm_json_value_or_null "$SM_PREINIT_DIR"
    printf ', "active_payload": "%s", "rescue_payload": "%s"},\n' \
      "$(sm_json_escape "$SM_ACTIVE_PAYLOAD")" "$(sm_json_escape "$SM_RESCUE_PAYLOAD")"
    printf '  "ownership_inventory_sha256": "%s",\n' "$SM_OWNERSHIP_SHA256"
    printf '  "originals_inventory_sha256": "%s",\n' "$SM_ORIGINALS_SHA256"
    printf '  "secure_dir_metadata_sha256": "%s",\n' "$SM_SECURE_DIR_SHA256"
    printf '  "mutable_namespaces": ["/data/adb/magisk.db", "/data/adb/magisk.db-wal", "/data/adb/magisk.db-shm", "/data/adb/modules", "/data/adb/modules_update", "/data/adb/post-fs-data.d", "/data/adb/service.d", "/data/adb/sepolicy.rule", "/cache/magisk.log", "/cache/magisk.log.bak"],\n'
    printf '  "payload": [\n'
    first=true
    while IFS="$SM_TAB" read -r path digest size mode uid gid context kind; do
      [ -n "$path" ] || continue
      [ "$first" = true ] || printf ',\n'
      printf '    {"path": "%s", "sha256": "%s", "size": %s, "mode": "%s", "uid": %s, "gid": %s, "selinux_context": ' \
        "$(sm_json_escape "$path")" "$digest" "$size" "$mode" "$uid" "$gid"
      sm_json_value_or_null "$context"
      printf ', "kind": "%s"}' "$kind"
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
  local path real paths=
  if [ "$SM_LEGACY_MIGRATION" = true ]; then
    paths=/system/etc/init/bootanim.rc.gz
    if [ "$SM_POLICY_MUTATED" = true ] && [ -n "$SM_POLICY_PATH" ]; then
      paths="$paths $SM_POLICY_PATH.gz"
    fi
  fi
  for path in $paths; do
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
  sm_collect_ownership || return 1
  sm_generate_journal || return 1
  sm_generate_manifest || return 1
  SM_MANIFEST_SHA256="$(sm_sha256_file "$SM_STATE_DIR/.install-manifest.json.new")" || return 1
  sm_write_transaction || return 1
  manifest_real="$(sm_real_path "$SM_SYSTEM_DIR/install-manifest.json")" || return 1
  staged_manifest="$(sm_parent "$manifest_real")/.install-manifest.json.new"
  "$SM_BB" cp -a "$SM_STATE_DIR/.install-manifest.json.new" "$staged_manifest" || return 1
  sm_atomic_publish "$staged_manifest" "$manifest_real" manifest-published || return 1
  "$SM_BB" cp -a "$manifest_real" "$SM_STATE_DIR/.install-manifest.copy.new" || return 1
  sm_atomic_publish "$SM_STATE_DIR/.install-manifest.copy.new" "$SM_MANIFEST_COPY" manifest-copy-published || return 1
  sm_verify_manifest_copies || return 1
  # Neither durable manifest depends on its construction copy after both
  # publications have reached disk. Remove all staging names before exposing
  # COMMITTED so a completed transaction is distinguishable from an interrupted
  # one without relying on a later install or boot to clean it up.
  sm_cleanup_staging || return 1
  SM_COMMIT_BOOT_ID="$(sm_current_boot_id)"
  sm_valid_uuid "$SM_COMMIT_BOOT_ID" || {
    sm_log "! Unable to record the installation boot identity"
    return 1
  }
  sm_update_state COMMITTED || return 1
  sm_failpoint committed || return 1
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
  sm_atomic_publish "$SM_STATE_DIR/.install-manifest.copy.new" "$SM_MANIFEST_COPY" "manifest-copy-state:$state" || return 1
  SM_MANIFEST_SHA256="$(sm_sha256_file "$manifest_real")" || return 1
  sm_verify_manifest_copies
}

sm_verify_boot() {
  local boot_id staged running_code client_code database_version verify_state root_identity root_digest
  sm_load_transaction || return 1
  case "$SM_STATE" in COMMITTED|BOOT_VERIFIED) ;; *) return 1 ;; esac
  verify_state="$SM_STATE"
  if ! sm_verify_manifest_copies; then
    if [ "$verify_state" = COMMITTED ]; then sm_update_state ROLLBACK_REQUIRED; else sm_update_state FAILED; fi
    return 1
  fi
  if ! sm_verify_owned; then
    if [ "$verify_state" = COMMITTED ]; then
      sm_update_state ROLLBACK_REQUIRED
    else
      sm_update_state FAILED
    fi
    sm_log "! System Mode boot verification failed"
    return 1
  fi
  [ -x "$SM_RUNTIME_PATH/magisk" ] || {
    sm_log "! System Mode runtime was not populated at $SM_RUNTIME_PATH"
    if [ "$verify_state" = COMMITTED ]; then sm_update_state ROLLBACK_REQUIRED; else sm_update_state FAILED; fi
    return 1
  }
  [ "$(getprop kitsune.system_mode.post_fs_data.ready)" = "$SM_INSTALL_ID" ] || {
    sm_log "! System Mode post-fs-data stage did not complete successfully"
    if [ "$verify_state" = COMMITTED ]; then sm_update_state ROLLBACK_REQUIRED; else sm_update_state FAILED; fi
    return 1
  }
  [ "$(getprop kitsune.system_mode.service.ready)" = "$SM_INSTALL_ID" ] || {
    sm_log "! System Mode service stage did not complete at the upstream boot trigger"
    if [ "$verify_state" = COMMITTED ]; then sm_update_state ROLLBACK_REQUIRED; else sm_update_state FAILED; fi
    return 1
  }
  [ "$(getprop kitsune.system_mode.boot_complete.ready)" = "$SM_INSTALL_ID" ] || {
    sm_log "! System Mode boot-complete stage did not complete successfully"
    if [ "$verify_state" = COMMITTED ]; then sm_update_state ROLLBACK_REQUIRED; else sm_update_state FAILED; fi
    return 1
  }
  running_code="$("$SM_BB" timeout 15 "$SM_RUNTIME_PATH/magisk" -V 9>&- 2>/dev/null)"
  [ "$SM_VERSION_CODE" = 0 ] || [ "$running_code" = "$SM_VERSION_CODE" ] || {
    sm_log "! System Mode daemon version does not match the committed payload"
    if [ "$verify_state" = COMMITTED ]; then sm_update_state ROLLBACK_REQUIRED; else sm_update_state FAILED; fi
    return 1
  }
  # Root can bypass directory permissions. Also prove an ordinary UID can
  # reach the socket, without requiring or changing that UID's superuser policy.
  client_code="$("$SM_BB" timeout 15 "$SM_BB" setuidgid 2000 "$SM_RUNTIME_PATH/magisk" -V 9>&- 2>/dev/null)" || client_code=
  [ -n "$client_code" ] && [ "$client_code" = "$running_code" ] || {
    sm_log "! System Mode daemon is inaccessible to an unprivileged client"
    if [ "$verify_state" = COMMITTED ]; then sm_update_state ROLLBACK_REQUIRED; else sm_update_state FAILED; fi
    return 1
  }
  database_version="$("$SM_BB" timeout 15 "$SM_RUNTIME_PATH/magisk" --sqlite 'PRAGMA user_version' 9>&- 2>/dev/null)" || database_version=
  [ "$database_version" = user_version=12 ] || {
    sm_log "! System Mode daemon could not open a supported user database"
    if [ "$verify_state" = COMMITTED ]; then sm_update_state ROLLBACK_REQUIRED; else sm_update_state FAILED; fi
    return 1
  }
  root_identity="$("$SM_BB" timeout 15 "$SM_RUNTIME_PATH/su" -c /system/bin/id 9>&- 2>/dev/null)" || {
    sm_log "! System Mode daemon did not complete a root request"
    if [ "$verify_state" = COMMITTED ]; then sm_update_state ROLLBACK_REQUIRED; else sm_update_state FAILED; fi
    return 1
  }
  case "$root_identity" in *uid=0*) ;; *)
    sm_log "! System Mode root request returned an unexpected identity"
    if [ "$verify_state" = COMMITTED ]; then sm_update_state ROLLBACK_REQUIRED; else sm_update_state FAILED; fi
    return 1
    ;;
  esac
  root_digest="$(printf '%s' "$root_identity" | "$SM_BB" sha256sum | "$SM_BB" awk '{ print $1 }')" || return 1
  sm_valid_hex "$root_digest" 64 || return 1
  if [ "$SM_STATE" = BOOT_VERIFIED ]; then
    # BOOT_VERIFIED is durable before rollback storage is removed. Retrying
    # verification must therefore finish that terminal cleanup instead of
    # leaving a harmless-but-blocking snapshot forever after a power cut.
    sm_cleanup_terminal_rollback
    return
  fi
  boot_id="$(sm_current_boot_id)"
  [ -n "$boot_id" ] || { sm_log "! Unable to read the boot identity"; return 1; }
  [ "$boot_id" != "$SM_COMMIT_BOOT_ID" ] || { sm_log "! A new boot is required before verification"; return 1; }
  [ "$boot_id" = "$SM_BOOT_ATTEMPT_ID" ] || {
    sm_log "! Boot verification was not registered by the committed launcher"
    sm_update_state ROLLBACK_REQUIRED
    return 1
  }
  staged="$SM_STATE_DIR/.boot-verified.env.new"
  {
    printf 'SCHEMA_VERSION=1\n'
    printf 'INSTALL_ID=%s\n' "$SM_INSTALL_ID"
    printf 'BOOT_ID=%s\n' "$boot_id"
    printf 'INIT_PATH=%s\n' "$SM_INIT_PATH"
    printf 'RUNTIME_PATH=%s\n' "$SM_RUNTIME_PATH"
    printf 'DAEMON_VERSION_CODE=%s\n' "$running_code"
    printf 'ROOT_IDENTITY_SHA256=%s\n' "$root_digest"
    printf 'POST_FS_DATA_STAGE_ID=%s\n' "$SM_INSTALL_ID"
    printf 'SERVICE_STAGE_ID=%s\n' "$SM_INSTALL_ID"
    printf 'BOOT_COMPLETE_STAGE_ID=%s\n' "$SM_INSTALL_ID"
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
  sm_cleanup_terminal_rollback || sm_log "W: Boot-verified rollback cleanup is incomplete"
  return 0
}

sm_restore_originals() {
  local scope="${1:-ordinary}" path existed digest size mode uid gid context backup real source actual label failed=0
  while IFS="$SM_TAB" read -r path existed digest size mode uid gid context backup; do
    [ -n "$path" ] || continue
    case "$path" in
      "$SM_SYSTEM_DIR"|"$SM_SYSTEM_DIR.rc"|"$SM_INIT_PATH"|"$SM_RESCUE_RC"|"$SM_RESCUE_DIR"|"$SM_POLICY_PATH"|/system/etc/init/bootanim.rc|/data/adb/magisk|/data/adb/magisk.db|/data/adb/magisk.db-wal|/data/adb/magisk.db-shm|/data/adb/modules|/data/adb/modules_update|/data/adb/post-fs-data.d|/data/adb/service.d|/data/adb/sepolicy.rule|/cache/magisk.log|/cache/magisk.log.bak|/system/addon.d/99-magisk.sh|/system/addon.d/magisk) ;;
      *) sm_log "! Refusing unrecognized original path $path"; return 1 ;;
    esac
    case "$path" in
      "$SM_RESCUE_RC"|"$SM_RESCUE_DIR") [ "$scope" = rescue ] || continue ;;
      *) [ "$scope" != rescue ] || continue ;;
    esac
    # Modules, policy choices and user scripts belong to the user, not the
    # installation. Restore their snapshot only when rolling back a failed
    # transaction; successful uninstall must preserve subsequent user changes.
    case "$path" in
      /data/adb/magisk.db|/data/adb/magisk.db-wal|/data/adb/magisk.db-shm|\
      /data/adb/modules|/data/adb/modules_update|/data/adb/post-fs-data.d|\
      /data/adb/service.d|/data/adb/sepolicy.rule|/cache/magisk.log|/cache/magisk.log.bak)
        continue
        ;;
    esac
    real="$(sm_real_path "$path")" || return 1
    if [ "$path" = /system/etc/init/bootanim.rc ]; then
      continue
    fi
    if [ "$path" = "$SM_POLICY_PATH" ] && [ "$SM_POLICY_MUTATED" != true ]; then
      continue
    fi
    label="${backup#original/}"
    label="${label%/data}"
    [ "original/$label/data" = "$backup" ] || { failed=1; break; }
    sm_destructive_target_safe "$label" "$real" || { failed=1; break; }
    sm_assert_owned_restore_target "$path" "$real" || { failed=1; break; }
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

sm_cleanup_uninstalled_metadata() {
  local include_originals="${1:-false}"
  "$SM_BB" rm -f "$SM_MANIFEST_COPY" "$SM_BOOT_PROOF" || return 1
  if [ "$include_originals" = true ]; then
    "$SM_BB" rm -f "$SM_OWNERSHIP_FILE" "$SM_JOURNAL_FILE" "$SM_ORIGINAL_FILE" \
      "$SM_SECURE_DIR_METADATA" || return 1
    sm_managed_path_safe "Original-backup cleanup target" \
      "$SM_STATE_DIR/original" "$SM_STATE_DIR/original" true || return 1
    "$SM_BB" rm -rf "$SM_STATE_DIR/original" || return 1
  fi
  sm_fsync "$SM_STATE_DIR"
}

sm_finalize_uninstalled_state() {
  local residue
  [ "$SM_STATE" = UNINSTALLED ] || return 1
  ! sm_path_present "$SM_SETUP_MARKER" && ! sm_path_present "$SM_SETUP_MARKER.new" &&
    ! sm_path_present "$SM_ROLLBACK_TERMINAL" && ! sm_path_present "$SM_ROLLBACK_TERMINAL.new" &&
    ! sm_path_present "$SM_PRIOR_TRANSACTION" && ! sm_path_present "$SM_PRIOR_TRANSACTION.new" || return 1
  ! sm_path_present "$SM_ROLLBACK_DIR" || return 1
  [ -f "$SM_TRANSACTION_FILE" ] && [ ! -L "$SM_TRANSACTION_FILE" ] || return 1
  residue="$($SM_BB find "$SM_STATE_DIR" -mindepth 1 -maxdepth 1 \
    ! -name transaction.env ! -name rollback -print)" || return 1
  [ -z "$residue" ] || return 1
  if sm_path_present "$SM_STATE_DIR/rollback"; then
    [ -d "$SM_STATE_DIR/rollback" ] && [ ! -L "$SM_STATE_DIR/rollback" ] || return 1
    [ -z "$($SM_BB find "$SM_STATE_DIR/rollback" -mindepth 1 -print)" ] || return 1
    "$SM_BB" rmdir "$SM_STATE_DIR/rollback" || return 1
  fi
  "$SM_BB" rm -f "$SM_TRANSACTION_FILE" || return 1
  sm_fsync "$SM_STATE_DIR" /data/adb || return 1
  "$SM_BB" rmdir "$SM_STATE_DIR" 2>/dev/null || true
  "$SM_BB" rmdir /data/adb/kitsune 2>/dev/null || true
  sm_fsync /data/adb
}

sm_validate_originals() {
  local path existed digest size mode uid gid context backup real source actual expected label container
  local seen='|'
  [ -f "$SM_ORIGINAL_FILE" ] || return 1
  if [ "$SM_ORIGINALS_SHA256" != "$(printf '%064d' 0)" ]; then
    [ "$(sm_sha256_file "$SM_ORIGINAL_FILE")" = "$SM_ORIGINALS_SHA256" ] || return 1
  fi
  while IFS="$SM_TAB" read -r path existed digest size mode uid gid context backup; do
    [ -n "$path" ] || continue
    if [ "$path" = "$SM_SYSTEM_DIR" ]; then
      label=payload
    elif [ "$path" = "$SM_INIT_PATH" ]; then
      label=init_rc
    elif [ "$path" = "$SM_SYSTEM_DIR.rc" ]; then
      label=legacy_rc
    elif [ -n "$SM_POLICY_PATH" ] && [ "$path" = "$SM_POLICY_PATH" ]; then
      label=policy
    elif [ "$path" = /system/etc/init/bootanim.rc ]; then
      label=bootanim
    elif [ "$path" = /data/adb/magisk ]; then
      label=runtime
    elif [ "$path" = /data/adb/magisk.db ]; then
      label=magisk_db
    elif [ "$path" = /data/adb/magisk.db-wal ]; then
      label=magisk_db_wal
    elif [ "$path" = /data/adb/magisk.db-shm ]; then
      label=magisk_db_shm
    elif [ "$path" = /data/adb/modules ]; then
      label=modules
    elif [ "$path" = /data/adb/modules_update ]; then
      label=modules_update
    elif [ "$path" = /data/adb/post-fs-data.d ]; then
      label=post_fs_data
    elif [ "$path" = /data/adb/service.d ]; then
      label=service
    elif [ "$path" = /data/adb/sepolicy.rule ]; then
      label=preinit_rule
    elif [ "$path" = /cache/magisk.log ]; then
      label=magisk_log
    elif [ "$path" = /cache/magisk.log.bak ]; then
      label=magisk_log_bak
    elif [ "$path" = /system/addon.d/99-magisk.sh ]; then
      label=addon_script
    elif [ "$path" = /system/addon.d/magisk ]; then
      label=addon_dir
    elif [ "$path" = "$SM_RESCUE_RC" ]; then
      label=rescue_rc
    elif [ "$path" = "$SM_RESCUE_DIR" ]; then
      label=rescue_dir
    else
      return 1
    fi
    expected="original/$label/data"
    [ "$backup" = "$expected" ] || return 1
    case "$seen" in *"|$label|"*) return 1 ;; esac
    seen="$seen$label|"
    source="$SM_STATE_DIR/$backup"
    container="${source%/data}"
    [ -d "$container" ] && [ ! -L "$container" ] || return 1
    case "$existed" in
      true)
        sm_valid_hex "$digest" 64 || return 1
        case "$size" in *[!0-9]*|'') return 1 ;; esac
        case "$mode" in 0[0-7][0-7][0-7]|0[0-7][0-7][0-7][0-7]) ;; *) return 1 ;; esac
        case "$uid:$gid" in *[!0-9:]*) return 1 ;; esac
        [ ! -f "$container/absent" ] && [ ! -L "$container/absent" ] || return 1
        sm_path_present "$source" || return 1
        [ "$(sm_digest_path "$source")" = "$digest" ] || return 1
        ;;
      false)
        [ "$digest:$size:$mode:$uid:$gid:$context" = '-:0:-:-:-:-' ] || return 1
        [ -f "$container/absent" ] && [ ! -L "$container/absent" ] || return 1
        [ "$("$SM_BB" cat "$container/absent")" = absent ] || return 1
        ! sm_path_present "$source" || return 1
        ;;
      *) return 1 ;;
    esac
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
  for label in payload init_rc bootanim runtime magisk_db magisk_db_wal magisk_db_shm \
    modules modules_update post_fs_data service preinit_rule magisk_log magisk_log_bak \
    addon_script addon_dir; do
    case "$seen" in *"|$label|"*) ;; *) return 1 ;; esac
  done
  if [ "$SM_SYSTEM_DIR.rc" != "$SM_INIT_PATH" ]; then
    case "$seen" in *'|legacy_rc|'*) ;; *) return 1 ;; esac
  fi
  if [ -n "$SM_POLICY_PATH" ]; then
    case "$seen" in *'|policy|'*) ;; *) return 1 ;; esac
  fi
  case "$seen" in
    *'|rescue_rc|'*) case "$seen" in *'|rescue_dir|'*) ;; *) return 1 ;; esac ;;
    *'|rescue_dir|'*) return 1 ;;
    *)
      # Only pre-rescue development receipts may omit both rows, and only
      # while no live rescue path could be accidentally absorbed as owned.
      real="$(sm_real_path "$SM_RESCUE_RC")" || return 1
      ! sm_path_present "$real" || return 1
      real="$(sm_real_path "$SM_RESCUE_DIR")" || return 1
      ! sm_path_present "$real" || return 1
      ;;
  esac
  return 0
}

sm_assert_no_unowned_files() {
  local root real item canonical unexpected
  for root in "$SM_SYSTEM_DIR" /data/adb/magisk "$SM_RESCUE_DIR"; do
    real="$(sm_real_path "$root")" || return 1
    [ -d "$real" ] || continue
    [ ! -L "$real" ] || { sm_log "! Owned root became a symlink: $root"; return 1; }
    unexpected="$(
      cd "$real" || exit 1
      "$SM_BB" find . -mindepth 1 ! -type f ! -type l ! -type d -print | "$SM_BB" head -n 1
    )" || return 1
    [ -z "$unexpected" ] || {
      sm_log "! Unsupported unowned node under $root: ${unexpected#./}"
      return 1
    }
    (
      cd "$real" || exit 1
      "$SM_BB" find . \( -type f -o -type l \) -print | "$SM_BB" sort
    ) | while IFS= read -r item; do
      canonical="$root/${item#./}"
      [ "$canonical" = "$SM_SYSTEM_DIR/install-manifest.json" ] && continue
      "$SM_BB" awk -F '\t' -v path="$canonical" '$1 == path { found=1 } END { exit !found }' "$SM_OWNERSHIP_FILE" || exit 1
    done || return 1
    (
      cd "$real" || exit 1
      "$SM_BB" find . -mindepth 1 -type d -print | "$SM_BB" sort
    ) | while IFS= read -r item; do
      canonical="$root/${item#./}"
      "$SM_BB" awk -F '\t' -v prefix="$canonical/" \
        'index($1, prefix) == 1 { found=1 } END { exit !found }' "$SM_OWNERSHIP_FILE" || {
          sm_log "! Unowned empty directory under $root: ${item#./}"
          exit 1
        }
    done || return 1
  done
  return 0
}

sm_uninstall() {
  sm_load_transaction || { sm_log "! A versioned System Mode manifest is required for uninstall"; return 1; }
  if [ "$SM_STATE" = UNINSTALLED ]; then
    # A prior uninstall reached the boot-safe terminal state but lost power
    # while deleting the now-inert rescue bytes or their retained originals.
    if [ -f "$SM_ORIGINAL_FILE" ]; then
      sm_validate_originals || return 1
      sm_restore_originals rescue || return 1
    fi
    sm_cleanup_uninstalled_metadata true || return 1
    sm_cleanup_terminal_rollback || return 1
    sm_finalize_uninstalled_state || return 1
    return 0
  fi
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
  SM_BOOT_ATTEMPT_ID=
  sm_remove_tree_safe "Prior rollback-root cleanup target" \
    "$SM_STATE_DIR/rollback" "$SM_STATE_DIR/rollback" || return 1
  "$SM_BB" mkdir -p "$SM_ROLLBACK_DIR" || return 1
  sm_snapshot_state_metadata || return 1
  sm_update_state PREFLIGHTED || return 1
  if ! sm_quiesce_magisk || ! sm_assert_mutable_namespaces_idle ||
     ! sm_snapshot_all || ! sm_assert_magisk_quiesced ||
     ! sm_assert_mutable_namespaces_idle; then
    sm_restore_preflight || sm_log "! Uninstall preflight recovery failed; use the verified external restore"
    return 1
  fi
  sm_update_state STAGED || return 1
  if ! sm_restore_originals ordinary; then
    sm_update_state ROLLBACK_REQUIRED
    sm_restore_snapshot
    return 1
  fi
  if ! sm_restore_secure_dir_metadata; then
    sm_update_state ROLLBACK_REQUIRED
    sm_restore_snapshot
    return 1
  fi
  if ! sm_cleanup_uninstalled_metadata false; then
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
  # The prior boot tree and terminal state are now durable. Remove the rescue
  # RC first, then its now-inert bytes. A cut in this cleanup window cannot
  # prevent boot and is safely retryable from the retained original inventory.
  sm_restore_originals rescue || return 1
  sm_cleanup_uninstalled_metadata true || return 1
  sm_cleanup_terminal_rollback || return 1
  sm_finalize_uninstalled_state || return 1
  return 0
}
