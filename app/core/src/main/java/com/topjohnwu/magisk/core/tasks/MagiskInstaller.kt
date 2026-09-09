package com.topjohnwu.magisk.core.tasks

import android.net.Uri
import android.os.Build
import android.os.Process
import android.os.SystemClock
import android.system.ErrnoException
import android.system.Os
import androidx.annotation.WorkerThread
import androidx.core.os.postDelayed
import com.topjohnwu.magisk.StubApk
import com.topjohnwu.magisk.core.AppApkPath
import com.topjohnwu.magisk.core.BuildConfig
import com.topjohnwu.magisk.core.Config
import com.topjohnwu.magisk.core.Const
import com.topjohnwu.magisk.core.Info
import com.topjohnwu.magisk.core.di.ServiceLocator
import com.topjohnwu.magisk.core.isRunningAsStub
import com.topjohnwu.magisk.core.ktx.copyAll
import com.topjohnwu.magisk.core.ktx.writeTo
import com.topjohnwu.magisk.core.utils.DataSourceChannel
import com.topjohnwu.magisk.core.utils.DummyList
import com.topjohnwu.magisk.core.utils.MediaStoreUtils
import com.topjohnwu.magisk.core.utils.MediaStoreUtils.inputStream
import com.topjohnwu.magisk.core.utils.MediaStoreUtils.openFd
import com.topjohnwu.magisk.core.utils.MediaStoreUtils.outputStream
import com.topjohnwu.magisk.core.utils.RootUtils
import com.topjohnwu.superuser.Shell
import com.topjohnwu.superuser.ShellUtils
import com.topjohnwu.superuser.internal.UiThreadHandler
import com.topjohnwu.superuser.nio.ExtendedFile
import com.topjohnwu.superuser.nio.FileSystemManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.apache.commons.compress.archivers.tar.TarArchiveEntry
import org.apache.commons.compress.archivers.tar.TarArchiveInputStream
import org.apache.commons.compress.archivers.tar.TarArchiveOutputStream
import org.apache.commons.compress.archivers.zip.ZipFile
import org.apache.commons.compress.compressors.lz4.FramedLZ4CompressorInputStream
import timber.log.Timber
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.FilterInputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.io.PushbackInputStream
import java.nio.ByteBuffer
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Locale
import java.util.concurrent.atomic.AtomicBoolean

object SystemModeConsent {
    private const val TTL_MILLIS = 5 * 60 * 1000L
    private const val TOKEN_BYTES = 32
    private val hex = "0123456789abcdef".toCharArray()
    private val random = SecureRandom()
    private var token: String? = null
    private var expiresAt = 0L

    @Synchronized
    fun issue(): String {
        val bytes = ByteArray(TOKEN_BYTES).also(random::nextBytes)
        val encoded = CharArray(bytes.size * 2)
        bytes.forEachIndexed { index, byte ->
            val value = byte.toInt() and 0xff
            encoded[index * 2] = hex[value ushr 4]
            encoded[index * 2 + 1] = hex[value and 0x0f]
        }
        return encoded.concatToString().also {
            token = it
            expiresAt = SystemClock.elapsedRealtime() + TTL_MILLIS
        }
    }

    @Synchronized
    internal fun consume(candidate: String?): Boolean {
        val issued = token ?: return false
        if (SystemClock.elapsedRealtime() >= expiresAt) {
            clear()
            return false
        }
        if (candidate == null)
            return false
        val valid = MessageDigest.isEqual(
            issued.toByteArray(StandardCharsets.US_ASCII),
            candidate.toByteArray(StandardCharsets.US_ASCII),
        )
        if (valid)
            clear()
        return valid
    }

    private fun clear() {
        token = null
        expiresAt = 0L
    }
}

