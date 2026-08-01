package com.topjohnwu.magisk.core

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob

/** Process-lifetime work that must not inherit an Activity or receiver lifetime. */
val appScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
