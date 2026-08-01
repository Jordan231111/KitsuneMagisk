package com.topjohnwu.magisk.core

import android.app.KeyguardManager
import androidx.lifecycle.MutableLiveData
import com.topjohnwu.magisk.StubApk
import com.topjohnwu.magisk.core.di.AppContext
import com.topjohnwu.magisk.core.ktx.getProperty
import com.topjohnwu.magisk.core.model.UpdateInfo
import com.topjohnwu.magisk.core.repository.NetworkService
import com.topjohnwu.magisk.core.repository.UpdateCheckResult
import com.topjohnwu.magisk.core.repository.UpdateUnavailableReason
import com.topjohnwu.superuser.ShellUtils.fastCmd
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

val isRunningAsStub get() = Info.stub != null

object Info {

    var stub: StubApk.Data? = null

    val EMPTY_REMOTE = UpdateInfo()

    private class RemoteState(
        val info: UpdateInfo,
        val result: UpdateCheckResult,
    )

    private val remoteFetchLock = Mutex()
    @Volatile
    private var remoteState = RemoteState(EMPTY_REMOTE, UpdateCheckResult.NotChecked)

    val remote get() = remoteState.info
    val updateCheckResult get() = remoteState.result

    fun resetRemote() {
        remoteState = RemoteState(EMPTY_REMOTE, UpdateCheckResult.NotChecked)
    }

    suspend fun getRemote(svc: NetworkService): UpdateInfo? {
        // Capture the state before waiting for the mutex. If another caller
        // completes the same request first, consume its result instead of
        // immediately repeating a failed or successful network operation.
        val observedState = remoteState
        return remoteFetchLock.withLock {
            val beforeFetch = remoteState
            if (beforeFetch !== observedState &&
                beforeFetch.result !is UpdateCheckResult.NotChecked
            ) {
                return@withLock if (beforeFetch.result is UpdateCheckResult.Success) {
                    beforeFetch.info
                } else {
                    null
                }
            }
            when (beforeFetch.result) {
                is UpdateCheckResult.Success -> return@withLock beforeFetch.info
                is UpdateCheckResult.Unavailable -> {
                    if (beforeFetch.result.reason != UpdateUnavailableReason.REQUEST_FAILED) {
                        return@withLock null
                    }
                }
                UpdateCheckResult.NotChecked -> Unit
            }

            val fetched = svc.fetchUpdate()
            // A settings or network reset may occur while this request is in
            // flight. Do not overwrite that newer state.
            if (remoteState === beforeFetch) {
                remoteState = when (fetched) {
                    is UpdateCheckResult.Success -> RemoteState(fetched.info, fetched)
                    is UpdateCheckResult.Unavailable -> RemoteState(EMPTY_REMOTE, fetched)
                    UpdateCheckResult.NotChecked -> beforeFetch
                }
            }
            val current = remoteState
            if (current.result is UpdateCheckResult.Success) current.info else null
        }
    }

    // Device state
    @JvmStatic val env by lazy { loadState() }
    @JvmField var isSAR = false
    var legacySAR = false
    var isAB = false
    @JvmField val isZygiskEnabled = System.getenv("ZYGISK_ENABLED") == "1"
    @JvmStatic val isFDE get() = crypto == "block"
    @JvmField var ramdisk = false
    var patchBootVbmeta = false
    var crypto = ""
    var noDataExec = false
    var isRooted = false
    var sulist = false
    var isBootPatched = false

    @JvmField var hasGMS = true
    @JvmField val isEmulator =
        getProperty("ro.kernel.qemu", "0") == "1" ||
            getProperty("ro.boot.qemu", "0") == "1"

    val isConnected = MutableLiveData(false)

    val showSuperUser: Boolean get() {
        return env.isActive && (Const.USER_ID == 0
                || Config.suMultiuserMode == Config.Value.MULTIUSER_MODE_USER)
    }

    val isDeviceSecure get() =
        AppContext.getSystemService(KeyguardManager::class.java).isDeviceSecure

    private fun loadState(): Env {
        val v = fastCmd("magisk -v").split(":".toRegex())
        return Env(
            v[0], v.size >= 3 && v[2] == "D",
            runCatching { fastCmd("magisk -V").toInt() }.getOrDefault(-1)
        )
    }

    class Env(
        val versionString: String = "",
        val isDebug: Boolean = false,
        code: Int = -1
    ) {
        val versionCode = when {
            code < Const.Version.MIN_VERCODE -> -1
            isRooted ->  code
            else -> -1
        }
        val isUnsupported = code > 0 && code < Const.Version.MIN_VERCODE
        val isActive = versionCode > 0
    }
}
