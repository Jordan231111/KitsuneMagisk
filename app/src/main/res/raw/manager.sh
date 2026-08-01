##################################
# Magisk app internal scripts
##################################

run_delay() {
  (sleep $1; $2)&
}

env_check() {
  for file in busybox magiskboot magiskinit util_functions.sh boot_patch.sh; do
    [ -f "$MAGISKBIN/$file" ] || return 1
  done
  if [ "$2" -ge 25000 ]; then
    [ -f "$MAGISKBIN/magiskpolicy" ] || return 1
  fi
  if [ "$2" -ge 25210 ] && [ -f "$MAGISKTMP/.magisk/config" ]; then
    [ -b "$MAGISKTMP/.magisk/device/preinit" ] || [ -b "$MAGISKTMP/.magisk/block/preinit" ] || return 2
  fi
  grep -xqF "MAGISK_VER='$1'" "$MAGISKBIN/util_functions.sh" || return 3
  grep -xqF "MAGISK_VER_CODE=$2" "$MAGISKBIN/util_functions.sh" || return 3
  return 0
}

cp_readlink() {
  local source="$1"
  local destination="$2"
  local file full
  if [ -z "$destination" ]; then
    cd "$source" || return 1
  else
    mkdir -p "$destination" || return 1
    cp -af "$source"/. "$destination" || return 1
    cd "$destination" || return 1
  fi
  for file in *; do
    if [ -L "$file" ]; then
      full="$(readlink -f "$file")" || { cd /; return 1; }
      rm "$file" || { cd /; return 1; }
      cp -af "$full" "$file" || { cd /; return 1; }
    fi
  done
  chmod -R 755 . || { cd /; return 1; }
  cd / || return 1
}

path_present() {
  [ -e "$1" ] || [ -L "$1" ]
}

fix_env() {
  local source="$1"
  local preserve_old="${2:-false}"
  local new="$NVBASE/.magisk-new"
  local old="$NVBASE/.magisk-old"
  local had_old=false

  # Roll an interrupted swap back before retrying it. If both names exist, the
  # previous process could have died before the persistent System Mode side was
  # committed, so silently deleting the old runtime would destroy recovery.
  if path_present "$old" && [ ! -d "$old" ]; then
    ui_print "! Invalid interrupted runtime transaction"
    return 1
  fi
  if path_present "$MAGISKBIN" && [ ! -d "$MAGISKBIN" ]; then
    ui_print "! Invalid Magisk runtime path"
    return 1
  fi
  rm -rf "$new" || return 1
  if [ -d "$old" ]; then
    if [ -d "$MAGISKBIN" ]; then
      mv "$MAGISKBIN" "$new" || return 1
      if ! mv "$old" "$MAGISKBIN"; then
        mv "$new" "$MAGISKBIN"
        return 1
      fi
      rm -rf "$new" || return 1
    else
      mv "$old" "$MAGISKBIN" || return 1
    fi
  fi
  mkdir -p "$new" || return 1
  if ! cp_readlink "$source" "$new" ||
     ! chown -R 0:0 "$new" ||
     ! chmod -R 755 "$new"; then
    rm -rf "$new"
    return 1
  fi
  chmod 700 "$NVBASE" || { rm -rf "$new"; return 1; }

  if path_present "$MAGISKBIN"; then
    mv "$MAGISKBIN" "$old" || { rm -rf "$new"; return 1; }
    had_old=true
  fi
  if ! mv "$new" "$MAGISKBIN"; then
    path_present "$old" && mv "$old" "$MAGISKBIN"
    return 1
  fi
  if [ "$preserve_old" = true ]; then
    SYSTEM_INSTALL_ENV_TRANSACTION=true
    SYSTEM_INSTALL_ENV_HAD_OLD="$had_old"
  elif ! rm -rf "$old"; then
    ui_print "W: Runtime installed, but the previous runtime copy remains"
  fi
  rm -rf "$source" || ui_print "W: Runtime installed, but staging cleanup failed"
  return 0
}

rollback_env() {
  local new="$NVBASE/.magisk-new"
  local old="$NVBASE/.magisk-old"
  [ "$SYSTEM_INSTALL_ENV_TRANSACTION" = true ] || return 0
  rm -rf "$new" || return 1
  if [ "$SYSTEM_INSTALL_ENV_HAD_OLD" = true ]; then
    path_present "$old" || return 1
    if path_present "$MAGISKBIN"; then
      mv "$MAGISKBIN" "$new" || return 1
    fi
    if ! mv "$old" "$MAGISKBIN"; then
      path_present "$new" && mv "$new" "$MAGISKBIN"
      return 1
    fi
    rm -rf "$new" || return 1
  else
    rm -rf "$MAGISKBIN" "$old" || return 1
  fi
  SYSTEM_INSTALL_ENV_TRANSACTION=false
  SYSTEM_INSTALL_ENV_HAD_OLD=false
  return 0
}

