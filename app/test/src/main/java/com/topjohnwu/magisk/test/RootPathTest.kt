package com.topjohnwu.magisk.test

import androidx.annotation.Keep
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@Keep
@RunWith(AndroidJUnit4::class)
class RootPathTest {
    @Test
    fun testOrdinaryApplicationCanRequestRoot() {
        // This target is a plain application. It has none of AppContext's PATH
        // additions, and the manager's preceding consent test enables auto-allow.
        val process = ProcessBuilder("su", "-c", "id").redirectErrorStream(true).start()
        try {
            val output = process.inputStream.bufferedReader().use { it.readText() }
            assertEquals("Ordinary app PATH must expose MagiskSU: $output", 0, process.waitFor())
            assertTrue(output.contains("uid=0"))
        } finally {
            process.destroy()
        }
    }
}
