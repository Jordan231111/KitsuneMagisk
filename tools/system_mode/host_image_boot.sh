#!/system/bin/sh
# Early host-image integration for a pre-authorized, per-instance root gate.
# The host stages this script and its exact APK payload in an immutable initrd.
# This route has post-fs-data timing; it does not provide magiskinit early mounts.

set -eu
M=/boot/bsr/payload
BB=$M/busybox
# Android's mksh closes the descriptor used by flock across exec.
if [ "${KITSUNE_HOST_BOOT_ASH:-}" != 1 ]; then
  export KITSUNE_HOST_BOOT_ASH=1 ASH_STANDALONE=1
  exec "$BB" sh "$0" "$@"
fi
T=/sbin
[ -f /data/adb/.bsr_root ] || exit 0
[ -d "$T" ] || T=/debug_ramdisk

case "$1" in
  post-fs-data)
    umask 077
    exec 9>/dev/.kitsune-host-image.lock
    "$BB" flock -n 9 || exit 1
    expected=$("$BB" sha256sum "$M/magisk.apk")
    expected=${expected%% *}
    if [ -f "$T/.magisk/host-image" ]; then
      [ "$("$BB" cat "$T/.magisk/host-image")" = "$expected" ]
      exit $?
    fi
    [ -d "$T" ] || exit 1
    "$M/magiskpolicy" --live --magisk 9>&-
    "$BB" mount -t tmpfs -o mode=0755 magisk "$T"
    /system/bin/chcon u:object_r:rootfs:s0 "$T"
    for file in magisk magiskpolicy stub.apk; do
      "$BB" cp "$M/$file" "$T/$file"
    done
    "$BB" chmod 0755 "$T/magisk" "$T/magiskpolicy"
    if [ -f "$M/magisk32" ]; then
      "$BB" cp "$M/magisk32" "$T/magisk32"
      "$BB" chmod 0755 "$T/magisk32"
    fi
    "$BB" ln -s ./magisk "$T/su"
    "$BB" ln -s ./magisk "$T/resetprop"
    "$BB" ln -s ./magisk "$T/magiskhide"
    "$BB" ln -s ./magiskpolicy "$T/supolicy"
    "$BB" mkdir -p "$T/.magisk/device" "$T/.magisk/worker"
    # Match magiskinit: clients can reach the authenticated socket, while the
    # worker remains private. Do not inherit the bootstrap's restrictive umask.
    "$BB" chmod 0711 "$T/.magisk" "$T/.magisk/device"
    "$BB" mount -t tmpfs -o mode=0000 magisk "$T/.magisk/worker"
    "$BB" mount --make-private "$T/.magisk/worker"
    "$BB" touch "$T/.magisk/config" "$T/.magisk/live"
    export MAGISKTMP=$T
    MAKEDEV=1 "$T/magisk" --preinit-device 9>&- || true
    rule="$T/.magisk/preinit/sepolicy.rule"
    if [ -f "$rule" ] && [ ! -e "$T/.magisk/preinit/kitsune-v2/disabled" ]; then
      "$M/magiskpolicy" --live --apply "$rule" 9>&-
    fi
    "$T/magisk" --post-fs-data 9>&-
    printf '%s\n' "$expected" > "$T/.magisk/host-image"
    ;;
  service|boot-complete|zygote-restart)
    "$T/magisk" "--$1"
    ;;
  *) exit 1 ;;
esac
