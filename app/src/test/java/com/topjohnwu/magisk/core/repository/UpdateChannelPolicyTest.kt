package com.topjohnwu.magisk.core.repository

import com.topjohnwu.magisk.core.Config
import com.topjohnwu.magisk.core.di.enforceUpdateTransportPolicy
import com.topjohnwu.magisk.core.model.MagiskJson
import com.topjohnwu.magisk.core.model.UpdateInfo
import com.topjohnwu.magisk.utils.APKInstall
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.security.MessageDigest
import java.io.File

class UpdateChannelPolicyTest {

    @Test
    fun `all inherited built-in channels fail closed`() {
        val channels = listOf(
            Config.Value.DEFAULT_CHANNEL,
            Config.Value.STABLE_CHANNEL,
            Config.Value.BETA_CHANNEL,
            Config.Value.CANARY_CHANNEL,
            Config.Value.DEBUG_CHANNEL,
        )
        channels.forEach { channel ->
            val result = UpdateChannelPolicy.resolve(channel, "")
            assertEquals(
                UpdateEndpointResolution.Unavailable(
                    UpdateUnavailableReason.PROJECT_SERVICE_NOT_CONFIGURED
                ),
                result
            )
        }
    }

    @Test
    fun `custom channel requires a non-empty HTTPS URL`() {
        val empty = UpdateChannelPolicy.resolve(Config.Value.CUSTOM_CHANNEL, "  ")
        assertEquals(
            UpdateEndpointResolution.Unavailable(UpdateUnavailableReason.EMPTY_CUSTOM_URL),
            empty
        )

        listOf(
            "http://updates.example.test/channel.json",
            "file:///tmp/channel.json",
            "//updates.example.test/channel.json",
            "https:///channel.json",
            "https://user:password@updates.example.test/channel.json",
            "https://updates.example.test/channel.json#fragment",
            "https://updates.example.test:bad/channel.json",
            "https://updates.example.test:0/channel.json",
            "https://updates.example.test:65536/channel.json",
            "https://[::1/channel.json",
            "https://updates.example.test/line\nbreak",
            "not a url",
        ).forEach { url ->
            assertEquals(
                UpdateEndpointResolution.Unavailable(UpdateUnavailableReason.INVALID_CUSTOM_URL),
                UpdateChannelPolicy.resolve(Config.Value.CUSTOM_CHANNEL, url)
            )
        }
    }

    @Test
    fun `valid custom HTTPS URL remains available`() {
        listOf(
            " https://updates.example.test/kitsune.json " to
                "https://updates.example.test/kitsune.json",
            "HTTPS://updates.example.test:443/kitsune.json?channel=custom" to
                "HTTPS://updates.example.test:443/kitsune.json?channel=custom",
        ).forEach { (input, expected) ->
            val result = UpdateChannelPolicy.resolve(Config.Value.CUSTOM_CHANNEL, input)
            assertTrue(result is UpdateEndpointResolution.Remote)
            assertEquals(expected, (result as UpdateEndpointResolution.Remote).url)
        }
    }

    @Test
    fun `transport configuration follows same-scheme redirects but rejects transitions`() {
        val client = OkHttpClient.Builder().enforceUpdateTransportPolicy().build()
        assertTrue(client.followRedirects)
        assertFalse(client.followSslRedirects)
    }

    @Test
    fun `update metadata requires HTTPS artifact URLs and a SHA-256 digest`() {
        val valid = UpdateInfo(
            MagiskJson(
                version = "31.0",
                versionCode = 31000,
                link = "https://updates.example.test/app.apk",
                note = "https://updates.example.test/notes.md",
                sha256 = "a".repeat(64),
            )
        )
        assertNull(UpdateChannelPolicy.validate(valid))

        listOf(
            valid.copy(magisk = valid.magisk.copy(version = "")),
            valid.copy(magisk = valid.magisk.copy(versionCode = 0)),
            valid.copy(magisk = valid.magisk.copy(link = "http://updates.example.test/app.apk")),
            valid.copy(magisk = valid.magisk.copy(note = "file:///tmp/notes.md")),
            valid.copy(magisk = valid.magisk.copy(sha256 = "")),
            valid.copy(magisk = valid.magisk.copy(sha256 = "g".repeat(64))),
        ).forEach { metadata ->
            assertEquals(
                UpdateUnavailableReason.INVALID_UPDATE_METADATA,
                UpdateChannelPolicy.validate(metadata)
            )
        }
    }

    @Test
    fun `artifact digest comparison accepts exact content only`() {
        val content = "verified update".toByteArray()
        val digest = MessageDigest.getInstance("SHA-256").digest(content)
        val expected = digest.joinToString("") { "%02x".format(it) }

        assertTrue(UpdateChannelPolicy.matchesSha256(expected.uppercase(), digest))
        assertFalse(UpdateChannelPolicy.matchesSha256("0".repeat(64), digest))
        assertFalse(UpdateChannelPolicy.matchesSha256("invalid", digest))
        assertFalse(UpdateChannelPolicy.matchesSha256(expected, digest.copyOf(31)))

        val file = File.createTempFile("kitsune-digest-", ".apk")
        try {
            file.writeBytes(content)
            assertTrue(APKInstall.matchesSha256(file, expected.uppercase()))
            assertFalse(APKInstall.matchesSha256(file, "0".repeat(64)))
            assertFalse(APKInstall.matchesSha256(file, "invalid"))
        } finally {
            file.delete()
        }
    }
}
