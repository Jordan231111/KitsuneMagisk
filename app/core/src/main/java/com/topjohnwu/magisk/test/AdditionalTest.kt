package com.topjohnwu.magisk.test

import android.os.Build
import android.os.ParcelFileDescriptor.AutoCloseInputStream
import androidx.annotation.Keep
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.uiautomator.By
import androidx.test.uiautomator.Until
import com.topjohnwu.magisk.core.model.module.LocalModule
import com.topjohnwu.magisk.core.model.module.OnlineModule
import com.topjohnwu.magisk.core.utils.RootUtils
import com.topjohnwu.magisk.test.Environment.Companion.EMPTY_ZYGISK
import com.topjohnwu.magisk.test.Environment.Companion.INVALID_ZYGISK
import com.topjohnwu.magisk.test.Environment.Companion.MOUNT_TEST
import com.topjohnwu.magisk.test.Environment.Companion.OVERFLOW_ZYGISK
import com.topjohnwu.magisk.test.Environment.Companion.REMOVE_TEST
import com.topjohnwu.magisk.test.Environment.Companion.SEPOLICY_RULE
import com.topjohnwu.magisk.test.Environment.Companion.SPECIAL_ZYGISK
import com.topjohnwu.magisk.test.Environment.Companion.UNLOAD_ZYGISK
import com.topjohnwu.magisk.test.Environment.Companion.UPGRADE_TEST
import com.topjohnwu.magisk.test.Environment.Companion.VALID_ZYGISK
import com.topjohnwu.magisk.test.Environment.Companion.WRONG_ABI_ZYGISK
import com.topjohnwu.magisk.test.Environment.Companion.ZERO_RANGE_ZYGISK
import com.topjohnwu.superuser.Shell
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.BeforeClass
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.TimeUnit
import java.util.regex.Pattern

@Keep
@RunWith(AndroidJUnit4::class)
class AdditionalTest : BaseTest {

    companion object {
        private const val SHELL_PKG = "com.android.shell"
        private const val LSPOSED_CATEGORY = "org.lsposed.manager.LAUNCH_MANAGER"
        private const val LSPOSED_PKG = "org.lsposed.manager"

        private lateinit var modules: List<LocalModule>

        @BeforeClass
        @JvmStatic
        fun before() {
            BaseTest.prerequisite()
            runBlocking {
                modules = LocalModule.installed()
            }
        }
    }

    @After
    fun teardown() {
        device.pressHome()
    }

    @Test
    fun testPrimaryGroupOverridesSupplementaryOrder() {
        val result = Shell.cmd("su -G 3002 -g 3001 -c " +
            "'/data/adb/magisk/busybox id -g; /data/adb/magisk/busybox id -G'").exec()
        assertTrue("Explicit group request failed", result.isSuccess)
        assertEquals("Primary group must follow -g", "3001", result.out.firstOrNull())
        assertEquals(setOf("3001", "3002"),
            result.out.getOrNull(1)?.substringBefore(" context=")?.split(' ')?.toSet())
    }

    @Test
    fun testRepeatedPrimaryGroupsSurviveVectorGrowth() {
        val options = (3001..3016).joinToString(" ") { "-g $it" }
        val result = Shell.cmd("su -G 3000 $options -c " +
            "'/data/adb/magisk/busybox id -g; /data/adb/magisk/busybox id -G'").exec()
        assertTrue("Repeated group request failed", result.isSuccess)
        assertEquals("3016", result.out.firstOrNull())
        assertEquals((3000..3016).map { it.toString() }.toSet(),
            result.out.getOrNull(1)?.substringBefore(" context=")?.split(' ')?.toSet())
    }

    @Test
    fun testModuleFilenamePreservesLettersAndRejectsControls() {
        val module = OnlineModule("fixture", "ViPER ABC", "v1.2", 1, "", "")
        assertEquals("ViPER_ABC-v1.2(1).zip", module.downloadFilename)
        for (invalid in listOf('\u0000', '\n', '\r', '\u001F', '\u007F', ':', '<', '>', '?', '|')) {
            assertEquals("A_Z-v1.2(1).zip", module.copy(name = "A${invalid}Z").downloadFilename)
        }
    }

    @Test
    fun testModuleCount() {
        var expected = 11
        if (Environment.mount()) expected++
        if (Environment.preinit()) expected++
        if (Environment.lsposed()) expected++
        if (Environment.shamiko()) expected++
        assertEquals("Module count incorrect", expected, modules.size)
    }

