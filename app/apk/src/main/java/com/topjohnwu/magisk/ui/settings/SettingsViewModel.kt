package com.topjohnwu.magisk.ui.settings

import android.widget.Toast
import androidx.lifecycle.viewModelScope
import com.topjohnwu.magisk.arch.BaseViewModel
import com.topjohnwu.magisk.core.AppContext
import com.topjohnwu.magisk.core.Config
import com.topjohnwu.magisk.core.Info
import com.topjohnwu.magisk.core.R
import com.topjohnwu.magisk.core.ktx.toast
import com.topjohnwu.magisk.core.utils.RootUtils
import com.topjohnwu.magisk.ui.navigation.Route
import com.topjohnwu.magisk.view.Shortcuts
import com.topjohnwu.magisk.core.model.HidePolicy
import com.topjohnwu.magisk.core.model.HideStatus
import com.topjohnwu.magisk.core.model.HideFailure
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

class SettingsViewModel : BaseViewModel() {

    val hideStatus = MutableStateFlow<HideStatus?>(null)
    val hideFailure = MutableStateFlow<Int?>(null)
    val hideBusy = MutableStateFlow(false)

    fun refreshHide() {
        viewModelScope.launch {
            try {
                hideStatus.value = HidePolicy.status()
                hideFailure.value = null
            } catch (e: HideFailure) {
                hideStatus.value = null
                hideFailure.value = e.messageRes
            }
        }
    }

    fun configureHide(operation: String, value: String? = null) {
        if (hideBusy.value) return
        hideBusy.value = true
        viewModelScope.launch {
            try {
                hideStatus.value = HidePolicy.configure(operation, value)
                hideFailure.value = null
            } catch (e: HideFailure) {
                val message = if (operation == "disable" && e.code == 8 && hideStatus.value?.isSuList == true)
                    R.string.sulist_disable_required else e.messageRes
                hideFailure.value = message
                showSnackbar(message)
            } finally {
                hideBusy.value = false
            }
        }
    }

    var authenticate: (onSuccess: () -> Unit) -> Unit = {}

    fun navigateToDenyList() {
        navigateTo(Route.DenyList)
    }

    fun requestAddShortcut() {
        Shortcuts.addHomeIcon(AppContext)
    }

    fun createHosts() {
        viewModelScope.launch {
            RootUtils.addSystemlessHosts()
            AppContext.toast(R.string.settings_hosts_toast, Toast.LENGTH_SHORT)
        }
    }

    fun withAuth(action: () -> Unit) = authenticate(action)

    fun notifyZygiskChange() {
        if (Config.zygisk != Info.isZygiskEnabled) showSnackbar(R.string.reboot_apply_change)
    }
}
