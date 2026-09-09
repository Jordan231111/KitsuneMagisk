package com.topjohnwu.magisk.ui.flash

import android.net.Uri
import androidx.core.net.toFile
import androidx.lifecycle.viewModelScope
import com.topjohnwu.magisk.arch.BaseViewModel
import com.topjohnwu.magisk.core.AppContext
import com.topjohnwu.magisk.core.Const
import com.topjohnwu.magisk.core.Info
import com.topjohnwu.magisk.core.ktx.reboot
import com.topjohnwu.magisk.core.ktx.synchronized
import com.topjohnwu.magisk.core.ktx.timeFormatStandard
import com.topjohnwu.magisk.core.ktx.toTime
import com.topjohnwu.magisk.core.ktx.writeTo
import com.topjohnwu.magisk.core.tasks.MagiskInstaller
import com.topjohnwu.magisk.core.utils.MediaStoreUtils
import com.topjohnwu.magisk.core.utils.MediaStoreUtils.displayName
import com.topjohnwu.magisk.core.utils.MediaStoreUtils.inputStream
import com.topjohnwu.magisk.core.utils.MediaStoreUtils.outputStream
import com.topjohnwu.magisk.terminal.TerminalEmulator
import com.topjohnwu.magisk.terminal.appendLineOnMain
import com.topjohnwu.magisk.terminal.runSuCommand
import com.topjohnwu.superuser.CallbackList
import com.topjohnwu.superuser.Shell
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import timber.log.Timber
import java.io.File
import java.io.FileNotFoundException
import java.io.IOException

class FlashViewModel : BaseViewModel() {

    enum class State {
        FLASHING, SUCCESS, FAILED
    }

    private val _flashState = MutableStateFlow(State.FLASHING)
    val flashState: StateFlow<State> = _flashState.asStateFlow()

    private val _showReboot = MutableStateFlow(Info.isRooted)
    val showReboot: StateFlow<Boolean> = _showReboot.asStateFlow()

    var flashAction: String = ""
    var flashUri: Uri? = null
    private var started = false

    private var emulator: TerminalEmulator? = null
    private val emulatorReady = CompletableDeferred<TerminalEmulator>()

    fun onEmulatorCreated(emu: TerminalEmulator) {
        if (emulator !== emu) {
            logItems.forEach { emu.appendLineOnMain(it) }
        }
        emulator = emu
        emulatorReady.complete(emu)
    }

    private val logItems = mutableListOf<String>().synchronized()
    private val outItems = object : CallbackList<String>() {
        override fun onAddElement(e: String?) {
            e ?: return
            emulator?.appendLineOnMain(e)
            logItems.add(e)
        }
    }

    // --- Actions ---

    fun startFlashing() {
        if (started) return
        started = true
        val action = flashAction
        val uri = flashUri

        viewModelScope.launch {
            val emu = emulatorReady.await()
            when (action) {
                Const.Value.FLASH_ZIP -> {
                    uri ?: return@launch
                    flashZip(emu, uri)
                }
                Const.Value.UNINSTALL -> {
                    _showReboot.value = false
                    onResult(withContext(Dispatchers.IO) {
                        MagiskInstaller.Uninstall(outItems, logItems).exec()
                    })
                }
                Const.Value.FLASH_MAGISK -> {
                    onResult(withContext(Dispatchers.IO) {
                        if (Info.isEmulator)
                            MagiskInstaller.Emulator(outItems, logItems).exec()
                        else
                            MagiskInstaller.Direct(outItems, logItems).exec()
                    })
                }
                Const.Value.FLASH_INACTIVE_SLOT -> {
                    _showReboot.value = false
                    onResult(withContext(Dispatchers.IO) {
                        MagiskInstaller.SecondSlot(outItems, logItems).exec()
                    })
                }
                Const.Value.FLASH_SYSTEM_MODE -> {
                    onResult(withContext(Dispatchers.IO) {
                        MagiskInstaller.SystemMode(uri?.toString(), outItems, logItems).exec()
                    })
                }
                Const.Value.RECOVER_SYSTEM_MODE -> {
                    onResult(withContext(Dispatchers.IO) {
                        MagiskInstaller.SystemModeRecovery(outItems, logItems).exec()
                    })
                }
                Const.Value.PATCH_FILE -> {
                    uri ?: return@launch
                    _showReboot.value = false
                    onResult(withContext(Dispatchers.IO) {
                        MagiskInstaller.Patch(uri, outItems, logItems).exec()
                    })
                }
                Const.Value.DOWNLOAD -> {
                    uri ?: return@launch
                    _showReboot.value = false
                    onResult(withContext(Dispatchers.IO) {
                        MagiskInstaller.Download(uri.toString(), outItems, logItems).exec()
                    })
                }
            }
        }
    }

