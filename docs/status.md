# KitsuneMagisk current support status

Last reviewed: 2026-07-31

Current hardening worktree: `codex/production-hardening`, based on `kitsune` at
`bcdf65f0af1882e46cd435379bee4535e4aab87f`

Official comparison points: Magisk `v30.7` at
`e8a58776f1d7bdf852072ad0baa6eceb9a1e4aac` and observed `master` at
`0404e0ca346d9bdeeb9fe9d951c599c05b2583f6`.

## Release level

KitsuneMagisk remains an experimental development line. Earlier maintainer/test artifacts exist,
but the resumed project has not shipped its first production release. The current hardening work
fixes several release-critical defects, but it is not yet a stable System Mode release. The inherited `31.0`
value is a fork compatibility and Android upgrade-ordering value; it does not mean that this branch
contains a newer Magisk core than official Magisk v30.7.

The repository no longer contains a signing keystore or a maintainer-specific `config.prop`.
Debug builds use the Android debug signer. Release builds fail closed unless an external signing
configuration is supplied. A configuration-free local build reports the source identity selected
by `build.py`; `31.0-kitsune` is retained only by an explicitly supplied compatibility/release
configuration or an already-published artifact.

## Current evidence

| Area | Evidence | Status |
|---|---|---|
| Host/build gates | Security, System Mode, migration, artifact, JVM, lint, shell syntax, and canonical all-ABI build gates exist. The artifact gate checks both ELF load segments and APK ZIP placement for 16 KiB pages. The final local run passed 87 host tests, JVM tests, zero-error lint, security ledgers, clean all-ABI debug/release builds, and the schema-3 four-APK artifact contract. | Passed locally; PR CI and a final merged-commit release rebuild remain required |
| Ordinary Magisk flow | Prior API 23/29/35 CI and API 35 ARM64 disposable-AVD records cover patched-ramdisk setup, reboot, manager self-test, and `su -c id`. The one real BlueStacks `Tiramisu64` instance also cold-booted the released comparison and intensive-test debug/release pair, then the exact final debug backend, with app/databin/daemon alignment and sole-root verification. | Preserved on tested targets; physical-device qualification remains open |
| Release signature trust | Release native code now enforces manager/stub certificate identity; debug-only relaxation remains explicit. The final four-APK artifact contract rejects the historical public TestKey. The intensive-test live release manager/backend matched the same external lab signer used by the final contract-tested release APK. | Fixed in this worktree; production key transition remains blocked |
| Hide/DenyList compatibility | Fresh installs simply use the current tables. When an older Kitsune/Delta test install already has legacy `hidelist` rows, the daemon keeps database `user_version=12`, creates a durable pre-change backup, transactionally unions valid rows into `denylist`, and records an internal completion marker. On `Tiramisu64`, the active DB and backup both passed `quick_check`; the backup stayed root-owned mode `0600`, schema 12, and unmarked while the active DB carried the complete marker. | Rollback-compatible implementation present |
| App/stub updater | Built-in inherited endpoints make no request and fail explicitly. Custom metadata is HTTPS-only, requires SHA-256, rejects cleartext redirects, verifies the downloaded bytes, and publishes the APK atomically. | Transport/artifact contract hardened; no project-owned service |
| Direct-System/System Mode | The action is debug-only, displays an explicit warning, uses the payload binary's compile identity rather than a possibly older running daemon, enters a private mount namespace, performs non-mutating policy capability inspection, checks remount/write capability, and rolls back exact policy/init sidecars on failure. The live release UI omitted the action; the debug UI rejected read-only `/system` before mutation with identical pre/post init-policy hashes. | Experimental; transaction and writable-target qualification incomplete |
| BlueStacks | The local adapter cleanly reinjected the released comparison, intensive-test pair, and exact final debug APK into the same installed BlueStacks Air 5.21.782.7501 `Tiramisu64` instance at `127.0.0.1:5555`; no application clone was used for the final comparison. Its ext4 `/system` is mounted read-only, so System Mode is unsupported on this image. Host logs show clean code-0 exits, orphaned bundled-ADB/player-lock failures, and—while restoring the unchanged known-good APK—a BlueStacks `launch_start_timeout`, failed `qemu-img info`, and forced power-down. The identical payload passed on immediate retry. | Root/backend adapter proven for this exact instance; observed intermittent failure is strongly BlueStacks-side; in-guest System Mode unsupported |
| Existing released artifact | The previous released manager/root backend remains useful as the behavioral comparison. Its certificate is the repository's former publicly exposed test key and must not be reused for a production release. | Comparison-only; not a production trust root |
| Zygisk/provider behavior | The manager reports built-in Zygisk as disabled while the installed external ReZygisk processes run. All three existing module directories remained enabled throughout the one-instance comparison, so this audit does not attribute the intermittent BlueStacks launch failures to those modules. ReZygisk v1.0.0 explicitly removed Kitsune support; NeoZygisk has a different ptrace/provider model and no universal Kitsune SuList contract. | Exactly one named provider/version still needs formal qualification |

