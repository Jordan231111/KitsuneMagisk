plugins {
    id("com.android.application")
    id("org.lsposed.lsparanoid")
}

lsparanoid {
    seed = if (RAND_SEED != 0) RAND_SEED else null
    includeDependencies = true
    global = true
}

android {
    namespace = "com.topjohnwu.magisk"

    defaultConfig {
        applicationId = "io.github.huskydg.magisk"
        versionCode = 1
        versionName = "1.0"
        // PR4 containment: inherited prior-maintainer endpoints are dead. PR10
        // will replace these fields with project-owned, digest-validated metadata.
        buildConfigField("boolean", "UPDATE_SERVICE_CONFIGURED", "false")
        buildConfigField("String", "APK_URL", "null")
        buildConfigField(
            "String",
            "PROJECT_URL",
            "\"https://github.com/Jordan231111/KitsuneMagisk\""
        )
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = false
            proguardFiles("proguard-rules.pro")
        }
    }
}

setupStub()

dependencies {
    implementation(project(":app:shared"))
}
