package com.topjohnwu.magisk.ui.systemmode

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalResources
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import com.topjohnwu.magisk.core.Const
import com.topjohnwu.magisk.core.Info
import com.topjohnwu.magisk.core.model.ManagerDiagnostics
import com.topjohnwu.magisk.ui.component.rememberConfirmDialog
import com.topjohnwu.magisk.ui.navigation.LocalNavigator
import com.topjohnwu.magisk.ui.navigation.Route
import kotlinx.coroutines.launch
import com.topjohnwu.magisk.core.R as CoreR

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SystemModeScreen(onBack: () -> Unit) {
    var state by remember { mutableStateOf<String?>(null) }
    val scope = rememberCoroutineScope()
    val navigator = LocalNavigator.current
    val resources = LocalResources.current
    val recovery = rememberConfirmDialog(onConfirm = { navigator.push(Route.Flash(Const.Value.RECOVER_SYSTEM_MODE)) })
    LaunchedEffect(Unit) { state = ManagerDiagnostics.systemState() }
    Scaffold(topBar = {
        TopAppBar(title = { Text(stringResource(CoreR.string.system_mode_title)) }, navigationIcon = {
            IconButton(onClick = onBack) {
                Icon(Icons.AutoMirrored.Filled.ArrowBack, stringResource(CoreR.string.back))
            }
        })
    }) { padding ->
        Column(Modifier.fillMaxSize().padding(padding).verticalScroll(rememberScrollState()).padding(20.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp)) {
            Text(stringResource(if (Info.isSystemMode) CoreR.string.system_mode_running else CoreR.string.system_mode_ordinary))
            Text(stringResource(CoreR.string.system_mode_state, state ?: stringResource(CoreR.string.loading)))
            Text(stringResource(CoreR.string.system_mode_install_summary))
            Button(onClick = { scope.launch { state = ManagerDiagnostics.systemState() } }) {
                Text(stringResource(CoreR.string.manager_refresh))
            }
            if (Info.isRooted && state != null && state != "ABSENT") {
                Button(onClick = { recovery.showConfirm(
                    title = resources.getString(CoreR.string.system_mode_recovery),
                    content = resources.getString(CoreR.string.system_mode_recovery_msg)) }) {
                    Text(stringResource(CoreR.string.system_mode_recovery))
                }
            }
            Text(stringResource(CoreR.string.system_mode_recovery_help))
        }
    }
}
