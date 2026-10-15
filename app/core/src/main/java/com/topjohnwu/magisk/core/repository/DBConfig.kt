package com.topjohnwu.magisk.core.repository

import com.topjohnwu.magisk.core.data.magiskdb.SettingsDao
import com.topjohnwu.magisk.core.data.magiskdb.StringDao
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.io.IOException
import com.topjohnwu.magisk.core.ktx.toast
import kotlinx.coroutines.runBlocking
import kotlin.properties.ReadWriteProperty
import kotlin.reflect.KProperty

interface DBConfig {
    companion object {
        val revision = MutableStateFlow(0)
        fun writeFailed() {
            revision.update { it + 1 }
            com.topjohnwu.magisk.core.AppContext.toast(
                com.topjohnwu.magisk.core.R.string.hide_operation_error, android.widget.Toast.LENGTH_LONG)
        }
    }
    val settingsDB: SettingsDao
    val stringDB: StringDao
    val coroutineScope: CoroutineScope

    fun dbSettings(
        name: String,
        default: Int
    ) = IntDBProperty(name, default)

    fun dbSettings(
        name: String,
        default: Boolean
    ) = BoolDBProperty(name, default)

    fun dbStrings(
        name: String,
        default: String,
        sync: Boolean = false
    ) = StringDBProperty(name, default, sync)

}

class IntDBProperty(
    private val name: String,
    private val default: Int
) : ReadWriteProperty<DBConfig, Int> {

    var value: Int? = null
    private var generation = 0
    private val writer = Mutex()

    @Synchronized
    override fun getValue(thisRef: DBConfig, property: KProperty<*>): Int {
        if (value == null)
            value = runBlocking { thisRef.settingsDB.fetch(name, default) }
        return value as Int
    }

    override fun setValue(thisRef: DBConfig, property: KProperty<*>, value: Int) {
        val request = synchronized(this) { ++generation }
        thisRef.coroutineScope.launch {
            writer.withLock {
                if (synchronized(this@IntDBProperty) { request != generation }) return@withLock
                try {
                    thisRef.settingsDB.put(name, value)
                    synchronized(this@IntDBProperty) { this@IntDBProperty.value = value }
                } catch (_: IOException) {
                    synchronized(this@IntDBProperty) { this@IntDBProperty.value = null }
                    DBConfig.writeFailed()
                }
            }
        }
    }

}

open class BoolDBProperty(
    name: String,
    default: Boolean
) : ReadWriteProperty<DBConfig, Boolean> {

    val base = IntDBProperty(name, if (default) 1 else 0)

    override fun getValue(thisRef: DBConfig, property: KProperty<*>): Boolean =
        base.getValue(thisRef, property) != 0

    override fun setValue(thisRef: DBConfig, property: KProperty<*>, value: Boolean) =
        base.setValue(thisRef, property, if (value) 1 else 0)
}

class StringDBProperty(
    private val name: String,
    private val default: String,
    private val sync: Boolean
) : ReadWriteProperty<DBConfig, String> {

    private var value: String? = null

    @Synchronized
    override fun getValue(thisRef: DBConfig, property: KProperty<*>): String {
        if (value == null)
            value = runBlocking {
                thisRef.stringDB.fetch(name, default)
            }
        return value!!
    }

    override fun setValue(thisRef: DBConfig, property: KProperty<*>, value: String) {
        suspend fun write() {
            if (value.isEmpty()) thisRef.stringDB.delete(name)
            else thisRef.stringDB.put(name, value)
            synchronized(this@StringDBProperty) { this@StringDBProperty.value = value }
        }
        if (sync) {
            runBlocking { write() }
        } else {
            thisRef.coroutineScope.launch {
                try { write() } catch (_: IOException) {
                    synchronized(this@StringDBProperty) { this@StringDBProperty.value = null }
                    DBConfig.writeFailed()
                }
            }
        }
    }
}
