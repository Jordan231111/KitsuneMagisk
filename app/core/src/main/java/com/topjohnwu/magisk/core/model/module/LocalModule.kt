package com.topjohnwu.magisk.core.model.module

import com.squareup.moshi.JsonDataException
import com.topjohnwu.magisk.core.Const
import com.topjohnwu.magisk.core.di.ServiceLocator
import com.topjohnwu.magisk.core.utils.RootUtils
import com.topjohnwu.superuser.Shell
import com.topjohnwu.superuser.nio.ExtendedFile
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import timber.log.Timber
import java.io.IOException
import java.util.Locale

data class LocalModule(
    val base: ExtendedFile,
) : Module() {
    private val svc get() = ServiceLocator.networkService

    override var id: String = ""
    override var name: String = ""
    override var version: String = ""
    override var versionCode: Int = -1
    var author: String = ""
    var description: String = ""
    var updateInfo: OnlineModule? = null
    var outdated = false
    private var updateUrl: String = ""

    private val removeFile = base.getChildFile("remove")
    private val disableFile = base.getChildFile("disable")
    private val updateFile = base.getChildFile("update")
    private val pendingDir get() =
        RootUtils.fs.getFile("${Const.MODULE_PATH}_update").getChildFile(base.name)
    val zygiskFolder = base.getChildFile("zygisk")

    val updated get() = updateFile.exists() || pendingDir.exists()
    val isRiru = (id == "riru-core") || base.getChildFile("riru").exists()
    val isZygisk = zygiskFolder.exists()
    val zygiskUnloaded = zygiskFolder.getChildFile("unloaded").exists()
    val hasAction = base.getChildFile("action.sh").exists()

    var enable: Boolean
        get() = !disableFile.exists()
        set(enable) {
            if (setEnabledState(enable)) {
                Shell.cmd("copy_preinit_files").submit()
            }
        }

    var remove: Boolean
        get() = removeFile.exists()
        set(remove) {
            if (remove) {
                if (updated) return
                removeFile.createNewFile()
                Shell.cmd("copy_preinit_files").submit()
            } else {
                removeFile.delete()
                Shell.cmd("copy_preinit_files").submit()
            }
        }

    private fun setEnabledState(enabled: Boolean): Boolean {
        val pending = pendingDir
        // Safe mode also disables pending updates. An explicit user toggle must
        // apply to that version before it becomes the installed module.
        val markers = if (pending.isDirectory) {
            arrayOf(pending.getChildFile("disable"), disableFile)
        } else {
            arrayOf(disableFile)
        }
        for (marker in markers) {
            if (enabled) {
                if (marker.exists() && !marker.delete()) return false
            } else if (!marker.exists() && !marker.createNewFile()) {
                return false
            }
        }
        return true
    }

    fun updateEnabled(enabled: Boolean) = setEnabledState(enabled) && refreshPreinit()

    fun updateRemoved(removed: Boolean): Boolean {
        if (removed) {
            if (updated) return false
            if (!removeFile.exists() && !removeFile.createNewFile()) return false
        } else if (removeFile.exists() && !removeFile.delete()) {
            return false
        }
        return refreshPreinit()
    }

    private fun refreshPreinit(): Boolean {
        val needsStorage = base.getChildFile("sepolicy.rule").exists() ||
            base.getChildFile("early-mount").exists()
        return Shell.cmd(
            "if [ -d \"\$MAGISKTMP/.magisk/preinit\" ] || " +
                "grep -qxF SYSTEMMODE=true /data/adb/magisk/config 2>/dev/null; " +
                "then copy_preinit_files; else ${if (needsStorage) "false" else "true"}; fi"
        ).exec().isSuccess
    }

    @Throws(NumberFormatException::class)
    private fun parseProps(props: List<String>) {
        for (line in props) {
            val prop = line.split("=".toRegex(), 2).map { it.trim() }
            if (prop.size != 2)
                continue

            val key = prop[0]
            val value = prop[1]
            if (key.isEmpty() || key[0] == '#')
                continue

            when (key) {
                "id" -> id = value
                "name" -> name = value
                "version" -> version = value
                "versionCode" -> versionCode = value.toInt()
                "author" -> author = value
                "description" -> description = value
                "updateJson" -> updateUrl = value
            }
        }
    }

    init {
        runCatching {
            parseProps(Shell.cmd("dos2unix < $base/module.prop").exec().out)
        }

        if (id.isEmpty()) {
            id = base.name
        }

        if (name.isEmpty()) {
            name = id
        }
    }

    suspend fun fetch(): Boolean {
        if (updateUrl.isEmpty())
            return false

        try {
            val json = svc.fetchModuleJson(updateUrl)
            updateInfo = OnlineModule(this, json)
            outdated = json.versionCode > versionCode
            return true
        } catch (e: IOException) {
            Timber.w(e)
        } catch (e: JsonDataException) {
            Timber.w(e)
        }

        return false
    }

    companion object {

        fun loaded() = RootUtils.fs.getFile(Const.MODULE_PATH).exists()

        suspend fun installed() = withContext(Dispatchers.IO) {
            RootUtils.fs.getFile(Const.MODULE_PATH)
                .listFiles()
                .orEmpty()
                .filter { !it.isFile && !it.isHidden }
                .map { LocalModule(it) }
                .sortedBy { it.name.lowercase(Locale.ROOT) }
        }
    }
}
