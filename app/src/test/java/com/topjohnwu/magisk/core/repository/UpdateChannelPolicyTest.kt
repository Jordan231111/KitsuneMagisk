package com.topjohnwu.magisk.core.repository

import com.topjohnwu.magisk.core.Config
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

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
}
