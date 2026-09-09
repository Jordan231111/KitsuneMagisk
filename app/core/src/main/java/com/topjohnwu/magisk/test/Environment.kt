package com.topjohnwu.magisk.test

import android.app.Notification
import android.os.Build
import androidx.annotation.Keep
import androidx.core.net.toUri
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.topjohnwu.magisk.core.BuildConfig.APP_PACKAGE_NAME
import com.topjohnwu.magisk.core.Const
import com.topjohnwu.magisk.core.download.DownloadNotifier
import com.topjohnwu.magisk.core.download.DownloadProcessor
import com.topjohnwu.magisk.core.ktx.cachedFile
import com.topjohnwu.magisk.core.model.module.LocalModule
import com.topjohnwu.magisk.core.tasks.AppMigration
import com.topjohnwu.magisk.core.tasks.FlashZip
import com.topjohnwu.magisk.core.tasks.MagiskInstaller
import com.topjohnwu.magisk.core.utils.RootUtils
import com.topjohnwu.superuser.CallbackList
import com.topjohnwu.superuser.Shell
import com.topjohnwu.superuser.ShellUtils
import com.topjohnwu.superuser.nio.ExtendedFile
import kotlinx.coroutines.runBlocking
import org.apache.commons.compress.archivers.zip.ZipFile
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.BeforeClass
import org.junit.Test
import org.junit.runner.RunWith
import timber.log.Timber
import java.io.File
import java.io.PrintStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

@Keep
@RunWith(AndroidJUnit4::class)
class Environment : BaseTest {

    companion object {
        @BeforeClass
        @JvmStatic
        fun before() = BaseTest.prerequisite()

        // Whether we're running with live setup (patches through live_setup.sh)
        fun isLiveSetup(): Boolean {
            val liveMarker = ShellUtils.fastCmd("echo \$MAGISKTMP/.magisk/live")
            return RootUtils.fs.getFile(liveMarker).exists()
        }

        // The kernel running on emulators < API 26 does not play well with
        // magic mount. Skip mount_test on those legacy platforms.
        fun mount(): Boolean {
            return Build.VERSION.SDK_INT >= 26
        }

        // It is possible that there are no suitable preinit partition to use.
        // We also skip this test when running within a live setup.
        fun preinit(): Boolean {
            return !isLiveSetup() && Shell.cmd("magisk --preinit-device").exec().isSuccess
        }

        fun lsposed(): Boolean {
            return Build.VERSION.SDK_INT in 28..37 && Const.CPU_ABI != "x86"
        }

        fun shamiko(): Boolean {
            return Build.VERSION.SDK_INT >= 27
        }

        private const val MODULE_UPDATE_PATH  = "/data/adb/modules_update"
        private const val MODULE_ERROR = "Module zip processing incorrect"
        const val MOUNT_TEST = "mount_test"
        const val SEPOLICY_RULE = "sepolicy_rule"
        const val INVALID_ZYGISK = "invalid_zygisk"
        const val OVERFLOW_ZYGISK = "overflow_zygisk"
        const val SPECIAL_ZYGISK = "special_zygisk"
        const val UNLOAD_ZYGISK = "unload_zygisk"
        const val VALID_ZYGISK = "valid_zygisk"
        const val WRONG_ABI_ZYGISK = "wrong_abi_zygisk"
        const val ZERO_RANGE_ZYGISK = "zero_range_zygisk"
        const val REMOVE_TEST = "remove_test"
        const val REMOVE_TEST_MARKER = "/dev/.remove_test_removed"
        const val EMPTY_ZYGISK = "empty_zygisk"
        const val UPGRADE_TEST = "upgrade_test"
    }

    object TimberLog : CallbackList<String>(Runnable::run) {
        override fun onAddElement(e: String) {
            Timber.i(e)
        }
    }

    private fun checkModuleZip(file: File) {
        // Make sure module processing is correct
        ZipFile.Builder().setFile(file).get().use { zip ->
            val meta = zip.entries
                .asSequence()
                .filter { it.name.startsWith("META-INF") }
                .toMutableList()
            assertEquals(MODULE_ERROR, 6, meta.size)

            val binary = zip.getInputStream(
                zip.getEntry("META-INF/com/google/android/update-binary")
            ).use { it.readBytes() }
            val ref = appContext.assets.open("module_installer.sh").use { it.readBytes() }
            assertArrayEquals(MODULE_ERROR, ref, binary)

            val script = zip.getInputStream(
                zip.getEntry("META-INF/com/google/android/updater-script")
            ).use { it.readBytes() }
            assertArrayEquals(MODULE_ERROR, "#MAGISK\n".toByteArray(), script)
        }
    }

