# Kitsune Hide/SuList v1

PR12 supplies the core policy and migration independently of the PR13 manager
screens. The [acceptance record](hide-pr12.json) covers the qualified emulator
tuples and explicit limitations. Production and physical-device release gates
remain in force. The binary advertises `kitsune.hide.v1` and the
running daemon exposes a root-only request at `0x4b490003`. Old daemons reject it.

## Selection and ownership

`magisk --hide status` reports the running mode, saved mode, running engine, saved
provider, readiness and whether another cold boot is required. `mode hide|sulist`
and `provider core|external` save the next boot's choice. They never change a
running zygote's interpretation of the list. Safe mode skips Hide initialization
and modules. With SuList active, live `disable` is refused; choose `mode hide` and
cold boot. Enabling SuList implies enforcement at that boot.

Normal Hide removes Magisk and module mounts from selected processes. SuList
inverts visibility: unselected processes lose those mounts and Zygisk module
callbacks; selected processes retain them. Selection does not grant root. An
unselected UID is rejected before the SU cache, even if it has a saved grant or
calls an absolute binary path. Selected UIDs still pass ordinary root-access,
user, policy, prompt and signer checks. Root and ADB remain recovery routes;
ADB still needs ordinary SU authorization. The verified normal/hidden manager,
SystemUI and Settings remain usable. System-server mounts are never removed.
SystemUI/Settings share Android's system UID; SU eligibility applies to that
security principal, while mount selection remains process-specific.
The registered, certificate-checked hidden stub retains mount visibility while
seeding or repairing its full payload. That exception never grants SU or a
provider's verified-manager flag before the payload signature is accepted.

Package/process rows apply to every installed Android user/profile. Each UID is
resolved from that user's package directory; a matching app ID in another user
alone is insufficient. Shared-UID packages necessarily share SU eligibility.
Their mount decisions can still select separate process names. Package and user
directory changes refresh the mapping, including installation into a work profile.
Missing/locked profiles never cause destructive deletion of saved rows.

