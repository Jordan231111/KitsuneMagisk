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
import kotlinx.coroutines.flow.first
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
            assertTrue(device.wait(Until.hasObject(By.text(appContext.getString(CoreR.string.select_patch_file))), 10000))
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

    private fun shellRequest(): java.io.InputStream {
        val binary = Shell.cmd("magisk --path").exec().out.single() + "/su"
        val caller = AutoCloseInputStream(uiAutomation.executeShellCommand("id -u"))
            .reader().use { it.readText().trim() }
        val command = if (caller == "0") "$binary -s $binary 2000 -c id" else "$binary -c id"
        return AutoCloseInputStream(uiAutomation.executeShellCommand(command))
    }

    private fun withConsentPolicy(block: () -> Unit) {
        val db = ServiceLocator.policyDB
        val settings = ServiceLocator.settingsDB
        val policy = runBlocking { db.fetch(2000) }
        val oldAuth = runBlocking { settings.fetch(Config.Key.SU_BIOMETRIC, -1) }
        val oldAccess = runBlocking { settings.fetch(Config.Key.ROOT_ACCESS, -1) }
        val auto = Config.suAutoResponse
        val timeout = Config.suDefaultTimeout
        val tapjack = Config.suTapjack
        try {
            Config.suAutoResponse = Config.Value.SU_PROMPT
            Config.suDefaultTimeout = 15
            Config.suTapjack = false
            Config.suAuth = false
            runBlocking { db.update(SuPolicy(2000, logging = false, notification = false, remain = 0)) }
            block()
        } finally {
            runBlocking {
                if (oldAuth == -1) settings.delete(Config.Key.SU_BIOMETRIC) else settings.put(Config.Key.SU_BIOMETRIC, oldAuth)
                if (oldAccess == -1) settings.delete(Config.Key.ROOT_ACCESS) else settings.put(Config.Key.ROOT_ACCESS, oldAccess)
                if (policy == null) db.delete(2000) else db.update(policy)
            }
            Config.suAutoResponse = auto
            Config.suDefaultTimeout = timeout
            Config.suTapjack = tapjack
            device.pressHome()
        }
    }

    private fun grantVisibleRequest() {
        assertTrue("Consent must be visible", device.wait(Until.hasObject(By.text(appContext.getString(CoreR.string.su_warning))), 15000))
        // The one-second consent delay is intentional tapjacking protection.
        android.os.SystemClock.sleep(1200)
        val grant = device.findObject(By.text(appContext.getString(CoreR.string.grant)))
        assertNotNull("Grant control must be accessible", grant)
        grant.click()
    }

    @Test fun testOnceGrantReplayAndImmediateRevocation() = withConsentPolicy {
        val prefs = ServiceLocator.timeoutPrefs
        val key = "com.android.shell"
        val old = prefs.getInt(key, -1)
        val monitor = instrumentation.addMonitor(android.content.IntentFilter(Intent.ACTION_VIEW).apply {
            addCategory(Intent.CATEGORY_DEFAULT)
        }, null, false)
        try {
            prefs.edit().putInt(key, 1).commit()
            lateinit var original: Intent
            shellRequest().reader().use { response ->
                val activity = monitor.waitForActivityWithTimeout(30000)
                assertNotNull(activity)
                original = Intent(activity.intent)
                grantVisibleRequest()
                assertTrue(response.readText().contains("uid=0"))
            }
            runBlocking {
                assertEquals(SuPolicy.QUERY, ServiceLocator.policyDB.fetch(2000)?.policy)
                val replay = SuRequestHandler(appContext.packageManager, ServiceLocator.policyDB)
                try { assertFalse(replay.start(original)) } finally { replay.close() }
            }
            Config.suAutoResponse = Config.Value.SU_AUTO_DENY
            shellRequest().reader().use { assertFalse("Once must not be cached as allow", it.readText().contains("uid=0")) }
            runBlocking { assertEquals(SuPolicy.DENY, ServiceLocator.policyDB.fetch(2000)?.policy) }
        } finally {
            instrumentation.removeMonitor(monitor)
            prefs.edit().apply { if (old == -1) remove(key) else putInt(key, old) }.commit()
        }
    }

    @Test fun testUnavailableCredentialRetainsLegacyRequirement() = withConsentPolicy {
        org.junit.Assume.assumeFalse(com.topjohnwu.magisk.core.Info.isDeviceSecure)
        runBlocking { ServiceLocator.settingsDB.put(Config.Key.SU_BIOMETRIC, 2) }
        assertTrue("Legacy biometric mode must remain enabled without enrollment", Config.suAuth)
        shellRequest().reader().use {
            grantVisibleRequest()
            assertFalse(it.readText().contains("uid=0"))
        }
        runBlocking { assertEquals(SuPolicy.QUERY, ServiceLocator.policyDB.fetch(2000)?.policy) }
    }

    @Test fun testSavedGrantRevocationInvalidatesCache() = withConsentPolicy {
        repeat(5) {
            runBlocking {
                ServiceLocator.policyDB.update(SuPolicy(2000, SuPolicy.ALLOW, 0,
                    logging = false, notification = false))
            }
            shellRequest().reader().use { assertTrue(it.readText().contains("uid=0")) }
            runBlocking {
                ServiceLocator.policyDB.update(SuPolicy(2000, SuPolicy.DENY, 0,
                    logging = false, notification = false))
            }
            shellRequest().reader().use {
                assertFalse("A revoked grant must not survive in the cache", it.readText().contains("uid=0"))
            }
        }
    }

    @Test fun testRepeatedConsentLaunchesReachManager() = withConsentPolicy {
        Config.suAutoResponse = Config.Value.SU_AUTO_DENY
        repeat(10) {
            runBlocking {
                ServiceLocator.policyDB.update(SuPolicy(2000, logging = false,
                    notification = false, remain = 0))
            }
            shellRequest().reader().use { assertFalse(it.readText().contains("uid=0")) }
            runBlocking {
                assertEquals("The manager must receive and answer each request",
                    SuPolicy.DENY, ServiceLocator.policyDB.fetch(2000)?.policy)
            }
        }
    }

    @Test fun testAccessChangeRejectsPendingGrant() = withConsentPolicy {
        shellRequest().reader().use {
            assertTrue(device.wait(Until.hasObject(By.text(appContext.getString(CoreR.string.su_warning))), 15000))
            runBlocking { ServiceLocator.settingsDB.put(Config.Key.ROOT_ACCESS, Config.Value.ROOT_ACCESS_DISABLED) }
            grantVisibleRequest()
            assertFalse(it.readText().contains("uid=0"))
        }
        runBlocking { assertEquals(SuPolicy.QUERY, ServiceLocator.policyDB.fetch(2000)?.policy) }
    }


    @Test fun testRejectedWritesDoNotReportSavedPolicyOrSettings() = runBlocking {
        val db = ServiceLocator.policyDB
        val settings = ServiceLocator.settingsDB
        val uid = 99899
        val key = "kitsune_pr13_probe"
        org.junit.Assume.assumeTrue(db.fetch(uid) == null && settings.fetch(key, -1) == -1)
        val scope = kotlinx.coroutines.CoroutineScope(kotlinx.coroutines.Dispatchers.IO + kotlinx.coroutines.SupervisorJob())
        val config = object : com.topjohnwu.magisk.core.repository.DBConfig {
            override val settingsDB = settings
            override val stringDB = ServiceLocator.stringDB
            override val coroutineScope = scope
            var probe by dbSettings(key, false)
        }
        try {
            db.update(SuPolicy(uid, SuPolicy.DENY, 0))
            settings.put(key, 0)
            db.exec("CREATE TRIGGER kitsune_pr13_policy_write BEFORE INSERT ON policies WHEN NEW.uid=$uid BEGIN SELECT RAISE(FAIL, \"probe\"); END")
            try { db.update(SuPolicy(uid, SuPolicy.ALLOW, 0)); fail("Rejected grant reported success") }
            catch (_: java.io.IOException) {}
            assertEquals(SuPolicy.DENY, db.fetch(uid)?.policy)
            db.exec("CREATE TRIGGER kitsune_pr13_policy_delete BEFORE DELETE ON policies WHEN OLD.uid=$uid BEGIN SELECT RAISE(FAIL, \"probe\"); END")
            try { db.delete(uid); fail("Rejected revoke reported success") }
            catch (_: java.io.IOException) {}
            assertNotNull(db.fetch(uid))
            db.exec("CREATE TRIGGER kitsune_pr13_setting_write BEFORE INSERT ON settings WHEN NEW.key=\"$key\" BEGIN SELECT RAISE(FAIL, \"probe\"); END")
            assertFalse(config.probe)
            val revision = com.topjohnwu.magisk.core.repository.DBConfig.revision
            val before = revision.value
            config.probe = true
            kotlinx.coroutines.withTimeout(5000) { revision.first { it != before } }
            assertFalse("Failed writes must not change the cached value", config.probe)
            assertEquals(0, settings.fetch(key))
        } finally {
            scope.coroutineContext[kotlinx.coroutines.Job]?.cancel()
            db.exec("DROP TRIGGER IF EXISTS kitsune_pr13_policy_write; DROP TRIGGER IF EXISTS kitsune_pr13_policy_delete; DROP TRIGGER IF EXISTS kitsune_pr13_setting_write;")
            db.delete(uid)
            settings.delete(key)
        }
    }

}
