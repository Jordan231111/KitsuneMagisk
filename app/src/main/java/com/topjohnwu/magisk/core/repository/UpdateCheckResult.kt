package com.topjohnwu.magisk.core.repository

import com.topjohnwu.magisk.core.Config
import com.topjohnwu.magisk.core.model.UpdateInfo
import com.topjohnwu.magisk.utils.APKInstall
import java.net.URI

sealed class UpdateCheckResult {
    object NotChecked : UpdateCheckResult()
    data class Success(val info: UpdateInfo) : UpdateCheckResult()
    data class Unavailable(val reason: UpdateUnavailableReason) : UpdateCheckResult()
}

enum class UpdateUnavailableReason {
    PROJECT_SERVICE_NOT_CONFIGURED,
    OFFLINE,
    EMPTY_CUSTOM_URL,
    INVALID_CUSTOM_URL,
    INVALID_UPDATE_METADATA,
    REQUEST_FAILED,
}

sealed class UpdateEndpointResolution {
    data class Remote(val url: String) : UpdateEndpointResolution()
    data class Unavailable(val reason: UpdateUnavailableReason) : UpdateEndpointResolution()
}

/**
 * Release containment for the inherited updater.
 *
 * The prior-maintainer channel URLs are all dead. Until PR10 adds project-owned,
 * digest-validated metadata, built-in channels fail closed without issuing a
 * request. A user-supplied custom channel remains available only over HTTPS.
 */
object UpdateChannelPolicy {
    private val sha256Pattern = Regex("^[0-9a-fA-F]{64}$")

    private fun isValidHttpsUrl(value: String): Boolean {
        if (value.isEmpty() || value != value.trim()) return false
        val uri = runCatching { URI(value) }.getOrNull() ?: return false
        return uri.scheme?.lowercase() == "https" &&
            !uri.host.isNullOrBlank() &&
            uri.userInfo == null &&
            uri.fragment == null &&
            (uri.port == -1 || uri.port in 1..65535)
    }

    fun resolve(channel: Int, customUrl: String): UpdateEndpointResolution {
        if (channel == Config.Value.CUSTOM_CHANNEL) {
            val normalized = customUrl.trim()
            if (normalized.isEmpty()) {
                return UpdateEndpointResolution.Unavailable(
                    UpdateUnavailableReason.EMPTY_CUSTOM_URL
                )
            }
            if (!isValidHttpsUrl(normalized)) {
                return UpdateEndpointResolution.Unavailable(
                    UpdateUnavailableReason.INVALID_CUSTOM_URL
                )
            }
            return UpdateEndpointResolution.Remote(normalized)
        }

        return UpdateEndpointResolution.Unavailable(
            UpdateUnavailableReason.PROJECT_SERVICE_NOT_CONFIGURED
        )
    }

    fun validate(info: UpdateInfo): UpdateUnavailableReason? {
        val apk = info.magisk
        if (apk.version.isBlank() ||
            apk.versionCode <= 0 ||
            !isValidHttpsUrl(apk.link) ||
            (apk.note.isNotEmpty() && !isValidHttpsUrl(apk.note)) ||
            !sha256Pattern.matches(apk.sha256)
        ) {
            return UpdateUnavailableReason.INVALID_UPDATE_METADATA
        }
        return null
    }

    fun matchesSha256(expected: String, actual: ByteArray): Boolean {
        return APKInstall.matchesSha256(actual, expected)
    }
}
