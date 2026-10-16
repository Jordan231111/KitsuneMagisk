# Use capabilities, not an inflated Magisk version or a version-name suffix.
require_capability() {
  magisk --features 2>/dev/null | grep -qxF "$1" ||
    abort "! This module requires $1"
}

if [ -d "$MODPATH/root" ]; then
  require_capability kitsune.module-root.v2
fi
if [ -d "$MODPATH/early-mount" ]; then
  require_capability kitsune.early-mount.v2
  magisk --early-mount-status >/dev/null 2>&1 ||
    abort "! This installation route cannot provide early mounting"
fi
if [ -f "$MODPATH/boot-completed.sh" ]; then
  require_capability kitsune.boot-completed.v1
fi