abstract class MagiskInstallImpl protected constructor(
    protected val console: MutableList<String>,
    private val logs: MutableList<String>
) {

    private lateinit var installDir: ExtendedFile
    private lateinit var srcBoot: ExtendedFile
    protected var systemModeOperation = false
        private set

    private val shell = Shell.getShell()
    private val useRootDir = shell.isRoot && Info.noDataExec
    protected val context get() = ServiceLocator.deContext

    private val rootFS get() = RootUtils.fs
    private val localFS get() = FileSystemManager.getLocal()

    private val destName: String by lazy {
        if (Config.randName) {
            val alpha = "abcdefghijklmnopqrstuvwxyz"
            val alphaNum = "$alpha${alpha.uppercase(Locale.ROOT)}0123456789"
            val random = SecureRandom()
            StringBuilder("magisk_patched-${BuildConfig.APP_VERSION_CODE}_").run {
                for (i in 1..5) {
                    append(alphaNum[random.nextInt(alphaNum.length)])
                }
                toString()
            }
        } else {
            "magisk_patched"
        }
    }

    private fun findImage(slot: String): Boolean {
        val cmd =
            "RECOVERYMODE=${Config.recovery} " +
            "VENDORBOOT=${Info.isVendorBoot} " +
            "SLOT=$slot " +
            "find_boot_image; echo \$BOOTIMAGE"
        val bootPath = ("($cmd)").fsh()
        if (bootPath.isEmpty()) {
            console.add("! Unable to detect target image")
            return false
        }
        srcBoot = rootFS.getFile(bootPath)
        console.add("- Target image: $bootPath")
        return true
    }

    private fun findImage(): Boolean {
        return findImage(Info.slot)
    }

    private fun findSecondary(): Boolean {
        val slot = if (Info.slot == "_a") "_b" else "_a"
        console.add("- Target slot: $slot")
        return findImage(slot)
    }

    private suspend fun extractFiles(): Boolean {
        console.add("- Device platform: ${Const.CPU_ABI}")
        console.add("- Installing: ${BuildConfig.APP_VERSION_NAME} (${BuildConfig.APP_VERSION_CODE})")

        installDir = localFS.getFile(context.filesDir.parent, "install")
        installDir.deleteRecursively()
        installDir.mkdirs()

        try {
            // Extract binaries
            if (isRunningAsStub) {
                ZipFile.builder().setFile(StubApk.current(context)).get().use { zf ->
                    zf.entries.asSequence().filter {
                        !it.isDirectory && it.name.startsWith("lib/${Const.CPU_ABI}/")
                    }.forEach {
                        val n = it.name.substring(it.name.lastIndexOf('/') + 1)
                        val name = n.substring(3, n.length - 3)
                        val dest = File(installDir, name)
                        zf.getInputStream(it).writeTo(dest)
                        dest.setExecutable(true)
                    }

                    val abi32 = Const.CPU_ABI_32
                    if (Process.is64Bit() && abi32 != null) {
                        val entry = zf.getEntry("lib/$abi32/libmagisk.so")
                        if (entry != null) {
                            val magisk32 = File(installDir, "magisk32")
                            zf.getInputStream(entry).writeTo(magisk32)
                        }
                    }
                }
            } else {
                val info = context.applicationInfo
                val libs = File(info.nativeLibraryDir).listFiles { _, name ->
                    name.startsWith("lib") && name.endsWith(".so")
                } ?: emptyArray()

                for (lib in libs) {
                    val name = lib.name.substring(3, lib.name.length - 3)
                    Os.symlink(lib.path, "$installDir/$name")
                }

                // Also extract magisk32 on 64-bit devices that supports 32-bit
                val abi32 = Const.CPU_ABI_32
                if (Process.is64Bit() && abi32 != null) {
                    val name = "lib/$abi32/libmagisk.so"
                    val entry = javaClass.classLoader!!.getResourceAsStream(name)
                    if (entry != null) {
                        val magisk32 = File(installDir, "magisk32")
                        entry.writeTo(magisk32)
                    }
                }
            }

            // Extract scripts
            for (script in listOf(
                "util_functions.sh",
                "boot_patch.sh",
                "addon.d.sh",
                "app_functions.sh",
                "uninstaller.sh",
                "module_installer.sh",
                "kitsune_system_install.sh",
                "kitsune_system_launcher.sh",
                "kitsune_system_rescue.sh",
                "system_mode_transaction.sh",
                "system_mode_verify.sh",
                "stub.apk",
            )) {
                val dest = File(installDir, script)
                context.assets.open(script).writeTo(dest)
            }
            // Extract chromeos tools
            File(installDir, "chromeos").mkdir()
            for (file in listOf("futility", "kernel_data_key.vbprivk", "kernel.keyblock")) {
                val name = "chromeos/$file"
                val dest = File(installDir, name)
                context.assets.open(name).writeTo(dest)
            }
        } catch (e: Exception) {
            console.add("! Unable to extract files")
            Timber.e(e)
            return false
        }

        if (useRootDir) {
            // Move everything to tmpfs to workaround Samsung bullshit
            rootFS.getFile(Const.TMPDIR).also {
                arrayOf(
                    "rm -rf $it",
                    "mkdir -p $it",
                    "cp_readlink $installDir $it",
                    "rm -rf $installDir"
                ).sh()
                installDir = it
            }
        }

        return true
    }

    private suspend fun InputStream.copyAndCloseOut(out: OutputStream) =
        out.use { copyAll(it, 1024 * 1024) }

    private class NoAvailableStream(s: InputStream) : FilterInputStream(s) {
        // Make sure available is never called on the actual stream and always return 0
        // to reduce max buffer size and avoid OOM
        override fun available() = 0
    }

    private class NoBootException : IOException()

    inner class BootItem(private val entry: TarArchiveEntry) {
        val name = entry.name.replace(".lz4", "")
        var file = installDir.getChildFile(name)

        suspend fun copyTo(tarOut: TarArchiveOutputStream) {
            entry.name = name
            entry.size = file.length()
            file.newInputStream().use {
                console.add("-- Writing   : $name")
                tarOut.putArchiveEntry(entry)
                it.copyAll(tarOut)
                tarOut.closeArchiveEntry()
            }
        }
    }

    @Throws(IOException::class)
    private suspend fun processTar(
        tarIn: TarArchiveInputStream,
        tarOut: TarArchiveOutputStream
    ): BootItem {
        console.add("- Processing tar file")
        var entry: TarArchiveEntry? = tarIn.nextEntry

        fun decompressedStream(): InputStream {
            val stream = if (tarIn.currentEntry.name.endsWith(".lz4"))
                FramedLZ4CompressorInputStream(tarIn, true) else tarIn
            return NoAvailableStream(stream)
        }

        var boot: BootItem? = null
        var initBoot: BootItem? = null
        var recovery: BootItem? = null

        while (entry != null) {
            val bootItem: BootItem?
            if (entry.name.startsWith("boot.img")) {
                bootItem = BootItem(entry)
                boot = bootItem
            } else if (entry.name.startsWith("init_boot.img")) {
                bootItem = BootItem(entry)
                initBoot = bootItem
            } else if (Config.recovery && entry.name.contains("recovery.img")) {
                bootItem = BootItem(entry)
                recovery = bootItem
            } else {
                bootItem = null
            }

            if (bootItem != null) {
                console.add("-- Extracting: ${bootItem.name}")
                decompressedStream().copyAndCloseOut(bootItem.file.newOutputStream())
            } else if (entry.name.contains("vbmeta.img")) {
                val rawData = decompressedStream().readBytes()
                // Valid vbmeta.img should be at least 256 bytes
                if (rawData.size < 256)
                    continue

                // vbmeta partition exist, disable boot vbmeta patch
                Info.patchBootVbmeta = false

                val name = entry.name.replace(".lz4", "")
                console.add("-- Patching  : $name")

                // Patch flags to AVB_VBMETA_IMAGE_FLAGS_HASHTREE_DISABLED |
                // AVB_VBMETA_IMAGE_FLAGS_VERIFICATION_DISABLED
                ByteBuffer.wrap(rawData).putInt(120, 3)

                // Fetch the next entry first before modifying current entry
                val vbmeta = entry
                entry = tarIn.nextEntry

                // Update entry with new information
                vbmeta.name = name
                vbmeta.size = rawData.size.toLong()

                // Write output
                tarOut.putArchiveEntry(vbmeta)
                tarOut.write(rawData)
                tarOut.closeArchiveEntry()
                continue
            } else if (entry.name.contains("userdata.img")) {
                console.add("-- Skipping  : ${entry.name}")
            } else {
                console.add("-- Copying   : ${entry.name}")
                tarOut.putArchiveEntry(entry)
                tarIn.copyAll(tarOut)
                tarOut.closeArchiveEntry()
            }
            entry = tarIn.nextEntry ?: break
        }

        // Patch priority: recovery > init_boot > boot
        return when {
            recovery != null -> {
                if (boot != null) {
                    // Repack boot image to prevent auto restore
                    arrayOf(
                        "cd $installDir",
                        "chmod -R 755 .",
                        "./magiskboot unpack boot.img",
                        "./magiskboot repack boot.img",
                        "cat new-boot.img > boot.img",
                        "./magiskboot cleanup",
                        "rm -f new-boot.img",
                        "cd /").sh()
                    boot.copyTo(tarOut)
                }
                recovery
            }
            initBoot != null -> {
                boot?.copyTo(tarOut)
                initBoot
            }
            boot != null -> boot
            else -> throw NoBootException()
        }
    }

    private suspend fun processFile(uri: Uri): Boolean {
        val outStream: OutputStream
        val outFile: MediaStoreUtils.UriFile
        var bootItem: BootItem? = null

        // Process input file
        try {
            PushbackInputStream(uri.inputStream().buffered(1024 * 1024), 512).use { src ->
                val head = ByteArray(512)
                if (src.read(head) != head.size) {
                    console.add("! Invalid input file")
                    return false
                }
                src.unread(head)

                val magic = head.copyOf(4)
                val tarMagic = head.copyOfRange(257, 262)

                srcBoot = if (tarMagic.contentEquals("ustar".toByteArray())) {
                    // tar file
                    outFile = MediaStoreUtils.getFile("$destName.tar")
                    val os = outFile.uri.outputStream().buffered(1024 * 1024)
                    outStream = TarArchiveOutputStream(os).also {
                        it.setBigNumberMode(TarArchiveOutputStream.BIGNUMBER_STAR)
                        it.setLongFileMode(TarArchiveOutputStream.LONGFILE_GNU)
                    }

                    try {
                        bootItem = processTar(TarArchiveInputStream(src), outStream)
                        bootItem.file
                    } catch (e: IOException) {
                        outStream.close()
                        outFile.delete()
                        throw e
                    }
                } else {
                    // raw image
                    outFile = MediaStoreUtils.getFile("$destName.img")
                    outStream = outFile.uri.outputStream()
                    val channel = FileInputStream(uri.openFd().fileDescriptor).channel
                    val boot = installDir.getChildFile("boot.img")

                    try {
                        if (magic.contentEquals("CrAU".toByteArray())) {
                            DataSourceChannel(channel).use { source ->
                                Payload(source).extract(boot, console, logs)
                            }
                        } else if (magic.contentEquals("PK\u0003\u0004".toByteArray())) {
                            ExtractImage(boot, console, logs).consume(DataSourceChannel(channel))
                        } else {
                            console.add("- Copying image to cache")
                            src.copyAndCloseOut(boot.newOutputStream())
                        }
                        boot
                    } catch (e: IOException) {
                        outStream.close()
                        outFile.delete()
                        throw e
                    }
                }
            }
        } catch (e: IOException) {
            if (e is NoBootException)
                console.add("! No boot image found")
            console.add("! Process error")
            Timber.e(e)
            return false
        }

        // Patch file
        if (!patchBoot()) {
            outFile.delete()
            return false
        }

        // Output file
        try {
            val newBoot = installDir.getChildFile("new-boot.img")
            if (bootItem != null) {
                bootItem.file = newBoot
                bootItem.copyTo(outStream as TarArchiveOutputStream)
            } else {
                newBoot.newInputStream().use { it.copyAll(outStream, 1024 * 1024) }
            }
            newBoot.delete()

            console.add("")
            console.add("****************************")
            console.add(" Output file is written to ")
            console.add(" $outFile ")
            console.add("****************************")
        } catch (e: IOException) {
            console.add("! Failed to output to $outFile")
            outFile.delete()
            Timber.e(e)
            return false
        } finally {
            outStream.close()
        }

        // Fix up binaries
        srcBoot.delete()
        "cp_readlink $installDir".sh()

        return true
    }

    private fun processUrl(url: String): Boolean {
        // Download image from url
        try {
            srcBoot = installDir.getChildFile("boot.img")
            ExtractImage(srcBoot, console, logs)
                .consume(DataSourceChannel(ServiceLocator.okhttp, url))
        } catch (e: IOException) {
            console.add("! Error: " + e.message)
            Timber.e(e)
            return false
        }

        // Patch file
        if (!patchBoot()) {
            return false
        }

        // Output file
        val outFile = MediaStoreUtils.getFile("$destName.img")
        try {
            val newBoot = installDir.getChildFile("new-boot.img")
            outFile.uri.outputStream().use { out ->
                FileInputStream(newBoot).use { input ->
                    input.copyTo(out)
                }
            }
            newBoot.delete()

            console.add("")
            console.add("****************************")
            console.add(" Output file is written to ")
            console.add(" $outFile ")
            console.add("****************************")
        } catch (e: IOException) {
            console.add("! Failed to output to $outFile")
            outFile.delete()
            Timber.e(e)
            return false
        }

        // Fix up binaries
        srcBoot.delete()
        "cp_readlink $installDir".sh()

        return true
    }

    private fun patchBoot(): Boolean {
        val newBoot = installDir.getChildFile("new-boot.img")
        if (!useRootDir) {
            // Create output files before hand
            newBoot.createNewFile()
            File(installDir, "stock_boot.img").createNewFile()
        }

        val cmds = arrayOf(
            "cd $installDir",
            "KEEPFORCEENCRYPT=${Config.keepEnc} " +
            "KEEPVERITY=${Config.keepVerity} " +
            "PATCHVBMETAFLAG=${Info.patchBootVbmeta} " +
            "RECOVERYMODE=${Config.recovery} " +
            "LEGACYSAR=${Info.legacySAR} " +
            "sh boot_patch.sh $srcBoot")
        val isSuccess = cmds.sh().isSuccess

        shell.newJob().add("./magiskboot cleanup", "cd /").exec()

        return isSuccess
    }

    private fun flashBoot() = "direct_install $installDir $srcBoot".sh().isSuccess

    private fun postOTA(): Boolean {
        "post_ota".sh()

        console.add("*************************************************************")
        console.add(" Next reboot will boot to second slot!")
        console.add(" Go back to System Updates and press Restart to complete OTA")
        console.add("*************************************************************")
        return true
    }

    private fun Array<String>.eq() = shell.newJob().add(*this).to(console, logs).enqueue()
    private fun String.sh() = shell.newJob().add(this).to(console, logs).exec()
    private fun Array<String>.sh() = shell.newJob().add(*this).to(console, logs).exec()
    private fun String.fsh() = ShellUtils.fastCmd(shell, this)
    private fun Array<String>.fsh() = ShellUtils.fastCmd(shell, *this)

    private fun systemModeTransactionPresent() =
        (
            "[ -e /data/adb/kitsune/system-mode/transaction.env ] || " +
                "[ -L /data/adb/kitsune/system-mode/transaction.env ]"
        ).sh().isSuccess

    private fun systemModeTerminalMarkerPresent() =
        (
            "[ -e /data/adb/.kitsune-system-mode-setup-v1.env ] || " +
                "[ -L /data/adb/.kitsune-system-mode-setup-v1.env ] || " +
                "[ -e /data/adb/.kitsune-system-mode-setup-v1.env.new ] || " +
                "[ -L /data/adb/.kitsune-system-mode-setup-v1.env.new ] || " +
                "[ -e /data/adb/.kitsune-system-mode-rollback-v1.env ] || " +
                "[ -L /data/adb/.kitsune-system-mode-rollback-v1.env ] || " +
                "[ -e /data/adb/.kitsune-system-mode-rollback-v1.env.new ] || " +
                "[ -L /data/adb/.kitsune-system-mode-rollback-v1.env.new ] || " +
                "[ -e /data/adb/.kitsune-system-mode-prior-v1.env ] || " +
                "[ -L /data/adb/.kitsune-system-mode-prior-v1.env ] || " +
                "[ -e /data/adb/.kitsune-system-mode-prior-v1.env.new ] || " +
                "[ -L /data/adb/.kitsune-system-mode-prior-v1.env.new ]"
        ).sh().isSuccess

    private fun systemModeStateStoragePresent() =
        (
            "[ -e /data/adb/.kitsune-system-mode-setup-v1.env ] || " +
                "[ -L /data/adb/.kitsune-system-mode-setup-v1.env ] || " +
                "[ -e /data/adb/.kitsune-system-mode-setup-v1.env.new ] || " +
                "[ -L /data/adb/.kitsune-system-mode-setup-v1.env.new ] || " +
                "[ -e /data/adb/.kitsune-system-mode-rollback-v1.env ] || " +
                "[ -L /data/adb/.kitsune-system-mode-rollback-v1.env ] || " +
                "[ -e /data/adb/.kitsune-system-mode-rollback-v1.env.new ] || " +
                "[ -L /data/adb/.kitsune-system-mode-rollback-v1.env.new ] || " +
                "[ -e /data/adb/.kitsune-system-mode-prior-v1.env ] || " +
                "[ -L /data/adb/.kitsune-system-mode-prior-v1.env ] || " +
                "[ -e /data/adb/.kitsune-system-mode-prior-v1.env.new ] || " +
                "[ -L /data/adb/.kitsune-system-mode-prior-v1.env.new ] || " +
                "[ -L /data/adb/kitsune/system-mode ] || " +
                "{ [ -e /data/adb/kitsune/system-mode ] && " +
                "[ ! -d /data/adb/kitsune/system-mode ]; } || " +
                "[ -L /data/adb/kitsune/system-mode/rollback ] || " +
                "{ [ -e /data/adb/kitsune/system-mode/rollback ] && " +
                "[ ! -d /data/adb/kitsune/system-mode/rollback ]; } || " +
                "find /data/adb/kitsune/system-mode -mindepth 1 -maxdepth 1 " +
                "! -name rollback -print -quit 2>/dev/null | grep -q . || " +
                "find /data/adb/kitsune/system-mode/rollback -mindepth 1 " +
                "-print -quit 2>/dev/null | grep -q ."
        ).sh().isSuccess

    private fun systemModeState() =
        (
            "awk 'index(\$0, \"STATE=\") == 1 { count++; value=substr(\$0, 7) } " +
                "END { if (count == 1) print value; else exit 1 }' " +
                "/data/adb/kitsune/system-mode/transaction.env 2>/dev/null"
        ).fsh()

    private fun hasSystemModeFootprint(): Boolean {
        val command =
            "grep -qx 'SYSTEMMODE=true' /system/etc/init/magisk/config 2>/dev/null || " +
                "grep -qx 'SYSTEMMODE=true' /data/adb/magisk/config 2>/dev/null || " +
                "[ -e /system/etc/init/magisk ] || [ -L /system/etc/init/magisk ] || " +
                "[ -f /system/etc/init/magisk/install-manifest.json ] || " +
                "[ -f /data/adb/kitsune/system-mode/install-manifest.json ] || " +
                "[ -e /system/etc/init/00-kitsune-magisk-rescue.rc ] || " +
                "[ -L /system/etc/init/00-kitsune-magisk-rescue.rc ] || " +
                "[ -e /system/etc/init/hw/00-kitsune-magisk-rescue.rc ] || " +
                "[ -L /system/etc/init/hw/00-kitsune-magisk-rescue.rc ] || " +
                "[ -e /system/etc/init/.kitsune-system-mode-rescue ] || " +
                "[ -L /system/etc/init/.kitsune-system-mode-rescue ] || " +
                "[ -e /system/etc/init/hw/.kitsune-system-mode-rescue ] || " +
                "[ -L /system/etc/init/hw/.kitsune-system-mode-rescue ] || " +
                "[ -L /system/etc/init/magisk.rc ] || " +
                "[ -L /system/etc/init/hw/magisk.rc ] || " +
                "{ [ -f /system/addon.d/99-magisk.sh ] && " +
                "grep -qx 'SYSTEMINSTALL=true' /system/addon.d/99-magisk.sh && " +
                "grep -Fq '/system/etc/init/magisk' /system/addon.d/99-magisk.sh; } || " +
                "[ -e /system/addon.d/magisk ] || [ -L /system/addon.d/magisk ] || " +
                "{ [ -f /system/etc/init/bootanim.rc ] && " +
                "grep -Fq '/system/etc/init/magisk' /system/etc/init/bootanim.rc && " +
                "grep -Fq -- '--setup-sbin' /system/etc/init/bootanim.rc && " +
                "grep -Fq -- '--post-fs-data' /system/etc/init/bootanim.rc; } || " +
                "[ -e /system/etc/init/bootanim.rc.gz ] || " +
                "[ -L /system/etc/init/bootanim.rc.gz ] || " +
                "[ -e /vendor/etc/selinux/precompiled_sepolicy.gz ] || " +
                "[ -L /vendor/etc/selinux/precompiled_sepolicy.gz ] || " +
                "[ -e /odm/etc/selinux/precompiled_sepolicy.gz ] || " +
                "[ -L /odm/etc/selinux/precompiled_sepolicy.gz ] || " +
                "[ -e /system/etc/selinux/precompiled_sepolicy.gz ] || " +
                "[ -L /system/etc/selinux/precompiled_sepolicy.gz ] || " +
                "[ -e /system_root/sepolicy.gz ] || [ -L /system_root/sepolicy.gz ] || " +
                "[ -e /system_root/sepolicy_debug.gz ] || " +
                "[ -L /system_root/sepolicy_debug.gz ] || " +
                "[ -e /system_root/sepolicy.unlocked.gz ] || " +
                "[ -L /system_root/sepolicy.unlocked.gz ] || " +
                "grep -qs '^# KitsuneMagisk System Mode schema ' " +
                "/system/etc/init/magisk.rc /system/etc/init/hw/magisk.rc " +
                "/vendor/etc/init/magisk.rc /odm/etc/init/magisk.rc " +
                "/product/etc/init/magisk.rc /system_ext/etc/init/magisk.rc"
        return command.sh().isSuccess
    }

    private fun systemModeNeedsManagedPath(): Boolean {
        val state = systemModeState()
        val footprint = hasSystemModeFootprint()
        val terminalMarker = systemModeTerminalMarkerPresent()
        val stateStorage = systemModeStateStoragePresent()
        if (state == "UNINSTALLED" && !footprint && !terminalMarker && !stateStorage)
            return false
        return terminalMarker || systemModeTransactionPresent() || stateStorage || footprint
    }

    private fun rejectOrdinaryInstallOverSystemMode(): Boolean {
        if (!systemModeNeedsManagedPath())
            return false
        console.add("! A System Mode installation or recovery is pending")
        console.add("! Use the explicit System Mode action; ordinary install is blocked")
        return true
    }

    protected suspend fun patchFile(file: Uri) = extractFiles() && processFile(file)

    protected suspend fun patchFile(url: String) = extractFiles() && processUrl(url)

    protected suspend fun direct() =
        !rejectOrdinaryInstallOverSystemMode() && findImage() && extractFiles() && patchBoot() && flashBoot()

    protected suspend fun directSystem(consent: String?): Boolean {
        if (!BuildConfig.DEBUG) {
            console.add("! System Mode is disabled in release builds")
            return false
        }
        if (!SystemModeConsent.consume(consent)) {
            console.add("! System Mode confirmation is missing or expired")
            return false
        }
        if (!Info.isRooted || Build.VERSION.SDK_INT < 25) {
            console.add("! System Mode requires bootstrap root on Android 7.1 or newer")
            return false
        }
        if (Info.hasMagiskState && !Info.isSystemMode) {
            console.add("! Ordinary Magisk is already installed")
            console.add("! Use the normal Magisk installation path on this device")
            return false
        }
        if (!extractFiles())
            return false

        return runSystemMode("install")
    }

    private suspend fun runSystemMode(action: String): Boolean {
        systemModeOperation = true
        val result = RootUtils.runSystemMode(action, installDir.path, AppApkPath) { console.add(it) }
        if (result == null) {
            console.add("! Root worker disconnected; restart once to recover the transaction")
            console.add("! Installer files have been retained for recovery")
            return false
        }
        if (!installDir.deleteRecursively())
            console.add("! Could not remove the completed installer working directory")
        return result == 0
    }

    protected suspend fun secondSlot() =
        !rejectOrdinaryInstallOverSystemMode() &&
            findSecondary() && extractFiles() && patchBoot() && flashBoot() && postOTA()

    protected suspend fun fixEnv() =
        !rejectOrdinaryInstallOverSystemMode() && extractFiles() && "fix_env $installDir".sh().isSuccess

    protected fun restore() =
        !rejectOrdinaryInstallOverSystemMode() && findImage() && "restore_imgs $srcBoot".sh().isSuccess

    protected suspend fun recoverSystemMode() = extractFiles() && runSystemMode("recover")

    protected suspend fun uninstall(): Boolean {
        val state = systemModeState()
        val footprint = hasSystemModeFootprint()
        val terminalMarker = systemModeTerminalMarkerPresent()
        val transaction = systemModeTransactionPresent()
        val stateStorage = systemModeStateStoragePresent()
        if (state == "UNINSTALLED" && !footprint && !terminalMarker && !stateStorage)
            return "run_uninstaller $AppApkPath".sh().isSuccess
        if (!terminalMarker && !transaction && !stateStorage && !footprint)
            return "run_uninstaller $AppApkPath".sh().isSuccess
        if (state !in systemModeStates) {
            console.add("! System Mode artifacts exist without a valid transaction state")
            console.add("! Ordinary uninstall is blocked; use verified recovery")
            return false
        }
        if (!extractFiles())
            return false

        return runSystemMode("uninstall")
    }

    @WorkerThread
    protected abstract suspend fun operations(): Boolean

    open suspend fun exec(): Boolean {
        if (!haveActiveSession.compareAndSet(false, true))
            return false
        var success = false
        return try {
            success = withContext(Dispatchers.IO) { operations() }
            success
        } finally {
            try {
                if (!success && ::installDir.isInitialized && !systemModeOperation) {
                    withContext(kotlinx.coroutines.NonCancellable + Dispatchers.IO) {
                        Shell.cmd("rm -rf \"$installDir\"").exec()
                    }
                }
            } finally {
                haveActiveSession.set(false)
            }
        }
    }

    companion object {
        private var haveActiveSession = AtomicBoolean(false)
        private val systemModeStates = setOf(
            "PREFLIGHTED",
            "STAGED",
            "COMMITTED",
            "BOOT_VERIFIED",
            "UNINSTALLED",
            "ROLLBACK_REQUIRED",
            "ROLLING_BACK",
            "FAILED",
        )
    }
}

