package com.topjohnwu.magisk.test

import android.app.Notification
import android.content.Context
import android.content.ContextWrapper
import android.os.Build
import androidx.annotation.Keep
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.topjohnwu.magisk.update.UpdateClient
import com.topjohnwu.magisk.update.UpdateFiles
import com.topjohnwu.magisk.update.UpdateMetadata
import com.topjohnwu.magisk.core.download.DownloadEngine
import com.topjohnwu.magisk.core.download.DownloadSession
import com.topjohnwu.magisk.core.download.Subject
import com.topjohnwu.magisk.core.model.UpdateInfo
import com.topjohnwu.magisk.utils.APKInstall
import com.topjohnwu.magisk.view.Notifications
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.runInterruptible
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.io.ByteArrayInputStream
import java.io.File
import java.io.FileInputStream
import java.io.FilterInputStream
import java.io.IOException
import java.io.InputStream
import java.io.InterruptedIOException
import java.io.SequenceInputStream
import java.net.HttpURLConnection
import java.net.URL
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicLong

@Keep
@RunWith(AndroidJUnit4::class)
class UpdateServiceTest : BaseTest {
    private lateinit var root: File
    private lateinit var fixtures: File
    private lateinit var isolated: Context
    private lateinit var trust: Map<String, Any>
    private lateinit var metadata: UpdateMetadata
    private lateinit var now: AtomicLong

    private class Session(override val context: Context) : DownloadSession {
        val attached = AtomicInteger()
        val finished = CountDownLatch(1)
        override fun attachNotification(id: Int, builder: Notification.Builder) { attached.incrementAndGet() }
        override fun onDownloadComplete() { finished.countDown() }
    }

    @Before
    fun prepare() {
        fixtures = File(InstrumentationRegistry.getArguments().getString("updateFixtures")
            ?: "/data/local/tmp/kitsune-update-fixtures")
        assertTrue("Generate and push the signed PR10 fixtures before testing", File(fixtures, "valid.json").isFile)
        root = File(appContext.cacheDir, "update-test-${UUID.randomUUID()}")
        assertTrue(root.mkdirs())
        isolated = object : ContextWrapper(appContext) {
            override fun getFilesDir() = File(root, "files").apply { mkdirs() }
            override fun createDeviceProtectedStorageContext(): Context = this
        }
        trust = UpdateMetadata.parse(File(fixtures, "trust.json").readText())
        val payload = JSONObject(String(android.util.Base64.decode(
            JSONObject(envelope("valid")).getString("payload"), android.util.Base64.NO_WRAP)))
        now = AtomicLong(payload.getLong("issued_at") + 1)
        metadata = client().accept(envelope("valid"), "beta")
    }

    @After
    fun cleanup() { if (::root.isInitialized) root.deleteRecursively() }

    private fun envelope(name: String) = File(fixtures, "$name.json").readText()

