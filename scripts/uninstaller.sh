#MAGISK
############################################
# Magisk Uninstaller (updater-script)
############################################

##############
# Preparation
##############

# Default permissions
umask 022

OUTFD=$2
COMMONDIR=$INSTALLER/assets
CHROMEDIR=$INSTALLER/assets/chromeos

if [ ! -f $COMMONDIR/util_functions.sh ]; then
  echo "! Unable to extract zip file!"
  exit 1
fi

# Load utility functions
. $COMMONDIR/util_functions.sh

setup_flashable

############
# Detection
############

if echo $MAGISK_VER | grep -q '\.'; then
  PRETTY_VER=$MAGISK_VER
else
  PRETTY_VER="$MAGISK_VER($MAGISK_VER_CODE)"
fi
print_title "Magisk $PRETTY_VER Uninstaller"

is_mounted /data || mount /data || abort "! Unable to mount /data, please uninstall with the Magisk app"
mount_partitions
check_data
$DATA_DE || abort "! Cannot access /data, please uninstall with the Magisk app"
get_flags

SYSTEM_MODE_UNINSTALL=false
SYSTEM_MODE_RECEIPT_STATE="$(grep_prop STATE /data/adb/kitsune/system-mode/transaction.env)"
if [ "$(grep_prop SYSTEMMODE /system/etc/init/magisk/config)" = "true" ] ||
   [ "$SYSTEM_MODE_RECEIPT_STATE" = BOOT_VERIFIED ] ||
   [ "$SYSTEM_MODE_RECEIPT_STATE" = COMMITTED ] ||
   [ "$SYSTEM_MODE_RECEIPT_STATE" = PREFLIGHTED ] ||
   [ "$SYSTEM_MODE_RECEIPT_STATE" = STAGED ] ||
   [ "$SYSTEM_MODE_RECEIPT_STATE" = ROLLBACK_REQUIRED ] ||
   [ "$SYSTEM_MODE_RECEIPT_STATE" = ROLLING_BACK ] ||
   [ "$SYSTEM_MODE_RECEIPT_STATE" = FAILED ]; then
  SYSTEM_MODE_UNINSTALL=true
else
  find_boot_image
fi

backup_restore(){
test -f "${1}.gz" || { test -f "$1" && gzip -k "$1"; }
test -f "${1}.gz" && { rm -rf "$1" && gzip -kdf "${1}.gz"; } || return 1
}

if ! $BOOTMODE; then
    ui_print "********************************************"
    ui_print " Due to the complex of recovery environment"
    ui_print " Uninstall Magisk in Recovery is not guaranteed"
    ui_print "********************************************"
fi

# Detect version and architecture
api_level_arch_detect

ui_print "- Device platform: $ABI"

if $SYSTEM_MODE_UNINSTALL; then

# Use kernel trick to clean up mirrors automatically when installer completed
MIRRORDIR="/proc/$$/attr"
ROOTDIR="$MIRRORDIR/system_root"
SYSTEMDIR="$MIRRORDIR/system"
VENDORDIR="$MIRRORDIR/vendor"
ODM_DIR="$MIRRORDIR/odm"

abort_install(){
  umount -l "$MIRRORDIR"
  rm -rf "$MIRRORDIR"
  abort "! Installaion faled"
}

if $BOOTMODE; then
    # setup mirrors to get the original content
    mount -t tmpfs -o 'mode=0755' tmpfs "$MIRRORDIR" || return 1
    if is_rootfs; then
        ROOTDIR=/
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
fi

ui_print "--- Uninstall Magisk in system partition"

blockdev --setrw /dev/block/mapper/system$SLOT 2>/dev/null
mount -o rw,remount /system || mount -o rw,remount /
mount -o rw,remount /system_root
mount -o rw,remount /vendor
mount -o rw,remount /odm
TRANSACTION=$COMMONDIR/system_mode_transaction.sh
SM_UNINSTALL_BB=$INSTALLER/lib/$ABI/libbusybox.so
[ -f "$TRANSACTION" ] || abort "! System Mode transaction support is missing"
[ -f "$SM_UNINSTALL_BB" ] || abort "! System Mode transaction runtime is missing"
chmod 755 "$SM_UNINSTALL_BB" || abort "! Cannot prepare System Mode transaction runtime"
. "$TRANSACTION" || abort "! Cannot load System Mode transaction support"
sm_configure "$COMMONDIR" "$MIRRORDIR" /system/etc/init/magisk "$SM_UNINSTALL_BB" || \
  abort "! Cannot initialize System Mode transaction support"
ui_print "- Restoring exact manifest-owned System Mode paths"
sm_uninstall || abort "! Exact System Mode uninstall refused; use the verified external restore"
SYSTEM_MODE_UNINSTALLED=true

else

ui_print "--- Uninstall Magisk in boot image"

[ -z $BOOTIMAGE ] && abort "! Unable to detect target image"
ui_print "- Target image: $BOOTIMAGE"

