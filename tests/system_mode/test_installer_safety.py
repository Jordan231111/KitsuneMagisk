from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts" / "kitsune_system_install.sh"
LAUNCHER = ROOT / "scripts" / "kitsune_system_launcher.sh"
RESCUE = ROOT / "scripts" / "kitsune_system_rescue.sh"
TRANSACTION = ROOT / "scripts" / "system_mode_transaction.sh"
VERIFIER = ROOT / "scripts" / "system_mode_verify.sh"
SETUP = ROOT / "app" / "buildSrc" / "src" / "main" / "java" / "Setup.kt"
PLUGIN = ROOT / "app" / "buildSrc" / "src" / "main" / "java" / "Plugin.kt"
MAGISK_INSTALLER = (
    ROOT / "app" / "core" / "src" / "main" / "java" / "com" /
    "topjohnwu" / "magisk" / "core" / "tasks" / "MagiskInstaller.kt"
)
INSTALL_VIEW_MODEL = (
    ROOT / "app" / "apk" / "src" / "main" / "java" / "com" /
    "topjohnwu" / "magisk" / "ui" / "install" / "InstallViewModel.kt"
)


def function_body(source: str, name: str) -> str:
    marker = f"{name}()"
    start = source.index(marker)
    next_function = source.find("\n}\n\n", start)
    if next_function < 0:
        if source.endswith("\n}\n"):
            next_function = len(source) - 3
        else:
            raise AssertionError(f"could not find end of {name}")
    return source[start:next_function + 3]


class MaintainedBaseSystemModeSafetyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.installer = INSTALLER.read_text(encoding="utf-8")
        cls.launcher = LAUNCHER.read_text(encoding="utf-8")
        cls.rescue = RESCUE.read_text(encoding="utf-8")
        cls.transaction = TRANSACTION.read_text(encoding="utf-8")
        cls.verifier = VERIFIER.read_text(encoding="utf-8")
        cls.setup = SETUP.read_text(encoding="utf-8")
        cls.plugin = PLUGIN.read_text(encoding="utf-8")
        cls.magisk_installer = MAGISK_INSTALLER.read_text(encoding="utf-8")
        cls.install_view_model = INSTALL_VIEW_MODEL.read_text(encoding="utf-8")

    def test_system_mode_is_a_dedicated_v30_7_installer(self) -> None:
        self.assertIn("kitsune_system_install.sh", self.setup)
        self.assertIn("kitsune_system_launcher.sh", self.setup)
        self.assertIn("kitsune_system_rescue.sh", self.setup)
        self.assertIn("class SystemMode", self.magisk_installer)
        self.assertNotIn("direct_install_system()", self.magisk_installer)
        self.assertNotIn("system_mode", (ROOT / "scripts" / "flash_script.sh").read_text())
        copy_payload = function_body(self.installer, "ks_copy_payload")
        self.assertIn('cp -aL "$source/$name"', copy_payload)
        for script in ("app_functions.sh", "uninstaller.sh", "module_installer.sh"):
            self.assertIn(script, copy_payload)
            self.assertIn(f'"{script}"', self.magisk_installer)

    def test_release_backend_rejects_before_transaction_preflight(self) -> None:
        validate = function_body(self.installer, "ks_validate_debug_payload")
        install = function_body(self.installer, "ks_install")
        self.assertIn(":MAGISK:D", validate)
        self.assertLess(install.index("ks_validate_debug_payload"), install.index("sm_begin_transaction"))

    def test_persistent_mode_rejects_a_dirty_source_identity(self) -> None:
        self.assertIn("sourceTreeDirty = gitTreeDirty", self.plugin)
        self.assertIn('"status", "--porcelain=v1"', self.plugin)
        self.assertIn("check(process.waitFor() == 0)", self.plugin)
        self.assertIn('findProperty("expectedSourceCommit")', self.plugin)
        self.assertIn("KITSUNE_SOURCE_DIRTY=${Config.sourceDirty}", self.setup)
        validate = function_body(self.installer, "ks_validate_identity")
        self.assertIn('KITSUNE_SOURCE_DIRTY:-true', validate)
        self.assertIn("exact clean commit", validate)

    def test_su_fork_cannot_inherit_the_daemon_log_mutex(self) -> None:
        daemon = (
            ROOT / "native" / "src" / "core" / "su" / "daemon.rs"
        ).read_text(encoding="utf-8")
        logging = (
            ROOT / "native" / "src" / "core" / "logging.rs"
        ).read_text(encoding="utf-8")
        child = daemon[daemon.index("let child = fork_with_logging_lock()") :]
        child = child[: child.index("if child < 0")]
        self.assertIn("let guard = MAGISK_LOGD_FD.lock()", logging)
        self.assertIn("let pid = unsafe { libc::fork() }", logging)
        self.assertLess(logging.index("libc::fork()"), logging.index("drop(guard)"))
        self.assertLess(child.index("android_logging()"), child.index('debug!("su: fork handler")'))
        self.assertLess(child.index("android_logging()"), child.index("client.write_pod(&0)"))

    def test_zygisk_child_restores_only_discovered_jni_families(self) -> None:
        hook = (
            ROOT / "native" / "src" / "core" / "zygisk" / "hook.cpp"
        ).read_text(encoding="utf-8")
        discovery = hook[
            hook.index("void HookContext::hook_zygote_jni()") :
            hook.index("void HookContext::restore_zygote_hook")
        ]
        for family in ("fork_app", "specialize_app", "fork_server"):
            self.assertIn(f"if (!replaced_{family})", discovery)
            self.assertIn(
                f"ranges::for_each({family}_methods, "
                "[](auto &m) { m.fnPtr = nullptr; });",
                discovery,
            )
        self.assertLess(
            discovery.index("strcmp(method.name, kSpecializeApp)"),
            discovery.index("if (!replaced_specialize_app)"),
        )

    def test_zygisk_memfd_remains_accessible_on_android_17(self) -> None:
        rules = (
            ROOT / "native" / "src" / "sepolicy" / "rules.rs"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'allow(["domain"], [proc], ["memfd_file"], '
            '["getattr", "read", "write", "map", "execute"]);',
            rules,
        )

    def test_zygisk_recognizes_android_17_qpr2_cgroup_signatures(self) -> None:
        source = ROOT / "native" / "src" / "core" / "zygisk"
        generator_path = source / "gen_jni_hooks.py"
        generator_bytes = generator_path.read_bytes()
        generator = generator_bytes.decode("utf-8")
        generated_path = source / "jni_hooks.hpp"
        generated_bytes = generated_path.read_bytes()
        generated = generated_bytes.decode("utf-8")
        self.assertIn('cgroup_uid = Argument("cgroup_uid", jint, False)', generator)
        self.assertIn("fas_c = ForkApp(", generator)
        self.assertIn("spec_c = SpecializeApp(", generator)

        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            copy = temporary / generator_path.name
            copy.write_bytes(generator_bytes)
            subprocess.run(
                [sys.executable, str(copy)],
                cwd=temporary,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(
                generated_bytes,
                (temporary / "jni_hooks.hpp").read_bytes(),
            )

        self.assertIn("std::array<JNINativeMethod, 13> fork_app_methods", generated)
        self.assertIn("std::array<JNINativeMethod, 8> specialize_app_methods", generated)
        fork = generated[
            generated.index("// nativeForkAndSpecialize_c") :
            generated.index("// nativeForkAndSpecialize_samsung_m")
        ]
        specialize = generated[
            generated.index("// nativeSpecializeAppProcess_c") :
            generated.index("// nativeSpecializeAppProcess_xr_u")
        ]
        self.assertIn(
            '"(III[II[[IILjava/lang/String;Ljava/lang/String;[I[IZ'
            'Ljava/lang/String;Ljava/lang/String;ZZ[Ljava/lang/String;'
            '[Ljava/lang/String;ZZZ)I"',
            fork,
        )
        self.assertIn(
            '"(III[II[[IILjava/lang/String;Ljava/lang/String;Z'
            'Ljava/lang/String;Ljava/lang/String;Z[Ljava/lang/String;'
            '[Ljava/lang/String;ZZZ)V"',
            specialize,
        )
        for method in (fork, specialize):
            self.assertIn("jint uid, jint cgroup_uid, jint gid", method)
            self.assertIn("env, clazz, uid, cgroup_uid, gid", method)

    def test_zygisk_rejects_malformed_module_libraries_before_dlopen(self) -> None:
        modules = (
            ROOT / "native" / "src" / "core" / "module.rs"
        ).read_text(encoding="utf-8")
        open_safe = modules[
            modules.index("fn open_fd_safe") : modules.index(
                "if open_zygisk && is_zygisk"
            )
        ]
        self.assertIn("OFlag::O_NONBLOCK", open_safe)
        self.assertIn("valid_zygisk_fd", open_safe)
        self.assertIn('warn!("{name}: invalid Zygisk ELF")', open_safe)
        self.assertLess(open_safe.index("valid_zygisk_fd"), open_safe.index("fd.into_raw_fd()"))

        validate = modules[
            modules.index("fn valid_zygisk_elf") : modules.index(
                "fn valid_zygisk_fd"
            )
        ]
        self.assertIn('header[..4] != *b"\\x7fELF"', validate)
        self.assertIn("header[4] != elf_class", validate)
        self.assertIn("!= machine", validate)
        self.assertIn("ehsize != header_size", validate)
        self.assertIn("phentsize != phdr_size", validate)
        self.assertIn("phdr_end > file_size", validate)
        self.assertIn("PT_LOAD", validate)
        self.assertIn("segment_offset.checked_add(file_bytes)", validate)
        self.assertIn("segment_end > file_size", validate)
        self.assertIn("file_bytes > memory_bytes", validate)
        self.assertIn("virtual_address.checked_add(file_bytes)", validate)
        self.assertIn("virtual_address.checked_add(memory_bytes)", validate)
        self.assertIn("rounded_memory_end > address_limit", validate)

        validate_fd = modules[
            modules.index("fn valid_zygisk_fd") : modules.index(
                "fn copy_module_to_memfd"
            )
        ]
        self.assertIn("fstat(source)", validate_fd)
        self.assertIn("SFlag::S_IFREG", validate_fd)
        self.assertIn("valid_zygisk_elf", validate_fd)
        self.assertLess(validate_fd.index("SFlag::S_IFREG"), validate_fd.index("valid_zygisk_elf"))

        copy = modules[
            modules.index("fn copy_module_to_memfd") : modules.index(
                "pub fn remove_modules"
            )
        ]
        self.assertIn("while offset < source_size", copy)
        self.assertIn("Some(libc::EINTR)", copy)
        self.assertIn("destination_attr.st_size == source_attr.st_size", copy)
        self.assertIn("valid_zygisk_elf(memfd", copy)
        self.assertNotIn("ptr::null_mut()", copy)

        conversion = modules[
            modules.index("let mut convert_to_memfd") : modules.index(
                "modules.iter_mut().for_each"
            )
        ]
        self.assertIn("libc::close(memfd)", conversion)
        self.assertIn("libc::close(fd)", conversion)
        self.assertIn("valid_zygisk_fd(fd, elf_class, machine)", conversion)
        self.assertNotIn("libc::lseek(fd", conversion)

        environment = (
            ROOT / "app" / "core" / "src" / "main" / "java" / "com" /
            "topjohnwu" / "magisk" / "test" / "Environment.kt"
        ).read_text(encoding="utf-8")
        self.assertIn("elfFixtures(truncated = true)", environment)
        self.assertIn("machineOverride = 0", environment)
        self.assertIn("zeroRange = true", environment)
        self.assertIn("addressOverflow = true", environment)
        self.assertIn('Shell.cmd("mkfifo $library")', environment)
        self.assertIn('"libzygisk_test.so"', environment)
        self.assertIn('"${Build.SUPPORTED_ABIS.first()}.so"', environment)
        sample = (
            ROOT / "app" / "test" / "src" / "main" / "cpp" /
            "zygisk_test.cpp"
        ).read_text(encoding="utf-8")
        self.assertIn("REGISTER_ZYGISK_MODULE(TestModule)", sample)
        self.assertIn("kitsune-zygisk-test-v1:minimal-resident", sample)
        self.assertIn("kitsune-zygisk-test-v1:minimal-dlclose", sample)
        self.assertIn("DLCLOSE_MODULE_LIBRARY", sample)
        cmake = (
            ROOT / "app" / "test" / "src" / "main" / "cpp" /
            "CMakeLists.txt"
        ).read_text(encoding="utf-8")
        self.assertIn("-fno-threadsafe-statics", cmake)
        self.assertIn("-nostdlib++", cmake)
        self.assertIn("KITSUNE_ZYGISK_TEST_DLCLOSE=1", cmake)

    def test_ui_requires_root_debug_and_android_7_1_or_newer(self) -> None:
        line = next(
            item for item in self.install_view_model.splitlines()
            if "val allowSystemMode" in item
        )
        self.assertIn("BuildConfig.DEBUG", line)
        self.assertIn("isRooted", line)
        self.assertIn("SDK_INT >= 25", line)
        self.assertIn("SystemModeWarningDialog", self.install_view_model)

    def test_manager_uses_a_private_mount_namespace(self) -> None:
        direct = self.magisk_installer[
            self.magisk_installer.index("protected suspend fun directSystem"):
            self.magisk_installer.index("protected suspend fun secondSlot")
        ]
        worker = (ROOT / "app/core/src/main/java/com/topjohnwu/magisk/core/utils/RootUtils.kt").read_text()
        self.assertIn('runSystemMode("install")', direct)
        self.assertIn('"unshare", "-m"', worker)
        self.assertIn('kitsune_system_install.sh', worker)
        self.assertIn('ProcessBuilder(command)', worker)
        self.assertIn('Binder.getCallingUid() != applicationInfo.uid', worker)
        self.assertIn('NonCancellable', worker)
        self.assertIn('BuildConfig.DEBUG', direct)
        self.assertLess(direct.index("BuildConfig.DEBUG"), direct.index("extractFiles"))

    def test_host_authorization_precedes_every_target_mutation(self) -> None:
        begin = function_body(self.transaction, "sm_begin_transaction")
        self.assertLess(begin.index("sm_validate_authorization"), begin.index("SM_STATE=PREFLIGHTED"))
        self.assertLess(begin.index("sm_validate_host_lease"), begin.index("sm_consume_authorization"))
        self.assertLess(begin.index("sm_consume_authorization"), begin.index("sm_probe_writable_directory"))
        self.assertIn("BACKUP_SHA256", function_body(self.transaction, "sm_validate_authorization"))
        self.assertIn("SERIAL_SHA256", function_body(self.transaction, "sm_validate_authorization"))
        self.assertIn("SOURCE_COMMIT", function_body(self.transaction, "sm_validate_authorization"))
        self.assertIn("ARTIFACT_SHA256", function_body(self.transaction, "sm_validate_authorization"))
        self.assertIn("QUALIFICATION_SHA256", function_body(self.transaction, "sm_validate_authorization"))
        self.assertIn("INSTANCE_IDENTITY_SHA256", function_body(self.transaction, "sm_validate_authorization"))
        lease = function_body(self.transaction, "sm_validate_host_lease")
        self.assertIn("LEASE_NONCE_SHA256", lease)
        self.assertIn("127.0.0.1:$SM_LEASE_PORT/$SM_AUTHORIZATION_ID", lease)
        self.assertIn("wget -qO-", lease)
        install = function_body(self.installer, "ks_install")
        self.assertLess(install.index("sm_begin_transaction"), install.index("ks_stage_payload"))

    def test_nonce_handoff_hashes_the_external_backup_once(self) -> None:
        cli = (
            ROOT / "tools" / "system_mode" / "kitsune.py"
        ).read_text(encoding="utf-8")
        handoff = cli[
            cli.index("def verify_host_handoff()") :
            cli.index("with HostLease(")
        ]
        self.assertEqual(1, handoff.count("load_qualification_evidence("))
        self.assertNotIn("evidence_from_record(", handoff)
        self.assertNotIn("verify_report_qualification(", handoff)
        self.assertEqual(2, handoff.count("verify_report_qualification_evidence("))

    def test_kernel_lock_and_atomic_auth_claim_cover_every_installer_action(self) -> None:
        main = function_body(self.installer, "ks_main")
        acquire = main.index('sm_acquire_lock "installer-$action"')
        dispatch = main.index('case "$action" in', acquire)
        release = main.index("sm_release_lock", dispatch)
        self.assertLess(acquire, dispatch)
        self.assertLess(dispatch, release)
        lock = function_body(self.transaction, "sm_acquire_lock")
        self.assertIn("flock -n 9", lock)
        self.assertIn("SM_LOCK_WAIT_ATTEMPTS", lock)
        consume = function_body(self.transaction, "sm_consume_authorization")
        self.assertLess(consume.index("sm_require_lock"), consume.index('mv "$SM_AUTHORIZATION_FILE" "$claim"'))
        self.assertIn("SM_AUTH_FILE_SHA256", consume)

    def test_remount_prepare_and_rollback_are_self_unwinding(self) -> None:
        prepare = function_body(self.transaction, "sm_prepare_persistent_mounts")
        restore = function_body(self.transaction, "sm_restore_persistent_mounts")
        rollback = function_body(self.installer, "ks_rollback")
        self.assertLess(prepare.index("sm_record_persistent_remount"), prepare.index("sm_remount rw"))
        self.assertIn("sm_restore_persistent_mounts", prepare)
        self.assertIn("sm_write_remount_journal", restore)
        self.assertIn("sm_prepare_persistent_mounts", rollback)

    def test_ownership_inventory_is_bound_and_proc_scan_fails_closed(self) -> None:
        receipt = function_body(self.transaction, "sm_write_transaction")
        manifest = function_body(self.transaction, "sm_generate_manifest")
        ownership = function_body(self.transaction, "sm_validate_ownership_inventory")
        ownership_rows = function_body(self.transaction, "sm_validate_ownership_rows")
        owned_entries = function_body(self.transaction, "sm_verify_owned_entries")
        idle = function_body(self.transaction, "sm_assert_mutable_namespaces_idle")
        self.assertIn("OWNERSHIP_SHA256", receipt)
        self.assertIn("ownership_inventory_sha256", manifest)
        self.assertIn("sm_sha256_file", ownership)
        self.assertIn("NF != 8", ownership_rows)
        self.assertIn("sm_owned_path_allowed", ownership_rows)
        self.assertIn('[ ! -L "$real" ]', owned_entries)
        self.assertIn("fdinfo", idle)
        self.assertIn("Cannot inspect process", idle)
        self.assertIn('substr($2, 2, 1) == "w"', idle)
        self.assertNotIn('substr($2, 4, 1) == "s"', idle)

    def test_persistent_state_and_authorization_reject_symlink_redirection(self) -> None:
        storage = function_body(self.transaction, "sm_validate_state_storage")
        authorization = function_body(self.transaction, "sm_validate_authorization")
        authorization_file = function_body(self.transaction, "sm_authorization_file_safe")
        self.assertIn('[ ! -L "$path" ]', storage)
        self.assertIn("sm_reject_unsafe_mode", storage)
        self.assertIn('sm_authorization_file_safe "$auth"', authorization)
        self.assertIn('[ ! -L "$path" ]', authorization_file)
        self.assertIn("0:600|2000:600", authorization_file)
        self.assertIn("sm_validate_state_storage", function_body(self.transaction, "sm_load_transaction"))

    def test_policy_is_validated_with_current_cli_but_not_persistently_rewritten(self) -> None:
        policy = function_body(self.installer, "ks_validate_policy")
        self.assertIn('--load "$policy" --save "$output" --magisk', policy)
        self.assertIn('--load-split --save "$output" --magisk', policy)
        self.assertIn('--save "$output.live" --magisk', policy)
        self.assertNotIn('--live --magisk', policy)
        self.assertNotIn('sm_atomic_publish', policy)
        self.assertNotIn('magiskinit', policy)
        strategies = function_body(self.transaction, "sm_select_strategies")
        self.assertIn("/sepolicy", strategies)
        self.assertIn("split) SM_POLICY_PATH=", strategies)

    def test_version_is_complete_before_init_switches_to_it(self) -> None:
        install = function_body(self.installer, "ks_install")
        restore_policy = install.index("sm_restore_legacy_policy")
        restore_bootanim = install.index("sm_restore_legacy_bootanim")
        publish_version = install.index("ks_publish_version")
        publish_rc = install.index("ks_publish_rc")
        prune_old = install.index("ks_remove_superseded_payload")
        runtime = install.index("ks_publish_runtime")
        commit = install.index("sm_commit_transaction")
        rescue_payload = install.index("ks_publish_rescue_payload")
        rescue_rc = install.index("ks_publish_rescue_rc")
        self.assertLess(rescue_payload, rescue_rc)
        self.assertLess(rescue_rc, restore_policy)
        self.assertLess(restore_policy, restore_bootanim)
        self.assertLess(restore_bootanim, publish_version)
        self.assertLess(publish_version, publish_rc)
        self.assertLess(publish_rc, prune_old)
        self.assertLess(prune_old, runtime)
        self.assertLess(runtime, commit)
        self.assertIn(
            'SM_ACTIVE_PAYLOAD="$SM_SYSTEM_PAYLOAD_PREFIX/$SM_TRANSACTION_ID"',
            self.transaction,
        )
        stage = function_body(self.installer, "ks_stage_payload")
        self.assertIn('sm_sha256_file "$stage/magisk.apk"', stage)
        self.assertIn("SM_ARTIFACT_SHA256", stage)

    def test_legacy_startup_sidecars_are_removed_only_inside_the_transaction(self) -> None:
        install = function_body(self.installer, "ks_install")
        remove_sidecars = install.index("sm_remove_legacy_sidecars")
        commit = install.index("sm_commit_transaction")
        self.assertLess(remove_sidecars, commit)
        policy_restore = function_body(self.transaction, "sm_restore_legacy_policy")
        self.assertIn("SM_ORIGINAL_FILE", policy_restore)
        self.assertIn("sm_digest_path", policy_restore)
        self.assertIn("sm_atomic_publish", policy_restore)
        begin = function_body(self.transaction, "sm_begin_transaction")
        self.assertIn("legacy_policy_mutated", begin)
        self.assertIn("legacy_migration", begin)
        sidecar = function_body(self.transaction, "sm_find_legacy_policy_sidecar")
        self.assertIn("/vendor/etc/selinux/precompiled_sepolicy", sidecar)
        self.assertIn('sm_path_present "$real.gz"', sidecar)
        self.assertIn('gzip -t "$real.gz"', sidecar)
        self.assertIn("ks_remove_legacy_rc", install)

    def test_rc_has_one_launcher_and_boot_stage_order(self) -> None:
        rc = function_body(self.installer, "ks_write_rc")
        self.assertEqual(1, rc.count("on post-fs-data"))
        self.assertIn("kitsune_system_launcher.sh prepare", rc)
        self.assertIn("kitsune_system_launcher.sh post-fs-data", rc)
        self.assertIn("kitsune_system_launcher.sh service", rc)
        self.assertIn("kitsune_system_launcher.sh boot-complete", rc)
        self.assertIn("--zygote-restart", rc)
        self.assertIn("system_mode_verify.sh", rc)
        self.assertIn("on property:vold.decrypt=trigger_restart_framework", rc)
        self.assertIn("on nonencrypted", rc)
        self.assertIn("kitsune.system_mode.service.phase=1", rc)
        self.assertIn("kitsune.system_mode.service.ready", rc)
        self.assertIn("kitsune.system_mode.boot_complete.ready", rc)
        self.assertLess(rc.index("post-fs-data"), rc.index("vold.decrypt=trigger_restart_framework"))
        self.assertLess(rc.index("vold.decrypt=trigger_restart_framework"), rc.index("launcher.sh service"))
        self.assertLess(rc.index("launcher.sh service"), rc.index("launcher.sh boot-complete"))

    def test_launcher_derives_runtime_bootstrap_from_upstream_live_setup(self) -> None:
        for required in (
            "/sbin",
            "/debug_ramdisk",
            "mount -t tmpfs",
            ".magisk/device",
            ".magisk/worker",
            "--preinit-device",
            "resetprop",
            "supolicy",
        ):
            self.assertIn(required, self.launcher)
        self.assertNotIn("--auto-selinux", self.launcher)
        self.assertNotIn("--setup-sbin", self.launcher)
        self.assertNotIn("rm -rf /root", self.launcher)
        self.assertIn("/dev/.kitsune-system-mode-sbin-", self.launcher)
        prepare = function_body(self.launcher, "ksl_prepare_runtime")
        self.assertIn('case "$preinit_result" in 0|1)', prepare)

    def test_launcher_runtime_setup_is_idempotent_for_one_boot(self) -> None:
        prepare = function_body(self.launcher, "ksl_prepare_runtime")
        self.assertIn('.kitsune-system-mode-$SM_INSTALL_ID', prepare)
        self.assertIn('"$target/magisk" -V', prepare)
        self.assertLess(prepare.index(".kitsune-system-mode-$SM_INSTALL_ID"), prepare.index('case "$target"'))

    def test_runtime_selection_never_mounts_through_an_sbin_symlink(self) -> None:
        strategies = function_body(self.transaction, "sm_select_strategies")
        mount_sbin = function_body(self.launcher, "ksl_mount_sbin")
        self.assertIn("[ -L /sbin ]", strategies)
        self.assertIn("[ ! -L /sbin ] || return 1", mount_sbin)

    def test_pending_state_recovers_before_daemon_start(self) -> None:
        case = self.launcher.index('case "$SM_STATE"')
        recover = self.launcher.index("ksl_recover_pending", case)
        policy = self.launcher.index("--live --magisk", recover)
        ready = self.launcher.index("ksl_set_stage_ready kitsune.system_mode.ready", policy)
        self.assertLess(recover, policy)
        self.assertLess(policy, ready)

    def test_prior_launcher_recovers_an_interrupted_upgrade(self) -> None:
        mismatch = self.launcher[self.launcher.index('if [ "$SM_ACTIVE_PAYLOAD" != "$KSL_PAYLOAD" ]'):]
        self.assertIn("PREFLIGHTED|STAGED|COMMITTED|ROLLBACK_REQUIRED|ROLLING_BACK", mismatch)
        self.assertIn("ksl_recover_pending", mismatch)
        first_case = mismatch.index('case "$SM_STATE"')
        main_case = mismatch.index('case "$SM_STATE"', first_case + 1)
        self.assertLess(mismatch.index("ksl_recover_pending"), main_case)

    def test_module_preinit_policy_is_applied_after_runtime_discovery(self) -> None:
        main = function_body(self.launcher, "ksl_prepare")
        self.assertLess(main.index("ksl_prepare_runtime"), main.index("ksl_apply_policy"))
        policy = function_body(self.launcher, "ksl_apply_policy")
        self.assertIn(".magisk/preinit/sepolicy.rule", policy)
        self.assertIn('--apply "$rule"', policy)
        self.assertIn('magiskpolicy" --live --magisk', policy)
        self.assertNotIn('--load "$SM_POLICY_SOURCE"', policy)

    def test_launcher_registers_one_boot_attempt_before_runtime_setup(self) -> None:
        register = self.launcher.index("sm_register_boot_attempt")
        policy = self.launcher.index("--live --magisk", register)
        ready = self.launcher.index("ksl_set_stage_ready kitsune.system_mode.ready", policy)
        self.assertLess(register, policy)
        self.assertLess(policy, ready)
        transaction = function_body(self.transaction, "sm_register_boot_attempt")
        self.assertIn("SM_BOOT_ATTEMPT_ID", transaction)
        self.assertIn("sm_update_state ROLLBACK_REQUIRED", transaction)

    def test_runtime_publication_has_durable_backup_and_publish_boundaries(self) -> None:
        runtime = function_body(self.installer, "ks_publish_runtime")
        self.assertLess(runtime.index('mv "$runtime" "$old"'), runtime.index('mv "$stage" "$runtime"'))
        self.assertIn("runtime-backed-up", runtime)
        self.assertIn("runtime-published", runtime)
        self.assertIn("sm_fsync_tree", runtime)
        self.assertIn("sm_fsync /data/adb", runtime)

    def test_boot_verification_binds_boot_id_daemon_version_and_owned_bytes(self) -> None:
        verify = function_body(self.transaction, "sm_verify_boot")
        self.assertIn("sm_verify_owned", verify)
        self.assertIn("SM_COMMIT_BOOT_ID", verify)
        self.assertIn("SM_BOOT_ATTEMPT_ID", verify)
        self.assertIn('magisk" -V', verify)
        self.assertIn("SM_VERSION_CODE", verify)
        self.assertIn('su" -c /system/bin/id', verify)
        self.assertIn("ROOT_IDENTITY_SHA256", verify)
        self.assertIn("kitsune.system_mode.service.ready", verify)
        self.assertIn("kitsune.system_mode.post_fs_data.ready", verify)
        self.assertIn("kitsune.system_mode.boot_complete.ready", verify)
        self.assertIn("SERVICE_STAGE_ID", verify)
        self.assertIn("ROLLBACK_REQUIRED", verify)

    def test_phase_markers_are_published_only_after_success(self) -> None:
        for function, command, marker in (
            ("ksl_post_fs_data", "--post-fs-data", "ksl_set_stage_ready kitsune.system_mode.post_fs_data.ready"),
            ("ksl_service", "--service", "ksl_set_stage_ready kitsune.system_mode.service.ready"),
            ("ksl_boot_complete", "--boot-complete", "ksl_set_stage_ready kitsune.system_mode.boot_complete.ready"),
        ):
            with self.subTest(function=function):
                body = function_body(self.launcher, function)
                self.assertLess(body.index(command), body.index(marker))
                self.assertIn("|| {", body)

    def test_invariant_rescue_precedes_boot_critical_mutation(self) -> None:
        install = function_body(self.installer, "ks_install")
        rescue_payload = install.index("ks_publish_rescue_payload")
        rescue_rc = install.index("ks_publish_rescue_rc")
        first_destructive = install.index("sm_restore_legacy_policy")
        self.assertLess(rescue_payload, rescue_rc)
        self.assertLess(rescue_rc, first_destructive)
        self.assertIn("PREFLIGHTED|STAGED|ROLLBACK_REQUIRED|ROLLING_BACK", self.rescue)
        self.assertIn("sm_register_boot_attempt", self.rescue)
        self.assertIn("sm_abort_transaction", self.rescue)
        self.assertIn("/dev/.kitsune-system-mode-rescue", self.rescue)
        restore = function_body(self.transaction, "sm_restore_snapshot")
        self.assertLess(restore.index("payload legacy_rc init_rc"), restore.index("rescue_rc rescue_dir"))
        uninstall = self.transaction[self.transaction.index("sm_uninstall()") :]
        self.assertLess(uninstall.index("sm_update_state UNINSTALLED"), uninstall.rindex("sm_restore_originals rescue"))

    def test_uninstall_is_manifest_owned_and_normal_uninstall_stays_separate(self) -> None:
        uninstall = function_body(self.installer, "ks_uninstall")
        self.assertIn("sm_uninstall", uninstall)
        self.assertIn("sm_validate_installed_state", self.transaction)
        self.assertIn("sm_assert_no_unowned_files", self.transaction)
        self.assertNotIn("*magisk*", uninstall)
        self.assertIn("run_uninstaller", self.magisk_installer)
        self.assertIn("systemModeTransactionPresent", self.magisk_installer)
        self.assertIn("hasSystemModeFootprint", self.magisk_installer)
        self.assertIn("systemModeStateStoragePresent", self.magisk_installer)
        self.assertIn("! -name rollback", self.magisk_installer)
        self.assertIn("[ -L /system/etc/init/00-kitsune-magisk-rescue.rc ]", self.magisk_installer)
        self.assertIn("artifacts exist without a valid transaction state", self.magisk_installer)
        self.assertIn("rejectOrdinaryInstallOverSystemMode", self.magisk_installer)
        managed = self.magisk_installer[
            self.magisk_installer.index("private fun systemModeNeedsManagedPath"):
            self.magisk_installer.index("private fun rejectOrdinaryInstallOverSystemMode")
        ]
        self.assertIn("!stateStorage", managed)
        uninstall_route = self.magisk_installer[
            self.magisk_installer.index("protected suspend fun uninstall"):
            self.magisk_installer.index("@WorkerThread", self.magisk_installer.index("protected suspend fun uninstall"))
        ]
        self.assertIn("!stateStorage", uninstall_route)
        self.assertIn("!terminalMarker", uninstall_route)
        no_unowned = function_body(self.transaction, "sm_assert_no_unowned_files")
        self.assertIn("! -type f ! -type l ! -type d", no_unowned)
        self.assertIn("Owned root became a symlink", no_unowned)
        self.assertIn("Unowned empty directory", no_unowned)

    def test_mutable_snapshot_and_restore_fail_closed_at_mount_boundaries(self) -> None:
        guard = function_body(self.transaction, "sm_managed_path_safe")
        snapshot = function_body(self.transaction, "sm_snapshot_one")
        restore = function_body(self.transaction, "sm_restore_snapshot")
        originals = function_body(self.transaction, "sm_restore_originals")
        self.assertIn("SM_MOUNTINFO_FILE", guard)
        self.assertIn("! -type f ! -type d ! -type l", guard)
        self.assertIn("sm_snapshot_source_safe", snapshot)
        self.assertIn("sm_destructive_target_safe", restore)
        self.assertIn("sm_destructive_target_safe", originals)

    def test_terminal_states_retry_sensitive_rollback_cleanup(self) -> None:
        recover = function_body(self.transaction, "sm_recover_pending")
        abort = function_body(self.transaction, "sm_abort_transaction")
        verify = function_body(self.transaction, "sm_verify_boot")
        uninstall = function_body(self.transaction, "sm_uninstall")
        for body in (recover, abort, verify, uninstall):
            self.assertIn("sm_cleanup_terminal_rollback", body)

    def test_data_adb_is_validated_before_first_setup_marker(self) -> None:
        begin = function_body(self.transaction, "sm_begin_transaction")
        self.assertLess(
            begin.index("sm_validate_secure_dir_base"),
            begin.index("sm_publish_setup_marker"),
        )

    def test_fault_classes_and_post_publish_recovery_are_present(self) -> None:
        for fault in (
            "enospc:",
            "erofs:",
            "short-write:",
            "fsync-file:",
            "fsync-parent:",
            "rename:",
            "process-death:",
            "reboot:",
        ):
            self.assertIn(fault, self.transaction)
        self.assertIn("sm_abort_transaction", self.verifier)
        self.assertIn("reboot -f", self.verifier)
        begin = function_body(self.transaction, "sm_begin_transaction")
        self.assertIn("sm_initialize_journal", begin)
        self.assertLess(begin.index("sm_initialize_journal"), begin.index("sm_update_state STAGED"))
        self.assertIn("sm_journal_mark", self.installer)

    def test_recovery_reboots_only_with_the_boot_tmpfs_busybox(self) -> None:
        launcher_reboot = function_body(self.launcher, "ksl_reboot")
        launcher_recovery = function_body(self.launcher, "ksl_recover_pending")
        verifier_reboot = function_body(self.verifier, "smv_reboot")
        verifier_recovery = self.verifier[self.verifier.index("if sm_abort_transaction"):]
        self.assertIn('ksl_reboot "$recovery_bb"', launcher_recovery)
        self.assertIn('"$reboot_bb" sleep 2', launcher_reboot)
        self.assertIn('"$reboot_bb" reboot -f', launcher_reboot)
        self.assertIn('"$SMV_BB" sleep 2', verifier_reboot)
        self.assertIn('"$SMV_BB" reboot -f', verifier_reboot)
        self.assertNotIn('"$SMV_SOURCE_BB" sleep', verifier_recovery)
        self.assertNotIn('rm -rf "$SMV_TMP"', verifier_recovery)

    def test_manifest_records_source_target_recovery_and_selected_strategies(self) -> None:
        manifest = function_body(self.transaction, "sm_generate_manifest")
        for field in (
            "source_commit",
            "upstream_base",
            "artifact_sha256",
            "version_code",
            "adapter_id",
            "serial_sha256",
            "fingerprint_sha256",
            "policy_mutated",
            "legacy_migration",
            "active_payload",
            "rescue_payload",
            "restore_command",
        ):
            self.assertIn(field, manifest)

    def test_system_mode_never_uses_legacy_filename_activation(self) -> None:
        combined = self.installer + self.launcher + self.transaction
        self.assertNotIn("systemmagisk", combined)
        self.assertNotIn("bootanim.rc", function_body(self.installer, "ks_write_rc"))

    def test_manager_requires_one_shot_consent_without_implicit_recovery(self) -> None:
        direct = self.magisk_installer[
            self.magisk_installer.index("protected suspend fun directSystem"):
            self.magisk_installer.index("protected suspend fun secondSlot")
        ]
        consent = self.magisk_installer[
            self.magisk_installer.index("object SystemModeConsent"):
            self.magisk_installer.index("abstract class MagiskInstallImpl")
        ]
        self.assertIn("SystemModeConsent.consume(consent)", direct)
        self.assertIn("confirmation is missing or expired", direct)
        self.assertNotIn("recoverSystemMode", direct)
        self.assertIn("MessageDigest.isEqual", consent)
        self.assertIn("if (valid)", consent)

    def test_manager_process_restoration_does_not_replay_privileged_action(self) -> None:
        flash = (
            ROOT / "app" / "apk" / "src" / "main" / "java" / "com" /
            "topjohnwu" / "magisk" / "ui" / "flash" / "FlashViewModel.kt"
        ).read_text(encoding="utf-8")
        recovery = flash[
            flash.index("fun recoverAfterProcessDeath"):
            flash.index("private fun onResult")
        ]
        self.assertIn("Reboot once", recovery)
        self.assertIn("onResult(false)", recovery)
        self.assertNotIn("startFlashing()", recovery)
        self.assertNotIn("MagiskInstaller.SystemMode", recovery)

    def test_manager_blocks_ordinary_magisk_and_restore_collisions(self) -> None:
        info = (
            ROOT / "app" / "core" / "src" / "main" / "java" / "com" /
            "topjohnwu" / "magisk" / "core" / "Info.kt"
        ).read_text(encoding="utf-8")
        restore = self.magisk_installer[
            self.magisk_installer.index("protected fun restore"):
            self.magisk_installer.index("protected suspend fun uninstall")
        ]
        direct = self.magisk_installer[
            self.magisk_installer.index("protected suspend fun directSystem"):
            self.magisk_installer.index("protected suspend fun secondSlot")
        ]
        self.assertIn("Info.hasMagiskState", self.install_view_model)
        self.assertIn("Info.isSystemMode", self.install_view_model)
        self.assertIn("/data/adb/magisk", info)
        self.assertNotIn("/data/adb/modules", info)
        self.assertIn("Info.hasMagiskState && !Info.isSystemMode", direct)
        self.assertIn("rejectOrdinaryInstallOverSystemMode", restore)

    def test_manager_session_lock_and_failure_cleanup_are_finally_safe(self) -> None:
        execute = self.magisk_installer[
            self.magisk_installer.index("open suspend fun exec"):
            self.magisk_installer.index("companion object", self.magisk_installer.index("open suspend fun exec"))
        ]
        self.assertIn("compareAndSet(false, true)", execute)
        self.assertIn("NonCancellable", execute)
        self.assertIn("finally", execute)
        self.assertLess(execute.rindex("finally"), execute.index("haveActiveSession.set(false)"))


if __name__ == "__main__":
    unittest.main()
