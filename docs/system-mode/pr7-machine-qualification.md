# System Mode installation

From code **31024**, System Mode installs directly in the app in both release and debug builds.
No computer, doctor report, authorization command or external backup is required.

1. Enable the emulator’s built-in root option and start Android.
2. Install KitsuneMagisk and grant its root request.
3. Choose **Install → System Mode**, confirm, and wait for completion.
4. Fully close and reopen the emulator. KitsuneMagisk should show the installed version.

Use the same System Mode action to update. Use **Uninstall** in the app to remove it.
The installer checks the system filesystem, boot layout, policy and available space itself.
A protected or read-only image that cannot be remounted needs a different installation route;
System Mode does not bypass a locked bootloader or Android verified boot.

The installer changes Magisk’s own runtime and init files. Temporary copies of those files allow
an interrupted update to recover and are removed after successful boot. User modules, root
policies and scripts stay in place; they are not copied into recovery snapshots. The full manager
APK is not duplicated in the persistent payload. Small ownership records support exact uninstall.
**Recover System Mode** can finish an interrupted operation.

Factory-reset recovery is not yet supported: clearing userdata removes the installation state.
See [#53](https://github.com/Jordan231111/KitsuneMagisk/issues/53).

On phones, use the normal [Magisk boot-image installation](../install.md#first-installation-on-a-phone)
when supported. Existing ordinary Magisk installations should use **Direct Install**.

The host tools in `tools/system_mode` remain development tools for collecting exact-target lab
results and exercising releases through code 31023. Their machine qualification, external disk
backups and live authorization are not requirements of the current app.
Historical lab results remain in [the PR7 record](mumu-pr7-2026-09-09.md) and
[the Windows record](pr8-windows-2026-10-08.md).
