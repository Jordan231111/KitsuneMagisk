package com.topjohnwu.magisk.ui.surequest

import android.content.Intent
import android.content.SharedPreferences
import android.graphics.drawable.Drawable
import android.os.CountDownTimer
import android.os.SystemClock
import androidx.core.content.edit
import androidx.lifecycle.viewModelScope
import com.topjohnwu.magisk.arch.BaseViewModel
import com.topjohnwu.magisk.core.AppContext
import com.topjohnwu.magisk.core.Config
import com.topjohnwu.magisk.core.data.magiskdb.PolicyDao
import com.topjohnwu.magisk.core.ktx.getLabel
import com.topjohnwu.magisk.core.model.su.SuPolicy.Companion.ALLOW
import com.topjohnwu.magisk.core.model.su.SuPolicy.Companion.DENY
import com.topjohnwu.magisk.core.su.SuRequestHandler
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.concurrent.TimeUnit.SECONDS

class SuRequestViewModel(
    policyDB: PolicyDao,
    private val timeoutPrefs: SharedPreferences
) : BaseViewModel() {

    data class UiState(
        val showUi: Boolean = false,
        val icon: Drawable? = null,
        val title: String = "",
        val packageName: String = "",
        val isSharedUid: Boolean = false,
        val userId: Int? = null,
        val selectedItemPosition: Int = 0,
        val grantEnabled: Boolean = false,
        val denyCountdown: Int = 0,
        val useTapjackProtection: Boolean = false,
    )

    private val _uiState = MutableStateFlow(UiState())
    val uiState: StateFlow<UiState> = _uiState.asStateFlow()

    var authenticate: (callback: (Boolean) -> Unit) -> Unit = { it(false) }
    var finishActivity: () -> Unit = {}

    val useTapjackProtection get() = _uiState.value.useTapjackProtection

    private val handler = SuRequestHandler(AppContext.packageManager, policyDB)
    private val millis = SECONDS.toMillis(Config.suDefaultTimeout.toLong().coerceIn(1, 60))
    private var timer = SuTimer(millis, 1000)
    private var started = false
    private var authenticating = false
    private var initialized = false
    private var responded = false

    fun setSelectedItemPosition(position: Int) {
        if (!authenticating && !responded) {
            _uiState.update { it.copy(selectedItemPosition = position.coerceIn(Config.Value.TIMEOUT_LIST.indices)) }
        }
    }

    fun grantPressed() {
        if (!initialized || responded || authenticating || !_uiState.value.grantEnabled) return
        if (handler.requiresAuthentication || Config.suAuth) {
            authenticating = true
            _uiState.update { it.copy(grantEnabled = false) }
            timer.cancel()
            timer = SuTimer((handler.expiresAt - SystemClock.elapsedRealtime()).coerceAtLeast(1), 1000)
            timer.start()
            authenticate { success ->
                if (success) respond(ALLOW, authenticated = true) else respond(DENY, remember = false)
            }
        } else {
            respond(ALLOW)
        }
    }

    fun denyPressed() {
        respond(DENY)
    }

    fun spinnerTouched() {
        // Changing duration never removes the request deadline.
    }

    fun handleRequest(intent: Intent) {
        if (started) return
        started = true
        viewModelScope.launch {
            if (handler.start(intent)) showDialog() else finishActivity()
        }
    }

    private fun showDialog() {
        val pm = handler.pm
        val info = handler.pkgInfo
        val app = info.applicationInfo

        val isSharedUid = info.sharedUserId != null
        val icon: Drawable?
        val title: String
        val packageName: String

        if (app == null) {
            icon = pm.defaultActivityIcon
            title = info.sharedUserId.toString()
            packageName = info.sharedUserId.toString()
        } else {
            icon = app.loadIcon(pm)
            title = app.getLabel(pm)
            packageName = info.packageName
        }

        val selectedPos = timeoutPrefs.getInt(packageName, 0).coerceIn(Config.Value.TIMEOUT_LIST.indices)
        _uiState.update {
            it.copy(
                showUi = true,
                icon = icon,
                title = title,
                packageName = packageName,
                isSharedUid = isSharedUid,
                userId = handler.userId.takeIf { it != com.topjohnwu.magisk.core.Const.USER_ID },
                selectedItemPosition = selectedPos,
                useTapjackProtection = Config.suTapjack,
                grantEnabled = false,
            )
        }
        viewModelScope.launch {
            delay(1000)
            _uiState.update { it.copy(grantEnabled = true) }
        }
        initialized = true
        timer = SuTimer(minOf(millis, (handler.expiresAt - SystemClock.elapsedRealtime()).coerceAtLeast(1)), 1000)
        timer.start()
    }

    fun activityDestroyed() {
        if (initialized && !responded) {
            respond(DENY, remember = false)
        }
    }

    override fun onCleared() {
        timer.cancel()
        handler.close()
    }

    private fun respond(action: Int, authenticated: Boolean = false, remember: Boolean = true) {
        if (!initialized || responded) return
        responded = true
        timer.cancel()

        val pos = _uiState.value.selectedItemPosition
        val pkg = _uiState.value.packageName
        timeoutPrefs.edit { putInt(pkg, pos) }

        viewModelScope.launch(Dispatchers.IO) {
            handler.respond(action, if (remember) Config.Value.TIMEOUT_LIST[pos] else -1, authenticated)
            withContext(Dispatchers.Main) {
                finishActivity()
            }
        }
    }

    private inner class SuTimer(
        millis: Long,
        interval: Long
    ) : CountDownTimer(millis, interval) {

        override fun onTick(remains: Long) {
            _uiState.update {
                it.copy(
                    denyCountdown = (remains / 1000).toInt() + 1
                )
            }
        }

        override fun onFinish() {
            _uiState.update { it.copy(denyCountdown = 0) }
            respond(DENY, remember = false)
        }
    }
}
