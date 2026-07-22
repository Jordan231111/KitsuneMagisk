package com.topjohnwu.magisk.core.repository

import com.topjohnwu.magisk.core.Config
import com.topjohnwu.magisk.core.di.enforceUpdateTransportPolicy
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.net.URI
import kotlin.random.Random

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
    fun `seeded URL property corpus agrees with the fail-closed contract`() {
        val random = Random(0x4B175A)
        val alphabet = (
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" +
                ":/?#[]@!$&'()*+,;=%._- \\\t\n"
            ).toCharArray()
        repeat(2_000) {
            val candidate = buildString {
                repeat(random.nextInt(0, 160)) {
                    append(alphabet[random.nextInt(alphabet.size)])
                }
            }
            val normalized = candidate.trim()
            val expected = runCatching { URI(normalized) }.getOrNull()?.let { uri ->
                normalized.isNotEmpty() &&
                    uri.scheme?.lowercase() == "https" &&
                    !uri.host.isNullOrBlank() &&
                    uri.userInfo == null &&
                    uri.fragment == null &&
                    (uri.port == -1 || uri.port in 1..65535)
            } == true
            val actual = UpdateChannelPolicy.resolve(Config.Value.CUSTOM_CHANNEL, candidate)
            assertEquals(candidate, expected, actual is UpdateEndpointResolution.Remote)
        }
    }

    @Test
    fun `generated valid HTTPS URLs remain accepted`() {
        val random = Random(0x48545450)
        repeat(1_000) { index ->
            val host = "node${random.nextInt(1, 1_000_000)}.example.test"
            val port = if (index % 3 == 0) ":${random.nextInt(1, 65536)}" else ""
            val url = "https://$host$port/channel/${random.nextInt()}.json?build=$index"
            assertTrue(
                url,
                UpdateChannelPolicy.resolve(Config.Value.CUSTOM_CHANNEL, url) is
                    UpdateEndpointResolution.Remote
            )
        }
    }

    @Test
    fun `transport follows same-scheme redirects but rejects scheme transitions`() {
        val client = OkHttpClient.Builder().enforceUpdateTransportPolicy().build()
        assertTrue(client.followRedirects)
        assertFalse(client.followSslRedirects)
    }
}
