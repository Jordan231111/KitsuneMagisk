package com.topjohnwu.magisk.core.su

import android.content.Intent
import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.net.LocalSocket
import android.net.LocalSocketAddress
import android.os.SystemClock
import com.topjohnwu.magisk.core.BuildConfig
import com.topjohnwu.magisk.core.Config
import com.topjohnwu.magisk.core.data.magiskdb.PolicyDao
import com.topjohnwu.magisk.core.ktx.getPackageInfo
import com.topjohnwu.magisk.core.model.su.SuPolicy
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import timber.log.Timber
import java.io.DataInputStream
import java.io.DataOutputStream

class SuRequestHandler(
    val pm: PackageManager,
    @Suppress("UNUSED_PARAMETER") policyDB: PolicyDao
) {
    private var socket: LocalSocket? = null
    private var responded = false
    var requiresAuthentication = true
        private set
    var expiresAt = 0L
        private set
    lateinit var pkgInfo: PackageInfo
        private set

    // The daemon supplies authoritative metadata over a single-use connection.
    // Untrusted launcher extras cannot choose a UID or persist a policy.
    suspend fun start(intent: Intent): Boolean = withContext(Dispatchers.IO) {
        try {
            val path = intent.getStringExtra("socket") ?: return@withContext false
            require(path.matches(Regex("/[^\\n]+/\\.magisk/su_[0-9]+_[0-9]+")))
            val connection = LocalSocket().also { socket = it }
            connection.soTimeout = 5000
            connection.connect(LocalSocketAddress(path, LocalSocketAddress.Namespace.FILESYSTEM))
            require(connection.peerCredentials.uid == 0)
            val input = DataInputStream(connection.inputStream)
            require(input.readInt() == 0x4b535531)
            val uid = input.readInt()
            val pid = input.readInt()
            val auth = input.readInt()
            val remaining = input.readInt()
            require(uid > 0 && pid > 0 && auth in 0..1 && remaining in 1..70000)
            requiresAuthentication = auth == 1 || Config.suAuth
            expiresAt = SystemClock.elapsedRealtime() + remaining - 1000
            pkgInfo = pm.getPackageInfo(uid, pid) ?: PackageInfo().apply {
                sharedUserId = (pm.getNameForUid(uid)
                    ?: throw PackageManager.NameNotFoundException()).substringBefore(':')
            }
            if (pkgInfo.packageName == BuildConfig.APP_PACKAGE_NAME) {
                respond(SuPolicy.DENY, -1)
                return@withContext false
            }
            when {
                Config.suAutoResponse == Config.Value.SU_AUTO_DENY -> {
                    respond(SuPolicy.DENY, 0)
                    false
                }
                Config.suAutoResponse == Config.Value.SU_AUTO_ALLOW && !requiresAuthentication -> {
                    respond(SuPolicy.ALLOW, 0)
                    false
                }
                else -> true
            }
        } catch (e: Exception) {
            Timber.e(e, "Cannot open SU consent request")
            close()
            false
        }
    }

    suspend fun respond(action: Int, time: Long, authenticated: Boolean = false) =
        withContext(Dispatchers.IO) {
            val connection = synchronized(this@SuRequestHandler) {
                if (responded) return@withContext
                responded = true
                socket
            } ?: return@withContext
            try {
                require(SystemClock.elapsedRealtime() < expiresAt)
                require(action == SuPolicy.DENY || !requiresAuthentication || authenticated)
                val policy = if (action == SuPolicy.ALLOW && Config.suRestrict) SuPolicy.RESTRICT else action
                connection.soTimeout = 3000
                DataOutputStream(connection.outputStream).apply {
                    writeInt(policy)
                    writeInt(if (time < 0) -1 else Math.multiplyExact(time, 60).toInt())
                    writeInt(if (authenticated) 1 else 0)
                    flush()
                }
                require(DataInputStream(connection.inputStream).readInt() == policy)
                SuEvents.notifyPolicyChanged()
            } catch (e: Exception) {
                Timber.e(e, "SU consent was not accepted")
            } finally {
                close()
            }
        }

    fun close() {
        runCatching { socket?.close() }
        socket = null
    }
}
