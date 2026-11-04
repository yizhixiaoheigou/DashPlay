import com.android.build.api.variant.ApplicationAndroidComponentsExtension
import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.plugin.serialization")
}

// Isolated, reproducible baseline. The Linux receiver checkout can be in use by another task.
val playport = rootProject.file("build/receiver")
val prepareReceiver = tasks.register<Exec>("prepareReceiver") {
    inputs.file(rootProject.file("../patches/series"))
    inputs.dir(rootProject.file("../patches"))
    inputs.dir(rootProject.file("patches"))
    inputs.file(rootProject.file("scripts/prepare-receiver.sh"))
    outputs.dir(playport)
    commandLine("sh", rootProject.file("scripts/prepare-receiver.sh").path)
}

android {
    namespace = "com.dashplay.android"
    compileSdk = 35
    buildFeatures { resValues = true }

    defaultConfig {
        applicationId = "com.dashplay.android"
        minSdk = 21
        ndk { abiFilters += listOf("armeabi-v7a", "arm64-v8a") }
        targetSdk = 35
        // The app has its own version line (0.3.0-beta.1); box releases use date versions (2026.10.31.3) and the two
        // must never be compared. A release sets both properties; see scripts/android-release.sh.
        versionCode = providers.gradleProperty("dashplayVersionCode").orElse("3").get().toInt().also { require(it > 0) }
        versionName = providers.gradleProperty("dashplayVersionName").orElse("0.3.0-beta.1").get()
        resValue("bool", "usb_phone_diagnostics", "false")
        testInstrumentationRunner = "com.dashplay.android.CertificateNetworkProbe"
    }

    signingConfigs {
        // Only exists once the owner puts the release key in place. Until then every build is debug-signed, and Android
        // refuses to upgrade a debug-signed install to a release-signed one (signers must match): the first
        // release-signed build needs a fresh install, which docs/安卓发布验收清单.md calls out.
        val releaseKey = rootProject.file("release-keystore.properties")
        if (releaseKey.isFile) {
            val key = Properties().apply { releaseKey.inputStream().use { load(it) } }
            create("release") {
                storeFile = rootProject.file(key.getProperty("storeFile"))
                storePassword = key.getProperty("storePassword")
                keyAlias = key.getProperty("keyAlias")
                keyPassword = key.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        debug {
            resValue("bool", "usb_phone_diagnostics",
                providers.gradleProperty("dashplayUsbPhoneDiagnostics").orElse("false").get().toBoolean().toString())
        }
        release {
            // R8 stays off until a release has been run through it: Kotlin reflection and the BouncyCastle provider are
            // easy to strip by accident, and a stripped update path is only noticed by users in the field.
            isMinifyEnabled = false
            isShrinkResources = false
            // Without the release key the beta is signed with the debug key rather than left unsigned: an unsigned APK
            // cannot be installed at all, so a beta built that way could not be handed to anyone. A later release key
            // means one fresh install (Android will not upgrade across signers).
            signingConfig = signingConfigs.findByName("release") ?: signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
        // java.nio.file (the receiver's state files) only exists from Android 8.0.
        isCoreLibraryDesugaringEnabled = true
    }

    packaging {
        resources {
            excludes += setOf(
                "META-INF/INDEX.LIST",
                "META-INF/io.netty.versions.properties",
                "META-INF/DEPENDENCIES",
                "META-INF/LICENSE*",
                "META-INF/NOTICE*",
                "META-INF/versions/9/OSGI-INF/MANIFEST.MF",
            )
        }
    }

    sourceSets.getByName("main").resources.srcDir(playport.resolve("server/src/main/resources"))

    /** The RTC bridge: prebuilt libdatachannel per ABI + rtc_bridge.c (see src/main/cpp/CMakeLists.txt). */
    externalNativeBuild {
        cmake {
            path = file("src/main/cpp/CMakeLists.txt")
            version = "3.22.1"
        }
    }

    ndkVersion = "27.0.12077973"

    lint {
        abortOnError = false
        checkDependencies = false
    }
}

// The receiver's server sources without Main.kt, which starts Netty (needs Android 8.0); DashPlayService starts it here.
val receiverServer = tasks.register<Sync>("receiverServer") {
    dependsOn(prepareReceiver)
    from(playport.resolve("server/src/main/kotlin")) { exclude("**/Main.kt") }
    into(layout.buildDirectory.dir("generated/receiverServer"))
}
tasks.matching { it.name.startsWith("compile") && it.name.endsWith("Kotlin") }.configureEach { dependsOn(receiverServer) }
tasks.matching { it.name.startsWith("process") && it.name.endsWith("JavaRes") }.configureEach { dependsOn(prepareReceiver) }

// Fault injection is opt-in for a diagnostic debug APK, never a release variant.
val rtcFaultTest = providers.gradleProperty("dashplayRtcFaultTest").orElse("false").map { it.toBooleanStrict() }
tasks.matching { it.name == "preReleaseBuild" }.configureEach {
    doFirst { check(!rtcFaultTest.get()) { "RTC fault injection cannot be packaged in release" } }
}
// The browser page (web/dist) ships as assets/web.
val buildWeb = tasks.register<Exec>("buildWeb") {
    inputs.property("rtcFaultTest", rtcFaultTest)
    environment("VITE_RTC_FAULT_TEST", if (rtcFaultTest.get()) "1" else "0")
    dependsOn(prepareReceiver)
    inputs.dir(playport.resolve("web/src"))
    inputs.file(playport.resolve("web/package-lock.json"))
    inputs.file(playport.resolve("web/index.html"))
    outputs.dir(playport.resolve("web/dist"))
    workingDir(playport.resolve("web"))
    commandLine("sh", "-c", "npm ci --no-audit --no-fund && npm test && npm run build")
}
val webAssets = tasks.register<Sync>("webAssets") {
    dependsOn(buildWeb)
    from(playport.resolve("web/dist"))
    into(layout.buildDirectory.dir("generated/webAssets/web"))
}
val checkPhoneConnection = tasks.register<Exec>("checkPhoneConnection") {
    dependsOn(buildWeb)
    commandLine("node", rootProject.file("scripts/check-phone-connection.mjs").path)
}
tasks.matching { it.name == "testDebugUnitTest" }.configureEach { dependsOn(checkPhoneConnection) }
extensions.configure<ApplicationAndroidComponentsExtension> {
    onVariants { variant ->
        variant.sources.kotlin?.addStaticSourceDirectory(playport.resolve("protocol/src/main/java").path)
        variant.sources.kotlin?.addStaticSourceDirectory(layout.buildDirectory.dir("generated/receiverServer").get().asFile.path)
        variant.sources.assets?.addStaticSourceDirectory(layout.buildDirectory.dir("generated/webAssets").get().asFile.path)
    }
}
tasks.matching { it.name.startsWith("merge") && it.name.endsWith("Assets") }.configureEach { dependsOn(webAssets) }
// The lint model reads the generated assets too (it walked generated/webAssets before the page was built).
tasks.matching { it.name.startsWith("generate") && it.name.contains("Lint") }.configureEach { dependsOn(webAssets) }
tasks.matching { it.name.startsWith("lint") }.configureEach { dependsOn(webAssets) }

dependencies {
    testImplementation("junit:junit:4.13.2")
    coreLibraryDesugaring("com.android.tools:desugar_jdk_libs_nio:2.1.5")
    implementation("org.bouncycastle:bcprov-jdk18on:1.79")
    implementation("org.bouncycastle:bcpkix-jdk18on:1.79")
    implementation("org.slf4j:slf4j-api:2.0.20")
    implementation("org.slf4j:slf4j-simple:2.0.20")
    implementation("net.sf.kxml:kxml2:2.3.0")
    implementation("io.ktor:ktor-server-core:3.6.0")
    // CIO, not Netty: Netty 4.2 needs Android 8.0. HTTPS is terminated in front of it (TlsFront).
    implementation("io.ktor:ktor-server-cio:3.6.0")
    implementation("io.ktor:ktor-server-websockets:3.6.0")
    implementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.11.0")
    implementation("org.jmdns:jmdns:3.6.3")
    // The car's address as a QR code on the home screen.
    implementation("com.google.zxing:core:3.5.3")
}
