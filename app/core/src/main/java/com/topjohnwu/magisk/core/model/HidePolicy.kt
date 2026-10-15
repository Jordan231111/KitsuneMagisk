package com.topjohnwu.magisk.core.model

import com.topjohnwu.magisk.core.R
import com.topjohnwu.superuser.Shell
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import org.json.JSONObject

/** The running mode owns list operations; a saved mode is applied only at boot. */
data class HideStatus(
    val ready: Boolean,
    val enabled: Boolean,
    val mode: String,
    val configuredMode: String,
    val rebootRequired: Boolean,
    val engine: String,
    val configuredProvider: String,
) {
    val isSuList get() = mode == "sulist"

    companion object {
        fun parse(text: String): HideStatus = JSONObject(text).run {
            require(getInt("schema") == 1 && getInt("database_version") == 12)
            val mode = getString("mode")
            val configured = getString("configured_mode")
            val engine = getString("engine")
            val provider = getString("configured_provider")
            require(mode in setOf("hide", "sulist") && configured in setOf("hide", "sulist"))
            require(engine in setOf("builtin", "logcat", "external"))
            require(provider in setOf("core", "external"))
            require(getString("selection_table") == if (mode == "sulist") "sulist" else "denylist")
            HideStatus(getBoolean("boot_ready"), getBoolean("enabled"), mode, configured,
                getBoolean("reboot_required"), engine, provider)
        }
    }
}

class HideFailure(val code: Int) : Exception() {
    val messageRes get() = when (code) {
        5 -> R.string.hide_invalid_name
        6 -> R.string.hide_namespace_error
        8 -> R.string.hide_restart_required
        9 -> R.string.hide_provider_error
        else -> R.string.hide_operation_error
    }
}

object HidePolicy {
    // Serialize reads and writes so the manager never renders optimistic policy.
    private val mutex = Mutex()
    @Volatile var lastResult: Int? = null
        private set

    fun quote(value: String) = "'" + value.replace("'", "'\\''") + "'"

    private fun run(command: String): Shell.Result = Shell.cmd("magisk --hide $command").exec()

    private fun readStatus(): HideStatus {
        val result = run("status")
        if (!result.isSuccess) throw HideFailure(result.code)
        return try { HideStatus.parse(result.out.joinToString("\n")) }
        catch (_: Exception) { throw HideFailure(7) }
    }

    suspend fun status(): HideStatus = withContext(Dispatchers.IO) {
        mutex.withLock { readStatus() }
    }

    suspend fun selection(): Pair<HideStatus, List<String>> = withContext(Dispatchers.IO) {
        mutex.withLock {
            val status = readStatus()
            val result = run("ls")
            if (!result.isSuccess) throw HideFailure(result.code)
            status to result.out
        }
    }

    suspend fun configure(operation: String, value: String? = null): HideStatus =
        withContext(Dispatchers.IO) {
            mutex.withLock {
                require(when (operation) {
                    "enable", "disable" -> value == null
                    "mode" -> value in setOf("hide", "sulist")
                    "provider" -> value in setOf("core", "external")
                    else -> false
                })
                mutate(listOfNotNull(operation, value).joinToString(" "))
                readStatus()
            }
        }

    suspend fun edit(mode: String, changes: List<Triple<String, String, Boolean>>) =
        withContext(Dispatchers.IO) {
            mutex.withLock {
                val status = readStatus()
                if (!status.ready || status.mode != mode || status.rebootRequired) throw HideFailure(8)
                for ((pkg, process, selected) in changes) {
                    mutate("${if (selected) "add" else "rm"} ${quote(pkg)} ${quote(process)}",
                        if (selected) 3 else 4)
                }
            }
        }

    private fun mutate(command: String, idempotent: Int = 0) {
        val result = run(command)
        val code = try {
            val json = JSONObject(result.out.joinToString("\n"))
            require(json.getInt("schema") == 1)
            json.getInt("code").also { require(it == result.code) }
        } catch (_: Exception) { 7 }
        lastResult = code
        if (code != 0 && code != idempotent) throw HideFailure(code)
    }
}