## Remaining release blockers

1. Use PR CI to rebuild the reviewed commit and rebuild any distribution artifacts from the final
   merged commit. The archived local APKs identify their `bcdf65f0` base and are pre-commit lab
   evidence, not release candidates.
2. Configure a new external production signing identity and design an explicit migration from the
   leaked historical certificate. Android package replacement and the old daemon's certificate
   trust make this a product migration, not a keystore swap.
3. Finish manifest-owned, crash-recoverable transactions across `/data/adb/magisk`, the persistent
   system payload, init/policy files, addon survival, upgrade, and uninstall. The current worktree
   improves rollback but is not power-loss atomic across every filesystem.
4. Qualify System Mode on at least one exact writable target through install, three cold boots,
   upgrade, reinstall, modules/root policy, uninstall, and snapshot/stock restore. The BlueStacks
   read-only-system rejection is valuable negative evidence, not a successful persistent install.
5. Keep release System Mode disabled until that lifecycle evidence exists. A successful compile,
   root shell, or normal Magisk backend does not prove persistent System Mode.
6. Forward-port the tested product behavior onto an audited current official stable rather than
   independently merging the roughly 985 official-master-only commits into this old core.
7. Establish a project-owned, signed or equivalently authenticated update service. The SHA-256 and
   atomic-download implementation protects supplied metadata but does not make an arbitrary custom
   endpoint trustworthy.
8. Select and qualify one Zygisk architecture. Do not enable built-in and external providers
   together, and do not advertise ReZygisk v1.0.0 as Kitsune-compatible.
9. Complete physical-device, commercial-emulator, ABI/runtime, recovery, module, hidden-manager,
   multiuser, SELinux, safe-mode, and failure-injection matrices for every support claim.
10. Resolve or explicitly carry the remaining dependency/security holds, including the no-fixed-
   version RSA timing advisory, through the release evidence ledger.

## Recommended development order

1. Review the current production-hardening work only after its final host/build/device report is
   green, then merge it as one coherent security/reliability change set.
2. Complete the current-line System Mode manifest/recovery transaction only to the extent needed to
   produce trustworthy comparison behavior.
3. Create the audited stable-based `next-system` line, preserve ordinary upstream Magisk and
   built-in Zygisk behavior, then port the System Mode vertical slice behind the same tests.
4. Run identical target snapshots against the earlier comparison build, hardened current line,
   and stable-based port; promote only the implementation that meets the complete lifecycle gate.
5. Before distributing the first production release, create one protected external release key and
   complete the transition away from the public historical test certificate.

Support remains attached to an exact emulator/device version, Android image, ABI, filesystem,
bootstrap, adapter, and tested lifecycle. See [`DEVELOPMENT_ROADMAP.md`](../DEVELOPMENT_ROADMAP.md)
for the ordered work and [the faithfulness audit](faithfulness-audit.md) for the historical rationale.
