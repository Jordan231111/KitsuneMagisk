package com.topjohnwu.magisk.core.su

import android.os.Bundle
import com.topjohnwu.magisk.core.Config
import com.topjohnwu.magisk.core.Info
import com.topjohnwu.magisk.core.di.ServiceLocator
import com.topjohnwu.magisk.core.tasks.MagiskInstaller
import com.topjohnwu.magisk.core.utils.RootUtils
import com.topjohnwu.superuser.Shell
import kotlinx.coroutines.runBlocking

object TestHandler {

    fun run(method: String): Bundle {
        val r = Bundle()

        fun setup(): Boolean {
            val console = mutableListOf<String>()
            val logs = mutableListOf<String>()
            val success = runBlocking {
                MagiskInstaller.Emulator(console, logs).exec()
            }
            if (!success) {
                val output = (console.asSequence() + logs.asSequence())
                    .map { it.trim() }
                    .filter { it.isNotEmpty() }
                    .joinToString("\n")
                    .takeLast(4096)
                r.putString(
                    "reason",
                    "setup failed (root=${Shell.getShell().isRoot})" +
                        if (output.isEmpty()) " without installer output" else "\n$output"
                )
            }
            return success
        }

        fun test(): Boolean {
            // Skip Zygisk check since this version doesn't have Zygisk
            // Note: Zygisk functionality has been removed from this fork

            // Make sure the Magisk app can get root
            val shell = Shell.getShell()
            if (!shell.isRoot) {
                r.putString("reason", "shell not root")
                return false
            }

            // Make sure the root service is running
            RootUtils.Connection.await()

            // Clear existing grant for ADB shell
            runBlocking {
                ServiceLocator.policyDB.delete(2000)
                Config.suAutoResponse = Config.Value.SU_AUTO_ALLOW
                Config.prefs.edit().commit()
            }
            return true
        }

        val b = runCatching {
            when (method) {
                "setup" -> setup()
                "test" -> test()
                else -> {
                    r.putString("reason", "unknown method")
                    false
                }
            }
        }.getOrElse {
            r.putString("reason", it.stackTraceToString())
            false
        }

        r.putBoolean("result", b)
        return r
    }
}
