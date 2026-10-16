# Historical Kitsune port ledger

Status: PR8 inventory accounted for; runtime parity and production promotion are separate gates.

The [machine-readable ledger](port-ledger.json) preserves all mandatory atomic IDs,
both moved-k4 source incarnations, and the original Full/Lite metadata and assets.
Commit/path assignments identify the responsible behavior; they do not establish that
the maintained implementation reproduces it. First/last fields are bounded observations
of published contracts and associated source, with per-row evidence limits. They are not
globally earliest release dates or compiled-APK/runtime attestations. Discontinuous changes
remain visible in the per-observation [source review](port-history/shipment-source-review.json).

Recorded source comparisons: 108/108. Reviewed shipment ranges:
108/108. These are source observations, not runtime acceptance.
The JSON retains exact historical paths/commits, upstream blob digests, affected users,
migration guidance and observation links for every row.

Run `python tools/port_ledger.py --report out/pr8/port-audit.json` from the repository
root. The command fails for unaccounted or changed paths, missing/corrupt archives,
missing frozen releases/assets, unaccounted metadata references and provenance gaps.
Failed retrievals have explicit evidence and a numbered disposition; they never become
recovered-byte claims. Add `--release` to reject every deferred feature and unresolved
archive disposition. Full Git history is required; this check uses no network access.
After a fresh clone, import the retained original Lite branch with:

```sh
git bundle verify docs/port-history/original-lite.bundle
git fetch --no-tags docs/port-history/original-lite.bundle refs/audit/pr8/original-lite:refs/audit/pr8/original-lite
```

## Archive dispositions

- **original-binary-source (PR10):** Fork-network IDs, immutable metadata, full byte digests and signer checks establish the retained observations. They do not authenticate the original publisher or bind every compiled member to a source tree. A historical Lite rebuild did not reproduce native text sections. Archive as evidence only. Build future managers/stubs from owned pinned source and signing keys; do not redistribute these bytes as a verified product. PR10 supplies owned signed distribution; historical signer/state transition qualification remains PR13/PR16.
- **stable-mutable-stub (PR10):** The original stable manager/stub download URLs return 404. The recovered stable APK and its independently signature-verified embedded stub have exact digests. The lost standalone stub URL is not byte-bound to that embedded member. Retire the dead endpoint. Use the owned PR10 clean-stub download contract with verified full-manager bytes and an explicit migration path; never label the embedded member as a recovered endpoint download.
- **moved-k4-compiled-source (PR10):** Both source incarnations and APK flavors are separately digested. Both APK manager scripts match d5b60627 and differ from 1279a82c in rootfs mirroring and /sbin handling; util functions match both after version substitution. Compiled native/app source correspondence is not established by these shell matches. Keep both observations and their 16 KiB/libsu differences. Do not restore a mutable tag or publish either archived APK as the selected candidate; PR10 owns signer/update migration.
- **lite-later-source (PR15):** The later Lite APK has a directly reviewed missing-/sbin shell change absent from the recovered Git tip. No complete later Lite native/app source binding is established. Preserve the artifact-only behavior in SYS-011 and test the retained capability on a qualified target in PR15. Do not infer compiled behavior from the earlier Lite source.

## Atomic decisions

Each deferral has an implementation owner and migration guidance in the JSON. These
are not acceptance results, and they cannot pass PR16. The [branch decision](pr8-branch-decision.md)
selects the development foundation while retaining these explicit product gates.

