package com.topjohnwu.magisk.core.tasks

import android.app.Activity
import android.app.ActivityOptions
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.widget.Toast
import com.topjohnwu.magisk.StubApk
import com.topjohnwu.magisk.core.AppApkPath
import com.topjohnwu.magisk.core.BuildConfig.APP_PACKAGE_NAME
import com.topjohnwu.magisk.core.Config
import com.topjohnwu.magisk.core.Const
import com.topjohnwu.magisk.core.R
import com.topjohnwu.magisk.core.ktx.await
import com.topjohnwu.magisk.core.ktx.toast
import com.topjohnwu.magisk.core.ktx.writeTo
import com.topjohnwu.magisk.core.signing.JarMap
import com.topjohnwu.magisk.core.signing.SignApk
import com.topjohnwu.magisk.core.utils.AXML
import com.topjohnwu.magisk.core.utils.Keygen
import com.topjohnwu.magisk.utils.APKInstall
import com.topjohnwu.superuser.Shell
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.runInterruptible
import timber.log.Timber
import java.io.File
import java.io.IOException
import java.io.OutputStream
import java.security.SecureRandom
import kotlin.random.asKotlinRandom

object AppMigration {

    private const val ALPHA = "abcdefghijklmnopqrstuvwxyz"
    private const val ALPHADOTS = "$ALPHA....."
    private const val ANDROID_MANIFEST = "AndroidManifest.xml"
    private const val TEST_PKG_NAME = "$APP_PACKAGE_NAME.test"

    // Some arbitrary limit
    const val MAX_LABEL_LENGTH = 32
    const val PLACEHOLDER = "COMPONENT_PLACEHOLDER"

    private fun genPackageName(): String {
        val random = SecureRandom()
        val len = 5 + random.nextInt(15)
        val builder = StringBuilder(len)
        var next: Char
        var prev = 0.toChar()
        for (i in 0 until len) {
            next = if (prev == '.' || i == 0 || i == len - 1) {
                ALPHA[random.nextInt(ALPHA.length)]
            } else {
                ALPHADOTS[random.nextInt(ALPHADOTS.length)]
            }
            builder.append(next)
            prev = next
        }
        if (!builder.contains('.')) {
            // Pick a random index and set it as dot
            val idx = random.nextInt(len - 2)
            builder[idx + 1] = '.'
        }
        return "com.$builder"
    }

    private fun classNameGenerator() = sequence {
        val c1 = mutableListOf<String>()
        val c2 = mutableListOf<String>()
        val c3 = mutableListOf<String>()
        val random = SecureRandom()
        val kRandom = random.asKotlinRandom()

        fun <T> chain(vararg iters: Iterable<T>) = sequence {
            iters.forEach { it.forEach { v -> yield(v) } }
        }

        for (a in chain('a'..'z', 'A'..'Z')) {
            if (a != 'a' && a != 'A') {
                c1.add("$a")
            }
            for (b in chain('a'..'z', 'A'..'Z', '0'..'9')) {
                c2.add("$a$b")
                for (c in chain('a'..'z', 'A'..'Z', '0'..'9')) {
                    c3.add("$a$b$c")
                }
            }
        }

        c1.shuffle(random)
        c2.shuffle(random)
        c3.shuffle(random)

        fun notJavaKeyword(name: String) = when (name) {
            "do", "if", "for", "int", "new", "try" -> false
            else -> true
        }

        fun List<String>.process() = asSequence().filter(::notJavaKeyword)

        val names = mutableListOf<String>()
        names.addAll(c1)
        names.addAll(c2.process().take(30))
        names.addAll(c3.process().take(30))

        while (true) {
            val seg = 2 + random.nextInt(4)
            val cls = StringBuilder()
            for (i in 0 until seg) {
                cls.append(names.random(kRandom))
                if (i != seg - 1)
                    cls.append('.')
            }
            // Old Android does not support capitalized package names
            // Check Android 7.0.0 PackageParser#buildClassName
            cls[0] = cls[0].lowercaseChar()
            yield(cls.toString())
        }
    }.distinct().iterator()

