package com.topjohnwu.magisk.test

import android.content.Intent
import android.net.Uri
import android.os.ParcelFileDescriptor.AutoCloseInputStream
import androidx.annotation.Keep
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.uiautomator.By
import androidx.test.uiautomator.Until
import com.topjohnwu.magisk.core.Config
import com.topjohnwu.magisk.core.Const
import com.topjohnwu.magisk.core.Info
import com.topjohnwu.magisk.core.cmp
import com.topjohnwu.magisk.core.di.ServiceLocator
import com.topjohnwu.magisk.core.model.HideFailure
import com.topjohnwu.magisk.core.model.HidePolicy
import com.topjohnwu.magisk.core.model.HideStatus
import com.topjohnwu.magisk.core.model.ManagerDiagnostics
import com.topjohnwu.magisk.core.model.su.SuPolicy
import com.topjohnwu.magisk.core.su.SuRequestHandler
import com.topjohnwu.magisk.core.tasks.ManagerMigration
import com.topjohnwu.magisk.core.tasks.MagiskInstaller
import com.topjohnwu.magisk.core.utils.MediaStoreUtils
import com.topjohnwu.magisk.core.utils.RootUtils
import com.topjohnwu.magisk.ui.MainActivity
import com.topjohnwu.superuser.Shell
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.flow.first
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.BeforeClass
import org.junit.Test
import org.junit.Assume.assumeTrue
import org.junit.runner.RunWith
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.UUID
import com.topjohnwu.magisk.core.R as CoreR

@Keep
@RunWith(AndroidJUnit4::class)
class ManagerContractTest : BaseTest {
    companion object {
        @BeforeClass @JvmStatic fun before() = BaseTest.prerequisite()
    }

    @Test fun testSystemModePatchDoesNotQueueAnOrdinaryUpdate() = runBlocking {
        assumeTrue(Info.isSystemMode)
        val context = ServiceLocator.deContext
        val inbox = File(context.filesDir.parent, "install")
        assertFalse("A prior ordinary update is pending", inbox.exists())
        assertTrue(inbox.mkdir())
        val sentinel = File(inbox, "unrelated")
        sentinel.writeText("preserve this inbox")
        val input = File.createTempFile("patch-contract-", ".img", context.cacheDir)
        // A bounded boot-v4 fixture. It is patched as a file, never flashed.
        val image = ByteBuffer.allocate(8192).order(ByteOrder.LITTLE_ENDIAN)
        image.put("ANDROID!".toByteArray())
        image.putInt(16)
        image.putInt(0)
        image.putInt(0)
        image.putInt(1584)
        image.position(40)
        image.putInt(4)
        image.position(4096)
        image.put(ByteArray(16) { 0x42 })
        input.writeBytes(image.array())
        val previousDir = Config.downloadDir
        val directory = "kitsune-patch-test-${UUID.randomUUID()}"
        Config.downloadDir = directory
        val console = mutableListOf<String>()
        try {
            val success = MagiskInstaller.Patch(Uri.fromFile(input), console, console).exec()
            assertTrue(console.toString(), success)
            assertEquals("preserve this inbox", sentinel.readText())
            assertEquals(listOf("unrelated"), inbox.list()?.toList())
            assertFalse(File(context.filesDir.parent, Const.PATCH_INSTALL_DIR).exists())
            assertFalse(RootUtils.fs.getFile(Const.PATCH_TMPDIR).exists())
        } finally {
            Config.downloadDir = previousDir
            input.delete()
            inbox.deleteRecursively()
            RootUtils.fs.getFile(MediaStoreUtils.fullPath(directory)).deleteRecursively()
        }
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
        // Avoid the large vararg array after a suspension point: the API 23
        // x86_64 runtime crashes in setOf here. Compare the same complete key set.
        assertEquals("android_api,daemon,hide,last_hide_result,last_system_operation," +
            "last_system_success,manager,schema,system_mode,transaction_state,zygisk",
            report.keys().asSequence().sorted().joinToString(","))
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

    @Test fun testMigrationRequiresAuthenticatedSingleUseHandoff() = runBlocking {
        val preferences = android.os.Bundle().apply { putString("locale", "en") }
        val intent = Intent().putExtra(Const.Key.PREV_CONFIG, preferences)
        assertNull(ManagerMigration.consume(appContext, intent, appContext.packageName))
        val token = ManagerMigration.prepare(appContext, appContext.packageName)
        try {
            intent.putExtra(ManagerMigration.EXTRA_TOKEN, token.nonce)
            assertNull(ManagerMigration.consume(appContext, intent, "com.android.shell"))
            val accepted = ManagerMigration.consume(appContext, intent, appContext.packageName)
            assertEquals(appContext.packageName, accepted?.source)
            assertEquals("en", accepted?.preferences?.getString("locale"))
            assertNull("A consumed migration cannot be replayed",
                ManagerMigration.consume(appContext, intent, appContext.packageName))
        } finally {
            ManagerMigration.discard(token)
        }
    }

    @Test fun testWarmNavigationAndInstallDestination() {
        val component = MainActivity::class.java.cmp(appContext.packageName).flattenToString()
        fun launch(extra: String = "", action: String = Intent.ACTION_MAIN) {
            val category = if (action == Intent.ACTION_MAIN) "-c android.intent.category.LAUNCHER" else ""
            val output = AutoCloseInputStream(uiAutomation.executeShellCommand(
                "am start -W -n $component -a $action $category $extra"
            )).reader().use { it.readText() }
            assertTrue("Cannot launch requested destination: $output",
                output.contains("Status: ok") || output.contains("Status: timeout"))
        }
        fun shown(resource: Int, timeout: Long) {
            val label = appContext.getString(resource)
            val visible = device.wait(Until.hasObject(By.text(label)), timeout)
            val hierarchy = if (visible) "" else java.io.ByteArrayOutputStream().use {
                device.dumpWindowHierarchy(it)
                it.toString("UTF-8")
            }
            assertTrue("Missing destination '$label': $hierarchy", visible)
        }
        try {
            launch("--es ${Const.Key.OPEN_SECTION} ${Const.Nav.SETTINGS}")
            shown(CoreR.string.settings_customization, 30000)
            launch("--es ${Const.Key.OPEN_SECTION} ${Const.Nav.INSTALL}")
            shown(CoreR.string.select_patch_file, 10000)
            device.pressBack()
            launch(action = Intent.ACTION_APPLICATION_PREFERENCES)
            shown(CoreR.string.settings_customization, 10000)
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
        val visible = device.wait(Until.hasObject(By.text(appContext.getString(CoreR.string.su_warning))), 15000)
        if (!visible) {
            // Retain the actual window state when a CI consent dialog is lost.
            val hierarchy = java.io.ByteArrayOutputStream()
            runCatching { device.dumpWindowHierarchy(hierarchy) }
            hierarchy.toString("UTF-8").take(16384).chunked(3000).forEach {
                android.util.Log.e("ConsentWindow", it)
            }
        }
        assertTrue("Consent must be visible", visible)
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
