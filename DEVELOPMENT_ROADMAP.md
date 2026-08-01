# KitsuneMagisk development roadmap

Last reconciled: 2026-07-31

This is the active execution plan. Historical commit analysis belongs in
[`docs/faithfulness-audit.md`](docs/faithfulness-audit.md), reproducible upstream and security
evidence belongs in [`docs/security-lab.md`](docs/security-lab.md), and current user-facing support
claims belong in [`docs/status.md`](docs/status.md).

## Product direction

KitsuneMagisk remains a Magisk fork, not a separate rooting framework. Development should preserve
the parts of Magisk users already depend on while making the fork's differentiators dependable.

Priorities, in order:

1. **Direct-System/System Mode** for controlled Android environments where boot-image installation
   is unavailable or impractical. This is the release-defining Kitsune feature.
2. **Safe root lifecycle**: install, cold boot, upgrade, reinstall, modules, superuser policy,
   uninstall, rollback, and a proven snapshot or stock-image restore.
3. **Ordinary Magisk compatibility**: Select-and-Patch, Direct Install, inactive-slot, recovery,
   modules, MagiskSU, and upstream boot/init behavior must not silently regress.
4. **Kitsune privacy behavior**: MagiskHide, DenyList, and SuList are important secondary features,
   but their database, namespace, and provider semantics must be measured rather than inferred from
   a UI label.
5. **Maintainer and research tooling**: differential tests, parser hardening, and emulator adapters
   should produce reproducible fixes and support records, not unsupported universal claims.

System Mode support is attached to an exact emulator/device version, Android image, ABI,
filesystem, bootstrap, and recovery procedure. A successful all-ABI build, an ADB connection, or a
working `su` shell does not prove persistent System Mode support.

## Release state

The resumed project has not shipped its first production release. Earlier maintainer releases and
local/test builds still matter as comparison and upgrade inputs, but they are not a production
trust root for the resumed project.

The current hardening worktree is `codex/production-hardening`, based on `kitsune` at
`bcdf65f0af1882e46cd435379bee4535e4aab87f`. The current official comparison is Magisk v30.7;
re-check the official release list when cutting a new base.

The inherited `31.0`/`31000` identity is a compatibility and Android upgrade-ordering value from
prior maintainers. It is not evidence that this tree contains a newer Magisk core than official
Magisk. Separate product version, Android version code, upstream base, compatibility epoch, and
protocol versions before the first production release.

## Start here

Work in this order. Do not select work from an older audit note ahead of these gates.

### 1. Finish and review the current hardening change

- Run the complete host, JVM, lint, shell, all-ABI debug/release, signing, ELF, and artifact-identity
  checks on the exact final source.
- Compare the released pre-change Kitsune backend with the final debug and release artifacts on the
  same recoverable ADB target, then restore its recorded starting payload and root state.
- Exercise the debug System Mode warning and immutable-filesystem rejection, then verify no mount,
  transaction, database, installer-session, or temporary-payload residue remains.
- Keep a small concurrent `su` regression in CI. Keep large stress loops and destructive lifecycle
  experiments local to disposable targets, record the result, and delete their temporary payloads.
- Merge this work as one security/reliability unit. Do not mix a broad upstream merge or unrelated
  dependency-major updates into it.

Exit: every retained test is green, the exact APK hashes/certificates are recorded, ordinary root
still works, release System Mode is unreachable, and remaining limitations are stated explicitly.

Local status on 2026-07-31: this exit is satisfied for the reviewed hardening candidate and its
single-instance evidence. The recorded local APKs are pre-commit evidence that identify the
`bcdf65f0` base; PR CI and any distribution build must rebuild the committed source so artifact
identity points at the exact reviewed commit.

### 2. Characterize one writable current-line System Mode target

Use a snapshot-capable LDPlayer, MuMu, Nox, custom image, or other controlled target with bootstrap
root and a genuinely writable persistent layout. BlueStacks ADB alone is not sufficient; the local
BlueStacks ext4 system image remains mounted read-only and is a negative capability record.