    @Test
    fun testLsposed() {
        assumeTrue(Environment.lsposed())

        val module = modules.find { it.id == "zygisk_lsposed" }
        assertNotNull("zygisk_lsposed is not installed", module)
        module!!
        assertFalse("zygisk_lsposed is not enabled", module.zygiskUnloaded)

        // Launch lsposed manager to ensure the module is active
        uiAutomation.executeShellCommand(
            "am start -c $LSPOSED_CATEGORY $SHELL_PKG/.BugreportWarningActivity"
        ).let { pfd -> AutoCloseInputStream(pfd).use { it.readBytes() } }

        val pattern = Pattern.compile("$LSPOSED_PKG:id/.*")
        assertNotNull(
            "LSPosed manager launch failed",
            device.wait(Until.hasObject(By.res(pattern)), TimeUnit.SECONDS.toMillis(10))
        )
    }

    @Test
    fun testModuleMount() {
        assumeTrue(Environment.mount())

        assertNotNull("$MOUNT_TEST is not installed", modules.find { it.id == MOUNT_TEST })
        assertTrue(
            "/system/fonts/newfile should exist",
            RootUtils.fs.getFile("/system/fonts/newfile").exists()
        )
        assertFalse(
            "/system/bin/screenrecord should not exist",
            RootUtils.fs.getFile("/system/bin/screenrecord").exists()
        )
        val egg = RootUtils.fs.getFile("/system/app/EasterEgg").list() ?: arrayOf()
        assertArrayEquals(
            "/system/app/EasterEgg should be replaced",
            egg,
            arrayOf("newfile")
        )
    }

    @Test
    fun testRootModuleApiAndBootCompleted() {
        assumeTrue(Environment.mount())
        assertEquals("root-api-v2", Shell.cmd("cat /system/etc/kitsune-root-v2").exec().out.joinToString(""))
        assertEquals("kitsune-root-v2", Shell.cmd("readlink /system/etc/kitsune-root-link").exec().out.joinToString(""))
        assertEquals("completed", Shell.cmd("cat /dev/.kitsune-boot-completed").exec().out.joinToString(""))
    }

    @Test
    fun testPendingModuleEnableAfterSafeMode() {
        assumeTrue(Environment.mount())
        val module = modules.first { it.id == MOUNT_TEST }
        val pending = RootUtils.fs.getFile("/data/adb/modules_update/$MOUNT_TEST")
        assertFalse(pending.exists())
        try {
            assertTrue(pending.mkdirs())
            pending.getChildFile("module.prop").newOutputStream().use {
                it.write("id=$MOUNT_TEST\nname=Pending state test\nversion=1\nversionCode=1\n".toByteArray())
            }
            // Reproduce an update left disabled by safe mode before activation.
            assertTrue(pending.getChildFile("disable").createNewFile())
            assertTrue(module.updateEnabled(false))
            assertTrue(module.updateEnabled(true))
            assertTrue(module.enable)
            assertFalse(pending.getChildFile("disable").exists())
            module.enable = false
            assertTrue(pending.getChildFile("disable").exists())
            assertFalse(module.base.getChildFile("update").exists())
            assertTrue(module.updated)
            assertFalse(module.updateRemoved(true))
            module.remove = true
            assertFalse(module.remove)
        } finally {
            assertTrue(Shell.cmd("rm -rf $pending").exec().isSuccess)
            assertTrue(module.updateEnabled(true))
        }
    }

    @Test
    fun testNestedManifestPayloadIsHashed() {
        assumeTrue(Environment.preinit() && Build.VERSION.SDK_INT >= 30)
        val module = modules.first { it.id == SEPOLICY_RULE }
        val file = module.base.getChildFile("early-mount/system/etc/manifest")
        try {
            file.newOutputStream().use { it.write("hash-me\n".toByteArray()) }
            assertTrue(Shell.cmd("copy_preinit_files").exec().isSuccess)
            assertTrue(Shell.cmd(
                "grep -qF /early-mount/system/etc/manifest \"\$MAGISKTMP/.magisk/preinit/kitsune-v2/current/manifest\""
            ).exec().isSuccess)
        } finally {
            file.delete()
            assertTrue(Shell.cmd("copy_preinit_files").exec().isSuccess)
        }
    }