    private fun client(mode: String = "success", apk: String = "manager.apk", fresh: Boolean = false): UpdateClient {
        val context = if (!fresh) isolated else object : ContextWrapper(appContext) {
            private val location = File(root, UUID.randomUUID().toString())
            override fun getFilesDir() = location.apply { mkdirs() }
            override fun createDeviceProtectedStorageContext(): Context = this
        }
        val file = File(fixtures, apk)
        return UpdateClient(context, trust, trust["certificate_sha256"] as String, 31001, { url ->
            object : HttpURLConnection(url) {
                override fun connect() {}
                override fun disconnect() {}
                override fun usingProxy() = false
                override fun getResponseCode(): Int = when {
                    mode == "unavailable" -> 503
                    mode.startsWith("redirect") && url.host == "github.com" -> 302
                    else -> 200
                }
                override fun getHeaderField(name: String?): String? = if (name == "Location") when (mode) {
                    "redirect-http" -> "http://github.com/unsafe.apk"
                    "redirect-host" -> "https://untrusted.invalid/manager.apk"
                    "redirect-userinfo" -> "https://user:pass@release-assets.githubusercontent.com/manager.apk"
                    "redirect-loop" -> url.toString()
                    else -> "https://release-assets.githubusercontent.com/manager.apk?fixture=1"
                } else null
                override fun getContentEncoding(): String? = if (mode == "encoding") "gzip" else null
                override fun getContentLength(): Int = when (mode) {
                    "no-length" -> -1
                    "length" -> file.length().toInt() + 1
                    else -> file.length().toInt()
                }
                override fun getInputStream(): InputStream {
                    if (url.path.endsWith(".json")) return ByteArrayInputStream(envelope("valid").toByteArray())
                    val input = FileInputStream(file)
                    if (mode == "extra") return SequenceInputStream(input, ByteArrayInputStream(byteArrayOf(1)))
                    return object : FilterInputStream(input) {
                        var count = 0
                        override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
                            if (mode == "truncated" && count >= 65536) return -1
                            if (mode == "interrupted" && count >= 65536) throw IOException("injected interruption")
                            return super.read(buffer, offset, length).also { if (it > 0) count += it }
                        }
                    }
                }
            }
        }, { now.get() })
    }

    private fun rejects(block: () -> Unit) {
        try { block(); fail("Untrusted update was accepted") } catch (_: IOException) {}
    }

    private fun failedDownload(mode: String = "success", fixture: String = "valid", apk: String = "manager.apk") {
        val target = File(root, "manager.apk").apply { writeText("previous verified manager") }
        val client = client(mode, apk, fresh = true)
        val candidate = client.accept(envelope(fixture), "beta")
        rejects { client.download(candidate, target, null) }
        assertEquals("previous verified manager", target.readText())
        assertFalse(root.listFiles()!!.any { it.name.endsWith(".part") })
    }

    @Test fun validDownloadMatchesExactCandidate() {
        val target = File(root, "manager.apk")
        client().download(metadata, target, null)
        assertEquals(metadata.manager.sha256, UpdateFiles.digest(target))
        assertEquals(metadata.manager.size, target.length())
    }

    @Test fun chunkedDownloadUsesSignedLength() {
        val target = File(root, "manager.apk")
        client("no-length").download(metadata, target, null)
        assertEquals(metadata.manager.sha256, UpdateFiles.digest(target))
    }

    @Test fun releaseAssetRedirectIsAllowed() {
        val target = File(root, "manager.apk")
        client("redirect-allowed").download(metadata, target, null)
        assertEquals(metadata.manager.sha256, UpdateFiles.digest(target))
    }

    @Test fun rejectsTransportFailuresWithoutReplacingCurrent() {
        for (mode in listOf("truncated", "interrupted", "length", "extra", "encoding",
            "redirect-http", "redirect-host", "redirect-userinfo", "redirect-loop", "unavailable")) {
            failedDownload(mode)
        }
    }

    @Test fun rejectsTamperedDigestAndSize() {
        failedDownload(fixture = "wrong-digest")
        failedDownload(fixture = "wrong-length")
        if (Build.VERSION.SDK_INT >= 24) failedDownload(fixture = "wrong-min-sdk")
    }

    @Test fun rejectsWrongApkPackageAndSigner() {
        failedDownload(fixture = "wrong-package", apk = "wrong-package.apk")
        failedDownload(fixture = "wrong-signer", apk = "wrong-signer.apk")
    }

    @Test fun rejectsSignedMetadataFaults() {
        for (name in listOf("expired", "future", "wrong-package-name", "wrong-certificate", "duplicate", "incompatible-android"))
            rejects { client().accept(envelope(name), "beta") }
        rejects { client().accept(envelope("valid"), "stable") }
        rejects { client().accept("{}", "beta") }
        rejects { client().accept(envelope("valid") + "{}", "beta") }
        val tampered = JSONObject(envelope("valid")).put("signature", "AAAA").toString()
        rejects { client().accept(tampered, "beta") }
    }

    @Test fun rejectsReplayAndChannelDowngrade() {
        rejects { client().accept(envelope("replay"), "beta") }
        rejects { client().accept(envelope("equivocation"), "beta") }
        rejects { client().accept(envelope("downgrade"), "stable") }
        rejects { client().accept(envelope("wrong-digest"), "beta") }
    }

    @Test fun acceptsSameMetadataAfterClientRestart() {
        assertEquals(metadata.payloadHash, client().accept(envelope("valid"), "beta").payloadHash)
    }

    @Test fun rejectsClockRollbackAndExpiry() {
        now.set(metadata.issuedAt + 1000)
        client().accept(envelope("valid"), "beta")
        now.set(metadata.issuedAt + 1)
        rejects { client().accept(envelope("valid"), "beta") }
        now.set(metadata.expiresAt)
        rejects { client().accept(envelope("valid"), "beta") }
    }

    @Test fun expiryDuringTransferPreservesCurrent() {
        val target = File(root, "manager.apk").apply { writeText("previous") }
        rejects { client().download(metadata, target) { _, _ -> now.set(metadata.expiresAt) } }
        assertEquals("previous", target.readText())
    }

    @Test fun cancellationPreservesCurrent() {
        val target = File(root, "manager.apk").apply { writeText("previous") }
        rejects { client().download(metadata, target) { _, _ -> throw InterruptedIOException("cancelled") } }
        assertEquals("previous", target.readText())
    }

    @Test fun coroutineCancellationInterruptsTransfer() = runBlocking {
        val target = File(root, "manager.apk").apply { writeText("previous") }
        val started = CountDownLatch(1)
        val blocked = CountDownLatch(1)
        val transfer = launch(Dispatchers.IO) {
            runInterruptible {
                client().download(metadata, target) { _, _ ->
                    started.countDown()
                    blocked.await(10, TimeUnit.SECONDS)
                }
            }
        }
        try {
            assertTrue("Transfer did not start", started.await(5, TimeUnit.SECONDS))
            transfer.cancelAndJoin()
            assertEquals("previous", target.readText())
            assertFalse(root.listFiles()!!.any { it.name.endsWith(".part") })
        } finally {
            transfer.cancelAndJoin()
            blocked.countDown()
        }
    }

    @Test fun rejectedUpdateFinishesDownloadSession() {
        val before = Notifications.mgr.activeNotifications.map { it.id }.toSet()
        val session = Session(isolated)
        val engine = DownloadEngine(session)
        val subject = Subject.App(UpdateInfo(signedMetadata = "{}", channel = "beta"))
        try {
            engine.download(subject)
            assertTrue("Failed update left the session running", session.finished.await(5, TimeUnit.SECONDS))
            assertEquals(1, session.attached.get())
            assertNull(subject.intent)
        } finally {
            engine.cancel()
            Notifications.mgr.activeNotifications.filter { it.id !in before }
                .forEach { Notifications.mgr.cancel(it.id) }
        }
    }

    @Test fun stoppedDownloadSessionCannotRestart() {
        val session = Session(isolated)
        val engine = DownloadEngine(session)
        engine.notifyUpdate(Notifications.nextId()) {}
        assertEquals(1, session.attached.get())
        engine.cancel()
        engine.reattach()
        engine.download(Subject.App(UpdateInfo(signedMetadata = "{}", channel = "beta")))
        assertEquals(1, session.attached.get())
    }

    @Test fun interruptedInstallAbandonsSession() {
        val installer = appContext.packageManager.packageInstaller
        val before = installer.mySessions.map { it.sessionId }.toSet()
        val stream = APKInstall.startSession(appContext).openStream(appContext)
        stream.write(byteArrayOf(1, 2, 3))
        try {
            Thread.currentThread().interrupt()
            rejects { stream.close() }
        } finally {
            Thread.interrupted()
        }
        val deadline = android.os.SystemClock.elapsedRealtime() + 2000
        while (installer.mySessions.any { it.sessionId !in before } &&
            android.os.SystemClock.elapsedRealtime() < deadline) android.os.SystemClock.sleep(20)
        assertEquals(before, installer.mySessions.map { it.sessionId }.toSet())
    }

    @Test fun corruptStateIsNotReset() {
        val state = File(isolated.filesDir, "updates/state.json")
        val saved = state.readText()
        val invalid = mutableListOf("{}", JSONObject(saved).put("unexpected", 0).toString())
        for (name in JSONObject(saved).keys()) {
            val changed = JSONObject(saved)
            changed.remove(name)
            invalid += changed.toString()
            invalid += changed.put("unexpected", 0).toString()
        }
        assertTrue(state.setWritable(true))
        for (value in invalid) {
            state.writeText(value)
            rejects { client().accept(envelope("valid"), "beta") }
            assertEquals(value, state.readText())
        }
    }

    @Test fun metadataServiceUnavailableDoesNotFallBack() {
        rejects { client("unavailable").fetch("https://raw.githubusercontent.com/fixture/beta.json", "beta") }
    }

    @Test fun concurrentClientsCreateStateDirectory() {
        val workers = Executors.newFixedThreadPool(8)
        try {
            repeat(32) {
                assertTrue(File(isolated.filesDir, "updates").deleteRecursively())
                val ready = CountDownLatch(8)
                val start = CountDownLatch(1)
                val clients = List(8) {
                    workers.submit<UpdateClient> {
                        ready.countDown()
                        assertTrue(start.await(10, TimeUnit.SECONDS))
                        client()
                    }
                }
                assertTrue(ready.await(10, TimeUnit.SECONDS))
                start.countDown()
                clients.forEach { assertNotNull(it.get(10, TimeUnit.SECONDS)) }
                assertTrue(File(isolated.filesDir, "updates").isDirectory)
            }
        } finally {
            workers.shutdownNow()
            assertTrue(workers.awaitTermination(10, TimeUnit.SECONDS))
        }
    }

    @Test fun verifiesCurrentManagerBeforeDynamicLoading() {
        val target = File(root, "current.apk")
        File(fixtures, "manager.apk").copyTo(target)
        UpdateFiles.verifyManager(appContext, target, metadata.fields["application_id"] as String,
            31001, trust["certificate_sha256"] as String, null)
        java.io.RandomAccessFile(target, "rw").use { file ->
            file.seek(4096)
            val value = file.readByte().toInt()
            file.seek(4096)
            file.writeByte(value xor 1)
        }
        rejects { UpdateFiles.verifyManager(appContext, target, metadata.fields["application_id"] as String,
            31001, trust["certificate_sha256"] as String, null) }
    }

    @Test fun unknownUtf8AndDuplicateFieldsAreRejected() {
        rejects { UpdateMetadata.utf8(byteArrayOf(0xff.toByte())) }
        rejects { UpdateMetadata.parse("{\"schema\":1,\"schema\":1}") }
        rejects { UpdateMetadata.parse("{\"number\":1.5}") }
        rejects { UpdateClient.channelForUrl("http://example.invalid/beta.json") }
        rejects { UpdateClient.channelForUrl("https://example.invalid/unsigned.json") }
        assertEquals("beta", UpdateClient.channelForUrl("https://example.invalid/beta.json"))
        rejects { UpdateFiles.read(ByteArrayInputStream(byteArrayOf(1)), 16, 0) }
    }
}