    private fun setupMountTest(root: ExtendedFile) {
        val error = "$MOUNT_TEST setup failed"
        val path = root.getChildFile(MOUNT_TEST)

        // Create /system/fonts/newfile
        val etc = path.getChildFile("system").getChildFile("fonts")
        assertTrue(error, etc.mkdirs())
        assertTrue(error, etc.getChildFile("newfile").createNewFile())

        // Create /system/app/EasterEgg/.replace
        val egg = path.getChildFile("system").getChildFile("app").getChildFile("EasterEgg")
        assertTrue(error, egg.mkdirs())
        assertTrue(error, egg.getChildFile(".replace").createNewFile())

        // Create /system/app/EasterEgg/newfile
        assertTrue(error, egg.getChildFile("newfile").createNewFile())

        // Delete /system/bin/screenrecord
        val bin = path.getChildFile("system").getChildFile("bin")
        assertTrue(error, bin.mkdirs())
        assertTrue(error, Shell.cmd("mknod $bin/screenrecord c 0 0").exec().isSuccess)

        assertTrue(error, Shell.cmd("set_default_perm $path").exec().isSuccess)
    }

    private fun setupSystemlessHost() {
        val error = "hosts setup failed"
        assertTrue(error, runBlocking { RootUtils.addSystemlessHosts() })
        assertTrue(error, RootUtils.fs.getFile(Const.MODULE_PATH).getChildFile("hosts").exists())
    }

    private fun setupSepolicyRuleModule(root: ExtendedFile) {
        val error = "$SEPOLICY_RULE setup failed"
        val path = root.getChildFile(SEPOLICY_RULE)
        assertTrue(error, path.mkdirs())

        // Add sepolicy patch
        PrintStream(path.getChildFile("sepolicy.rule").newOutputStream()).use {
            it.println("type magisk_test domain")
        }

        assertTrue(error, Shell.cmd(
            "set_default_perm $path",
            "copy_preinit_files"
        ).exec().isSuccess)
    }

    private fun setupEmptyZygiskModule(root: ExtendedFile) {
        val error = "$EMPTY_ZYGISK setup failed"
        val path = root.getChildFile(EMPTY_ZYGISK)

        // Create an empty zygisk folder
        val module = LocalModule(path)
        assertTrue(error, module.zygiskFolder.mkdirs())
    }

    private fun setupInvalidZygiskModule(root: ExtendedFile) {
        val error = "$INVALID_ZYGISK setup failed"
        val path = root.getChildFile(INVALID_ZYGISK)

        // Create complete ELF headers whose load segments extend past EOF.
        val module = LocalModule(path)
        assertTrue(error, module.zygiskFolder.mkdirs())
        for ((abi, elf) in elfFixtures(truncated = true)) {
            module.zygiskFolder.getChildFile(abi).newOutputStream().use {
                it.write(elf)
            }
        }

        assertTrue(error, Shell.cmd("set_default_perm $path").exec().isSuccess)
    }

    private fun setupWrongAbiZygiskModule(root: ExtendedFile) {
        val error = "$WRONG_ABI_ZYGISK setup failed"
        val path = root.getChildFile(WRONG_ABI_ZYGISK)
        val module = LocalModule(path)
        assertTrue(error, module.zygiskFolder.mkdirs())
        for ((abi, elf) in elfFixtures(machineOverride = 0)) {
            module.zygiskFolder.getChildFile(abi).newOutputStream().use {
                it.write(elf)
            }
        }
        assertTrue(error, Shell.cmd("set_default_perm $path").exec().isSuccess)
    }

    private fun setupOverflowZygiskModule(root: ExtendedFile) {
        val error = "$OVERFLOW_ZYGISK setup failed"
        val path = root.getChildFile(OVERFLOW_ZYGISK)
        val module = LocalModule(path)
        assertTrue(error, module.zygiskFolder.mkdirs())
        for ((abi, elf) in elfFixtures(addressOverflow = true)) {
            module.zygiskFolder.getChildFile(abi).newOutputStream().use {
                it.write(elf)
            }
        }
        assertTrue(error, Shell.cmd("set_default_perm $path").exec().isSuccess)
    }

    private fun setupZeroRangeZygiskModule(root: ExtendedFile) {
        val error = "$ZERO_RANGE_ZYGISK setup failed"
        val path = root.getChildFile(ZERO_RANGE_ZYGISK)
        val module = LocalModule(path)
        assertTrue(error, module.zygiskFolder.mkdirs())
        for ((abi, elf) in elfFixtures(zeroRange = true)) {
            module.zygiskFolder.getChildFile(abi).newOutputStream().use {
                it.write(elf)
            }
        }
        assertTrue(error, Shell.cmd("set_default_perm $path").exec().isSuccess)
    }