    @Test
    fun testPreinitPublicationFailure() {
        assumeTrue(Environment.preinit() && Build.VERSION.SDK_INT >= 30)
        val module = modules.first { it.id == SEPOLICY_RULE }
        try {
            assertTrue(module.updateEnabled(false))
            assertTrue(Shell.cmd("test ! -e \"\$MAGISKTMP/.magisk/preinit/kitsune-v2/current/modules/$SEPOLICY_RULE\"").exec().isSuccess)
            assertTrue(module.updateEnabled(true))
            val old = Shell.cmd("readlink \"\$MAGISKTMP/.magisk/preinit/kitsune-v2/current\"").exec().out
            assertTrue(Shell.cmd("(cp() { return 1; }; ! copy_preinit_files)").exec().isSuccess)
            assertEquals(old, Shell.cmd("readlink \"\$MAGISKTMP/.magisk/preinit/kitsune-v2/current\"").exec().out)
            assertTrue(Shell.cmd("test -f \"\$MAGISKTMP/.magisk/preinit/kitsune-v2/disabled\"").exec().isSuccess)
            assertTrue(Shell.cmd(
                "(df() { printf 'Filesystem 1024-blocks Used Available Capacity Mounted\\nfixture 16384 16380 4 100%% /metadata\\n'; }; ! copy_preinit_files)"
            ).exec().isSuccess)
            assertEquals(old, Shell.cmd("readlink \"\$MAGISKTMP/.magisk/preinit/kitsune-v2/current\"").exec().out)
            val sparse = module.base.getChildFile("early-mount/system/etc/space-probe")
            try {
                assertTrue(Shell.cmd("truncate -s 41943040 $sparse").exec().isSuccess)
                assertTrue(Shell.cmd("! copy_preinit_files").exec().isSuccess)
                assertEquals(old, Shell.cmd("readlink \"\$MAGISKTMP/.magisk/preinit/kitsune-v2/current\"").exec().out)
            } finally {
                sparse.delete()
            }
        } finally {
            assertTrue(module.updateEnabled(true))
        }
    }

    @Test
    fun testBootCompletedRunsOnce() {
        assumeTrue(Environment.mount())
        assertTrue(Shell.cmd("magisk --boot-complete", "magisk --boot-complete").exec().isSuccess)
        assertEquals(listOf("completed"), Shell.cmd("cat /dev/.kitsune-boot-completed").exec().out)
    }

    @Test
    fun testEarlyMountCleanup() {
        assumeTrue(Environment.preinit() && Build.VERSION.SDK_INT >= 30)
        val result = Shell.cmd("magisk --hide exec /system/bin/sh -c 'head -n1 /system/etc/hosts'").exec()
        assertTrue(result.isSuccess)
        assertFalse("A hidden early mount became visible after removing its overlay",
            result.out.any { it.contains("kitsune-early-v2") })
    }

    @Test
    fun testEarlyConsumer() {
        assumeTrue(Environment.preinit() && Build.VERSION.SDK_INT >= 30)
        assertEquals(listOf("# kitsune-early-v2", "early"), Shell.cmd("cat /dev/.kitsune-early").exec().out)
        assertTrue(Shell.cmd("test -f /dev/.kitsune-pfs").exec().isSuccess)
        assertEquals(listOf("600 0"), Shell.cmd("stat -c '%a %u' \"\$MAGISKTMP/.magisk/early-mount-api\"").exec().out)
        if (RootUtils.fs.getFile("/vendor/build.prop").isFile) {
            val target = Shell.cmd("readlink -f /vendor/build.prop").exec()
            assertTrue(target.isSuccess)
            val mounts = Shell.cmd("cat /proc/1/mountinfo").exec()
            assertTrue(mounts.isSuccess && mounts.out.any {
                it.contains(" ${target.out.single()} ") && it.contains(" - tmpfs magisk ")
            })
        }
        val expected = Shell.cmd("cat /data/adb/modules/$SEPOLICY_RULE/test_hosts_context").exec()
        val label = Shell.cmd("/system/bin/ls -Z \"\$MAGISKTMP/.magisk/early-mount/0\"").exec()
        assertTrue("Early context read failed: ${label.err}", expected.isSuccess && label.isSuccess)
        assertEquals("Early payload must retain the original system label", expected.out.single(),
            label.out.single().trim().substringBefore(' '))
        assertTrue("The reader must be an application UID", android.os.Process.myUid() >= 10000)
        assertTrue(java.io.File("/proc/self/attr/current").readText().startsWith("u:r:untrusted_app"))
        // The ordinary hosts module intentionally overrides the early file later.
        val hosts = modules.find { it.id == "hosts" && it.enable && !it.remove }
        val visible = hosts?.base?.getChildFile("system/etc/hosts")?.newInputStream()
            ?.bufferedReader()?.use { it.readLine() } ?: "# kitsune-early-v2"
        assertEquals(visible, java.io.File("/system/etc/hosts").bufferedReader().use { it.readLine() })
    }