commit_env() {
  [ "$SYSTEM_INSTALL_ENV_TRANSACTION" = true ] || return 1
  if ! rm -rf "$NVBASE/.magisk-old" "$NVBASE/.magisk-new"; then
    ui_print "W: Runtime installed, but transactional backup cleanup is incomplete"
  fi
  SYSTEM_INSTALL_ENV_TRANSACTION=false
  SYSTEM_INSTALL_ENV_HAD_OLD=false
  return 0
}

install_addond(){
    local installDir="$MAGISKBIN"
    local AppApkPath="$1"
    local SYSTEM_INSTALL="$2"
    local defer_remount="${3:-false}"
    [ -z "$SYSTEM_INSTALL" ] && SYSTEM_INSTALL=false
    local addond=/system/addon.d
    local script_backup="$addond/.99-magisk.sh.kitsune-old"
    local dir_backup="$addond/.magisk.kitsune-old"
    local BLOCKNAME
    local failed=0
    local publish_started=false
    test ! -d "$addond" && return 0
    ui_print "- Adding addon.d survival script"
    BLOCKNAME="/dev/block/system_block.$(random_str 5 20)"
    rm -rf "$BLOCKNAME"
    if is_rootfs; then
        mkblknode "$BLOCKNAME" /system
    else
        mkblknode "$BLOCKNAME"  /
    fi
    blockdev --setrw "$BLOCKNAME" || { rm -rf "$BLOCKNAME"; return 1; }
    rm -rf "$BLOCKNAME"
    mount -o rw,remount / || return 1
    mount -o rw,remount /system || {
        [ "$defer_remount" = true ] || mount -o ro,remount /
        return 1
    }
    if path_present "$script_backup" || path_present "$dir_backup"; then
        ui_print "! Interrupted addon.d transaction requires recovery"
        if [ "$defer_remount" != true ]; then
            mount -o ro,remount /system
            mount -o ro,remount /
        fi
        return 1
    fi
    if path_present "$addond/99-magisk.sh"; then
        mv "$addond/99-magisk.sh" "$script_backup" || failed=1
    fi
    if [ "$failed" = 0 ] && path_present "$addond/magisk"; then
        mv "$addond/magisk" "$dir_backup" || failed=1
    fi
    [ "$failed" != 0 ] || publish_started=true
    if [ "$SYSTEM_INSTALL" = "true" ]; then
        [ "$failed" = 0 ] && cp -prLf "$installDir"/. /system/etc/init/magisk || failed=1
        [ "$failed" = 0 ] && cp "$installDir/addon.d.sh" "$addond/99-magisk.sh" || failed=1
        [ "$failed" = 0 ] && cp "$AppApkPath" /system/etc/init/magisk/magisk.apk || failed=1
        [ "$failed" = 0 ] && chmod 755 /system/etc/init/magisk/* || failed=1
        [ "$failed" = 0 ] && sed -i "s/^SYSTEMINSTALL=.*/SYSTEMINSTALL=true/g" "$addond/99-magisk.sh" || failed=1
    else
        [ "$failed" = 0 ] && mkdir -p "$addond/magisk" || failed=1
        [ "$failed" = 0 ] && cp -prLf "$installDir"/. "$addond/magisk" || failed=1
        [ "$failed" = 0 ] && mv "$addond/magisk/boot_patch.sh" "$addond/magisk/boot_patch.sh.in" || failed=1
        [ "$failed" = 0 ] && mv "$addond/magisk/addon.d.sh" "$addond/99-magisk.sh" || failed=1
        [ "$failed" = 0 ] && cp "$AppApkPath" "$addond/magisk/magisk.apk" || failed=1
    fi
    if [ "$failed" != 0 ]; then
        ui_print "! Failed to install addon.d; restoring previous files"
        # Before publication starts, a failed backup rename leaves its source
        # untouched. Do not delete that original while restoring an earlier
        # backup that did succeed. Once publication starts, both old paths were
        # either absent or safely renamed, so remove only the new partial data.
        if [ "$publish_started" = true ]; then
            rm -rf "$addond/99-magisk.sh" "$addond/magisk" || failed=2
        fi
        if path_present "$script_backup"; then
            mv "$script_backup" "$addond/99-magisk.sh" || failed=2
        fi
        if path_present "$dir_backup"; then
            mv "$dir_backup" "$addond/magisk" || failed=2
        fi
    else
        if ! rm -rf "$script_backup" "$dir_backup"; then
            ui_print "W: addon.d installed, but rollback-copy cleanup is incomplete"
        fi
    fi
    if [ "$defer_remount" != true ]; then
        mount -o ro,remount /
        mount -o ro,remount /system
    fi
    [ "$failed" = 0 ]
}

direct_install() {
  echo "- Flashing new boot image"
  flash_image $1/new-boot.img $2
  case $? in
    1)
      echo "! Insufficient partition size"
      return 1
      ;;
    2)
      echo "! $2 is read only"
      return 2
      ;;
  esac

  rm -f $1/new-boot.img
  if ! fix_env "$1"; then
    echo "! Boot image was flashed, but the Magisk runtime could not be published"
    return 1
  fi
  run_migrations || echo "! Runtime installed, but legacy backup migration was incomplete"
  copy_preinit_files || echo "! Runtime installed, but pre-init module files were not refreshed"
  install_addond "$3" || echo "! Runtime installed, but addon.d survival was not refreshed"
  return 0
}