    private fun patch(
        context: Context,
        apk: File, out: OutputStream,
        pkg: String, label: CharSequence
    ): Boolean {
        val pm = context.packageManager
        val info = pm.getPackageArchiveInfo(apk.path, 0)?.applicationInfo ?: return false
        val origLabel = info.nonLocalizedLabel.toString()
        try {
            JarMap.open(apk, true).use { jar ->
                val je = jar.getJarEntry(ANDROID_MANIFEST)
                val xml = AXML(jar.getRawData(je))
                val generator = classNameGenerator()
                val p = xml.patchStrings {
                    when {
                        it.contains(APP_PACKAGE_NAME) -> it.replace(APP_PACKAGE_NAME, pkg)
                        it.contains(PLACEHOLDER) -> generator.next()
                        it == origLabel -> label.toString()
                        else -> it
                    }
                }
                if (!p) return false

                // Write apk changes
                jar.getOutputStream(je).use { it.write(xml.bytes) }
                val keys = Keygen()
                SignApk.sign(keys.cert, keys.key, jar, out, context.cacheDir)
                return true
            }
        } catch (e: Exception) {
            Timber.e(e)
            return false
        }
    }

    private fun patchTest(apk: File, out: File, pkg: String): Boolean {
        try {
            JarMap.open(apk, true).use { jar ->
                val je = jar.getJarEntry(ANDROID_MANIFEST)
                val xml = AXML(jar.getRawData(je))
                val p = xml.patchStrings {
                    when (it) {
                        APP_PACKAGE_NAME -> pkg
                        TEST_PKG_NAME -> "$pkg.test"
                        else -> it
                    }
                }
                if (!p) return false

                // Write apk changes
                jar.getOutputStream(je).use { it.write(xml.bytes) }
                val keys = Keygen()
                out.outputStream().use { SignApk.sign(keys.cert, keys.key, jar, it, out.parentFile) }
                return true
            }
        } catch (e: Exception) {
            Timber.e(e)
            return false
        }
    }

    private fun launchApp(context: Context, pkg: String, token: String? = null): Boolean {
        val intent = context.packageManager.getLaunchIntentForPackage(pkg) ?: return false
        if (token != null) {
            intent.putExtra(Const.Key.PREV_CONFIG, Config.toBundle())
            intent.putExtra(ManagerMigration.EXTRA_TOKEN, token)
        }
        val options = ActivityOptions.makeBasic()
        if (Build.VERSION.SDK_INT >= 34) {
            options.setShareIdentityEnabled(true)
        }
        try {
            context.startActivity(intent, options.toBundle())
        } catch (e: RuntimeException) {
            Timber.e(e)
            return false
        }
        if (context is Activity) {
            context.finish()
        }
        return true
    }

    private suspend fun launchMigration(context: Context, pkg: String): Boolean {
        val token = ManagerMigration.prepare(context, pkg)
        if (launchApp(context, pkg, token.nonce)) return true
        ManagerMigration.discard(token)
        return false
    }

    suspend fun patchAndHide(context: Context, label: String, pkg: String? = null): Boolean {
        val stub = File(context.cacheDir, "stub.apk")
        try {
            context.assets.open("stub.apk").writeTo(stub)
        } catch (e: IOException) {
            Timber.e(e)
            return false
        }

        // Generate a new random signature and package name if needed
        val pkg = pkg ?: genPackageName()
        Config.keyStoreRaw = ""

        // Check and patch the test APK
        try {
            val info = context.packageManager.getApplicationInfo(TEST_PKG_NAME, 0)
            val testApk = File(info.sourceDir)
            val testRepack = File(context.cacheDir, "test.apk")
            if (!patchTest(testApk, testRepack, pkg))
                return false
            val cmd = "adb_pm_install $testRepack $pkg.test"
            if (!Shell.cmd(cmd).exec().isSuccess)
                return false
        } catch (e: PackageManager.NameNotFoundException) {
        }

        val repack = File(context.cacheDir, "patched.apk")
        repack.outputStream().use {
            if (!patch(context, stub, it, pkg, label))
                return false
        }

        // Install and auto launch app
        val cmd = "adb_pm_install $repack $pkg"
        if (Shell.cmd(cmd).exec().isSuccess) {
            val previous = Config.suManager
            Config.suManager = pkg
            Shell.cmd("touch $AppApkPath").exec()
            var launched = false
            try {
                launched = launchMigration(context, pkg)
                return launched
            } finally {
                if (!launched) Config.suManager = previous
            }
        } else {
            return false
        }
    }