Regular processes match exactly. `isolated` rows match a process-name prefix at
an Android `:` boundary; `com.example` does not match `com.example.other`.
App zygotes are a shared parent: use the isolated **package** prefix (or explicitly
select the package's `_zygote` process) when its children must inherit a clean
namespace. A service-specific prefix cannot independently undo the namespace
inherited from that shared parent. The owning package UID is verified before
interpreting a normal UID's `_zygote` name as an isolated-parent selection.

Built-in Zygisk cleans the selected specialization and suppresses its module
callbacks. With Zygisk off, the maintained log-based engine cleans the process's
current private namespace asynchronously. It checks trusted zygote lineage, UID,
full name, and namespace identity with a bounded 200 ms specialization wait. It
anchors `/proc` and namespace descriptors, rechecks the parent, rejects init's
namespace, never SIGSTOPs apps, and has one log reader across enable/disable
cycles. Cleanup workers use the daemon's existing logging lock when forking,
so they cannot inherit that mutex from another thread. This engine does not
promise cleanup before the first instruction or callback. A missing log event or expired deadline is not reported as successful
cleanup. Relaunch after policy changes. On pidfd-capable kernels the core also
terminates affected ordinary app processes by pidfd; older kernels require an
explicit relaunch. Zygotes/system_server are not killed by list editing.

## CLI and provider contract

`magiskhide` retains the historical success/failure exits, including disabled
`status`/`sulist` returning 1. `magisk --hide` provides the structured v1 exits. `add PKG [PROCESS]`,
`rm PKG [PROCESS]`, and `ls` operate on the **running mode's** table. An omitted
process defaults to the package for add, and removes that package's rows for rm.
Lists use `package|process` lines. Status/check and mutation results use JSON
schema 1. Mutation exits are 0 success, 3 duplicate, 4 absent, 5 invalid name,
6 namespace unavailable, 7 operation/database error, 8 cold boot required, and
9 unsupported/conflicting provider. Invalid CLI syntax exits 2. Privileged
connection failure exits 1. Repeated add/remove does not change saved state.

`--denylist` retains upstream CLI exit conventions and always reads/writes the
negative `denylist`, even during SuList. It never exports an inverted allowlist
to old providers. Raw SQL writes are not the live selection API; use the CLI.
List writes are parameterized and committed before publishing memory or killing
an affected process. `exec COMMAND...` verifies namespace creation/private
propagation before unmounting, then preserves the command exit status (126/127
for exec failure). It cannot alter the caller's namespace on an isolation error.

`--hide check UID PROCESS` returns `unmount`, `su_eligible`, and `manager` booleans.
It is an observation of current policy, not an SU authorization token or proof
that a provider acted. `manager` uses the daemon's verified manager identity in
that user, including hidden installs. No normal-manager `requester` row is forged.
Status/check contain no package inventory or private app data.

The [pinned ReZygisk source adapter](../tools/zygisk-adapters/README.md) queries
this contract. External mode gives it sole namespace ownership; the core's log
monitor and built-in engine do not enforce concurrently. The adapter skips
selected processes' module callbacks and requests cleanup on failed policy
queries. Preloaded provider/library mappings are not claimed invisible. Its
module declaration is a capability claim by an already privileged root module,
not a sandbox or injection-health attestation. Built-in must be disabled before
selecting external. Known unadapted ReZygisk/NeoZygisk/ZygiskNext installations
reject core enforcement and SuList activation rather than silently running two
unmount engines. Other root modules must obey the same one-owner contract.

## Database and downgrade

The public SQLite version remains **12**. Raising it would make historical
Kitsune v12 daemons erase user policy. New private settings are additive. Fresh
databases create the tables and one validated `hide_migration_v13` marker without
an unnecessary backup. Existing un-reconciled databases receive a root-owned
0600 online SQLite snapshot at `/data/adb/magisk.db.hide-v1.bak`, including WAL
contents. Publication never overwrites the first backup, rejects symlinks,
unsafe ownership/modes and corrupt databases, and syncs the file and directory.

The transaction conservatively unions nonempty legacy `hidelist` pairs into
canonical `denylist`, retains all original tables and policies, and leaves
`sulist` independent. An existing valid PR26 marker skips the union, preventing
removed selections from reappearing. Invalid markers and unsupported newer
schemas fail without erasing policy. Old v7–v10 upgrades retain Hide data rather
than dropping it. Runtime name validation ignores malformed rows without
modifying the preserved audit source.

Downgrade to a canonical-denylist v12 Kitsune daemon retains the schema and rows;
its old runtime semantics are not the new provider protocol. Disable/remove the
v1 external adapter and select the intended old mode before switching. Older
hidelist-only releases require a deliberate export/import of current negative
selections, or restoration of the pre-migration snapshot while the daemon is
stopped. The retained legacy table is an audit source, not a continuously mirrored
second authority. Experimental schema 13 and unrelated newer schemas are refused;
restore their known owning backup instead of forcing a version number down.

## Historical mechanism decisions

The retained behavior is root/module visibility, separate inverse selection,
independent no-Zygisk operation, exact exec isolation, and recoverable migration.
The old ptrace monitor is replaced by the maintained specialization/log paths.
Its fixed 32768-PID bitsets, asynchronous detach/signal ownership and unbounded
wait paths are not copied into the new daemon. Users requiring a ptrace provider
can use the pinned adapted external engine; do not infer universal detection or
attestation bypass from either implementation.

The maintained mount tree exposes SU/applets in a suitable PATH directory and
removes its overlays with the selected namespace. The historical unconditional
`/system/bin` relocation, APEX relocation and delayed-until-boot-complete mount
are not additional overlays. Modules/scripts continue to have the maintained
pre-boot-complete root route. The daemon keeps its maintained namespace; a clean
namespace is made for the requesting process/exec, never installed globally.

Historical randomized daemon socket and request FIFO names are retired. The
maintained socket lives under the owned runtime directory and authenticates
peer credentials, executable identity and operation privileges; naming entropy
is not authorization. Per-boot/random FIFO compatibility would add stale endpoint
and restart state without protecting these existing checks. Hidden manager
package handling remains independent and is retained. No socket path is a
public provider API.

When SELinux is active, including permissive mode, the zygote context check is
mandatory. Only positively absent SELinux enables the root-peer fallback, which
also requires the zygote name, approved app_process executable and init parent.
Permission/I/O errors cannot enable it. The kernel
[active-LSM list](https://docs.kernel.org/admin-guide/LSM/index.html) distinguishes
AppArmor container contexts from SELinux when that interface is available. Disabled-policy runtime qualification is
separate from enforcing/permissive testing; physical Waydroid/OEM qualification
remains PR15.

Lite users can retain no injection, no enabled modules, and inverse SuList in the
single maintained product. The CLI does not enable Zygisk or install modules.
The separate obsolete Lite APK/update channel is not restored; PR13 supplies its
manager-facing migration and PR16 owns release delivery. PR14 supplies the
[versioned module APIs](module-contract.md) and [layered mount cleanup evidence](module-pr14.json).


## Qualified configurations and remaining gates

| Engine | Exact runtime coverage | Selection and module callbacks | Manager |
|---|---|---|---|
| Maintained built-in | API 35 ARM64 enforcing debug/release; MuMu Pro 1.9.2 API 32 ARM64 permissive System Mode | Normal Hide and inverse SuList; selected/remote/isolated/nested/app-zygote cases; USAP pool consumption | Normal and hidden; bootstrap, wrong-signer refusal and restore |
| Core log monitor, Zygisk off | API 35 ARM64 enforcing debug/release; same MuMu tuple | Asynchronous normal/inverse namespace cleanup, no injection; bounded lifecycle/resource checks | Verified manager and bootstrap visibility retained |
| Pinned ReZygisk + v1 source adapter | API 35 ARM64 and same MuMu tuple; four-ABI compilation | Core policy query, inverse selection, per-process cleanup and callback suppression; final private-flag mask has an Android callback test | Core-verified UID, including hidden bootstrap/restore |
| Stock ReZygisk/NeoZygisk/ZygiskNext | Not qualified for this v1 contract | No automatic SuList compatibility claim; missing adapter is refused | PR11 comparisons remain historical evidence |

The record distinguishes the full provider integration ZIP from the final
private-flag mask cleanup and its exact Android callback test. No provider binary
is published here. PR16 must qualify the exact bytes of any release artifact.

Original Lite (`669d2da1`) forces SuList in code and may not persist its choice.
Before upgrading, run `magiskhide sulist enable` on that installation, preserve
its `sulist` rows, and disable existing modules/providers for the equivalent
no-module/no-injection configuration. Do not infer Lite from a table name: Full
also has a `sulist` table. Recheck broad legacy isolated prefixes against the new
process-name boundary and select full process prefixes where needed.

PR14 verifies cleanup of its early mounts after ordinary parent overlays are detached.
PR15 owns real phones and an
actual SELinux-disabled/Waydroid target. Both installed AVD kernels stayed
permissive despite disabled requests; only the absence/error/other-LSM guards
are claimed tested for that branch. PR13 supplies [manager screens and diagnostics](manager-contract.md).