BINDIR=$INSTALLER/lib/$ABI
cd $BINDIR
for file in lib*.so; do mv "$file" "${file:3:${#file}-6}"; done
cd /
cp -af $CHROMEDIR/. $BINDIR/chromeos
chmod -R 755 $BINDIR

############
# Uninstall
############

cd $BINDIR

CHROMEOS=false

ui_print "- Unpacking boot image"
# Dump image for MTD/NAND character device boot partitions
if [ -c $BOOTIMAGE ]; then
  nanddump -f boot.img $BOOTIMAGE
  BOOTNAND=$BOOTIMAGE
  BOOTIMAGE=boot.img
fi
./magiskboot unpack "$BOOTIMAGE"

case $? in
  1 )
    abort "! Unsupported/Unknown image format"
    ;;
  2 )
    ui_print "- ChromeOS boot image detected"
    CHROMEOS=true
    ;;
esac

# Restore the original boot partition path
[ "$BOOTNAND" ] && BOOTIMAGE=$BOOTNAND

# Detect boot image state
ui_print "- Checking ramdisk status"
if [ -e ramdisk.cpio ]; then
  ./magiskboot cpio ramdisk.cpio test
  STATUS=$?
else
  # Stock A only system-as-root
  STATUS=0
fi
case $((STATUS & 3)) in
  0 )  # Stock boot
    ui_print "- Stock boot image detected"
    ;;
  1 )  # Magisk patched
    ui_print "- Magisk patched image detected"
    # Find SHA1 of stock boot image
    ./magiskboot cpio ramdisk.cpio "extract .backup/.magisk config.orig"
    if [ -f config.orig ]; then
      chmod 0644 config.orig
      SHA1=$(grep_prop SHA1 config.orig)
      rm config.orig
    fi
    BACKUPDIR=/data/magisk_backup_$SHA1
    if [ -d $BACKUPDIR ]; then
      ui_print "- Restoring stock boot image"
      flash_image $BACKUPDIR/boot.img.gz $BOOTIMAGE
      for name in dtb dtbo dtbs; do
        [ -f $BACKUPDIR/${name}.img.gz ] || continue
        IMAGE=$(find_block $name$SLOT)
        [ -z $IMAGE ] && continue
        ui_print "- Restoring stock $name image"
        flash_image $BACKUPDIR/${name}.img.gz $IMAGE
      done
    else
      ui_print "! Boot image backup unavailable"
      ui_print "- Restoring ramdisk with internal backup"
      ./magiskboot cpio ramdisk.cpio restore
      if ! ./magiskboot cpio ramdisk.cpio "exists init"; then
        # A only system-as-root
        rm -f ramdisk.cpio
      fi
      ./magiskboot repack $BOOTIMAGE
      # Sign chromeos boot
      $CHROMEOS && sign_chromeos
      ui_print "- Flashing restored boot image"
      flash_image new-boot.img $BOOTIMAGE || abort "! Insufficient partition size"
    fi
    ;;
  2 )  # Unsupported
    ui_print "! Boot image patched by unsupported programs"
    abort "! Cannot uninstall"
    ;;
esac

fi

if $SYSTEM_MODE_UNINSTALL; then
  ui_print "- Preserving every path outside the System Mode ownership manifest"
else
  if $BOOTMODE; then
    ui_print "- Removing modules"
    magisk --remove-modules -n
  fi

  ui_print "- Removing Magisk files"
  rm -rf \
  /cache/magisk /cache/magisk.log /cache/magisk.apk /cache/unblock \
  /data/magisk /data/magisk.img /data/magisk_merge.img /data/cache/magisk /data/property/magisk \
  /data/Magisk.apk /data/busybox /data/custom_ramdisk_patch.sh /data/adb/magisk /data/adb/magisk.db \
  /data/adb/modules /data/adb/modules_update \
  /data/unencrypted/magisk /metadata/magisk /persist/magisk /mnt/vendor/persist/magisk

  ADDOND=/system/addon.d/99-magisk.sh
  if [ -f $ADDOND ]; then
    blockdev --setrw /dev/block/mapper/system$SLOT 2>/dev/null
    mount -o rw,remount /system || mount -o rw,remount /
    rm -f $ADDOND
  fi
fi

cd /

if $BOOTMODE; then
  ui_print "********************************************"
  ui_print " The Magisk app will uninstall itself, and"
  ui_print " the device will reboot after a few seconds"
  ui_print "********************************************"
  case "$4" in
    ""|*[!A-Za-z0-9._]*|.*|*.|*..*)
      (sleep 8; /system/bin/reboot)&
      ;;
    *.*)
      (sleep 4; pm uninstall "$4" >/dev/null 2>&1; sleep 4; /system/bin/reboot)&
      ;;
    *)
      (sleep 8; /system/bin/reboot)&
      ;;
  esac
else
  ui_print "********************************************"
  ui_print " The Magisk app will not be uninstalled"
  ui_print " Please uninstall it manually after reboot"
  ui_print "********************************************"
  recovery_cleanup
  ui_print "- Done"
fi

rm -rf $TMPDIR
exit 0
