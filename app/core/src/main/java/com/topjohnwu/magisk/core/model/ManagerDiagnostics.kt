package com.topjohnwu.magisk.core.model

import android.os.Build
import com.topjohnwu.magisk.core.BuildConfig
import com.topjohnwu.magisk.core.Info
import com.topjohnwu.superuser.Shell
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject

object ManagerDiagnostics {
    @Volatile var lastSystemOperation: String? = null
    @Volatile var lastSystemSuccess: Boolean? = null

    val transactionStates = setOf("PREFLIGHTED", "STAGED", "APPLYING", "COMMITTED",
        "BOOT_VERIFIED", "ROLLING_BACK", "ROLLED_BACK", "UNINSTALLING", "UNINSTALLED", "FAILED")

    fun parseTransaction(lines: List<String>): String {
        if (lines == listOf("ABSENT")) return "ABSENT"
        val schema = lines.filter { it.startsWith("SCHEMA_VERSION=") }
        val states = lines.filter { it.startsWith("STATE=") }
        if (schema != listOf("SCHEMA_VERSION=1") || states.size != 1) return "UNKNOWN"
        return states.single().substringAfter('=').takeIf { it in transactionStates } ?: "UNKNOWN"
    }

    suspend fun systemState(): String = withContext(Dispatchers.IO) {
        val result = Shell.cmd(
            "if [ ! -e /data/adb/kitsune/system-mode ]; then echo ABSENT; " +
                "else head -c 65536 /data/adb/kitsune/system-mode/transaction.env; fi"
        ).exec()
        if (result.isSuccess) parseTransaction(result.out) else "UNKNOWN"
    }

    /** Deliberate allowlist: no logcat, properties, command text or package inventory. */
    suspend fun collect(): JSONObject = withContext(Dispatchers.IO) {
        val out = JSONObject().put("schema", 1)
            .put("manager", JSONObject(BuildConfig.BUILD_INFO_JSON))
            .put("android_api", Build.VERSION.SDK_INT)
            .put("system_mode", Info.isSystemMode)
            .put("transaction_state", systemState())
            .put("last_hide_result", lastOrNull(HidePolicy.lastResult))
            .put("last_system_operation", lastOrNull(lastSystemOperation))
            .put("last_system_success", lastOrNull(lastSystemSuccess))
        for ((key, command) in listOf("daemon" to "--daemon-version-json", "zygisk" to "--zygisk-status")) {
            val result = Shell.cmd("magisk $command").exec()
            out.put(key, if (result.isSuccess) runCatching { JSONObject(result.out.joinToString("\n")) }
                .getOrDefault(JSONObject().put("available", false)) else JSONObject().put("available", false))
        }
        out.put("hide", try {
            val status = HidePolicy.status()
            JSONObject().put("mode", status.mode).put("configured_mode", status.configuredMode)
                .put("engine", status.engine).put("provider", status.configuredProvider)
                .put("enabled", status.enabled).put("boot_ready", status.ready)
                .put("reboot_required", status.rebootRequired)
        } catch (e: HideFailure) { JSONObject().put("code", e.code) })
        out
    }

    private fun lastOrNull(value: Any?) = value ?: JSONObject.NULL
}