- Record the exact emulator/image version, fingerprint, ABI, page size, mount sources, filesystems,
  AVB/dm-verity state, init import path, SELinux policy source, bootstrap root, and restore command.
- Treat emulator host lifecycle as part of the adapter contract: stop the exact bundled ADB server
  when it outlives the player, attest the launched application path, and distinguish a host-lock or
  player-exit failure from an Android/Magisk boot failure.
- Run the released comparison build before the hardened debug build on restored copies of the same
  snapshot.
- Cover install, three cold boots, host restart, root policy, one simple module, upgrade, reinstall,
  failure rollback, uninstall, and verified snapshot/image restore.
- Disable vendor root after installation wherever persistent independence is claimed.

Exit: one exact writable target has a repeatable baseline-versus-hardened lifecycle record. If no
target passes, keep System Mode experimental and use the failures to define the adapter work.

### 3. Finish the current-line System Mode transaction only as needed

The present hardening adds preflight checks, a private mount namespace, non-mutating policy
inspection, exact in-process rollback copies, and safer runtime publication. It is not yet
power-loss atomic across persistent system files, `/data/adb/magisk`, addon survival, and uninstall.

Required before enabling System Mode in a release build:

- one versioned install manifest containing owned paths, original metadata/digests, payload
  digests, adapter, policy/init strategy, source identity, and transaction state;
- deterministic recovery of interrupted `STAGED`, `COMMITTED`, `ROLLBACK_REQUIRED`, and
  `UNINSTALLED` states after process death or power loss;
- exact-path, hash-aware uninstall with no wildcard deletion;
- an external verified backup or snapshot, not only rollback files on the modified image;
- layout-aware runtime selection instead of assuming `/sbin` everywhere;
- explicit recovery-mode selection with no filename magic or unconditional boot-image prerequisite;
- a proven init import and actual next-boot SELinux policy selection, including split/precompiled
  policy layouts;
- API 23-24 excluded from System Mode unless a dedicated persistent launch path is cold-boot proven.

Carry fixes on the old core only when they are necessary to obtain trustworthy comparison behavior
or a deliberate current-line candidate. Do not modernize both old and new cores indefinitely.

### 4. Forward-port the product onto the latest audited official stable

Create `next-system` from the latest official stable at branch cut (v30.7 at this checkpoint). This
is a forward-port of Kitsune's product knowledge, not a rewrite of Magisk.

- Build and boot the pristine official base first.
- Preserve upstream Select-and-Patch, Direct Install, inactive-slot, recovery, modules, MagiskSU,
  current SELinux, and built-in Zygisk behavior in the baseline.
- Port System Mode as the first end-to-end vertical slice: capability probe, staging, manifest,
  launcher/init integration, policy strategy, transaction recovery, uninstall, and adapters.
- Reuse maintained upstream live-setup and policy primitives where they satisfy the tested contract.
  Add a native hook only for a named missing capability with a failing target-backed test.
- Run the same snapshots and lifecycle driver against current `kitsune` and `next-system`.

Exit: promote `next-system` only if it meets or beats the current line on required targets and keeps
ordinary Magisk behavior. Preserve the old implementation as a tag after promotion rather than
maintaining two permanent products.

### 5. Qualify hiding and one Zygisk architecture

- Keep `denylist` as the canonical normal-hide table on the current line.
- Treat the one-time legacy `hidelist` union as compatibility for existing older Kitsune/Delta test
  databases, not as a fresh-install requirement or a recurring synchronizer. The database remains
  version 12 so an older published daemon does not destructively reject it.
- Specify app, CLI, daemon, provider, work-profile/multiuser, SELinux, and rollback behavior for
  MagiskHide/DenyList/SuList before claiming compatibility.
- Qualify exactly one Zygisk model and version first. ReZygisk v1.0.0 removed Kitsune support;
  NeoZygisk uses a different provider/injection model. Do not enable conflicting providers or infer
  SuList support from a shared table name.

Exit: the chosen provider and hiding modes pass one explicit matrix without a universal hiding or
attestation-bypass claim.