| ID | Status | Historical behavior | Observed shipment range | Decision | Owner |
|---|---|---|---|---|---|
| `SYS-001` | E | Direct-System core install on a writable system layout | `original-stable` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `SYS-002` | E | Manager entry, eligibility, warning, results, and recovery UI | `original-stable` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `SYS-003` | E | Persistent payload, ABI binaries, config, init triggers, tmpfs/applet bootstrap | `original-stable` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `SYS-004` | E | Live/offline SELinux policy selection, backup, patch, load, and restoration | `original-stable` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `SYS-005` | E | Upgrade/reinstall and migration from legacy payload, RC, config, sidecar, and addon layouts | `original-stable` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `SYS-006` | E | Exact System Mode uninstall and restoration of init/policy/runtime state | `original-stable` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `SYS-007` | E | Explicit recovery/ZIP `SYSTEMMODE` entry | `original-stable` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `SYS-008` | E | Implicit privileged `systemmagisk` filename trigger | `original-stable` → `k31-25fa2159` | `retired-with-evidence` | PR15 |
| `SYS-009` | E | Custom-ROM OTA/`addon.d` survival | `original-stable` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `SYS-010` | E | Historical API 23-24 System Mode availability/claim | `original-stable` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `HID-001` | E | Classic MagiskHide enable/disable/status independent of SuList and built-in Zygisk | `original-stable` → `k31-25fa2159` | `ported-and-tested-in-PR12` | PR12 |
| `HID-002` | E | Per-package/process add/remove/list persistence and CLI | `original-stable` → `k31-25fa2159` | `ported-and-tested-in-PR12` | PR12 |
| `HID-003` | E | Manager list/filter/system-app/expansion/per-process selection UI | `original-stable` → `k31-25fa2159` | `ported-and-tested-in-PR13` | PR13 |
| `HID-004` | E | `magiskhide exec` isolated mount-namespace command | `original-stable` → `k31-25fa2159` | `ported-and-tested-in-PR12` | PR12 |
| `HID-005` | E | SuList enable/disable, Hide dependency, reboot-only transition, and live-disable refusal | `original-stable` → `k31-25fa2159` | `ported-and-tested-in-PR12` | PR12 |
| `HID-006` | E | SuList settings/list UI and reboot-mismatch messaging | `original-stable` → `k31-25fa2159` | `ported-and-tested-in-PR13` | PR13 |
| `HID-007` | E | Inverse root/module visibility plus automatic manager/SystemUI/Settings enrollment | `original-stable` → `k31-25fa2159` | `ported-and-tested-in-PR12` | PR12 |
| `HID-008` | E | Ptrace zygote/fork monitor, USAP, preinitialized processes, app zygotes, and restarts | `original-stable` → `k31-25fa2159` | `retired-with-evidence` | PR12 |
| `HID-009` | E | Isolated/prefix matching, shared UID, secondary users/work profiles, and package rescans | `original-stable` → `k31-25fa2159` | `ported-and-tested-in-PR12` | PR12 |
| `HID-010` | E | Per-process cleanup of current Magisk/module/injected mounts; PR14 early-mount API is HID-010-early | `original-stable` → `k31-25fa2159` | `ported-and-tested-in-PR12` | PR12 |
| `HID-011` | E | Daemon's own internal namespace visibility after `ba6ed414` | `original-stable` → `k31-25fa2159` | `retired-with-evidence` | PR12 |
| `HID-012` | E | Dynamic `/system/bin` MagiskSU/applet exposure and zygote-restart unmount | `b149cd26` → `k31-25fa2159` | `retired-with-evidence` | PR12 |
| `HID-013` | E | Randomized `/dev` daemon socket and SU-request FIFO endpoints | `b149cd26` → `k31-25fa2159` | `retired-with-evidence` | PR12 |
| `HID-014` | E | Core enforcing/permissive/SELinux-absence guards; exact disabled/container runtime is HID-014-device | `k31-70068d25` → `k31-25fa2159` | `ported-and-tested-in-PR12` | PR12 |
| `HID-015` | E | `hidelist`/`sulist`/`denylist` schemas, settings, CLI/provider source of truth, and rollback migration | `original-stable` → `k31-25fa2159` | `ported-and-tested-in-PR12` | PR12 |
| `HID-016` | O | Published SU injection relocation from /system/bin to /apex/com.android.runtime/bin, /debug_ramdisk/su fallback, and compatibility-driven reversion | `metadata-bb4af859-note-md` → `metadata-2976a6f5-note-md` | `retired-with-evidence` | PR12 |
| `HID-017` | E | MagiskSU exposure delayed until boot complete | `metadata-2976a6f5-note-md` → `k31-25fa2159` | `retired-with-evidence` | PR12 |
| `HID-018` | O | Lite product's always-enforced inverse SuList/whitelist and migration to the selected product | `metadata-96724b2e-note_lite-md` → `metadata-64403347-note_lite-md` | `ported-and-tested-in-PR12` | PR12 |
| `HID-019` | O | Lite product's log-based process monitoring lifecycle and visibility contract | `metadata-96724b2e-note_lite-md` → `metadata-64403347-note_lite-md` | `ported-and-tested-in-PR12` | PR12 |
| `ZYG-001` | O | Older-tag built-in API v5, module loading, companion IPC, memfd loading, unloading and Riru exclusion | `b149cd26` → `k27-2-4-archive` | `inherited-and-tested` | PR11 |
| `ZYG-001-hide` | O | Older-tag built-in Zygisk integration with classic Hide/SuList | `b149cd26` → `k27-2-4-archive` | `ported-and-tested-in-PR12` | PR12 |
| `ZYG-002` | O | Ptrace-init injection architecture | `original-stable` → `k27-2-4-archive` | `retired-with-evidence` | PR11 |
| `ZYG-003` | O | GrapheneOS Android 14/QPR2 JNI specialization descriptors | `metadata-bb4af859-note-md` → `k27-2-4-archive` | `ported-and-tested-in-PR11` | PR11 |
| `ZYG-003-device` | O | Physical GrapheneOS Android 14/QPR2 injection and recovery qualification | `metadata-bb4af859-note-md` → `k27-2-4-archive` | `deferred-to-PR15` | PR15 |
| `ZYG-004` | O | Futile Hide handling for Zygisk module libraries | `b149cd26` → `k27-2-4-archive` | `retired-with-evidence` | PR11 |
| `ZYG-005` | O | Faster revert/unmount path from `7072f9e9` | `b149cd26` → `k27-2-4-archive` | `retired-with-evidence` | PR11 |
| `ZYG-006` | E | Zygote restart-monitor reliability fix from `be732edb` | `k27-2-1` → `k31-25fa2159` | `inherited-and-tested` | PR11 |
| `ZYG-007` | E | Built-in removal, mandatory external-only direction and provider boundary | `k31-70068d25` → `k31-25fa2159` | `retired-with-evidence` | PR11 |
| `ZYG-007-adapter` | E | External-provider Next-manager discovery, Hide/SuList and process-selection integration | `k31-70068d25` → `k31-25fa2159` | `ported-and-tested-in-PR12` | PR12 |
| `ZYG-008` | O | Lite product's explicitly no-Zygisk behavior and migration/retirement impact | `metadata-96724b2e-note_lite-md` → `metadata-64403347-note_lite-md` | `ported-and-tested-in-PR11` | PR11 |
| `MOD-001` | E | Global early-mount files and ordering | `original-stable` → `k31-25fa2159` | `deferred-to-PR14` | PR14 |
| `MOD-002` | E | Per-module `early-mount` storage, install, disable, and removal | `original-stable` → `k31-25fa2159` | `deferred-to-PR14` | PR14 |
| `MOD-003` | E | `initrc.d` init-RC injection | `original-stable` → `k31-25fa2159` | `deferred-to-PR14` | PR14 |
| `MOD-004` | E | `MODDIR/root` standard and expanded partition API | `metadata-29aaacaa-note-md` → `k31-25fa2159` | `deferred-to-PR14` | PR14 |
| `MOD-005` | O | Conditional Magisk binary injection when PATH already exposes tools | `metadata-bb4af859-note-md` → `metadata-c5d500af-note-md` | `deferred-to-PR14` | PR14 |
| `MOD-006` | E | Systemless-hosts read-only and visibility behavior | `metadata-bb4af859-note-md` → `k31-25fa2159` | `deferred-to-PR14` | PR14 |
| `MOD-007` | E | Mirrorless magic-mount topology and visibility | `metadata-51fad676-note-md` → `k31-25fa2159` | `deferred-to-PR14` | PR14 |
| `MOD-008` | E | Shared tmpfs mount propagation to later namespaces | `metadata-2976a6f5-note-md` → `k31-25fa2159` | `deferred-to-PR14` | PR14 |
| `MOD-009` | E | `REMOVE` char-device whiteout deletion | `original-stable` → `k31-25fa2159` | `deferred-to-PR14` | PR14 |
| `MOD-010` | E | `.replace` directory semantics | `original-stable` → `k31-25fa2159` | `deferred-to-PR14` | PR14 |
| `MOD-011` | E | Live `sepolicy.rule` application and pre-init refresh/synchronization | `original-stable` → `k31-25fa2159` | `deferred-to-PR14` | PR14 |
| `MOD-012` | E | Empty-line-safe module policy parser | `k27-2-1` → `k31-25fa2159` | `deferred-to-PR14` | PR14 |
| `MOD-013` | E | `action.sh` discovery and execution backend | `k27-2-2` → `k31-25fa2159` | `deferred-to-PR14` | PR14 |
| `MOD-014` | E | Module Action manager surface | `k27-2-2` → `k31-25fa2159` | `ported-and-tested-in-PR13` | PR13 |
| `MOD-015` | E | Per-module `boot-completed.sh` stage | `k27-2-3` → `k31-25fa2159` | `deferred-to-PR14` | PR14 |
| `MOD-016` | E | Ordinary install/update/disable/remove/rollback, boot scripts, interruption, and safe mode | `original-stable` → `k31-25fa2159` | `deferred-to-PR14` | PR14 |
| `MOD-017` | E | Pre-init storage preference, no-`/persist` policy, and cache/metadata/data migration | `k27-2-1` → `k31-25fa2159` | `deferred-to-PR14` | PR14 |
| `MOD-018` | E | `.magisk/block` to `.magisk/device` layout compatibility | `b149cd26` → `k31-25fa2159` | `deferred-to-PR14` | PR14 |
| `MOD-019` | O | `/apex` peer-group propagation and its namespace/detection behavior | `metadata-96724b2e-note_lite-md` → `metadata-7fd85617-note-md` | `deferred-to-PR14` | PR14 |
| `MOD-020` | E | Correct symlink handling distinct from `REMOVE` whiteout character devices, including upgrade from the shipped bug | `metadata-bb4af859-note-md` → `k31-25fa2159` | `deferred-to-PR14` | PR14 |
| `MOD-021` | O | Historical module-template ZIP bytes, endpoint, guards, extended APIs and author migration | `module-template` → `module-template` | `deferred-to-PR14` | PR14 |
| `MOD-022` | O | Lite product's published modules-unsupported contract and user migration | `metadata-96724b2e-note_lite-md` → `metadata-64403347-note_lite-md` | `deferred-to-PR14` | PR14 |
| `SU-001` | E | Disabled, system-credential, and manager biometric authentication modes | `metadata-51fad676-note-md` → `k31-25fa2159` | `ported-and-tested-in-PR13` | PR13 |
| `SU-002` | E | Prompt/allow/deny/revoke/timeout, namespace, shared UID, multiuser, and no-manager lifecycle | `original-stable` → `k31-25fa2159` | `ported-and-tested-in-PR13` | PR13 |
| `SU-003` | E | Deterministic manager policy-row retrieval (`LIMIT 1`) | `k27-2-4-inherited` → `k31-25fa2159` | `inherited-and-tested` | PR13 |
| `SU-004` | E | Core authorization/restorecon behavior and SELinux-absence checks; exact disabled targets are SU-004-device | `original-stable` → `k31-25fa2159` | `ported-and-tested-in-PR12` | PR12 |
| `ID-001` | E | Kitsune package/application identity and upgrade path from `io.github.huskydg.magisk` | `original-stable` → `k31-25fa2159` | `ported-and-tested-in-PR9` | PR9 |
| `APP-001` | E | Kitsune Mask name, labels, logos/colors, attribution, and unofficial-build warning | `original-stable` → `k31-25fa2159` | `ported-and-tested-in-PR13` | PR13 |
| `APP-002` | E | Hidden-manager create/launch/upgrade/restore plus `com.*` random package generation | `k27-2-4-inherited` → `k31-25fa2159` | `ported-and-tested-in-PR13` | PR13 |
| `APP-003` | E | Manager/stub signature enforcement and retirement of the global bypass | `metadata-29aaacaa-note-md` → `k31-25fa2159` | `retired-with-evidence` | PR10 |
| `APP-004` | E | API 34 download engine, user-initiated jobs, foreground-service behavior, and suspendable I/O | `k27-2-1` → `k31-25fa2159` | `ported-and-tested-in-PR10` | PR10 |
| `APP-005` | E | Stub API 34 loading, expected-version self-repair, and manager-driven stub upgrade | `original-stable` → `k31-25fa2159` | `ported-and-tested-in-PR10` | PR10 |
| `APP-006` | E | HuskyDG/1q23 endpoints, channels, full-manager/stub URL migration, and failure behavior | `original-stable` → `k31-25fa2159` | `ported-and-tested-in-PR10` | PR10 |
| `APP-007` | E | FakeGMS ProviderInstaller restricted to system GMS | `b149cd26` → `k31-25fa2159` | `inherited-and-tested` | PR13 |
| `APP-008` | E | Inactive-slot OTA prompt requiring the System Updates Restart action | `k27-2-1` → `k31-25fa2159` | `ported-and-tested-in-PR13` | PR13 |
| `APP-009` | E | Module-action close/focus/auto-close behavior | `k27-2-3` → `k31-25fa2159` | `retired-with-evidence` | PR13 |
| `APP-010` | E | Flash/action orientation-lock behavior | `k27-2-3` → `k31-25fa2159` | `ported-and-tested-in-PR13` | PR13 |
| `APP-011` | E | Maintained zh-CN, zh-TW, Vietnamese, Russian, and Turkish fork strings/locales | `original-stable` → `k31-25fa2159` | `ported-and-tested-in-PR13` | PR13 |
| `APP-012` | E | Historical non-obfuscated manager/stub disposition | `k31-70068d25` → `k31-25fa2159` | `retired-with-evidence` | PR10 |
| `APP-013` | E | libsu `5.3.0` transition, `5.2.2` rollback and custom `DispatcherExecutor` task semantics | `original-stable` → `k31-25fa2159` | `inherited-and-tested` | PR13 |
| `ID-002` | E | Fake `29999`/`31000` identity and migration to truthful compatibility fields | `k31-70068d25` → `k31-25fa2159` | `retired-with-evidence` | PR9 |
| `SEC-001` | E | Debug ADB-shell automatic root, confined to a non-publishable lab flavor or retired | `metadata-51fad676-note-md` → `k31-25fa2159` | `retired-with-evidence` | PR15 |
| `SEC-002` | E | Public `TestKey-2024.jks`, runtime signature bypass, and production-signer migration | `metadata-29aaacaa-note-md` → `k31-25fa2159` | `retired-with-evidence` | PR10 |
| `DIST-001` | E | v31 debug/release manager-and-stub four-APK asset surface and source/tag provenance | `k31-70068d25` → `k31-25fa2159` | `ported-and-tested-in-PR10` | PR10 |
| `DIST-002` | E | Both observed `v27.2-kitsune-4` source/release incarnations and intervening behavior | `k27-2-4-inherited` → `k27-2-4-archive` | `ported-and-tested-in-PR10` | PR10 |
| `DIST-003` | O | Original Full stable/R65/R66 manager-stub channels, assets and upgrade migration | `original-stable` → `metadata-05003cd3-note-md` | `retired-with-evidence` | PR13 |
| `DIST-004` | O | Original Lite always-SuList/no-Zygisk/no-modules/log-monitoring product and migration/retirement | `metadata-96724b2e-note_lite-md` → `metadata-64403347-note_lite-md` | `retired-with-evidence` | PR12 |
| `BOOT-001` | E | P-521 boot-image signing and verification | `k27-2-4-inherited` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `BOOT-002` | E | 16 KiB binary/ZIP/runtime support, including disable/re-enable history | `k27-2-4-inherited` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `BOOT-003` | E | Tar input/output with entries larger than 8 GiB | `k27-2-1` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `BOOT-004` | E | `magiskboot split -n` compressed-kernel preservation | `k27-2-1` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `BOOT-005` | E | 2SI transition avoiding `/init` hexpatch when possible | `metadata-96724b2e-note_lite-md` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `BOOT-006` | E | Sony `init.real` handling removal and resulting legacy-Sony compatibility | `k27-2-1` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `DEV-001` | E | Emulator `/sbin` PATH and missing-directory creation | `original-stable` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `DEV-002` | E | Vivo `do_mount_check` kernel workaround | `k27-2-2` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `DEV-003` | E | Historical Samsung PROCA disable/rename workaround | `k27-2-2` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `ORD-001` | E | Select-and-Patch for `boot`, `init_boot`, `vendor_boot`, recovery, and supported archives | `original-stable` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `ORD-002` | E | Ordinary Direct Install | `original-stable` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `ORD-003` | E | Inactive-slot installation and OTA completion | `original-stable` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `ORD-004` | E | Ordinary recovery ZIP install/uninstall and stock restoration | `original-stable` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `ORD-005` | E | Emulator live setup independent of persistent System Mode | `original-stable` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `ORD-006` | E | MagiskBoot, `resetprop`, `magiskpolicy`, multicall applets, and bundled BusyBox compatibility | `original-stable` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `ORD-007` | E | Read-only resetprop serial-counter behavior and property waiter/override semantics | `metadata-02ba306e-note-md` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `BOOT-007` | E | Require the full /system/etc/init/hw/init.rc path before selecting the Android 11 init-RC location | `metadata-51fad676-note-md` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `SYS-011` | O | Later Lite selects existing /debug_ramdisk when /sbin is absent instead of creating /sbin | `metadata-64403347-note_lite-md` → `metadata-64403347-note_lite-md` | `deferred-to-PR15` | PR15 |
| `HID-010-early` | E | Cleanup of the separately implemented PR14 early-mount and expanded-partition API | `original-stable` → `k31-25fa2159` | `deferred-to-PR14` | PR14 |
| `HID-014-device` | E | Exact SELinux-disabled/Waydroid runtime qualification | `k31-70068d25` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |
| `SU-004-device` | E | SU and restorecon on exact SELinux-disabled/container targets | `original-stable` → `k31-25fa2159` | `deferred-to-PR15` | PR15 |

## Archive limits

Metadata bytes are retained under `port-history/metadata/` with immutable commit URLs
and SHA-256 digests. APK and source-ZIP observations record exact byte digests and
signer checks; downloaded binary/source archives remain in the ignored local lab cache.
A mirror, matching version label, or co-published source ZIP does not by itself prove
the original publisher or a reproducible source-to-APK relationship. The unresolved
dispositions above block the release check. Original metadata and module assets are attributed to HuskyDG
and their recorded source repositories; the project retains its GPL licensing.

Generated by `python tools/port_ledger.py --markdown docs/port-ledger.md`.
