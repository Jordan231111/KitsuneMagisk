package com.topjohnwu.magisk.test

import android.content.Intent
import android.content.IntentFilter
import android.os.ParcelFileDescriptor.AutoCloseInputStream
import androidx.annotation.Keep
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.topjohnwu.magisk.core.Config
import com.topjohnwu.magisk.core.Info
import com.topjohnwu.magisk.core.di.ServiceLocator
import com.topjohnwu.magisk.core.model.su.SuPolicy
import com.topjohnwu.magisk.view.Notifications
import com.topjohnwu.superuser.ShellUtils.fastCmd
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeoutOrNull
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.BeforeClass
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.TimeUnit

@Keep
@RunWith(AndroidJUnit4::class)
class MagiskAppTest : BaseTest {

    companion object {
        @BeforeClass
        @JvmStatic
        fun before() = BaseTest.prerequisite()
    }

    @Test
    fun testZygisk() {
        assertTrue("Zygisk should be enabled", Info.isZygiskEnabled)
    }

    @Test
    fun testSuNotifications() {
        // Exercise both notifications on every API, including pre-O builders.
        Notifications.suNotification(true, "Kitsune test")
        Notifications.suNotification(false, "Kitsune test")
    }

    @Test
    fun testSuRequest() {
        // Bypass the need to actually show a dialog
        Config.suAutoResponse = Config.Value.SU_AUTO_ALLOW
        Config.prefs.edit().commit()

        // Inject an undetermined + mute logging policy for ADB shell
        val policy = SuPolicy(
            uid = 2000,
            logging = false,
            notification = false,
            remain = 0L
        )
        runBlocking {
            ServiceLocator.policyDB.update(policy)
        }

        val filter = IntentFilter(Intent.ACTION_VIEW)
        filter.addCategory(Intent.CATEGORY_DEFAULT)
        val monitor = instrumentation.addMonitor(filter, null, false)

        // Try to call su from ADB shell
        val caller = AutoCloseInputStream(uiAutomation.executeShellCommand("id -u"))
            .reader().use { it.readText().trim() }
        assertTrue("Unexpected UiAutomation UID: $caller", caller == "0" || caller == "2000")
        val su = fastCmd("magisk --path") + "/su"
        val cmd = if (caller == "0") {
            // Some vendor images, like API 23, execute automation commands as
            // root. Drop to shell first so the inner request exercises policy.
            "$su -s $su 2000 -c id"
        } else {
            "$su -c id"
        }
        try {
            AutoCloseInputStream(uiAutomation.executeShellCommand(cmd)).reader().use {
                // Cold Cuttlefish can take over ten seconds to create the UI.
                // Stay below the daemon's seventy-second response deadline.
                val suRequest = monitor.waitForActivityWithTimeout(TimeUnit.SECONDS.toMillis(30))
                assertNotNull("SuRequestActivity is not launched", suRequest)
                assertTrue(
                    "Cannot grant root permission from shell",
                    it.readText().contains("uid=0")
                )
            }
        } finally {
            instrumentation.removeMonitor(monitor)
        }

        // Check that the database is updated
        runBlocking {
            // The response FIFO is written before the asynchronous policy update.
            var observed = ServiceLocator.policyDB.fetch(2000)
            withTimeoutOrNull(TimeUnit.SECONDS.toMillis(10)) {
                while (observed?.policy != SuPolicy.ALLOW) {
                    delay(50)
                    observed = ServiceLocator.policyDB.fetch(2000)
                }
            }
            val policy = observed
                ?: throw AssertionError("PolicyDB is invalid")
            assertEquals("Policy for shell is incorrect", SuPolicy.ALLOW, policy.policy)
        }
    }
}
