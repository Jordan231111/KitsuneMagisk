# Kitsune manager and SU consent

PR13 connects the maintained Compose manager to PR7 and PR12. System Mode remains
experimental and debug-only. Production promotion, real phones, early mounting
and release migration retain their PR14–PR16 gates.

The [PR13 acceptance record](manager-pr13.json) identifies tested commits,
artifacts, emulator lanes and limits. The final PR checks are required before merge.

## Hide and SuList

The list and settings use `magisk --hide`, never raw SQL or the upstream negative
DenyList API. The running mode owns selection. A saved mode/provider change
requires a cold boot; the screen disables edits until that change is applied.
Every mutation is checked and followed by a fresh list read. A failed batch can
partly succeed, and the displayed selection reflects that result. Process names
are shell-quoted and validated again by the core. Custom/legacy process rows
remain removable. Filtering, system applications, expansion and process-level
selection are retained. Selection applies across Android users, as documented
in [the core policy](hide-policy.md), and does not itself grant root.

The built-in Zygisk choice remains explicit. External Hide requires the PR12
adapter; provider absence, conflicts, namespace failures, invalid names, reboot
requirements and database/connection errors are actionable failures, not checked
boxes. An engine label is not an external injection-health claim.

## SU consent and authentication

The `kitsune.su-consent.v1` capability identifies a paired manager/core protocol.
The daemon creates a private, per-request Unix socket under its owned runtime
path, accepts only the verified manager UID, and supplies the actual caller UID, PID, authentication
requirement and remaining deadline over that connection. The manager checks the
root peer. Launcher extras carry only the socket path; they cannot choose a UID,
FIFO file or remembered grant. The endpoint is removed after acceptance and the
connection is answered once. Older FIFO requests fail closed: update core and
manager together using the supported install route.

