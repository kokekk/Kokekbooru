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
        versionName = "0.1.4-beta"

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
        version = "3.11"
        pip {
            install("fastapi==0.111.0")
            install("uvicorn==0.30.1")
            install("pydantic==2.7.4")
            install("pillow==10.3.0")
            install("numpy==1.26.4")
        }
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.microsoft.onnxruntime:onnxruntime-android:1.22.0")
}