run_uninstaller() {
  rm -rf /dev/tmp
  mkdir -p /dev/tmp/install
  unzip -o "$1" "assets/*" "lib/*" -d /dev/tmp/install
  INSTALLER=/dev/tmp/install sh /dev/tmp/install/assets/uninstaller.sh dummy 1 "$1"
}

restore_imgs() {
  [ -z $SHA1 ] && return 1
  local BACKUPDIR=/data/magisk_backup_$SHA1
  [ -d $BACKUPDIR ] || return 1

  get_flags
  find_boot_image

  for name in dtb dtbo; do
    [ -f $BACKUPDIR/${name}.img.gz ] || continue
    local IMAGE=$(find_block $name$SLOT)
    [ -z $IMAGE ] && continue
    flash_image $BACKUPDIR/${name}.img.gz $IMAGE
  done
  [ -f $BACKUPDIR/boot.img.gz ] || return 1
  flash_image $BACKUPDIR/boot.img.gz $BOOTIMAGE
}

post_ota() {
  cd $NVBASE
  cp -f $1 bootctl
  rm -f $1
  chmod 755 bootctl
  ./bootctl hal-info || return
  SLOT_NUM=0
  [ $(./bootctl get-current-slot) -eq 0 ] && SLOT_NUM=1
  ./bootctl set-active-boot-slot $SLOT_NUM
  cat << EOF > post-fs-data.d/post_ota.sh
/data/adb/bootctl mark-boot-successful
rm -f /data/adb/bootctl
rm -f /data/adb/post-fs-data.d/post_ota.sh
EOF
  chmod 755 post-fs-data.d/post_ota.sh
  cd /
}

add_hosts_module() {
  # Do not touch existing hosts module
  [ -d $NVBASE/modules/hosts ] && return
  cd $NVBASE/modules
  mkdir -p hosts/system/etc
  cat << EOF > hosts/module.prop
id=hosts
name=Systemless Hosts
version=1.0
versionCode=1
author=Magisk
description=Magisk app built-in systemless hosts module
EOF
  magisk --clone /system/etc/hosts hosts/system/etc/hosts
  touch hosts/update
  cd /
}

adb_pm_install() {
  local tmp=/data/local/tmp/temp.apk
  cp -f "$1" $tmp
  chmod 644 $tmp
  su 2000 -c pm install -g $tmp || pm install -g $tmp || su 1000 -c pm install -g $tmp
  local res=$?
  rm -f $tmp
  if [ $res = 0 ]; then
    ( magisk magiskhide sulist && magisk magiskhide add "$2" ) &
    appops set "$2" REQUEST_INSTALL_PACKAGES allow
  fi
  return $res
}

check_boot_ramdisk() {
  # Create boolean ISAB
  ISAB=true
  [ -z $SLOT ] && ISAB=false

  # If we are A/B, then we must have ramdisk
  $ISAB && return 0

  # If we are using legacy SAR, but not A/B, assume we do not have ramdisk
  if $LEGACYSAR; then
    # Override recovery mode to true
    RECOVERYMODE=true
    return 1
  fi

  return 0
}

check_encryption() {
  if $ISENCRYPTED; then
    if [ $SDK_INT -lt 24 ]; then
      CRYPTOTYPE="block"
    else
      # First see what the system tells us
      CRYPTOTYPE=$(getprop ro.crypto.type)
      if [ -z $CRYPTOTYPE ]; then
        # If not mounting through device mapper, we are FBE
        if grep ' /data ' /proc/mounts | grep -qv 'dm-'; then
          CRYPTOTYPE="file"
        else
          # We are either FDE or metadata encryption (which is also FBE)
          CRYPTOTYPE="block"
          grep -q ' /metadata ' /proc/mounts && CRYPTOTYPE="file"
        fi
      fi
    fi
  else
    CRYPTOTYPE="N/A"
  fi
}

run_action() {
  local MODID="$1"
  cd "/data/adb/modules/$MODID"
  sh ./action.sh
  local RES=$?
  cd /
  return $RES
}

##########################
# Non-root util_functions
##########################

mount_partitions() {
  [ "$(getprop ro.build.ab_update)" = "true" ] && SLOT=$(getprop ro.boot.slot_suffix)
  # Check whether non rootfs root dir exists
  SYSTEM_AS_ROOT=false
  grep ' / ' /proc/mounts | grep -qv 'rootfs' && SYSTEM_AS_ROOT=true

  LEGACYSAR=false
  grep ' / ' /proc/mounts | grep -q '/dev/root' && LEGACYSAR=true
}

get_flags() {
  KEEPVERITY=$SYSTEM_AS_ROOT
  ISENCRYPTED=false
  [ "$(getprop ro.crypto.state)" = "encrypted" ] && ISENCRYPTED=true
  KEEPFORCEENCRYPT=$ISENCRYPTED
  if [ -n "$(getprop ro.boot.vbmeta.device)" -o -n "$(getprop ro.boot.vbmeta.size)" ]; then
    PATCHVBMETAFLAG=false
  elif getprop ro.product.ab_ota_partitions | grep -wq vbmeta; then
    PATCHVBMETAFLAG=false
  else
    PATCHVBMETAFLAG=true
  fi
  [ -z $RECOVERYMODE ] && RECOVERYMODE=false
}