abstract class ConsoleInstaller(
    console: MutableList<String>,
    logs: MutableList<String>
) : MagiskInstallImpl(console, logs) {
    override suspend fun exec(): Boolean {
        val success = super.exec()
        if (success) {
            console.add("- All done!")
        } else {
            console.add("! Installation failed")
        }
        return success
    }
}

abstract class CallBackInstaller : MagiskInstallImpl(DummyList, DummyList) {
    suspend fun exec(callback: (Boolean) -> Unit): Boolean {
        val success = exec()
        callback(success)
        return success
    }
}

class MagiskInstaller {

    class Patch(
        private val uri: Uri,
        console: MutableList<String>,
        logs: MutableList<String>
    ) : ConsoleInstaller(console, logs) {
        override suspend fun operations() = patchFile(uri)
    }

    class Download(
        private val url: String,
        console: MutableList<String>,
        logs: MutableList<String>
    ) : ConsoleInstaller(console, logs) {
        override suspend fun operations() = patchFile(url)
    }

    class SecondSlot(
        console: MutableList<String>,
        logs: MutableList<String>
    ) : ConsoleInstaller(console, logs) {
        override suspend fun operations() = secondSlot()
    }

    class Direct(
        console: MutableList<String>,
        logs: MutableList<String>
    ) : ConsoleInstaller(console, logs) {
        override suspend fun operations() = direct()
    }

