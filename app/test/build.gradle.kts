plugins {
    id("com.android.application")
}

android {
    namespace = "com.topjohnwu.magisk.test"

    defaultConfig {
        applicationId = "$APP_ID.test"
        versionCode = 1
        versionName = "1.0"
        buildConfigField("String", "APP_PACKAGE_NAME", "\"$APP_ID\"")
        manifestPlaceholders["magiskAppId"] = APP_ID
        manifestPlaceholders["magiskTestAppId"] = "$APP_ID.test"
        proguardFile("proguard-rules.pro")
    }

    buildTypes {
        release {
            isMinifyEnabled = true
        }
    }

    buildFeatures {
        buildConfig = true
    }
}

setupTestApk()

dependencies {
    implementation(libs.test.runner)
    implementation(libs.test.rules)
    implementation(libs.test.junit)
    implementation(libs.test.uiautomator)
}