run_migrations() { return; }

grep_prop() { return; }

get_sulist_status(){
    SULISTMODE=false
    if magisk magiskhide sulist; then
        SULISTMODE=true
    fi
}

##############################
# Magisk Delta Custom install script
##############################

# define
MAGISKSYSTEMDIR="/system/etc/init/magisk"
SYSTEM_INSTALL_TRANSACTION=false
SYSTEM_INSTALL_BEGIN_COMPLETE=false
SYSTEM_INSTALL_MIRROR=
SYSTEM_INSTALL_HAD_DIR=false
SYSTEM_INSTALL_HAD_RC=false
SYSTEM_INSTALL_SEPOL=
SYSTEM_INSTALL_SEPOL_HAD_GZ=false
SYSTEM_INSTALL_BOOTANIM=false
SYSTEM_INSTALL_BOOTANIM_HAD_GZ=false
SYSTEM_INSTALL_CREATED_RUNTIME_DIR=
SYSTEM_INSTALL_ENV_TRANSACTION=false
SYSTEM_INSTALL_ENV_HAD_OLD=false
STAGED_FILE_HAD_GZ=false

random_str(){
local FROM
local TO
FROM="$1"; TO="$2"
tr -dc A-Za-z0-9 </dev/urandom | head -c $(($FROM+$(($RANDOM%$(($TO-$FROM+1))))))
}

magiskrc(){
local MAGISKTMP="$1"

# use "magisk --auto-selinux" to automatically switching selinux state

cat <<EOF
on post-fs-data
    start logd
    exec u:r:su:s0 root root -- $MAGISKSYSTEMDIR/magiskpolicy --live --magisk
    exec u:r:magisk:s0 root root -- $MAGISKSYSTEMDIR/magiskpolicy --live --magisk
    exec u:r:update_engine:s0 root root -- $MAGISKSYSTEMDIR/magiskpolicy --live --magisk
    exec u:r:su:s0 root root -- $MAGISKSYSTEMDIR/$magisk_name --auto-selinux --setup-sbin $MAGISKSYSTEMDIR $MAGISKTMP
    exec u:r:su:s0 root root -- $MAGISKTMP/magisk --auto-selinux --post-fs-data
on nonencrypted
    exec u:r:su:s0 root root -- $MAGISKTMP/magisk --auto-selinux --service
on property:vold.decrypt=trigger_restart_framework
    exec u:r:su:s0 root root -- $MAGISKTMP/magisk --auto-selinux --service
on property:sys.boot_completed=1
    mkdir /data/adb/magisk 755
    exec u:r:su:s0 root root -- $MAGISKTMP/magisk --auto-selinux --boot-complete
   
on property:init.svc.zygote=restarting
    exec u:r:su:s0 root root -- $MAGISKTMP/magisk --auto-selinux --zygote-restart
   
on property:init.svc.zygote=stopped
    exec u:r:su:s0 root root -- $MAGISKTMP/magisk --auto-selinux --zygote-restart
EOF
}

backup_restore(){
    # if gz is not found and orig file is found, backup to gz
    if [ ! -f "${1}.gz" ] && [ -f "$1" ]; then
        gzip -k "$1" && return 0
    elif [ -f "${1}.gz" ]; then
    # if gz found, restore from gz
        rm -rf "$1" && gzip -kdf "${1}.gz" && return 0
    fi
    return 1
}

stage_file_rollback(){
    local file="$1"
    STAGED_FILE_HAD_GZ=false
    [ -f "$file" ] || return 1
    ! path_present "$file.kitsune-old" || return 1
    ! path_present "$file.gz.kitsune-old" || return 1
    if ! cp -af "$file" "$file.kitsune-old"; then
        rm -f "$file.kitsune-old" "$file.gz.kitsune-old"
        return 1
    fi
    if [ -f "$file.gz" ]; then
        if ! cp -af "$file.gz" "$file.gz.kitsune-old"; then
            rm -f "$file.kitsune-old" "$file.gz.kitsune-old"
            return 1
        fi
        STAGED_FILE_HAD_GZ=true
    fi
    return 0
}

restore_staged_file(){
    local file="$1"
    local had_gz="$2"
    local failed=0
    [ -f "$file.kitsune-old" ] || return 1
    if [ "$had_gz" = true ] && [ ! -f "$file.gz.kitsune-old" ]; then
        return 1
    fi
    if ! mv -f "$file.kitsune-old" "$file"; then
        failed=1
    fi
    if [ "$had_gz" = true ]; then
        if ! mv -f "$file.gz.kitsune-old" "$file.gz"; then
            failed=1
        fi
    else
        rm -f "$file.gz" "$file.gz.kitsune-old" || failed=1
    fi
    return "$failed"
}

discard_staged_file(){
    rm -f "$1.kitsune-old" "$1.gz.kitsune-old"
}