    private fun setupSpecialZygiskModule(root: ExtendedFile) {
        val error = "$SPECIAL_ZYGISK setup failed"
        val path = root.getChildFile(SPECIAL_ZYGISK)
        val module = LocalModule(path)
        assertTrue(error, module.zygiskFolder.mkdirs())
        for ((abi, _) in elfFixtures()) {
            val library = module.zygiskFolder.getChildFile(abi)
            assertTrue(error, Shell.cmd("mkfifo $library").exec().isSuccess)
        }
        assertTrue(error, Shell.cmd("set_default_perm $path").exec().isSuccess)
    }

    private fun setupValidZygiskModule(root: ExtendedFile) {
        setupZygiskTestModule(root, VALID_ZYGISK, "libzygisk_test.so")
    }

    private fun setupUnloadZygiskModule(root: ExtendedFile) {
        setupZygiskTestModule(root, UNLOAD_ZYGISK, "libzygisk_unload_test.so")
    }

    private fun setupZygiskTestModule(root: ExtendedFile, id: String, sourceName: String) {
        val error = "$id setup failed"
        val path = root.getChildFile(id)
        val module = LocalModule(path)
        assertTrue(error, module.zygiskFolder.mkdirs())

        val source = File(testContext.applicationInfo.nativeLibraryDir, sourceName)
        assertTrue(error, source.isFile)
        val library = module.zygiskFolder.getChildFile("${Build.SUPPORTED_ABIS.first()}.so")
        source.inputStream().use { input ->
            library.newOutputStream().use { output -> input.copyTo(output) }
        }
        assertTrue(error, Shell.cmd("set_default_perm $path").exec().isSuccess)
    }

    private fun elfFixtures(
        truncated: Boolean = false,
        machineOverride: Int? = null,
        zeroRange: Boolean = false,
        addressOverflow: Boolean = false,
    ): Array<Pair<String, ByteArray>> = arrayOf(
        "armeabi-v7a.so" to elfEnvelope(
            1, machineOverride ?: 40, truncated, zeroRange, addressOverflow
        ),
        "arm64-v8a.so" to elfEnvelope(
            2, machineOverride ?: 183, truncated, zeroRange, addressOverflow
        ),
        "x86.so" to elfEnvelope(
            1, machineOverride ?: 3, truncated, zeroRange, addressOverflow
        ),
        "x86_64.so" to elfEnvelope(
            2, machineOverride ?: 62, truncated, zeroRange, addressOverflow
        ),
        "riscv64.so" to elfEnvelope(
            2, machineOverride ?: 243, truncated, zeroRange, addressOverflow
        ),
    )

    private fun elfEnvelope(
        elfClass: Int,
        machine: Int,
        truncated: Boolean,
        zeroRange: Boolean,
        addressOverflow: Boolean,
    ): ByteArray {
        val headerSize = if (elfClass == 1) 52 else 64
        val programHeaderSize = if (elfClass == 1) 32 else 56
        val programHeaderCount = if (zeroRange) 2 else 1
        val segmentOffset = headerSize + programHeaderSize * programHeaderCount
        val size = segmentOffset + if (truncated) 0 else 2
        val elf = ByteBuffer.allocate(size).order(ByteOrder.LITTLE_ENDIAN)
        elf.put(0, 0x7f)
        elf.put(1, 0x45)
        elf.put(2, 0x4c)
        elf.put(3, 0x46)
        elf.put(4, elfClass.toByte())
        elf.put(5, 1)
        elf.put(6, 1)
        elf.putShort(16, 3)
        elf.putShort(18, machine.toShort())
        elf.putInt(20, 1)
        if (elfClass == 1) {
            elf.putInt(28, headerSize)
            elf.putShort(40, headerSize.toShort())
            elf.putShort(42, programHeaderSize.toShort())
            elf.putShort(44, programHeaderCount.toShort())
            elf.putInt(headerSize, 1)
            elf.putInt(headerSize + 4, segmentOffset)
            if (addressOverflow) elf.putInt(headerSize + 8, -1)
            elf.putInt(headerSize + 16, 1)
            elf.putInt(headerSize + 20, if (addressOverflow) 2 else 1)
            if (zeroRange) {
                val second = headerSize + programHeaderSize
                elf.putInt(second, 1)
                elf.putInt(second + 4, size + 1)
            }
        } else {
            elf.putLong(32, headerSize.toLong())
            elf.putShort(52, headerSize.toShort())
            elf.putShort(54, programHeaderSize.toShort())
            elf.putShort(56, programHeaderCount.toShort())
            elf.putInt(headerSize, 1)
            elf.putLong(headerSize + 8, segmentOffset.toLong())
            if (addressOverflow) elf.putLong(headerSize + 16, -1)
            elf.putLong(headerSize + 32, 1)
            elf.putLong(headerSize + 40, if (addressOverflow) 2 else 1)
            if (zeroRange) {
                val second = headerSize + programHeaderSize
                elf.putInt(second, 1)
                elf.putLong(second + 8, size.toLong() + 1)
            }
        }
        return elf.array()
    }

