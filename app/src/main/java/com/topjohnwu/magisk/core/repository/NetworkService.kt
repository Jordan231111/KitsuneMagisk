package com.topjohnwu.magisk.core.repository

import com.topjohnwu.magisk.core.Config
import com.topjohnwu.magisk.core.Info
import com.topjohnwu.magisk.core.data.GithubPageServices
import com.topjohnwu.magisk.core.data.RawServices
import kotlinx.coroutines.CancellationException
import retrofit2.HttpException
import timber.log.Timber
import java.io.IOException

class NetworkService(
    private val pages: GithubPageServices,
    private val raw: RawServices
) {
    suspend fun fetchUpdate(): UpdateCheckResult {
        val endpoint = UpdateChannelPolicy.resolve(
            Config.updateChannel,
            Config.customChannelUrl
        )
        if (endpoint is UpdateEndpointResolution.Unavailable) {
            return UpdateCheckResult.Unavailable(endpoint.reason)
        }
        if (Info.isConnected.value != true) {
            return UpdateCheckResult.Unavailable(UpdateUnavailableReason.OFFLINE)
        }

        endpoint as UpdateEndpointResolution.Remote
        return try {
            UpdateCheckResult.Success(pages.fetchUpdateJSON(endpoint.url))
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            Timber.w(e, "Update metadata request failed")
            UpdateCheckResult.Unavailable(UpdateUnavailableReason.REQUEST_FAILED)
        }
    }

    private inline fun <T> wrap(factory: () -> T): T {
        return try {
            factory()
        } catch (e: HttpException) {
            throw IOException(e)
        }
    }

    // Fetch files
    suspend fun fetchFile(url: String) = wrap { raw.fetchFile(url) }
    suspend fun fetchString(url: String) = wrap { raw.fetchString(url) }
    suspend fun fetchModuleJson(url: String) = wrap { raw.fetchModuleJson(url) }
}