begin_system_installation(){
    local mirror="${1:-$MIRRORDIR}"
    local target="$mirror${MAGISKSYSTEMDIR}"
    local legacy="$mirror${MAGISKSYSTEMDIR}.rc"
    case "$mirror" in
        /|"/proc/$$/attr") ;;
        *) ui_print "! Refusing transaction with an invalid System Mode mirror"; return 1 ;;
    esac
    [ "$SYSTEM_INSTALL_TRANSACTION" = false ] || return 1
    if [ -e "$target.kitsune-old" ] || [ -L "$target.kitsune-old" ] ||
       [ -e "$legacy.kitsune-old" ] || [ -L "$legacy.kitsune-old" ]; then
        ui_print "! Interrupted System Mode transaction requires recovery"
        return 1
    fi

    SYSTEM_INSTALL_TRANSACTION=true
    SYSTEM_INSTALL_BEGIN_COMPLETE=false
    SYSTEM_INSTALL_MIRROR="$mirror"
    SYSTEM_INSTALL_HAD_DIR=false
    SYSTEM_INSTALL_HAD_RC=false
    if [ -e "$target" ] || [ -L "$target" ]; then
        if ! mv "$target" "$target.kitsune-old"; then
            SYSTEM_INSTALL_TRANSACTION=false
            SYSTEM_INSTALL_MIRROR=
            return 1
        fi
        SYSTEM_INSTALL_HAD_DIR=true
    fi
    if [ -e "$legacy" ] || [ -L "$legacy" ]; then
        if ! mv "$legacy" "$legacy.kitsune-old"; then
            cleanup_system_installation
            return 1
        fi
        SYSTEM_INSTALL_HAD_RC=true
    fi
    SYSTEM_INSTALL_BEGIN_COMPLETE=true
    return 0
}

cleanup_system_installation(){
    local mirror="${1:-$SYSTEM_INSTALL_MIRROR}"
    local target="$mirror${MAGISKSYSTEMDIR}"
    local legacy="$mirror${MAGISKSYSTEMDIR}.rc"
    local failed=0
    [ "$SYSTEM_INSTALL_TRANSACTION" = true ] || return 0
    [ "$mirror" = "$SYSTEM_INSTALL_MIRROR" ] || {
        ui_print "! Refusing rollback from a different System Mode mirror"
        return 1
    }
    # Never destroy the replacement until every backup that should exist has
    # been verified. A missing rollback source is a recoverable/manual state;
    # deleting the remaining working payload would make it data loss.
    if [ "$SYSTEM_INSTALL_HAD_DIR" = true ] &&
       [ ! -e "$target.kitsune-old" ] && [ ! -L "$target.kitsune-old" ]; then
        ui_print "! System Mode payload rollback backup is missing"
        return 1
    fi
    if [ "$SYSTEM_INSTALL_HAD_RC" = true ] &&
       [ ! -e "$legacy.kitsune-old" ] && [ ! -L "$legacy.kitsune-old" ]; then
        ui_print "! System Mode init rollback backup is missing"
        return 1
    fi
    # No replacement can have been published before the backup phase finished.
    # During a partial begin, leave every path that did not move untouched and
    # only restore the paths whose backups exist.
    if [ "$SYSTEM_INSTALL_BEGIN_COMPLETE" = true ]; then
        rm -rf "$target" "$legacy" || failed=1
    fi
    if [ "$SYSTEM_INSTALL_HAD_DIR" = true ]; then
        { [ -e "$target.kitsune-old" ] || [ -L "$target.kitsune-old" ]; } &&
            mv "$target.kitsune-old" "$target" || failed=1
    fi
    if [ "$SYSTEM_INSTALL_HAD_RC" = true ]; then
        { [ -e "$legacy.kitsune-old" ] || [ -L "$legacy.kitsune-old" ]; } &&
            mv "$legacy.kitsune-old" "$legacy" || failed=1
    fi
    if [ -n "$SYSTEM_INSTALL_SEPOL" ]; then
        restore_staged_file "$mirror$SYSTEM_INSTALL_SEPOL" \
            "$SYSTEM_INSTALL_SEPOL_HAD_GZ" || failed=1
    fi
    if [ "$SYSTEM_INSTALL_BOOTANIM" = true ]; then
        restore_staged_file "$mirror/system/etc/init/bootanim.rc" \
            "$SYSTEM_INSTALL_BOOTANIM_HAD_GZ" || failed=1
    fi
    if [ -n "$SYSTEM_INSTALL_CREATED_RUNTIME_DIR" ]; then
        rmdir "$SYSTEM_INSTALL_CREATED_RUNTIME_DIR" || failed=1
    fi
    if [ "$failed" = 0 ]; then
        SYSTEM_INSTALL_TRANSACTION=false
        SYSTEM_INSTALL_BEGIN_COMPLETE=false
        SYSTEM_INSTALL_MIRROR=
        SYSTEM_INSTALL_HAD_DIR=false
        SYSTEM_INSTALL_HAD_RC=false
        SYSTEM_INSTALL_SEPOL=
        SYSTEM_INSTALL_SEPOL_HAD_GZ=false
        SYSTEM_INSTALL_BOOTANIM=false
        SYSTEM_INSTALL_BOOTANIM_HAD_GZ=false
        SYSTEM_INSTALL_CREATED_RUNTIME_DIR=
    fi
    return "$failed"
}

