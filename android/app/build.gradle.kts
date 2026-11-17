plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

android {
    namespace = "com.kokekk.kokekbooru"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.kokekk.kokekbooru"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.5-beta"

        ndk {
            abiFilters += listOf("arm64-v8a", "x86_64")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
}

chaquopy {
    defaultConfig {
        version = "3.10"
        pip {
            install("pillow")
            install("numpy")
            install("pydantic==1.10.15")
            install("fastapi==0.103.2")
            install("uvicorn==0.23.2")
        }
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.microsoft.onnxruntime:onnxruntime-android:1.22.0")
}
