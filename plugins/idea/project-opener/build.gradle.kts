plugins {
    id("org.jetbrains.kotlin.jvm") version "2.0.21"
    id("org.jetbrains.intellij.platform") version "2.3.0"
}

group = "com.idegym"
version = "1.0.0"

kotlin { jvmToolchain(17) }

repositories {
    mavenCentral()
    intellijPlatform { defaultRepositories() }
}

dependencies {
    intellijPlatform {
        // Build against IntelliJ IDEA Community 2025.2.4 (build series 252).
        // The plugin uses only com.intellij.modules.platform APIs so the compiled ZIP is
        // compatible with any IDEA 2025.2+ build.
        intellijIdeaCommunity("2025.2.4")
        instrumentationTools()
    }
}