commit_system_installation(){
    local mirror="${1:-$SYSTEM_INSTALL_MIRROR}"
    local target="$mirror${MAGISKSYSTEMDIR}"
    local legacy="$mirror${MAGISKSYSTEMDIR}.rc"
    [ "$SYSTEM_INSTALL_TRANSACTION" = true ] || return 1
    [ "$mirror" = "$SYSTEM_INSTALL_MIRROR" ] || return 1
    local cleanup_failed=0
    rm -rf "$target.kitsune-old" "$legacy.kitsune-old" || cleanup_failed=1
    if [ -n "$SYSTEM_INSTALL_SEPOL" ]; then
        discard_staged_file "$mirror$SYSTEM_INSTALL_SEPOL" || cleanup_failed=1
        ! path_present "$mirror$SYSTEM_INSTALL_SEPOL.kitsune-old" || cleanup_failed=1
        ! path_present "$mirror$SYSTEM_INSTALL_SEPOL.gz.kitsune-old" || cleanup_failed=1
    fi
    if [ "$SYSTEM_INSTALL_BOOTANIM" = true ]; then
        discard_staged_file "$mirror/system/etc/init/bootanim.rc" || cleanup_failed=1
        ! path_present "$mirror/system/etc/init/bootanim.rc.kitsune-old" || cleanup_failed=1
        ! path_present "$mirror/system/etc/init/bootanim.rc.gz.kitsune-old" || cleanup_failed=1
    fi
    ! path_present "$target.kitsune-old" || cleanup_failed=1
    ! path_present "$legacy.kitsune-old" || cleanup_failed=1
    SYSTEM_INSTALL_TRANSACTION=false
    SYSTEM_INSTALL_BEGIN_COMPLETE=false
    SYSTEM_INSTALL_MIRROR=
    SYSTEM_INSTALL_HAD_DIR=false
    SYSTEM_INSTALL_HAD_RC=false
    SYSTEM_INSTALL_SEPOL=
    SYSTEM_INSTALL_SEPOL_HAD_GZ=false
    SYSTEM_INSTALL_BOOTANIM=false
    SYSTEM_INSTALL_BOOTANIM_HAD_GZ=false
    SYSTEM_INSTALL_CREATED_RUNTIME_DIR=
    [ "$cleanup_failed" = 0 ] || \
        ui_print "W: System Mode installed, but transactional backup cleanup is incomplete"
    return 0
}

installer_cleanup(){
    if $BOOTMODE; then
        umount -l "/proc/$$/attr"
    else
        recovery_cleanup
    fi
}