    @Suppress("DEPRECATION")
    suspend fun hide(activity: Activity, label: String) {
        val dialog = android.app.ProgressDialog(activity).apply {
            setTitle(activity.getString(R.string.hide_app_title))
            isIndeterminate = true
            setCancelable(false)
            show()
        }
        val success = withContext(Dispatchers.IO) {
            try { patchAndHide(activity, label) } catch (e: IOException) {
                Timber.e(e)
                false
            }
        }
        if (!success) {
            dialog.dismiss()
            activity.toast(R.string.failure, Toast.LENGTH_LONG)
        }
    }

    @Suppress("DEPRECATION")
    suspend fun restoreWithoutRoot(context: Context): Boolean = withContext(Dispatchers.IO) {
        val hiddenPackage = context.applicationContext.packageName
        if (hiddenPackage == APP_PACKAGE_NAME) return@withContext false
        val completed = java.util.concurrent.atomic.AtomicBoolean(false)
        val apk = StubApk.current(context)
        val session = APKInstall.startSession(context, APP_PACKAGE_NAME,
            { context.toast(R.string.failure, Toast.LENGTH_LONG) },
            {
                val verified = runCatching {
                    val flags = PackageManager.GET_SIGNATURES
                    val expected = context.packageManager.getPackageArchiveInfo(apk.path, flags)?.signatures
                    val actual = context.packageManager.getPackageInfo(APP_PACKAGE_NAME, flags).signatures
                    expected?.size == 1 && actual?.size == 1 && expected[0] == actual[0]
                }.getOrDefault(false)
                if (verified && launchApp(context, APP_PACKAGE_NAME)) {
                    completed.set(true)
                    // Android owns both confirmations when no privileged installer exists.
                    runCatching {
                        context.startActivity(Intent(Intent.ACTION_DELETE,
                            android.net.Uri.fromParts("package", hiddenPackage, null))
                            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                    }.onFailure {
                        context.toast(R.string.hidden_cleanup, Toast.LENGTH_LONG)
                        runCatching {
                            context.startActivity(Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                                android.net.Uri.fromParts("package", hiddenPackage, null))
                                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
                        }
                    }
                } else {
                    context.toast(R.string.failure, Toast.LENGTH_LONG)
                }
            })
        try {
            session.openStream(context).use { out ->
                apk.inputStream().use { input -> input.copyTo(out) }
            }
            val action = session.waitIntent()
            if (action != null) {
                withContext(Dispatchers.Main) { context.startActivity(action) }
                true
            } else completed.get()
        } catch (e: IOException) {
            Timber.e(e)
            false
        } catch (e: SecurityException) {
            Timber.e(e)
            false
        } catch (e: android.content.ActivityNotFoundException) {
            Timber.e(e)
            false
        }
    }

    suspend fun restoreApp(context: Context): Boolean = try {
        restoreWithRoot(context)
    } catch (e: IOException) {
        Timber.e(e)
        false
    }

    private suspend fun restoreWithRoot(context: Context): Boolean {
        val apk = StubApk.current(context)
        val cmd = "adb_pm_install $apk $APP_PACKAGE_NAME"
        if (Shell.cmd(cmd).await().isSuccess) {
            val previous = Config.suManager
            Config.suManager = ""
            Shell.cmd("touch $AppApkPath").exec()
            var launched = false
            try {
                launched = launchMigration(context, APP_PACKAGE_NAME)
                return launched
            } finally {
                if (!launched) Config.suManager = previous
            }
        }
        return false
    }

    @Suppress("DEPRECATION")
    suspend fun restore(activity: Activity) {
        val dialog = android.app.ProgressDialog(activity).apply {
            setTitle(activity.getString(R.string.restore_img_msg))
            isIndeterminate = true
            setCancelable(false)
            show()
        }
        if (!restoreApp(activity)) {
            activity.toast(R.string.failure, Toast.LENGTH_LONG)
        }
        dialog.dismiss()
    }

    suspend fun upgradeStub(context: Context, apk: File): Intent? {
        val label = context.applicationInfo.nonLocalizedLabel
        val pkg = context.packageName
        val session = APKInstall.startSession(context)
        return runInterruptible(Dispatchers.IO) {
            session.openStream(context).use {
                if (!patch(context, apk, it, pkg, label)) {
                    return@runInterruptible null
                }
            }
            session.waitIntent()
        }
    }
}
