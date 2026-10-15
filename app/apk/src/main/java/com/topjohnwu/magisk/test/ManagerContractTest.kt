package com.topjohnwu.magisk.test

import android.content.Intent
import android.os.ParcelFileDescriptor.AutoCloseInputStream
import androidx.annotation.Keep
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.uiautomator.By
import androidx.test.uiautomator.Until
import com.topjohnwu.magisk.core.Config
import com.topjohnwu.magisk.core.Const
import com.topjohnwu.magisk.core.cmp
import com.topjohnwu.magisk.core.di.ServiceLocator
import com.topjohnwu.magisk.core.model.HideFailure
import com.topjohnwu.magisk.core.model.HidePolicy
import com.topjohnwu.magisk.core.model.HideStatus
import com.topjohnwu.magisk.core.model.ManagerDiagnostics
import com.topjohnwu.magisk.core.model.su.SuPolicy
import com.topjohnwu.magisk.core.su.SuRequestHandler
import com.topjohnwu.magisk.ui.MainActivity
import com.topjohnwu.superuser.Shell
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.BeforeClass
import org.junit.Test
import org.junit.runner.RunWith
import com.topjohnwu.magisk.core.R as CoreR

@Keep
@RunWith(AndroidJUnit4::class)
class ManagerContractTest : BaseTest {
    companion object {
        @BeforeClass @JvmStatic fun before() = BaseTest.prerequisite()
    }

    @Test fun testHideSelectionAndModeMismatch() = runBlocking {
        val status = HidePolicy.status()
        val pkg = "io.github.kitsune.pr13.fixture"
        val changes = listOf(Triple(pkg, "$pkg:worker", true))
        try {
            HidePolicy.edit(status.mode, changes)
            assertTrue(HidePolicy.selection().second.contains("$pkg|$pkg:worker"))
            HidePolicy.edit(status.mode, changes) // Idempotence
            try {
                HidePolicy.edit(if (status.isSuList) "hide" else "sulist", changes)
                fail("A stale UI cannot write to another mode")
            } catch (e: HideFailure) { assertEquals(8, e.code) }
            try {
                HidePolicy.edit(status.mode, listOf(Triple("bad'; echo INJECTED", "bad", true)))
                fail("Invalid package accepted")
            } catch (e: HideFailure) { assertEquals(5, e.code) }
        } finally {
            HidePolicy.edit(status.mode, listOf(Triple(pkg, "$pkg:worker", false)))
        }
    }

    @Test fun testStatusParsingAndRedaction() = runBlocking {
        assertEquals("UNKNOWN", ManagerDiagnostics.parseTransaction(listOf("SCHEMA_VERSION=1", "STATE=BOOT_VERIFIED", "STATE=STAGED")))
        assertEquals("UNKNOWN", ManagerDiagnostics.parseTransaction(listOf("SCHEMA_VERSION=2", "STATE=BOOT_VERIFIED")))
        assertEquals("BOOT_VERIFIED", ManagerDiagnostics.parseTransaction(listOf("SCHEMA_VERSION=1", "STATE=BOOT_VERIFIED")))
        for (verdict in listOf("supported", "blocked", "needs_evidence", "error")) {
            val report = JSONObject().put("schema_version", 1).put("assessment",
                JSONObject().put("verdict", verdict).put("primary_reason", "INIT_IMPORT_UNPROVEN"))
            assertEquals("$verdict: INIT_IMPORT_UNPROVEN", ManagerDiagnostics.parseDoctor(report.toString()))
        }
        assertTrue(runCatching { HideStatus.parse("{\"schema\":2}") }.isFailure)
        val report = ManagerDiagnostics.collect()
        val allowed = setOf("schema", "manager", "android_api", "system_mode", "transaction_state",
            "last_hide_result", "last_system_operation", "last_system_success", "daemon", "zygisk", "hide")
        assertEquals(allowed, report.keys().asSequence().toSet())
        assertFalse(report.toString().contains("android_id"))
        assertFalse(report.toString().contains("serialno"))
    }

    @Test fun testForgedAndObsoleteRequestsCannotPersistPolicy() = runBlocking {
        val before = ServiceLocator.policyDB.fetch(2000)?.toMap()
        val handler = SuRequestHandler(appContext.packageManager, ServiceLocator.policyDB)
        try {
            assertFalse(handler.start(Intent().putExtra("uid", 2000).putExtra("pid", 1)
                .putExtra("fifo", appContext.filesDir.resolve("forged-request").path)))
            assertFalse(handler.start(Intent().putExtra("socket", "/data/local/tmp/forged")))
            assertEquals(before, ServiceLocator.policyDB.fetch(2000)?.toMap())
        } finally { handler.close() }
    }

    @Test fun testWarmNavigationAndInstallDestination() {
        val component = MainActivity::class.java.cmp(appContext.packageName).flattenToString()
        fun launch(extra: String) {
            AutoCloseInputStream(uiAutomation.executeShellCommand(
                "am start -W -n $component -a android.intent.action.MAIN -c android.intent.category.LAUNCHER $extra"
            )).reader().use { it.readText() }
        }
        try {
            launch("--es ${Const.Key.OPEN_SECTION} ${Const.Nav.SETTINGS}")
            assertTrue(device.wait(Until.hasObject(By.text(appContext.getString(CoreR.string.settings_customization))), 30000))
            launch("--es ${Const.Key.OPEN_SECTION} ${Const.Nav.INSTALL}")
            assertTrue(device.wait(Until.hasObject(By.text(appContext.getString(CoreR.string.install_method_title))), 10000))
            device.pressBack()
            launch("--es ${Const.Key.OPEN_SECTION} ${Const.Nav.SETTINGS}")
            assertTrue(device.wait(Until.hasObject(By.text(appContext.getString(CoreR.string.settings_customization))), 10000))
        } finally { device.pressHome() }
    }

    @Test fun testSuTimeoutAndAuthenticationCannotAutoGrant() = runBlocking {
        val policy = ServiceLocator.policyDB.fetch(2000)
        val auto = Config.suAutoResponse
        val timeout = Config.suDefaultTimeout
        val auth = Config.suAuth
        val tapjack = Config.suTapjack
        try {
            Config.suTapjack = false
            Config.suDefaultTimeout = 3
            Config.suAuth = true
            Config.suAutoResponse = Config.Value.SU_AUTO_ALLOW
            ServiceLocator.policyDB.update(SuPolicy(2000, logging = false, notification = false, remain = 0))
            val binary = Shell.cmd("magisk --path").exec().out.single() + "/su"
            val caller = AutoCloseInputStream(uiAutomation.executeShellCommand("id -u")).reader().use { it.readText().trim() }
            val command = if (caller == "0") "$binary -s $binary 2000 -c id" else "$binary -c id"
            AutoCloseInputStream(uiAutomation.executeShellCommand(command)).reader().use {
                assertTrue("Authenticated auto-allow must show consent", device.wait(Until.hasObject(By.text(appContext.getString(CoreR.string.su_warning))), 30000))
                assertFalse("Timed-out request granted root", it.readText().contains("uid=0"))
            }
            assertEquals("Timeout cannot create a remembered grant", SuPolicy.QUERY, ServiceLocator.policyDB.fetch(2000)?.policy)
        } finally {
            Config.suAutoResponse = auto
            Config.suDefaultTimeout = timeout
            Config.suAuth = auth
            Config.suTapjack = tapjack
            if (policy == null) ServiceLocator.policyDB.delete(2000) else ServiceLocator.policyDB.update(policy)
            device.pressHome()
        }
    }
}
