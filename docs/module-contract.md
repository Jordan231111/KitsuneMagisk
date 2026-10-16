# Module API and recovery contract

PR14 implements module-root v2, early-mount v2 and the bounded boot-completed
stage on the maintained Magisk core. Runtime qualification is recorded separately;
compiled capabilities do not qualify a device or an installation route. Authors can
start from the [capability-checked template](../tools/module-template/README.md).

## Installation and ordinary mounts

Module IDs contain ASCII letters, digits, `.`, `_` and `-`, and cannot begin
with a dot. Installation/customization uses a private staging directory. The
published pending update is complete and synced; a previous pending update is
recoverable after an interrupted rename. At boot, the old installed directory is
retained until its replacement has been renamed and synced. Failed activation
skips the old ordinary module while a new version is pending; it does not mix
old scripts with a new early plan. Disabled state survives
updates. Safe mode also disables pending modules before they can be activated. Hidden staging and rollback directories are never loaded as modules.

Modules run and contribute files in bytewise module-ID order. The first ordinary
file wins a collision; `system/` files take precedence over `root/` files at the
same target. This replaces the historical dependence on filesystem enumeration
order. Modules must not depend on install order to select a winner.

The existing Rust mount engine implements `system/` and `root/` together, including
regular files, symlinks, 0:0 character-device deletion nodes and `.replace`.
`root/` accepts existing directories named `system`, `vendor`, `product`,
`system_ext`, `odm`, `system_dlkm`, `vendor_dlkm`, `odm_dlkm`, `oem`, `prism`,
`optics`, `my_custom`, `my_engineering`, `my_heytap`, `my_manifest`, `my_preload`,
`my_product`, `my_region`, `my_stock`, `my_version`, `my_company`, `my_carrier`
and `my_bigball`. Standard vendor/product/system_ext aliases beneath `/system`
use that same tree. Missing partitions are logged and skipped. A partition root
is never replaced with tmpfs just to create a previously absent partition.

`root/apex` is unsupported. APEX activation and process-specific mount namespaces
require an APEX-aware module; a post-fs-data directory overlay is not an APEX
replacement API. This is an explicit migration from the historical broad root API.

`action.sh`, `post-fs-data.sh`, `service.sh`, systemless hosts and PATH injection
continue through the maintained core. Hosts are edited through the module file;
the read-only runtime module mount is not a writable hosts API.

## Early mounting

Check both `magisk --features` for `kitsune.early-mount.v2` and
`magisk --early-mount-status` for the running route. A successful status requires
MagiskInit and available pre-init storage. Managed System Mode and live setup
cannot advertise that they run before Android init's early consumers. An installer
rejects a module requesting early mounting when the current route lacks it.

Input is `MODDIR/early-mount/` or the explicitly managed global
`/data/adb/early-mount.d/`. Run `copy_preinit_files` after editing global input. A `disable` marker in that
global directory suppresses it. Safe mode and remove-modules create the marker;
remove it explicitly before re-enabling global input.
Do not copy a historical cache tree into a newly selected partition automatically.

V2 publishes only existing regular files below system, vendor, product, system_ext
and odm. Standard partition aliases are recognized. Paths use ASCII letters/digits and `._-+@` within components. Other symlink traversal,
new files/directories, `.replace`, device nodes and early whiteouts are unsupported;
use ordinary mounts for these operations. The historical early implementation also
only bound existing non-symlink targets. Init binaries, linkers and the init/SELinux
configuration directories cannot be replaced through the early-file interface.

The publisher builds a private generation with a source hash manifest and module
metadata, syncs it and atomically switches `current`. A disabled marker is made
durable first. Failure leaves early modules inhibited and the previous complete
generation available. One previous generation is retained; unpublished generations are removed under
the publication lock. Preflight accounts for logical file sizes and directories
and reserves free space for Android, including after an interrupted copy. Native init validates
hashes, root ownership, non-writable-to-others files, path types, a 512-file/32-MiB
limit and a two-second processing budget. An invalid snapshot also suppresses its module policy. Global/module conflicts reject the
complete plan and name both owners. No ambiguous last-writer-wins rule survives.

Payloads are copied into a private tmpfs, given the existing target's ownership,
permissions and SELinux context, then bound before Android init continues. The
payload tmpfs is remounted read-only. A failed publication detaches already
published mounts in reverse order. If an enabled module or global input needs a
failed/disabled early snapshot, post-fs-data enters safe mode and disables modules
before running their scripts. Re-enable repaired input explicitly. The underlying partition remains unchanged.
Hide/SuList sees these as `magisk` mounts and removes them in selected namespaces.

Hold volume-down for three seconds during MagiskInit to bypass the early snapshot
and module policy, then disable modules in the ordinary safe-mode path. A durable
`kitsune-v2/disabled` file in the selected pre-init directory also inhibits it and
can be created from recovery while data is encrypted. Removing modules or entering
safe mode inhibits the stored snapshot. Stock boot-image restoration remains the
external recovery path if the device cannot reach its early key check.

## Init RC and boot-completed scripts

Modern MagiskInit routes accept `early-mount/initrc.d/*.rc` with a restricted,
versioned grammar. Service names start with `kitsune_<module-id>_` (or
`kitsune_global_`), executable paths start with `/system/bin/`, and every service
must include `disabled`, `oneshot` and `timeout_period 10`. Optional root user/group
and `seclabel u:r:magisk:s0` are supported. Allowed action events are `early-init`,
`init`, `post-fs` and `boot`; actions may start a service declared in the same file
or set a `sys.kitsune.*` property. Duplicate service names reject the plan.

Arbitrary imports, property-trigger loops, service overrides, critical/restarting
services and synchronous `exec`/`insmod` are not carried into v2. Move work into a
bounded oneshot service, or ordinary `post-fs-data.sh`/`service.sh` as appropriate.
The published historical template's unbounded `insmod` example needs migration.
Android init's [service timeout and oneshot rules](https://android.googlesource.com/platform/system/core/+/android16-release/init/README.md)
provide the service deadline. These are privileged module APIs, not a sandbox for
hostile root code or a guarantee that a wedged kernel driver is killable.

`boot-completed.sh` starts once per boot, in module-ID order, after the boot-complete
callback. Each script has a ten-second deadline and the whole batch a two-minute
budget; timeouts are logged and the script process group is killed. Disable/remove
markers are rechecked before each script. Safe mode runs none of them. Use
`service.sh` for long-running service work.

## Policy and pre-init storage

Policy publication is atomic and reflects install/update/disable/remove. Managed
System Mode uses its transaction-owned `/data/adb/sepolicy.rule` after data becomes
available, even when no raw pre-init partition is eligible. Update
selection never mixes a partly extracted replacement with an old module. A failed
copy cannot leave a truncated active rule file. Disabling a module takes effect
at the next clean policy load: arbitrary live policy changes are not reversible,
so this API does not pretend that removing `sepolicy.rule` removes rules already
loaded in the kernel. The core's own root policy must still load if module policy
storage is unreadable. SELinux absence never creates a fake enforcing result.

New storage selection excludes `/persist` and `/mnt/vendor/persist`; device
calibration is not module storage. Existing persist contents remain untouched for recovery, but are not replayed
as module policy or early input. Repatch the boot image to select permitted storage.
The daemon does not expose a writable persist link.
An empty runtime placeholder does not count as persistent pre-init storage. Selection retains upstream encryption/filesystem
checks and never mounts a block device writable. Migration requires publishing to
the currently selected location and repatching the relevant boot image; old storage
is retained until that boot and recovery are verified. Missing storage means early
mounting is unavailable; ordinary Magisk/module use remains independent.
