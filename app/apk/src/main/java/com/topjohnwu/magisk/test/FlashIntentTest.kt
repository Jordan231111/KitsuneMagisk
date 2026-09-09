package com.topjohnwu.magisk.test

import android.os.ParcelFileDescriptor.AutoCloseInputStream
import androidx.annotation.Keep
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
            val component = MainActivity::class.java.cmp(appContext.packageName).flattenToString()
            // Use an external caller; launching from the background target app
            // is delayed or blocked after earlier tests press Home.
            val command = "am start -W --user 0 -n '$component' -a ${FlashUtils.INTENT_FLASH} " +
                "--es ${FlashUtils.EXTRA_FLASH_ACTION} ${Const.Value.FLASH_ZIP} " +
                "--es ${FlashUtils.EXTRA_FLASH_URI} file:///data/local/tmp/$name"
            val output = AutoCloseInputStream(uiAutomation.executeShellCommand(command))
                .reader().use { it.readText() }
            assertTrue("Cannot launch external request: $output", output.contains("Status: ok"))
            val message = appContext.getString(CoreR.string.confirm_install, name)
            assertTrue(
                "External flash must wait for consent",
                device.wait(Until.hasObject(By.text(message)), 10000)
            )
            device.pressBack()
            assertTrue("Dismissal must close the request", device.wait(Until.gone(By.text(message)), 5000))
        } finally {
            Config.askedHome = askedHome
            device.pressHome()
        }
    }
}
