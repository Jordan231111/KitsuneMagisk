package com.topjohnwu.magisk.ui.install

import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Parcelable
import android.system.Os
import android.text.Spanned
import android.text.SpannedString
import android.widget.Toast
import androidx.databinding.Bindable
import androidx.lifecycle.LiveData
import androidx.lifecycle.MutableLiveData
import androidx.lifecycle.Observer
import androidx.lifecycle.viewModelScope
import com.topjohnwu.magisk.BR
import com.topjohnwu.magisk.BuildConfig
import com.topjohnwu.magisk.R
import com.topjohnwu.magisk.arch.BaseViewModel
import com.topjohnwu.magisk.core.Config
import com.topjohnwu.magisk.core.Const
import com.topjohnwu.magisk.core.Info
import com.topjohnwu.magisk.core.base.ContentResultCallback
import com.topjohnwu.magisk.core.di.AppContext
import com.topjohnwu.magisk.core.ktx.toast
import com.topjohnwu.magisk.core.repository.NetworkService
import com.topjohnwu.magisk.databinding.set
import com.topjohnwu.magisk.dialog.SecondSlotWarningDialog
import com.topjohnwu.magisk.dialog.SystemModeWarningDialog
import com.topjohnwu.magisk.events.GetContentEvent
import com.topjohnwu.magisk.ui.flash.FlashFragment
import io.noties.markwon.Markwon
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.parcelize.Parcelize
import timber.log.Timber
import java.io.File
import java.io.FileOutputStream
import java.io.IOException

class InstallViewModel(svc: NetworkService, markwon: Markwon) : BaseViewModel() {

    val isRooted get() = Info.isRooted
    val skipOptions = Info.isEmulator || (Info.isSAR && !Info.isFDE && Info.ramdisk)
    val noSecondSlot = !isRooted || !Info.isAB || Info.isEmulator
    val allowSystemInstall =
        BuildConfig.DEBUG && isRooted && !Info.isBootPatched &&
            Build.VERSION.SDK_INT >= Build.VERSION_CODES.N_MR1

    @get:Bindable
    var step = if (skipOptions) 1 else 0
        set(value) = set(value, field, { field = it }, BR.step)

    private var methodId = -1
    private var spuriousMethodId = -1

    @get:Bindable
    var method
        get() = methodId
        set(value) = set(value, methodId, { methodId = it }, BR.method) {
            if (it == spuriousMethodId) {
                spuriousMethodId = -1
                return@set
            }
            when (it) {
                R.id.method_patch -> {
                    GetContentEvent("*/*", UriCallback()).publish()
                }
                R.id.method_inactive_slot -> {
                    SecondSlotWarningDialog().show()
                }
                R.id.method_direct_system -> {
                    SystemModeWarningDialog().show()
                }
            }
        }

    private fun resetMethod() {
        spuriousMethodId = methodId
        method = -1
    }

    private val _uri = MutableLiveData<Uri?>()
    val data: LiveData<Uri?> get() = _uri

    private val sourceObserver = Observer<PatchSource?> { source ->
        when (source) {
            is PatchSource.File -> _uri.value = source.uri
            PatchSource.Cancelled -> resetMethod()
            null -> return@Observer
        }
        patchSource.value = null
    }

    @get:Bindable
    var notes: Spanned = SpannedString("")
        set(value) = set(value, field, { field = it }, BR.notes)

    init {
        patchSource.observeForever(sourceObserver)
        viewModelScope.launch(Dispatchers.IO) {
            try {
                // Keep user-facing release notes separate from the old status-table
                // cache, which Markwon rendered as one unreadable paragraph.
                val file = File(
                    AppContext.cacheDir,
                    "release-notes-${BuildConfig.VERSION_CODE}.md",
                )
                val text = when {
                    // The cache name is version-specific, so a successful fetch
                    // is immutable for this build and need not be repeated each
                    // time the install screen is opened.
                    file.exists() -> file.readText()
                    Const.Url.CHANGELOG_URL.isEmpty() -> ""
                    else -> {
                        val fetched = svc.fetchString(Const.Url.CHANGELOG_URL)
                        val staging = File(file.parentFile, "${file.name}.tmp")
                        try {
                            FileOutputStream(staging).use { output ->
                                output.write(fetched.toByteArray())
                                output.fd.sync()
                            }
                            Os.rename(staging.path, file.path)
                        } catch (e: Exception) {
                            if (e is CancellationException) throw e
                            Timber.w(e, "Unable to cache release notes")
                        } finally {
                            staging.delete()
                        }
                        fetched
                    }
                }
                val spanned = markwon.toMarkdown(text)
                withContext(Dispatchers.Main) {
                    notes = spanned
                }
            } catch (e: IOException) {
                Timber.e(e)
            }
        }
    }

    override fun onCleared() {
        patchSource.removeObserver(sourceObserver)
        super.onCleared()
    }

    fun install() {
        when (method) {
            R.id.method_patch -> FlashFragment.patch(data.value!!).navigate(true)
            R.id.method_direct -> FlashFragment.flash(0).navigate(true)
            R.id.method_inactive_slot -> FlashFragment.flash(1).navigate(true)
            R.id.method_direct_system -> FlashFragment.flash(2).navigate(true)
            else -> error("Unknown value")
        }
    }

    override fun onSaveState(state: Bundle) {
        state.putParcelable(INSTALL_STATE_KEY, InstallState(
            methodId,
            step,
            Config.keepVerity,
            Config.keepEnc,
            Config.recovery
        ))
    }

    override fun onRestoreState(state: Bundle) {
        state.getParcelable<InstallState>(INSTALL_STATE_KEY)?.let {
            methodId = it.method
            step = it.step
            Config.keepVerity = it.keepVerity
            Config.keepEnc = it.keepEnc
            Config.recovery = it.recovery
        }
    }

    private sealed class PatchSource {
        class File(val uri: Uri) : PatchSource()
        object Cancelled : PatchSource()
    }

    @Parcelize
    class UriCallback : ContentResultCallback {
        override fun onActivityLaunch() {
            AppContext.toast(R.string.patch_file_msg, Toast.LENGTH_LONG)
        }
        override fun onActivityResult(result: Uri) {
            patchSource.value = PatchSource.File(result)
        }
        override fun onActivityCancel() {
            patchSource.value = PatchSource.Cancelled
        }
    }

    @Parcelize
    class InstallState(
        val method: Int,
        val step: Int,
        val keepVerity: Boolean,
        val keepEnc: Boolean,
        val recovery: Boolean,
    ) : Parcelable

    companion object {
        private const val INSTALL_STATE_KEY = "install_state"
        private val patchSource = MutableLiveData<PatchSource?>()
    }
}