### 6. Establish production identity and update ownership

Signing should stay simple:

- debug builds use the automatic Android debug key;
- CI release-shaped builds use an ephemeral, unpublished key only to test release behavior;
- distribution uses one long-lived external maintainer keystore and one four-field property file;
- private keys and credentials never live in the repository.

Before the first production release, create that one production key, record its certificate, and
define the transition from prior artifacts signed by the public historical test key. This is a
one-time release operation, not a new daily build system.

Also implement a project-owned authenticated update service. HTTPS, SHA-256 verification, and
atomic local publication contain bad transport/artifact states, but they do not make an arbitrary
custom metadata server a trusted project service.

Exit: the exact tested APK is signed by the recorded production identity, has a published checksum
and source commit, upgrades only where intended, and is fetched through project-owned metadata.

## Database compatibility in plain terms

There are three cases:

| Starting state | Behavior |
|---|---|
| Fresh install with no tables | Create current tables and an internal completion marker; no user-data backup or import. |
| Existing older Kitsune/Delta v12 database | Make one verified `magisk.db.v12.bak`, then transactionally add valid legacy `hidelist` rows missing from `denylist`; preserve `hidelist` and `sulist`. |
| Short-lived local v13 development database | Validate its exact completion marker and normalize `user_version` back to 12 so older released daemons can reopen it. |

The internal table name `hide_migration_v13` is historical. It does not mean the released schema is
now version 13. The import is deliberately one-time: repeating it on every daemon start would
reactivate an entry a user intentionally removed from `denylist` while a stale legacy row remained.

See [`docs/hide-migration-v13.md`](docs/hide-migration-v13.md) for the narrow implementation and
recovery contract.

## Test placement

Keep remote gates high-yield and deterministic:

- changed-line whitespace, shell/Python parsing, host contract/fault tests;
- JVM unit tests and Android lint;
- all-ABI debug and release builds with different signers;
- signer, embedded identity, required library, duplicate-entry, and 16 KiB ELF/ZIP checks;
- API 23, 29, and modern AVD normal-Magisk boot/root smoke;
- a small concurrent `su` regression and bounded sanitizer corpus.

Keep these local until a dedicated isolated runner exists:

- hundreds or thousands of concurrent `su`, parser, or failure-injection iterations;
- writable-System Mode mutation, forced process death, power-loss simulation, and uninstall;
- commercial emulator snapshots and physical-device flashing/recovery;
- vendor-root toggles and host-image adapter experiments.

Promote a local finding to CI as the smallest deterministic regression that would have caught it.
Do not retain giant generated payloads or duplicate a host test in multiple workflows.

## Merge and dependency policy

- PRs #22, #23, and #24 are already merged. No currently open maintainer-authored PR is waiting to
  be merged at this checkpoint.
- Dependabot PRs are proposals, not an ordered development plan. Patch/minor updates may be rebased
  and tested after hardening; coordinated crypto/Rust majors belong in a separate migration based on
  the chosen official stable.
- Never merge official `master` wholesale into the old core. Observe current fixes and backport only
  a narrow change with a relevant regression, or inherit it through the stable-based port.
- Keep generated upstream, SBOM, RustSec, license, signature, and architecture evidence reproducible;
  do not hand-edit results merely to make a gate green.

## Production release gate

A build may be called production-ready only when all of these are true:

- source/base/version/certificate/checksum identity is coherent and reproducible;
- ordinary advertised Magisk install and recovery routes pass;
- every advertised System Mode target passes its full lifecycle and restore procedure;
- unsupported immutable/verified layouts fail before persistent mutation;
- interrupted transactions and uninstall restore exact owned state;
- SELinux remains enforcing where claimed and the intended policy is loaded after cold boot;
- database fresh-install, existing-v12 compatibility, deliberate rollback, and uninstall are tested;
- hidden-manager and selected provider behavior pass with exact versions;
- no known release blocker is converted into an undocumented warning or marketing claim.

Until then, publish only clearly labeled development evidence, not a stable release.