    fun recoverAfterProcessDeath() {
        if (started) return
        started = true
        outItems.add("! The installer process was interrupted")
        if (flashAction == Const.Value.FLASH_SYSTEM_MODE ||
            flashAction == Const.Value.RECOVER_SYSTEM_MODE || Info.isSystemMode) {
            outItems.add("! Reboot once to verify or recover System Mode before another installation")
        } else {
            outItems.add("! Check the device state before reopening the requested action")
        }
        onResult(false)
    }

    private fun onResult(success: Boolean) {
        _flashState.value = if (success) State.SUCCESS else State.FAILED
    }

    private suspend fun flashZip(emu: TerminalEmulator, uri: Uri) {
        val installDir = File(AppContext.cacheDir, "flash")
        val result = withContext(Dispatchers.IO) {
            try {
                installDir.deleteRecursively()
                installDir.mkdirs()

                val zipFile = if (uri.scheme == "file") {
                    uri.toFile()
                } else {
                    File(installDir, "install.zip").also {
                        try {
                            uri.inputStream().writeTo(it)
                        } catch (e: IOException) {
                            val msg = if (e is FileNotFoundException) "Invalid Uri" else "Cannot copy to cache"
                            return@withContext msg to null
                        }
                    }
                }

                val binary = File(installDir, "update-binary")
                AppContext.assets.open("module_installer.sh").use { it.writeTo(binary) }

                val name = uri.displayName
                null to Triple(installDir, zipFile, name)
            } catch (e: IOException) {
                Timber.e(e)
                "Unable to extract files" to null
            }
        }

        val (error, prepResult) = result
        if (prepResult == null) {
            emu.appendLineOnMain("! ${error ?: "Installation failed"}")
            _flashState.value = State.FAILED
            return
        }

        val (dir, zipFile, displayName) = prepResult

        val success = withContext(Dispatchers.IO) {
            try {
                runSuCommand(
                    emu,
                    "printf '%s\\n' ${shellQuote("- Installing $displayName")}; " +
                    "sh ${shellQuote("$dir/update-binary")} dummy 1 ${shellQuote(zipFile.absolutePath)}; " +
                    "EXIT=\$?; " +
                    "if [ \$EXIT -ne 0 ]; then echo '! Installation failed'; fi; " +
                    "exit \$EXIT"
                )
            } finally {
                // Finish cleanup before another installation reuses this workspace.
                Shell.cmd("cd /", "rm -rf ${shellQuote(dir.path)} ${shellQuote(Const.TMPDIR)}").exec()
            }
        }

        _flashState.value = if (success) State.SUCCESS else State.FAILED
    }

    private fun shellQuote(value: String) = "'" + value.replace("'", "'\\''") + "'"

    fun saveLog() {
        viewModelScope.launch(Dispatchers.IO) {
            val name = "magisk_install_log_%s.log".format(
                System.currentTimeMillis().toTime(timeFormatStandard)
            )
            val file = MediaStoreUtils.getFile(name)
            file.uri.outputStream().bufferedWriter().use { writer ->
                val transcript = emulator?.screen?.transcriptText
                if (flashAction == Const.Value.FLASH_ZIP && transcript != null) {
                    writer.write(transcript)
                } else {
                    synchronized(logItems) {
                        logItems.forEach {
                            writer.write(it)
                            writer.newLine()
                        }
                    }
                }
            }
            showSnackbar(file.toString())
        }
    }

    fun restartPressed() = reboot()
}