    @Test
    fun testGlobalEarlyApplicationRead() {
        assumeTrue(Shell.cmd("test \"$(getprop sys.kitsune.pfsglobal)\" = reached").exec().isSuccess)
        assertTrue("The reader must be an application UID", android.os.Process.myUid() >= 10000)
        assertTrue(java.io.File("/proc/self/attr/current").readText().startsWith("u:r:untrusted_app"))
        assertEquals("# global-early-v2", java.io.File("/system/etc/hosts").bufferedReader().use { it.readLine() })
    }

    @Test
    fun testSepolicyRule() {
        assumeTrue(Environment.preinit())

        assertNotNull("$SEPOLICY_RULE is not installed", modules.find { it.id == SEPOLICY_RULE })
        assertTrue(
            "Module sepolicy.rule is not applied",
            Shell.cmd("magiskpolicy --print-rules | grep -q magisk_test").exec().isSuccess
        )
    }

    @Test
    fun testEmptyZygiskModule() {
        val module = modules.find { it.id == EMPTY_ZYGISK }
        assertNotNull("$EMPTY_ZYGISK is not installed", module)
        module!!
        assertTrue("$EMPTY_ZYGISK should be zygisk unloaded", module.zygiskUnloaded)
    }

    @Test
    fun testInvalidZygiskModule() {
        val module = modules.find { it.id == INVALID_ZYGISK }
        assertNotNull("$INVALID_ZYGISK is not installed", module)
        module!!
        assertTrue("$INVALID_ZYGISK should be zygisk unloaded", module.zygiskUnloaded)
    }

    @Test
    fun testWrongAbiZygiskModule() {
        val module = modules.find { it.id == WRONG_ABI_ZYGISK }
        assertNotNull("$WRONG_ABI_ZYGISK is not installed", module)
        module!!
        assertTrue("$WRONG_ABI_ZYGISK should be zygisk unloaded", module.zygiskUnloaded)
    }

    @Test
    fun testOverflowZygiskModule() {
        val module = modules.find { it.id == OVERFLOW_ZYGISK }
        assertNotNull("$OVERFLOW_ZYGISK is not installed", module)
        module!!
        assertTrue("$OVERFLOW_ZYGISK should be zygisk unloaded", module.zygiskUnloaded)
    }

    @Test
    fun testZeroRangeZygiskModule() {
        val module = modules.find { it.id == ZERO_RANGE_ZYGISK }
        assertNotNull("$ZERO_RANGE_ZYGISK is not installed", module)
        module!!
        assertTrue("$ZERO_RANGE_ZYGISK should be zygisk unloaded", module.zygiskUnloaded)
    }

    @Test
    fun testSpecialZygiskModule() {
        val module = modules.find { it.id == SPECIAL_ZYGISK }
        assertNotNull("$SPECIAL_ZYGISK is not installed", module)
        module!!
        assertTrue("$SPECIAL_ZYGISK should be zygisk unloaded", module.zygiskUnloaded)
    }

    @Test
    fun testValidZygiskModule() {
        val module = modules.find { it.id == VALID_ZYGISK }
        assertNotNull("$VALID_ZYGISK is not installed", module)
        module!!
        assertFalse("$VALID_ZYGISK should pass the Zygisk ELF gate", module.zygiskUnloaded)
    }

    @Test
    fun testUnloadZygiskModule() {
        val module = modules.find { it.id == UNLOAD_ZYGISK }
        assertNotNull("$UNLOAD_ZYGISK is not installed", module)
        module!!
        assertFalse("$UNLOAD_ZYGISK should load and unload cleanly", module.zygiskUnloaded)
    }

    @Test
    fun testRemoveModule() {
        assertNull("$REMOVE_TEST should be removed", modules.find { it.id == REMOVE_TEST })
        assertTrue(
            "Uninstaller of $REMOVE_TEST should be run",
            RootUtils.fs.getFile(Environment.REMOVE_TEST_MARKER).exists()
        )
    }

    @Test
    fun testModuleUpgrade() {
        val module = modules.find { it.id == UPGRADE_TEST }
        assertNotNull("$UPGRADE_TEST is not installed", module)
        module!!
        assertFalse("$UPGRADE_TEST should be disabled", module.enable)
        assertTrue(
            "$UPGRADE_TEST should be updated",
            module.base.getChildFile("post-fs-data.sh").exists()
        )
        assertFalse(
            "$UPGRADE_TEST should be updated",
            module.base.getChildFile("service.sh").exists()
        )
    }
}