The daemon bounds launcher retries and the complete response to seventy seconds,
including partial writes. It accepts only deny, allow or restricted allow and
known policy durations. It rejects disconnected callers, expired responses,
invalid authentication flags, changed root/multiuser settings and lost SuList
eligibility. Policy persistence happens in the daemon before acknowledgement;
the manager never persists a grant from an untrusted intent. Persistence uses an
atomic replacement with preserved notification/logging fields, compatible with
Android 6 SQLite; [UPSERT requires SQLite 3.24](https://www.sqlite.org/lang_upsert.html#history)
and failed on the older runtime lane. A one-time grant
is not reused through the three-second policy cache. CLI policy changes invalidate
that cache. Manager policy edits and database settings confirm the persisted value
before reporting success; failed writes reload policy or reset the settings view.
Legacy fingerprint migration retains its requirement if persistence fails. Root and the verified manager retain their existing recovery paths.

The policy cache owns its `Arc` references under a mutex and serializes publication
with CLI invalidation. It never holds that mutex while waiting for consent. Launcher
children clear the daemon worker's blocked signal mask before `app_process`, matching
the existing C++ execution helper. Signal masks survive [fork and exec](https://man7.org/linux/man-pages/man7/signal.7.html);
the controlled Android 15 probe reproduced eight crashes with the inherited mask
and eight successful launches with a cleared mask. Repeated manager decisions and
240 concurrent shell-UID requests exercise the corrected paths.

Authentication protects **new grants and manager policy edits**. It does not
retroactively revoke existing grants or authenticate every command under a saved
grant. Disabled authentication still requires explicit consent unless the user
has deliberately selected automatic response. With authentication enabled,
automatic allow must show consent and authenticate. Both the daemon's requirement
and the manager's setting must be satisfied. Removing a screen lock never turns
authentication off. Cancellation, unavailable credentials, lockout, timeout and
process death before a valid response cannot issue a grant; late callbacks are ignored. The visible
request timer remains bounded when changing duration or opening authentication.

Historical mode 0 remains disabled. Every nonzero `su_biometric` value, including
system mode 1 and manager-biometric mode 2, requires Android-controlled
authentication. The separate `BIOMETRIC_WEAK` manager prompt is retired: the old
helper's availability check could silently disable it after enrollment/hardware
changes, while a separate weak-authenticator path added fallback state without
strengthening the daemon's authorization boundary. Existing nonzero values stay
nonzero until the user authenticates a settings change. Android owns confirmation
of the device PIN, pattern or password and its lockout behavior. Its [credential confirmation contract](https://developer.android.com/reference/android/app/KeyguardManager#createConfirmDeviceCredentialIntent(java.lang.CharSequence,java.lang.CharSequence))
returns no intent when a credential is unavailable; the manager denies that case.

Shared UIDs retain one policy row per Linux UID. Owner-managed requests display
the actual Android user while the daemon alone maps persistence to the owner's
policy UID. A root-service lookup resolves profile packages without assuming that
the owner has installed them. Unresolved policy rows remain visible by UID; the
UI does not delete grants merely because a profile is locked or unavailable. Owner-only, owner-managed and
per-user behavior remain daemon decisions. Hidden and normal managers use the
same signer checks and protocol. SELinux-disabled eligibility remains PR12/PR15;
the manager does not bypass a backend denial.

## System Mode, recovery and diagnostics

The status screen reads the recorded transaction without sourcing its contents.
Duplicate/unknown state or schema fields are shown as unknown. An imported doctor
JSON is a bounded, read-only review summary; it never authorizes installation.
The installer still rechecks the exact target, live host lease, manifests and
external backup. Recovery invokes the existing managed recovery entry point after
confirmation. A stored BOOT_VERIFIED state is a receipt, not a fresh disk audit.
If Android cannot start, use the verified external backup. Emulator guidance calls
for fully closing and reopening the player.

Diagnostic export uses an allowlist of build identities, API, provider/Hide state,
transaction state and operation result codes. It excludes package selections,
serials, app commands, properties, environment variables and raw logs. Operation
results are session observations, not durable receipts. Raw root logs remain
viewable locally and can be saved separately. Keep exported compatibility details in maintainer reports.

## Historical manager behavior

Randomized manager packages retain the historical `com.*` shape and generated
signer. This reduces casual package-name matching; it is not invisibility or an
attestation guarantee. Payload signature checks, PR10 verified updates, bootstrap,
launch and restore remain mandatory. Without root, the hidden launcher renders
its recovery dialog and uses Android's installer to restore the full APK. It
checks the installed signer before asking Android to remove the old hidden app;
installation or uninstall cancellation does not silently claim restored root.
Rootless restoration uses default app preferences. Rooted hide/restore transfers
only the existing permitted preference subset through a root-authorized nonce bound
to the target UID and the sender's package, UID and signing certificate. Atomic
consumption rejects replay. Ordinary exported intents cannot supply migration
authority or trigger package-migration cleanup. Automatic-response, tapjacking,
reauthentication, request-timeout and shortcut-prompt preferences retain their
existing migration exclusions.
Stub revision 42 carries the launcher/preferences and uninstall-permission changes;
older stubs upgrade through the existing signed self-update path when root is
available, or open their Android app settings for cleanup after rootless restore. The global signature bypass remains retired.
Optional instrumentation is re-signed only when its installed APK shares the full
manager's signing identity; claiming the test package name is not sufficient.
The root service is connected before rendering rooted UI, including the old-stub
upgrade path; the signed self-update is requested from the initialized activity.

For original Full stable/R65/R66 installations, back up first and restore the old
full manager before changing development lines. Install the owned Next manager and
core through the supported route, reconfigure app preferences, then hide afresh.
Old publisher channels and arbitrary cross-signer handoffs are not Next update
authorities. The v12 core data migration and retained root policy/module data are
separate from app preferences; PR16 still owns the production-certificate transition.

The separate obsolete Lite distribution remains retired. Its usable configuration
is SuList, no enabled modules and Zygisk off. Users must persist SuList before
upgrading from an old Lite core, as described in the core policy. No table-name
heuristic silently converts Full into Lite.

The maintained product name and experimental warning retain the selected Next
identity, with upstream attribution. The inherited system-only GMS provider
restriction and deterministic policy `LIMIT 1` are retained. Module actions use
the maintained terminal. Orientation remains locked during flash/action screens;
action output remains available until explicitly closed, rather than silently
auto-closing successful output. This is the explicit replacement for the older
close/focus/auto-close variants. Background actions must not be replayed after
process death. Inactive-slot installation points users to Android System Updates'
Restart action; PR15 retains real-device OTA qualification.

Prompt protection also suppresses accessibility controls on the consent surface.
Its toggle is available on every API so trusted screen-reader users can opt out
while retaining Android credential authentication. Android 12+ overlay blocking
remains enabled independently.

New product strings cover Simplified/Traditional Chinese, Vietnamese, Russian and
Turkish, with Android's default-language fallback for other locales. The maintained
libsu dispatcher remains in use; its root-service, parallel-request and hidden-
manager tests qualify the replacement instead of reintroducing the old 5.x fork.

A prepared MuMu boot exposed a missing standard `su` entry after vendor root was
disabled, even though the owned runtime and daemon remained valid. The maintained
mount tree now also considers missing PATH directories under protected root-owned
system/vendor/product/system_ext parents. It creates them through the existing
mount machinery, never by writing the persistent partition or adding a second
late unmount engine. Existing symlinks and writable/unowned parents do not qualify
for this new candidate path. Application-PATH root is a required runtime test.
Missing children directly beneath a separate partition root are excluded: the
existing mount engine deliberately will not replace that root with tmpfs. The
ordinary-app fixture does not inherit the manager's `/debug_ramdisk` PATH addition,
so it detects a missing injection that a manager-only root test would miss.

The rooted installer sets per-app install-source authorization only on API 26+,
matching [Android's install-source contract](https://developer.android.com/reference/android/content/pm/PackageManager#canRequestPackageInstalls()).
The obsolete call on API 24 was unnecessary and exposed the native `appops`
client's Binder-shutdown crash. The crash check remains enabled.
