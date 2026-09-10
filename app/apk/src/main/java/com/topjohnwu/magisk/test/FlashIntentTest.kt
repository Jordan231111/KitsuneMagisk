package com.topjohnwu.magisk.test

import android.os.Build
import android.os.ParcelFileDescriptor.AutoCloseInputStream
import androidx.annotation.Keep
import androidx.core.net.toUri
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.uiautomator.By
import androidx.test.uiautomator.Until
import com.topjohnwu.magisk.core.Const
import com.topjohnwu.magisk.core.Config
import com.topjohnwu.magisk.core.cmp
import com.topjohnwu.magisk.ui.MainActivity
import com.topjohnwu.magisk.ui.flash.FlashUtils
import org.junit.Assert.assertTrue
import org.junit.BeforeClass
import org.junit.Test
import org.junit.runner.RunWith
import java.io.ByteArrayOutputStream
import com.topjohnwu.magisk.core.R as CoreR

@Keep
@RunWith(AndroidJUnit4::class)
class FlashIntentTest : BaseTest {
    companion object {
        @BeforeClass
        @JvmStatic
        fun before() = BaseTest.prerequisite()
    }

    @Test
    fun testExternalFlashRequiresConfirmation() {
        val name = "kitsune-untrusted-test.zip"
        val askedHome = Config.askedHome
        try {
            // Isolate this request from the hidden manager's optional shortcut dialog.
            Config.askedHome = true
            val activity = MainActivity::class.java.cmp(appContext.packageName)
            val component = activity.flattenToString()
            val message = appContext.getString(CoreR.string.confirm_install, name)
            fun checkConfirmation() {
                val shown = device.wait(Until.hasObject(By.text(message)), 10000)
                val hierarchy = if (shown) "" else ByteArrayOutputStream().use {
                    device.dumpWindowHierarchy(it)
                    it.toString("UTF-8")
                }
                assertTrue("Flash must wait for consent: $hierarchy", shown)
                device.pressBack()
                assertTrue("Dismissal must close the request", device.wait(Until.gone(By.text(message)), 5000))
            }
            // Use an external caller; launching from the background target app
            // is delayed or blocked after earlier tests press Home.
            val command = "am start -W --user 0 -n $component -a ${FlashUtils.INTENT_FLASH} " +
                "--es ${FlashUtils.EXTRA_FLASH_ACTION} ${Const.Value.FLASH_ZIP} " +
                "--es ${FlashUtils.EXTRA_FLASH_URI} file:///data/local/tmp/$name"
            val output = AutoCloseInputStream(uiAutomation.executeShellCommand(command))
                .reader().use { it.readText() }
            if (output.contains("Status: ok")) {
                checkConfirmation()
            } else {
                // Android 13+ can reject an external action that does not match
                // this launcher's filters, even with an explicit component.
                assertTrue("Cannot launch external request: $output",
                    Build.VERSION.SDK_INT >= 33 && output.contains("Error type 3"))
                @Suppress("DEPRECATION")
                val info = appContext.packageManager.getActivityInfo(activity, 0)
                assertTrue("The tested launcher must exist and be exported", info.exported)
            }

            // Also exercise the notification's creator identity on every API.
            val launch = "am start -W --user 0 -n $component -a android.intent.action.MAIN " +
                "-c android.intent.category.LAUNCHER"
            val launched = AutoCloseInputStream(uiAutomation.executeShellCommand(launch))
                .reader().use { it.readText() }
            assertTrue("Cannot foreground the manager: $launched", launched.contains("Status: ok"))
            val request = FlashUtils.installIntent(appContext, "file:///data/local/tmp/$name".toUri())
            try {
                request.send()
                checkConfirmation()
            } finally {
                request.cancel()
            }
        } finally {
            Config.askedHome = askedHome
            device.pressHome()
        }
    }
}
