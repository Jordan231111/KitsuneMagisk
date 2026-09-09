package com.topjohnwu.magisk.test

import android.content.Intent
import androidx.annotation.Keep
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.uiautomator.By
import androidx.test.uiautomator.Until
import com.topjohnwu.magisk.core.Const
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
        val intent = Intent(FlashUtils.INTENT_FLASH).apply {
            component = MainActivity::class.java.cmp(appContext.packageName)
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
            putExtra(FlashUtils.EXTRA_FLASH_ACTION, Const.Value.FLASH_ZIP)
            putExtra(FlashUtils.EXTRA_FLASH_URI, "file:///data/local/tmp/$name")
        }
        appContext.startActivity(intent)
        val message = appContext.getString(CoreR.string.confirm_install, name)
        assertTrue(
            "External flash must wait for consent",
            device.wait(Until.hasObject(By.text(message)), 10000)
        )
        device.findObject(By.text(appContext.getString(android.R.string.cancel))).click()
        assertTrue("Cancel must close the request", device.wait(Until.gone(By.text(message)), 5000))
        device.pressHome()
    }
}