    private fun setupRemoveModule(root: ExtendedFile) {
        val error = "$REMOVE_TEST setup failed"
        val path = root.getChildFile(REMOVE_TEST)

        // Create a new module but mark is as "remove"
        val module = LocalModule(path)
        assertTrue(error, path.mkdirs())
        // Create uninstaller script
        path.getChildFile("uninstall.sh").newOutputStream().writer().use {
            it.write("touch $REMOVE_TEST_MARKER")
        }
        assertTrue(error, path.getChildFile("service.sh").createNewFile())
        module.remove = true

        assertTrue(error, Shell.cmd("set_default_perm $path").exec().isSuccess)
    }

    private fun setupUpgradeModule(root: ExtendedFile, update: ExtendedFile) {
        val error = "$UPGRADE_TEST setup failed"
        val oldPath = root.getChildFile(UPGRADE_TEST)
        val newPath = update.getChildFile(UPGRADE_TEST)

        // Create an existing module but mark as "disable
        val module = LocalModule(oldPath)
        assertTrue(error, oldPath.mkdirs())
        module.enable = false
        // Install service.sh into the old module
        assertTrue(error, oldPath.getChildFile("service.sh").createNewFile())

        // Create an upgrade module
        assertTrue(error, newPath.mkdirs())
        // Install post-fs-data.sh into the new module
        assertTrue(error, newPath.getChildFile("post-fs-data.sh").createNewFile())

        assertTrue(error, Shell.cmd(
            "set_default_perm $oldPath",
            "set_default_perm $newPath",
        ).exec().isSuccess)
    }

    @Test
    fun recoverSystemMode() {
        assertTrue("System Mode recovery failed", runBlocking {
            MagiskInstaller.SystemModeRecovery(TimberLog, TimberLog).exec()
        })
    }

    @Test
    fun setupEnvironment() {
        runBlocking {
            assertTrue(
                "Magisk setup failed",
                MagiskInstaller.Emulator(TimberLog, TimberLog).exec()
            )
        }

        val notify = object : DownloadNotifier {
            override val context = appContext
            override fun notifyUpdate(id: Int, editor: (Notification.Builder) -> Unit) {}
        }
        val processor = DownloadProcessor(notify)

        val shamiko = appContext.cachedFile("shamiko.zip")
        runBlocking {
            testContext.assets.open("shamiko.zip").use {
                processor.handleModule(it, shamiko.toUri())
            }
            checkModuleZip(shamiko)
            if (shamiko()) {
                assertTrue(
                    "Shamiko installation failed",
                    FlashZip(shamiko.toUri(), TimberLog, TimberLog).exec()
                )
            }
        }

        val lsp = appContext.cachedFile("lsposed.zip")
        runBlocking {
            testContext.assets.open("lsposed.zip").use {
                processor.handleModule(it, lsp.toUri())
            }
            checkModuleZip(lsp)
            if (lsposed()) {
                assertTrue(
                    "LSPosed installation failed",
                    FlashZip(lsp.toUri(), TimberLog, TimberLog).exec()
                )
            }
        }

        val root = RootUtils.fs.getFile(Const.MODULE_PATH)
        val update = RootUtils.fs.getFile(MODULE_UPDATE_PATH)
        if (mount()) { setupMountTest(update) }
        if (preinit()) { setupSepolicyRuleModule(update) }
        setupSystemlessHost()
        setupEmptyZygiskModule(update)
        setupInvalidZygiskModule(update)
        setupWrongAbiZygiskModule(update)
        setupOverflowZygiskModule(update)
        setupZeroRangeZygiskModule(update)
        setupSpecialZygiskModule(update)
        setupValidZygiskModule(update)
        setupUnloadZygiskModule(update)
        setupRemoveModule(root)
        setupUpgradeModule(root, update)
    }

    @Test
    fun setupAppHide() {
        runBlocking {
            assertTrue(
                "App hiding failed",
                AppMigration.patchAndHide(
                    context = appContext,
                    label = "Settings",
                    pkg = "repackaged.$APP_PACKAGE_NAME"
                )
            )
        }
    }

    @Test
    fun setupAppRestore() {
        runBlocking {
            assertTrue(
                "App restoration failed",
                AppMigration.restoreApp(appContext)
            )
        }
    }
}