direct_install_system(){
    print_title "Magisk Delta (System Mode)" "by HuskyDG"
    print_title "Powered by Magisk"
    api_level_arch_detect
    local INSTALLDIR="$1"
    local defer_cleanup="${2:-false}"
    MIRRORDIR=
    SYSTEM_INSTALL_SEPOL=
    SYSTEM_INSTALL_SEPOL_HAD_GZ=false
    SYSTEM_INSTALL_BOOTANIM=false
    SYSTEM_INSTALL_BOOTANIM_HAD_GZ=false
    SYSTEM_INSTALL_CREATED_RUNTIME_DIR=
    SYSTEM_INSTALL_BEGIN_COMPLETE=false
    if [ "$API" -le 24 ]; then
        ui_print "! System Mode is not qualified on API 23-24"
        return 1
    fi
    local mode_binary="$INSTALLDIR/magisk32"
    [ "$IS64BIT" = true ] && mode_binary="$INSTALLDIR/magisk64"
    # -v asks the running daemon for its version, which can legitimately still
    # be a release daemon during a debug upgrade. -c reports this payload
    # binary's compile-time identity and is therefore the mode we must gate.
    if [ ! -x "$mode_binary" ] ||
       ! "$mode_binary" -c 2>/dev/null | grep -q ':MAGISK:D '; then
        ui_print "! System Mode is disabled in release builds"
        return 1
    fi

    ui_print "- Remount system partition as read-write"
    # Use kernel trick to clean up mirrors automatically when installer completed
    MIRRORDIR="/proc/$$/attr"
    local ROOTDIR SYSTEMDIR VENDORDIR ODM_DIR

    ROOTDIR="$MIRRORDIR/system_root"
    SYSTEMDIR="$MIRRORDIR/system"
    VENDORDIR="$MIRRORDIR/vendor"
    ODM_DIR="$MIRRORDIR/odm"

    local MAGISKTMP_TO_INSTALL=/sbin

    if $BOOTMODE; then
        umount -l "/proc/$$/attr"
        # setup mirrors to get the original content
        mount -t tmpfs -o 'mode=0755' tmpfs "$MIRRORDIR" || return 1
        if is_rootfs; then
            ROOTDIR=/
            force_bind_mount "/" "$ROOTDIR" || return 1
            mkdir "$SYSTEMDIR"
            force_bind_mount "/system" "$SYSTEMDIR" || return 1
        else
            mkdir "$ROOTDIR"
            force_bind_mount "/" "$ROOTDIR" || return 1
            if mountpoint -q /system; then
                mkdir "$SYSTEMDIR"
                force_bind_mount "/system" "$SYSTEMDIR" || return 1
            else
                ln -fs ./system_root/system "$SYSTEMDIR"
            fi
        fi

        # check if /vendor is seperated fs
        if mountpoint -q /vendor; then
            mkdir "$VENDORDIR"
            force_bind_mount "/vendor" "$VENDORDIR" || return 1
         else
            ln -fs ./system/vendor "$VENDORDIR"
        fi

        # check if /odm is seperated fs
        if mountpoint -q /odm; then
            mkdir "$ODM_DIR"
            force_bind_mount "/odm" "$ODM_DIR" || return 1
         else
            ln -fs ./system_root/odm "$ODM_DIR"
        fi
    else
        MIRRORDIR="/"
        ROOTDIR="$MIRRORDIR/system_root"
        SYSTEMDIR="$MIRRORDIR/system"
        VENDORDIR="$MIRRORDIR/vendor"
        ODM_DIR="$MIRRORDIR/odm"
        ui_print "- Mount system partitions as read-write..."
        remount_check rw "$ROOTDIR" 0 || { warn_system_ro; return 1; }
        remount_check rw "$SYSTEMDIR" 0 || { warn_system_ro; return 1; }
        remount_check rw "$VENDORDIR" 0 || { warn_system_ro; return 1; }
        remount_check rw "$ODM_DIR" 0 || { warn_system_ro; return 1; }

    fi


    ui_print "- Cleaning up enviroment..."
    {
        local checkfile="$MIRRORDIR/system/.check_$(random_str 10 20)"
        local required_kb available_kb
        required_kb="$(du -sk "$INSTALLDIR" | awk 'NR == 1 { print $1 }')"
        available_kb="$(df -Pk "$SYSTEMDIR" | awk 'END { print $4 }')"
        case "$required_kb" in ''|*[!0-9]*) ui_print "! Unable to measure install payload"; return 1 ;; esac
        case "$available_kb" in ''|*[!0-9]*) ui_print "! Unable to verify free space on system"; return 1 ;; esac
        if [ "$available_kb" -lt $((required_kb + 4096)) ]; then
            ui_print "! Insufficient free space on system"
            return 1
        fi
        # A single block proves the selected mirror is actually writable.
        dd if=/dev/zero of="$checkfile" bs=4096 count=1 2>/dev/null || \
            { rm -rf "$checkfile"; ui_print "! Insufficient free space or system write protection"; return 1; }
        rm -f "$checkfile" || { ui_print "! Unable to remove system write probe"; return 1; }
    }
    local magisk magisk_applet=magisk32 magisk_name=magisk32
    if [ "$IS64BIT" = true ]; then
        magisk_name=magisk64
        magisk_applet=magisk64
        [ -f "$INSTALLDIR/magisk32" ] && magisk_applet="magisk32 magisk64"
    fi

    ui_print "- Copy files to system partition"
    for magisk in $magisk_applet magiskpolicy magiskinit stub.apk; do
        [ -f "$INSTALLDIR/$magisk" ] || { ui_print "! Missing install payload: $magisk"; return 1; }
    done
    begin_system_installation "$MIRRORDIR" || return 1
    local runtime_dir="$ROOTDIR$MAGISKTMP_TO_INSTALL"
    if [ ! -d "$runtime_dir" ]; then
        if [ -e "$runtime_dir" ] || [ -L "$runtime_dir" ]; then
            ui_print "! Runtime path exists but is not a directory: $MAGISKTMP_TO_INSTALL"
            return 1
        fi
        mkdir "$runtime_dir" || { ui_print "! Can't create runtime path $MAGISKTMP_TO_INSTALL"; return 1; }
        SYSTEM_INSTALL_CREATED_RUNTIME_DIR="$runtime_dir"
    fi
    mkdir -p "$MIRRORDIR$MAGISKSYSTEMDIR" || return 1
    for magisk in $magisk_applet magiskpolicy magiskinit stub.apk; do
        cat "$INSTALLDIR/$magisk" >"$MIRRORDIR$MAGISKSYSTEMDIR/$magisk" || { ui_print "! Unable to write Magisk binaries to system"; return 1; }
    done
    echo -e "SYSTEMMODE=true\nRECOVERYMODE=false" >"$MIRRORDIR$MAGISKSYSTEMDIR/config" || return 1
    if ! chcon -R u:object_r:system_file:s0 "$MIRRORDIR$MAGISKSYSTEMDIR"; then
        if [ -d /sys/fs/selinux ]; then
            ui_print "! Unable to label System Mode payload"
            return 1
        fi
        ui_print "W: SELinux is inactive; payload labeling was skipped"
    fi
    chmod -R 700 "$MIRRORDIR$MAGISKSYSTEMDIR" || return 1

    if [ "$API" -gt 24 ]; then

        # Parse and serialize the current live policy without changing it. The
        # previous probe added a permissive domain to the running policy and
        # left that broader policy active even when installation later failed.
        {
            if $BOOTMODE; then
                ui_print "- Validate the current SELinux policy"
                if [ -d /sys/fs/selinux ]; then
                    local live_policy_probe="$INSTALLDIR/live-sepolicy.probe"
                    if ! "$INSTALLDIR/magiskpolicy" --save "$live_policy_probe" &>/dev/null; then
                        rm -f "$live_policy_probe"
                        ui_print "! Unable to parse the current SELinux policy"
                        return 1
                    fi
                    rm -f "$live_policy_probe" || return 1
                fi
            else
                ui_print "W: It's impossible to check kernel compatible in recovery mode"
                ui_print "W: Please make sure your kernel can dynamic patch SELinux Policy"
            fi
            if ! is_rootfs; then
              {
                ui_print "- Patch sepolicy file"
                local sepol file
                for file in /vendor/etc/selinux/precompiled_sepolicy /odm/etc/selinux/precompiled_sepolicy /system/etc/selinux/precompiled_sepolicy /system_root/sepolicy /system_root/sepolicy_debug /system_root/sepolicy.unlocked; do
                    if [ -f "$MIRRORDIR$file" ]; then
                        sepol="$file"
                        break
                    fi
                done
                if [ -z "$sepol" ]; then
                    ui_print "! Cannot find sepolicy file"
                    return 1
                else
                    ui_print "- Target sepolicy is $sepol"
                    stage_file_rollback "$MIRRORDIR$sepol" || \
                        { ui_print "! Transaction backup failed"; return 1; }
                    SYSTEM_INSTALL_SEPOL="$sepol"
                    SYSTEM_INSTALL_SEPOL_HAD_GZ="$STAGED_FILE_HAD_GZ"
                    backup_restore "$MIRRORDIR$sepol" || \
                        { ui_print "! Backup failed"; return 1; }
                    # copy file to cache
                    cp -af "$MIRRORDIR$sepol" "$INSTALLDIR/sepol.in" || return 1
                    if ! "$INSTALLDIR/magiskinit" --patch-sepol "$INSTALLDIR/sepol.in" "$INSTALLDIR/sepol.out" || ! cp -af "$INSTALLDIR/sepol.out" "$MIRRORDIR$sepol"; then
                        ui_print "! Unable to patch sepolicy file"
                        rm -f "$INSTALLDIR/sepol.in" "$INSTALLDIR/sepol.out"
                        return 1
                    fi
                    rm -f "$INSTALLDIR/sepol.in" "$INSTALLDIR/sepol.out" || return 1
                    ui_print "- Patching sepolicy file success!"
                fi
              }
            fi
        }
        ui_print "- Add init boot script"
        local hijackrc
        {
            hijackrc="$MIRRORDIR/system/etc/init/magisk.rc" 
            if [ -f "$MIRRORDIR/system/etc/init/bootanim.rc" ]; then
                stage_file_rollback "$MIRRORDIR/system/etc/init/bootanim.rc" || \
                    { ui_print "! Transaction backup failed"; return 1; }
                SYSTEM_INSTALL_BOOTANIM=true
                SYSTEM_INSTALL_BOOTANIM_HAD_GZ="$STAGED_FILE_HAD_GZ"
                backup_restore "$MIRRORDIR/system/etc/init/bootanim.rc" || return 1
                hijackrc="$MIRRORDIR/system/etc/init/bootanim.rc"
            fi
        }
        echo "$(magiskrc "$MAGISKTMP_TO_INSTALL")" >>"$hijackrc" || return 1
    fi

    ui_print "[*] Reflash your ROM if your ROM is unable to start"
    ui_print "    and do not use this method to install Magisk" 

    if [ "$defer_cleanup" != true ]; then
        commit_system_installation || return 1
        $BOOTMODE && installer_cleanup
    fi
    return 0
}