    class SystemMode(
        private val consent: String?,
        console: MutableList<String>,
        logs: MutableList<String>
    ) : ConsoleInstaller(console, logs) {
        override suspend fun operations() = directSystem(consent)
    }

    class Emulator(
        console: MutableList<String>,
        logs: MutableList<String>
    ) : ConsoleInstaller(console, logs) {
        override suspend fun operations() = fixEnv()
    }

    class SystemModeRecovery(
        console: MutableList<String>,
        logs: MutableList<String>
    ) : ConsoleInstaller(console, logs) {
        override suspend fun operations() = recoverSystemMode()
    }

    class Uninstall(
        console: MutableList<String>,
        logs: MutableList<String>
    ) : ConsoleInstaller(console, logs) {
        override suspend fun operations() = uninstall()

        override suspend fun exec(): Boolean {
            val success = super.exec()
            if (success) {
                UiThreadHandler.handler.postDelayed(3000) {
                    if (systemModeOperation)
                        RootUtils.uninstallSelf()
                    else
                        Shell.cmd("pm uninstall ${context.packageName}").exec()
                }
            }
            return success
        }
    }

    class Restore : CallBackInstaller() {
        override suspend fun operations() = restore()
    }

    class FixEnv : CallBackInstaller() {
        override suspend fun operations() = fixEnv()
    }
}
