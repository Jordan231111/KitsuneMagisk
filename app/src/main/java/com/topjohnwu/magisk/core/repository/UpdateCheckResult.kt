package com.topjohnwu.magisk.core.repository

import com.topjohnwu.magisk.core.Config
import com.topjohnwu.magisk.core.model.UpdateInfo
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
    fun resolve(channel: Int, customUrl: String): UpdateEndpointResolution {
        if (channel == Config.Value.CUSTOM_CHANNEL) {
            val normalized = customUrl.trim()
            if (normalized.isEmpty()) {
                return UpdateEndpointResolution.Unavailable(
                    UpdateUnavailableReason.EMPTY_CUSTOM_URL
                )
            }
            val uri = runCatching { URI(normalized) }.getOrNull()
            if (uri == null ||
                uri.scheme?.lowercase() != "https" ||
                uri.host.isNullOrBlank() ||
                uri.userInfo != null ||
                uri.fragment != null ||
                (uri.port != -1 && uri.port !in 1..65535)
            ) {
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
}
