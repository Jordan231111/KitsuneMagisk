from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]
MANAGER = ROOT / "app" / "src" / "main" / "res" / "raw" / "manager.sh"
FLASH_SCRIPT = ROOT / "scripts" / "flash_script.sh"
ADDON_SCRIPT = ROOT / "scripts" / "addon.d.sh"
INSTALLER = (
    ROOT
    / "app"
    / "src"
    / "main"
    / "java"
    / "com"
    / "topjohnwu"
    / "magisk"
    / "core"
    / "tasks"
    / "MagiskInstaller.kt"
)
INSTALL_VIEW_MODEL = (
    ROOT
    / "app"
    / "src"
    / "main"
    / "java"
    / "com"
    / "topjohnwu"
    / "magisk"
    / "ui"
    / "install"
    / "InstallViewModel.kt"
)
SYSTEM_MODE_DIALOG = (
    ROOT
    / "app"
    / "src"
    / "main"
    / "java"
    / "com"
    / "topjohnwu"
    / "magisk"
    / "dialog"
    / "SystemModeWarningDialog.kt"
)


def function_body(source: str, name: str) -> str:
    start = source.index(f"{name}()")
    next_function = source.find("\n}\n\n", start)
    if next_function < 0:
        raise AssertionError(f"could not find end of {name}")
    return source[start:next_function + 3]


class SystemModeInstallerSafetyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = MANAGER.read_text(encoding="utf-8")
        cls.flash_script = FLASH_SCRIPT.read_text(encoding="utf-8")
        cls.addon_script = ADDON_SCRIPT.read_text(encoding="utf-8")
        cls.installer = INSTALLER.read_text(encoding="utf-8")
        cls.install_view_model = INSTALL_VIEW_MODEL.read_text(encoding="utf-8")
        cls.system_mode_dialog = SYSTEM_MODE_DIALOG.read_text(encoding="utf-8")

    def test_failed_preflight_cannot_delete_existing_install(self) -> None:
        direct = function_body(self.source, "direct_install_system")
        begin = direct.index('begin_system_installation "$MIRRORDIR"')
        self.assertLess(direct.index("remount_check rw"), begin)
        self.assertLess(direct.index("Missing install payload"), begin)
        self.assertNotIn("cleanup_system_installation", direct[:begin])

        rollback = function_body(self.source, "cleanup_system_installation")
        guard = rollback.index('[ "$SYSTEM_INSTALL_TRANSACTION" = true ] || return 0')
        first_delete = rollback.index('rm -rf "$target" "$legacy"')
        self.assertLess(guard, first_delete)
        self.assertLess(rollback.index("rollback backup is missing"), first_delete)
        self.assertLess(
            rollback.index('[ "$SYSTEM_INSTALL_BEGIN_COMPLETE" = true ]'),
            first_delete,
        )

    def test_upgrade_rollback_preserves_previous_system_payload(self) -> None:
        begin = function_body(self.source, "begin_system_installation")
        rollback = function_body(self.source, "cleanup_system_installation")
        commit = function_body(self.source, "commit_system_installation")
        self.assertIn('mv "$target" "$target.kitsune-old"', begin)
        self.assertIn('mv "$target.kitsune-old" "$target"', rollback)
        self.assertIn('rm -rf "$target.kitsune-old"', commit)

    def test_policy_and_init_rollback_preserve_exact_preupgrade_bytes(self) -> None:
        direct = function_body(self.source, "direct_install_system")
        rollback = function_body(self.source, "cleanup_system_installation")
        commit = function_body(self.source, "commit_system_installation")
        self.assertLess(
            direct.index('stage_file_rollback "$MIRRORDIR$sepol"'),
            direct.index('backup_restore "$MIRRORDIR$sepol"'),
        )
        self.assertLess(
            direct.index(
                'stage_file_rollback "$MIRRORDIR/system/etc/init/bootanim.rc"'
            ),
            direct.index(
                'backup_restore "$MIRRORDIR/system/etc/init/bootanim.rc"'
            ),
        )
        self.assertIn('restore_staged_file "$mirror$SYSTEM_INSTALL_SEPOL"', rollback)
        self.assertIn('"$SYSTEM_INSTALL_SEPOL_HAD_GZ"', rollback)
        self.assertIn('"$SYSTEM_INSTALL_BOOTANIM_HAD_GZ"', rollback)
        self.assertIn(
            'path_present "$mirror$SYSTEM_INSTALL_SEPOL.kitsune-old"', commit
        )
        self.assertIn(
            'path_present "$mirror/system/etc/init/bootanim.rc.gz.kitsune-old"',
            commit,
        )

    def test_selinux_preflight_does_not_mutate_live_policy(self) -> None:
        direct = function_body(self.source, "direct_install_system")
        self.assertNotIn('permissive su', direct)
        self.assertNotIn('magiskpolicy" --live', direct)
        self.assertIn('magiskpolicy" --save "$live_policy_probe"', direct)

    def test_live_databin_uses_an_atomic_directory_swap(self) -> None:
        fix_env = function_body(self.source, "fix_env")
        populate = fix_env.index('cp_readlink "$source" "$new"')
        preserve = fix_env.index('mv "$MAGISKBIN" "$old"', populate)
        publish = fix_env.index('mv "$new" "$MAGISKBIN"', preserve)
        self.assertLess(populate, preserve)
        self.assertLess(preserve, publish)
        self.assertIn('mv "$old" "$MAGISKBIN"', fix_env[publish:])

    def test_system_mode_rolls_back_the_runtime_until_system_commit(self) -> None:
        fix_env = function_body(self.source, "fix_env")
        rollback = function_body(self.source, "rollback_env")
        commit = function_body(self.source, "commit_env")
        xdirect = function_body(self.source, "xdirect_install_system")

        self.assertIn('SYSTEM_INSTALL_ENV_TRANSACTION=true', fix_env)
        self.assertIn('mv "$old" "$MAGISKBIN"', rollback)
        self.assertIn('rm -rf "$NVBASE/.magisk-old"', commit)
        self.assertIn('fix_env "$1" true', xdirect)
        addond = xdirect.index('install_addond "$2" "true" "true"')
        rollback_call = xdirect.index("rollback_env", addond)
        system_commit = xdirect.index("commit_system_installation", rollback_call)
        runtime_commit = xdirect.index("commit_env", system_commit)
        self.assertLess(addond, rollback_call)
        self.assertLess(rollback_call, system_commit)
        self.assertLess(system_commit, runtime_commit)

    def test_runtime_swap_executes_rollback_and_commit_paths(self) -> None:
        script = textwrap.dedent(
            r"""
            set -eu
            export NVBASE="$2/nv"
            export MAGISKBIN="$NVBASE/magisk"
            ui_print() { :; }
            chown() { return 0; }
            . "$1"

            mkdir -p "$MAGISKBIN" "$2/source-one"
            touch "$MAGISKBIN/old-runtime" "$2/source-one/new-runtime"
            fix_env "$2/source-one" true
            test -e "$MAGISKBIN/new-runtime"
            test -e "$NVBASE/.magisk-old/old-runtime"
            test "$SYSTEM_INSTALL_ENV_TRANSACTION" = true

            rollback_env
            test -e "$MAGISKBIN/old-runtime"
            test ! -e "$MAGISKBIN/new-runtime"
            test ! -e "$NVBASE/.magisk-old"

            mkdir -p "$2/source-two"
            touch "$2/source-two/final-runtime"
            fix_env "$2/source-two" true
            commit_env
            test -e "$MAGISKBIN/final-runtime"
            test ! -e "$NVBASE/.magisk-old"
            test "$SYSTEM_INSTALL_ENV_TRANSACTION" = false

            mkdir -p "$NVBASE/.magisk-old" "$2/source-three"
            mv "$MAGISKBIN/final-runtime" "$NVBASE/.magisk-old/old-runtime"
            touch "$MAGISKBIN/interrupted-runtime" "$2/source-three/retried-runtime"
            fix_env "$2/source-three" true
            test -e "$MAGISKBIN/retried-runtime"
            test -e "$NVBASE/.magisk-old/old-runtime"
            test ! -e "$NVBASE/.magisk-old/interrupted-runtime"
            rollback_env
            test -e "$MAGISKBIN/old-runtime"
            test ! -e "$MAGISKBIN/interrupted-runtime"
            """
        )
        with tempfile.TemporaryDirectory(prefix="kitsune-runtime-transaction-") as temp:
            result = subprocess.run(
                ["bash", "-c", script, "runtime-transaction", str(MANAGER), temp],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_cleanup_does_not_remount_the_guest_root_read_only(self) -> None:
        cleanup = function_body(self.source, "installer_cleanup")
        self.assertIn('umount -l "/proc/$$/attr"', cleanup)
        self.assertNotIn("remount /", cleanup)

    def test_system_mode_runs_in_a_one_off_private_mount_namespace(self) -> None:
        direct = self.installer[
            self.installer.index("protected suspend fun direct_system()") :
            self.installer.index("protected suspend fun secondSlot()")
        ]
        unshare = direct.index('unshare -m')
        isolate = direct.index('mount --make-rprivate /')
        install = direct.index('xdirect_install_system')
        self.assertLess(unshare, isolate)
        self.assertLess(isolate, install)
        self.assertIn('includeSystemModeManager = true', direct)
        self.assertIn('rm -f "${\'$\'}1/system_mode_manager.sh"', direct)
        manager_source = direct.index('system_mode_manager.sh" || exit 1')
        util_source = direct.index('util_functions.sh" || exit 1')
        self.assertLess(manager_source, util_source)

        manager_direct = function_body(self.source, "direct_install_system")
        self.assertNotIn("mount --make-rprivate", manager_direct)

    def test_release_backend_cannot_dispatch_system_mode(self) -> None:
        direct = self.installer[
            self.installer.index("protected suspend fun direct_system()") :
            self.installer.index("protected suspend fun secondSlot()")
        ]
        self.assertIn("if (!BuildConfig.DEBUG)", direct)
        self.assertIn("System Mode is disabled in release builds", direct)
        self.assertLess(
            direct.index("if (!BuildConfig.DEBUG)"),
            direct.index("extractFiles(includeSystemModeManager = true)"),
        )
        manager_direct = function_body(self.source, "direct_install_system")
        self.assertIn("System Mode is disabled in release builds", manager_direct)
        self.assertLess(
            manager_direct.index("System Mode is disabled in release builds"),
            manager_direct.index("Remount system partition as read-write"),
        )

    def test_system_mode_confirmation_guards_the_install_dispatch(self) -> None:
        selection = self.install_view_model[
            self.install_view_model.index("var method") :
            self.install_view_model.index("private fun resetMethod")
        ]
        install = self.install_view_model[
            self.install_view_model.index("fun install()") :
            self.install_view_model.index("override fun onSaveState")
        ]
        self.assertNotIn("SystemModeWarningDialog", selection)
        self.assertLess(
            install.index("SystemModeWarningDialog"),
            install.index("FlashFragment.flash(2)"),
        )
        positive = self.system_mode_dialog.index("ButtonType.POSITIVE")
        confirm = self.system_mode_dialog.index("onConfirm()", positive)
        negative = self.system_mode_dialog.index("ButtonType.NEGATIVE", confirm)
        self.assertLess(positive, confirm)
        self.assertLess(confirm, negative)

    def test_recovery_entry_points_reject_release_before_mutation(self) -> None:
        self.assertIn('"$MODE_BINARY" -c', self.flash_script)
        self.assertNotIn('"$MODE_BINARY" -v', self.flash_script)
        flash_gate = self.flash_script.index(":MAGISK:D ")
        self.assertLess(flash_gate, self.flash_script.index("remove_system_su"))
        self.assertLess(flash_gate, self.flash_script.index("rm -rf $MAGISKBIN/*"))
        self.assertLess(flash_gate, self.flash_script.index("direct_install_system"))

        addon_main = self.addon_script[
            self.addon_script.index("main()") : self.addon_script.index('\ncase "$1" in')
        ]
        self.assertIn('"$mode_binary" -c', addon_main)
        self.assertNotIn('"$mode_binary" -v', addon_main)
        addon_gate = addon_main.index(":MAGISK:D ")
        self.assertLess(addon_gate, addon_main.index("remove_system_su"))
        self.assertLess(addon_gate, addon_main.index("direct_install_system"))

    def test_recovery_system_mode_does_not_require_a_boot_image(self) -> None:
        for script in (self.flash_script, self.addon_script):
            with self.subTest(script="flash" if script is self.flash_script else "addon"):
                guard = script.index('if [ "$SYSTEMINSTALL" != "true" ]; then')
                find = script.index("find_boot_image", guard)
                end = script.index("\n  fi", find)
                self.assertLess(guard, find)
                self.assertLess(find, end)

    def test_recovery_never_sources_a_stale_or_missing_manager_script(self) -> None:
        for script in (self.flash_script, self.addon_script):
            with self.subTest(script="flash" if script is self.flash_script else "addon"):
                remove = script.index("rm -f ./manager.sh")
                extract = script.index('unzip -oj', remove)
                require = script.index("[ -f ./manager.sh ]", extract)
                source = script.index(". ./manager.sh ||", require)
                self.assertLess(remove, extract)
                self.assertLess(extract, require)
                self.assertLess(require, source)

    def test_system_addon_uses_the_restored_system_payload(self) -> None:
        main = self.addon_script[
            self.addon_script.index("main()") : self.addon_script.index('\ncase "$1" in')
        ]
        self.assertIn("system_apk=$MAGISKBIN/magisk.apk", main)
        self.assertIn('direct_install_system "$MAGISKBIN"', main)
        self.assertNotIn("MAGISKBINTMP", main)
        self.assertNotIn('$ADDOND/magisk/magisk.apk', main)

    def test_successful_non_deferred_recovery_install_commits_transaction(self) -> None:
        direct = function_body(self.source, "direct_install_system")
        self.assertIn('if [ "$defer_cleanup" != true ]', direct)
        self.assertIn("commit_system_installation || return 1", direct)
        self.assertNotIn('if $BOOTMODE && [ "$defer_cleanup" != true ]', direct)

    def test_new_runtime_directory_is_owned_by_the_transaction(self) -> None:
        direct = function_body(self.source, "direct_install_system")
        rollback = function_body(self.source, "cleanup_system_installation")
        begin = direct.index('begin_system_installation "$MIRRORDIR"')
        create = direct.index('mkdir "$runtime_dir"', begin)
        self.assertLess(begin, create)
        self.assertIn('SYSTEM_INSTALL_CREATED_RUNTIME_DIR="$runtime_dir"', direct)
        self.assertIn('rmdir "$SYSTEM_INSTALL_CREATED_RUNTIME_DIR"', rollback)

    def test_addond_replacement_keeps_rollback_copies(self) -> None:
        addond = function_body(self.source, "install_addond")
        preserve = addond.index('mv "$addond/99-magisk.sh" "$script_backup"')
        publish = addond.index('cp "$installDir/addon.d.sh" "$addond/99-magisk.sh"')
        restore = addond.index('mv "$script_backup" "$addond/99-magisk.sh"')
        self.assertLess(preserve, publish)
        self.assertGreater(restore, publish)

        xdirect = function_body(self.source, "xdirect_install_system")
        self.assertIn('install_addond "$2" "true" "true"', xdirect)
        self.assertNotIn("run_migrations", xdirect)

    def run_manager_harness(
        self,
        harness: str,
        *,
        expose_addond_path: bool = False,
        expose_system_path: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        source = self.source
        if expose_addond_path or expose_system_path:
            start = source.index("install_addond(){")
            end = source.index("\n}\n\ndirect_install()", start) + 3
            function = source[start:end]
        if expose_addond_path:
            declaration = "    local addond=/system/addon.d"
            self.assertEqual(1, function.count(declaration))
            function = function.replace(declaration, '    local addond="$4"')
        if expose_system_path:
            self.assertIn("/system/etc/init/magisk", function)
            function = function.replace(
                "/system/etc/init/magisk", "${SYSTEM_TEST_DIR}"
            )
        if expose_addond_path or expose_system_path:
            source = source[:start] + function + source[end:]
        with tempfile.TemporaryDirectory(prefix="kitsune-installer-fault-") as temp:
            return subprocess.run(
                [
                    "bash",
                    "-c",
                    source + "\n" + textwrap.dedent(harness),
                    "installer-fault",
                    temp,
                ],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

    def test_partial_system_backup_failure_preserves_every_original(self) -> None:
        result = self.run_manager_harness(
            r"""
            set -u
            ui_print() { :; }
            root="$1"
            MAGISKSYSTEMDIR="$root/system/etc/init/magisk"
            target="/$MAGISKSYSTEMDIR"
            legacy="$target.rc"
            mkdir -p "$target"
            printf old-payload > "$target/payload"
            printf old-legacy > "$legacy"
            mv() {
                if [ "$1" = "$legacy" ] &&
                   [ "$2" = "$legacy.kitsune-old" ]; then
                    return 1
                fi
                command mv "$@"
            }

            ! begin_system_installation /
            test "$(cat "$target/payload")" = old-payload
            test "$(cat "$legacy")" = old-legacy
            test ! -e "$target.kitsune-old"
            test ! -e "$legacy.kitsune-old"
            """
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_partial_addond_backup_failure_preserves_every_original(self) -> None:
        result = self.run_manager_harness(
            r"""
            set -u
            ui_print() { :; }
            random_str() { printf mock; }
            is_rootfs() { return 0; }
            mkblknode() { return 0; }
            blockdev() { return 0; }
            mount() { return 0; }
            root="$1"
            addond="$root/addon.d"
            MAGISKBIN="$root/runtime"
            mkdir -p "$addond/magisk" "$MAGISKBIN"
            printf old-script > "$addond/99-magisk.sh"
            printf old-directory > "$addond/magisk/payload"
            printf apk > "$root/app.apk"
            failed_source="$addond/magisk"
            failed_destination="$addond/.magisk.kitsune-old"
            mv() {
                if [ "$1" = "$failed_source" ] &&
                   [ "$2" = "$failed_destination" ]; then
                    return 1
                fi
                command mv "$@"
            }

            ! install_addond "$root/app.apk" false true "$addond"
            test "$(cat "$addond/99-magisk.sh")" = old-script
            test "$(cat "$addond/magisk/payload")" = old-directory
            test ! -e "$addond/.99-magisk.sh.kitsune-old"
            test ! -e "$addond/.magisk.kitsune-old"
            """,
            expose_addond_path=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_addond_replaces_dangling_script_symlink_without_following_it(self) -> None:
        result = self.run_manager_harness(
            r"""
            set -u
            ui_print() { :; }
            random_str() { printf mock; }
            is_rootfs() { return 0; }
            mkblknode() { return 0; }
            blockdev() { return 0; }
            mount() { return 0; }
            root="$1"
            addond="$root/addon.d"
            MAGISKBIN="$root/runtime"
            SYSTEM_TEST_DIR="$root/system/etc/init/magisk"
            mkdir -p "$addond" "$MAGISKBIN" "$SYSTEM_TEST_DIR"
            printf payload > "$MAGISKBIN/payload"
            printf 'SYSTEMINSTALL=false\n' > "$MAGISKBIN/addon.d.sh"
            printf apk > "$root/app.apk"
            ln -s "$root/escaped" "$addond/99-magisk.sh"

            install_addond "$root/app.apk" true true "$addond"
            test ! -L "$addond/99-magisk.sh"
            test ! -e "$root/escaped"
            grep -qx 'SYSTEMINSTALL=true' "$addond/99-magisk.sh"
            test ! -e "$addond/.99-magisk.sh.kitsune-old"
            """,
            expose_addond_path=True,
            expose_system_path=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_staged_file_rejects_dangling_sidecar_without_following_it(self) -> None:
        result = self.run_manager_harness(
            r"""
            set -u
            ui_print() { :; }
            root="$1"
            target="$root/policy"
            escaped="$root/escaped"
            printf original > "$target"
            ln -s "$escaped" "$target.kitsune-old"

            ! stage_file_rollback "$target"
            test "$(cat "$target")" = original
            test -L "$target.kitsune-old"
            test ! -e "$escaped"
            """
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_failed_payload_cleanup_finishes_before_session_unlock(self) -> None:
        self.assertNotIn('Shell.cmd("rm -rf $installDir").submit()', self.installer)
        operation = self.installer.index('operations().also { success = it }')
        cleanup = self.installer.index('finishOperation(success)', operation)
        unlock = self.installer.index('haveActiveSession.set(false)', cleanup)
        self.assertLess(operation, cleanup)
        self.assertLess(cleanup, unlock)
        self.assertIn('finally {\n                    finishOperation(success)', self.installer)

    def test_normal_install_reports_an_essential_runtime_publish_failure(self) -> None:
        direct = function_body(self.source, "direct_install")
        self.assertIn('if ! fix_env "$1"', direct)
        self.assertIn('Boot image was flashed, but the Magisk runtime', direct)


if __name__ == "__main__":
    unittest.main()
