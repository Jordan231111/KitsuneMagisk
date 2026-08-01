package com.topjohnwu.magisk.core

import android.annotation.SuppressLint
import android.annotation.TargetApi
import android.app.Notification
import android.app.job.JobInfo
import android.app.job.JobParameters
import android.app.job.JobScheduler
import android.content.Context
import androidx.core.content.getSystemService
import com.topjohnwu.magisk.BuildConfig
import com.topjohnwu.magisk.core.base.BaseJobService
import com.topjohnwu.magisk.core.di.ServiceLocator
import com.topjohnwu.magisk.core.download.DownloadEngine
import com.topjohnwu.magisk.core.download.Subject
import com.topjohnwu.magisk.core.repository.UpdateChannelPolicy
import com.topjohnwu.magisk.core.repository.UpdateEndpointResolution
import com.topjohnwu.magisk.view.Notifications
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import java.util.concurrent.TimeUnit

class JobService : BaseJobService() {

    @Volatile
    private var mSession: Session? = null
    private val serviceScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    @Volatile
    private var updateJob: Job? = null

    @TargetApi(value = 34)
    inner class Session(
        @Volatile
        private var params: JobParameters
    ) : DownloadEngine.Session {

        @Volatile
        private var stopped = false

        override val context get() = this@JobService
        val engine = DownloadEngine(this)

        fun updateParams(params: JobParameters) {
            this.params = params
            engine.reattach()
        }

        override fun attachNotification(id: Int, builder: Notification.Builder) {
            setNotification(params, id, builder.build(), JOB_END_NOTIFICATION_POLICY_REMOVE)
        }

        override fun onDownloadComplete() {
            if (mSession === this) {
                mSession = null
            }
            if (!stopped) {
                jobFinished(params, false)
            }
        }

        fun stop() {
            stopped = true
            engine.cancel()
        }
    }

    @SuppressLint("NewApi")
    override fun onStartJob(params: JobParameters): Boolean {
        return when (params.jobId) {
            Const.ID.CHECK_UPDATE_JOB_ID -> checkUpdate(params)
            Const.ID.DOWNLOAD_JOB_ID -> downloadFile(params)
            else -> false
        }
    }

    override fun onStopJob(params: JobParameters?): Boolean {
        return if (params?.jobId == Const.ID.CHECK_UPDATE_JOB_ID) {
            val job = updateJob
            if (job?.isActive == true) {
                job.cancel()
                updateJob = null
                true
            } else {
                false
            }
        } else {
            mSession?.stop()
            mSession = null
            false
        }
    }

    override fun onDestroy() {
        mSession?.stop()
        mSession = null
        serviceScope.cancel()
        super.onDestroy()
    }

    @TargetApi(value = 34)
    private fun downloadFile(params: JobParameters): Boolean {
        params.transientExtras.classLoader = Subject::class.java.classLoader
        val subject = params.transientExtras
            .getParcelable(DownloadEngine.SUBJECT_KEY, Subject::class.java) ?:
            return false

        val session = mSession?.also {
            it.updateParams(params)
        } ?: run {
            Session(params).also { mSession = it }
        }

        session.engine.download(subject)
        return true
    }

    private fun checkUpdate(params: JobParameters): Boolean {
        // Start lazily so the completion path can never run before updateJob
        // publishes the exact Job instance used for compare-and-clear.
        val job = serviceScope.launch(start = CoroutineStart.LAZY) {
            try {
                val info = Info.getRemote(ServiceLocator.networkService)
                if (info != null && Info.env.isActive &&
                    BuildConfig.VERSION_CODE < info.magisk.versionCode
                ) {
                    Notifications.updateAvailable()
                }
            } finally {
                if (currentCoroutineContext().isActive) {
                    jobFinished(params, false)
                }
                val completed = currentCoroutineContext()[Job]
                if (updateJob === completed) {
                    updateJob = null
                }
            }
        }
        updateJob = job
        job.start()
        return true
    }

    companion object {
        fun schedule(context: Context) {
            val scheduler = context.getSystemService<JobScheduler>() ?: return
            val endpointAvailable = UpdateChannelPolicy.resolve(
                Config.updateChannel,
                Config.customChannelUrl
            ) is UpdateEndpointResolution.Remote
            if (Config.checkUpdate && endpointAvailable) {
                val cmp = JobService::class.java.cmp(context.packageName)
                val info = JobInfo.Builder(Const.ID.CHECK_UPDATE_JOB_ID, cmp)
                    .setPeriodic(TimeUnit.HOURS.toMillis(12))
                    .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                    .setRequiresDeviceIdle(true)
                    .build()
                scheduler.schedule(info)
            } else {
                scheduler.cancel(Const.ID.CHECK_UPDATE_JOB_ID)
            }
        }
    }
}