xdirect_install_system() {
  direct_install_system "$1" true || { cleanup_system_installation || ui_print "! System Mode rollback incomplete"; installer_cleanup; return 1; }
  fix_env "$1" true || { cleanup_system_installation || ui_print "! System Mode rollback incomplete"; installer_cleanup; return 1; }
  install_addond "$2" "true" "true" || {
    rollback_env || ui_print "! Runtime rollback incomplete"
    cleanup_system_installation || ui_print "! System Mode rollback incomplete"
    installer_cleanup
    return 1
  }
  commit_system_installation || {
    rollback_env || ui_print "! Runtime rollback incomplete"
    cleanup_system_installation || ui_print "! System Mode rollback incomplete"
    installer_cleanup
    return 1
  }
  commit_env || ui_print "W: Runtime transaction cleanup was incomplete"
  installer_cleanup
  return 0
}



#############
# Initialize
#############

app_init() {
  mount_partitions
  RAMDISKEXIST=false
  check_boot_ramdisk && RAMDISKEXIST=true
  get_flags
  run_migrations
  SHA1=$(grep_prop SHA1 $MAGISKTMP/.magisk/config)
  check_encryption
  get_sulist_status
  BOOTIMAGE_PATCHED=false
  [ ! -z "$SHA1" ] && BOOTIMAGE_PATCHED=true
}

export BOOTMODE=true
