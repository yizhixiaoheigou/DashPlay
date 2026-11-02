buildscript {
    dependencies {
        // Ktor 3.6 and kotlinx-serialization 1.11 are built with Kotlin 2.4; AGP 9's bundled Kotlin is older.
        classpath("org.jetbrains.kotlin:kotlin-gradle-plugin:2.4.20")
    }
}

plugins {
    id("com.android.application") version "9.1.0" apply false
    id("org.jetbrains.kotlin.plugin.serialization") version "2.4.20" apply false
}
