plugins {
    id("org.jetbrains.kotlin.jvm") version "2.0.21"
    id("org.jetbrains.intellij.platform") version "2.3.0"
}

group = "com.idegym"
version = "1.0.0"

kotlin {
    jvmToolchain(17)
}

repositories {
    mavenCentral()
    intellijPlatform {
        defaultRepositories()
    }
}

dependencies {
    intellijPlatform {
        // Build against IntelliJ IDEA Community for a smaller download.
        // The plugin only uses com.intellij.modules.platform APIs, so it is
        // binary-compatible with PyCharm Professional at runtime.
        intellijIdeaCommunity("2024.3")
        instrumentationTools()
    }
}
