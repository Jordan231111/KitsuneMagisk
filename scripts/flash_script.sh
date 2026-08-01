#MAGISK
############################################
# Magisk Flash Script (updater-script)
############################################

##############
# Preparation
##############

# Default permissions
umask 022

OUTFD=$2
COMMONDIR=$INSTALLER/assets
CHROMEDIR=$INSTALLER/assets/chromeos

APK="$3"
MAGISKBINTMP=$INSTALLER/bin

if [ ! -f $COMMONDIR/util_functions.sh ]; then
  echo "! Unable to extract zip file!"
  exit 1
fi

# Load utility functions
. $COMMONDIR/util_functions.sh
mkdir $MAGISKBINTMP

getvar SYSTEMMODE
SYSTEMINSTALL="$SYSTEMMODE"
[ -z "$SYSTEMINSTALL" ] && SYSTEMINSTALL=false

setup_flashable

############
# Detection
############

if echo $MAGISK_VER | grep -q '\.'; then
  PRETTY_VER=$MAGISK_VER
else
  PRETTY_VER="$MAGISK_VER($MAGISK_VER_CODE)"
fi
print_title "Magisk $PRETTY_VER Installer"

is_mounted /data || mount /data || is_mounted /cache || mount /cache
mount_partitions
check_data
get_flags
if [ "$SYSTEMINSTALL" != "true" ]; then
  find_boot_image
  [ -z "$BOOTIMAGE" ] && abort "! Unable to detect target image"
  ui_print "- Target image: $BOOTIMAGE"
fi

# Detect version and architecture
api_level_arch_detect

[ $API -lt 23 ] && abort "! Magisk only support Android 6.0 and above"

ui_print "- Device platform: $ABI"

BINDIR=$INSTALLER/lib/$ABI
cd $BINDIR
for file in lib*.so; do mv "$file" "${file:3:${#file}-6}"; done
cd /
cp -af $INSTALLER/lib/$ABI32/libmagisk32.so $BINDIR/magisk32 2>/dev/null

# Recovery flashing bypasses the Kotlin install UI. Refuse a release backend
# before legacy-root removal, runtime publication, or any System Mode remount.
if [ "$SYSTEMINSTALL" = "true" ]; then
  MODE_BINARY=$BINDIR/magisk32
  [ "$IS64BIT" = "true" ] && MODE_BINARY=$BINDIR/magisk64
  chmod 755 "$MODE_BINARY" || abort "! Unable to inspect System Mode payload"
  "$MODE_BINARY" -c 2>/dev/null | grep -q ':MAGISK:D ' || \
    abort "! System Mode is disabled in release builds"
fi

# Legacy system-root cleanup is part of ordinary boot-image installation, not
# the manifest-owned System Mode transaction.
if [ "$SYSTEMINSTALL" != "true" ]; then
  $BOOTMODE || remove_system_su
fi

##############
# Environment
##############

ui_print "- Constructing environment"

# Build System Mode only in the installer staging directory. xdirect_install_system
# snapshots the old runtime before fix_env publishes this complete tree.
INSTALL_ENV=$MAGISKBIN
if [ "$SYSTEMINSTALL" = "true" ]; then
  INSTALL_ENV=$MAGISKBINTMP
fi
rm -rf "$INSTALL_ENV"/* 2>/dev/null
mkdir -p "$INSTALL_ENV" 2>/dev/null
cp -af $BINDIR/. $COMMONDIR/. $BBBIN "$INSTALL_ENV"

# Remove files only used by the Magisk app
rm -f "$INSTALL_ENV/bootctl" "$INSTALL_ENV/main.jar" \
  "$INSTALL_ENV/module_installer.sh" "$INSTALL_ENV/uninstaller.sh"

cat "$APK" >"$INSTALL_ENV/magisk.apk"
if [ "$SYSTEMINSTALL" != "true" ]; then
  cp -af "$MAGISKBIN"/* "$MAGISKBINTMP"
fi

chmod -R 755 "$INSTALL_ENV"
chmod -R 755 $MAGISKBINTMP


##################
# Image Patching
##################

ADDOND=/system/addon.d
ADDOND_MAGISK=$ADDOND/magisk

if [ "$SYSTEMINSTALL" == "true" ]; then
  rm -f ./manager.sh
  unzip -oj "$APK" "res/raw/manager.sh" || \
    abort "! System Mode installer is missing"
  [ -f ./manager.sh ] || abort "! System Mode installer is missing"
  BOOTMODE_OLD="$BOOTMODE"
  . ./manager.sh || abort "! Unable to load System Mode installer"
  rm -f ./manager.sh
  BOOTMODE="$BOOTMODE_OLD"
  . $COMMONDIR/util_functions.sh
  xdirect_install_system "$MAGISKBINTMP" "$APK" || abort "! Installation failed"
else
  install_magisk
fi

# addon.d
if [ "$SYSTEMINSTALL" != "true" ] && [ -d /system/addon.d ]; then
  ui_print "- Adding addon.d survival script"
  blockdev --setrw /dev/block/mapper/system$SLOT 2>/dev/null
  mount -o rw,remount /system || mount -o rw,remount /
  rm -rf $ADDOND/99-magisk.sh 2>/dev/null
  rm -rf $ADDOND/magisk 2>/dev/null
  if [ $ADDOND_MAGISK == $ADDOND/magisk ]; then
    mkdir -p $ADDOND/magisk
  fi
  cp -af $MAGISKBINTMP/* $ADDOND_MAGISK
  if [ $ADDOND_MAGISK == $ADDOND/magisk ]; then
    mv $ADDOND/magisk/boot_patch.sh $ADDOND/magisk/boot_patch.sh.in
  fi
  mv $ADDOND_MAGISK/addon.d.sh $ADDOND/99-magisk.sh
fi

# Cleanups
if [ "$SYSTEMINSTALL" != "true" ]; then
  $BOOTMODE || recovery_cleanup
fi
rm -rf $TMPDIR

ui_print "- Done"
exit 0
