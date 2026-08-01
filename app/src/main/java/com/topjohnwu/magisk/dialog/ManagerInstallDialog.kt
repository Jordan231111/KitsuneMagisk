package com.topjohnwu.magisk.dialog

import com.topjohnwu.magisk.R
import com.topjohnwu.magisk.core.Info
import com.topjohnwu.magisk.core.di.AppContext
import com.topjohnwu.magisk.core.di.ServiceLocator
import com.topjohnwu.magisk.core.download.DownloadEngine
import com.topjohnwu.magisk.core.download.Subject
import com.topjohnwu.magisk.core.ktx.writeTextAtomically
import com.topjohnwu.magisk.view.MagiskDialog
import timber.log.Timber
import java.io.File

class ManagerInstallDialog : MarkDownDialog() {

    private val svc get() = ServiceLocator.networkService

    override suspend fun getMarkdownText(): String {
        val remote = Info.remote.magisk
        if (remote.note.isEmpty()) return ""
        val cache = File(AppContext.cacheDir, "update-note-${remote.versionCode}.md")
        if (cache.isFile) return cache.readText()
        return svc.fetchString(remote.note).also {
            runCatching { cache.writeTextAtomically(it) }
                .onFailure { error -> Timber.w(error, "Unable to cache update notes") }
        }
    }

    override fun build(dialog: MagiskDialog) {
        super.build(dialog)
        dialog.apply {
            setCancelable(true)
            setButton(MagiskDialog.ButtonType.POSITIVE) {
                text = R.string.install
                onClick { DownloadEngine.startWithActivity(activity, Subject.App()) }
            }
            setButton(MagiskDialog.ButtonType.NEGATIVE) {
                text = android.R.string.cancel
            }
        }
    }

}
