package com.topjohnwu.magisk.core.tasks

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.Process
import com.topjohnwu.magisk.core.Const
import com.topjohnwu.magisk.core.di.ServiceLocator
import com.topjohnwu.magisk.update.UpdateMetadata
import java.util.UUID

/** Root authorizes a handoff; exported intent extras are not migration authority. */
object ManagerMigration {
    const val EXTRA_TOKEN = "manager_migration_token"
    private val noncePattern = Regex("[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}")
    private val packagePattern = Regex("[A-Za-z_][A-Za-z0-9_]*(\\.[A-Za-z_][A-Za-z0-9_]*)+")
    private val certificatePattern = Regex("[a-f0-9]{64}")

    data class Token(val nonce: String, val key: String, val record: String)
    data class Handoff(val source: String, val preferences: Bundle)

    private fun key(uid: Int) = "manager_migration_$uid"

    @Suppress("DEPRECATION")
    private fun certificate(context: Context, pkg: String): String {
        val signatures = context.packageManager.getPackageInfo(pkg, PackageManager.GET_SIGNATURES).signatures
        require(signatures?.size == 1)
        return UpdateMetadata.sha256(signatures[0].toByteArray())
    }

    suspend fun prepare(context: Context, target: String): Token {
        val source = context.packageName
        require(packagePattern.matches(source) && packagePattern.matches(target))
        val uid = context.packageManager.getApplicationInfo(target, 0).uid
        val sourceUid = context.applicationInfo.uid
        require(uid > 0 && sourceUid > 0)
        val nonce = UUID.randomUUID().toString()
        val record = "$nonce:$source:$target:$sourceUid:${certificate(context, source)}"
        return Token(nonce, key(uid), record).also { ServiceLocator.stringDB.put(it.key, it.record) }
    }

    private suspend fun claim(key: String, record: String): Boolean {
        require(key.matches(Regex("manager_migration_[0-9]+")))
        require(record.matches(Regex("[A-Za-z0-9_.:-]{1,2048}")))
        // Both strings use the restricted alphabet above. One SQLite command
        // serializes deletion and changes(), including on Android 6 SQLite.
        return ServiceLocator.stringDB.exec(
            "DELETE FROM strings WHERE key=\"$key\" AND value=\"$record\"; SELECT changes() AS consumed"
        ) { it["consumed"]?.toIntOrNull() }.singleOrNull() == 1
    }

    suspend fun discard(token: Token) {
        claim(token.key, token.record)
    }

    suspend fun consume(context: Context, intent: Intent, caller: String?): Handoff? {
        val nonce = intent.getStringExtra(EXTRA_TOKEN)?.takeIf(noncePattern::matches) ?: return null
        if (caller == null || !packagePattern.matches(caller)) return null
        val key = key(Process.myUid())
        val record = ServiceLocator.stringDB.fetch(key)
        val parts = record.split(':')
        if (parts.size != 5 || parts[0] != nonce || parts[1] != caller ||
            parts[2] != context.packageName || !packagePattern.matches(parts[2]) ||
            parts[3].toIntOrNull()?.let { it > 0 } != true ||
            !certificatePattern.matches(parts[4])) return null
        val verified = runCatching {
            context.packageManager.getApplicationInfo(caller, 0).uid.toString() == parts[3] &&
                certificate(context, caller) == parts[4]
        }.getOrDefault(false)
        if (!verified || !claim(key, record)) return null
        val preferences = intent.getBundleExtra(Const.Key.PREV_CONFIG) ?: return null
        return Handoff(caller, preferences)
    }
}
