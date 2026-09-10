package com.topjohnwu.magisk.test

import androidx.annotation.Keep
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.topjohnwu.magisk.terminal.TerminalEmulator
import com.topjohnwu.magisk.terminal.runSuCommand
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.BeforeClass
import org.junit.Test
import org.junit.runner.RunWith

@Keep
@RunWith(AndroidJUnit4::class)
class TerminalTest : BaseTest {
    companion object {
        @BeforeClass
        @JvmStatic
        fun before() = BaseTest.prerequisite()
    }

    @Test
    fun testCommandStatusAndTty() {
        val terminal = TerminalEmulator(80, 24, 8, 16, null)
        assertTrue("Command must have a terminal", runSuCommand(terminal, "test -t 1"))
        assertFalse("Nonzero command status must fail", runSuCommand(terminal, "exit 37"))
        assertFalse("A signal must fail", runSuCommand(terminal, "sh -c 'kill -TERM \$\$'"))
    }
}
