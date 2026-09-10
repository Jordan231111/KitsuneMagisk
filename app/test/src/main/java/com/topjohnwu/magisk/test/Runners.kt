package com.topjohnwu.magisk.test

import android.os.Bundle
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.runner.AndroidJUnitRunner
import androidx.test.uiautomator.Configurator

open class TestRunner : AndroidJUnitRunner() {
    override fun onCreate(arguments: Bundle) {
        // Support short-hand ".ClassName"
        arguments.getString("class")?.let {
            val classArg = it.split(",").joinToString(separator = ",") { clz ->
                if (clz.startsWith(".")) {
                    "com.topjohnwu.magisk.test$clz"
                } else {
                    clz
                }
            }
            arguments.putString("class", classArg)
        }
        super.onCreate(arguments)
    }
}

class AppTestRunner : TestRunner() {
    override fun onCreate(arguments: Bundle) {
        // Tests wait for explicit UI conditions. Waiting for animations and
        // unrelated accessibility events to idle can consume those deadlines.
        Configurator.getInstance().waitForIdleTimeout = 0
        // Force using the target context's classloader to run tests
        arguments.putString("classLoader", TestClassLoader::class.java.name)
        super.onCreate(arguments)
    }
}

private val targetClassLoader inline get() =
    InstrumentationRegistry.getInstrumentation().targetContext.classLoader

class TestClassLoader : ClassLoader(targetClassLoader)
