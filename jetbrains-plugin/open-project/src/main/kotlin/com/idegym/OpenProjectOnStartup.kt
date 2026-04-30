package com.idegym

import com.intellij.ide.AppLifecycleListener
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.diagnostic.Logger
import com.intellij.openapi.project.ProjectManager

private val LOG = Logger.getInstance(OpenProjectOnStartup::class.java)

/**
 * Opens the project at [IDEGYM_PROJECT_ROOT] when the IDE starts up.
 *
 * This listener is a no-op when:
 * - the environment variable is not set, or
 * - a project is already open (e.g., the path was passed as a CLI argument).
 *
 * Registered in plugin.xml as an [AppLifecycleListener] so it fires after the
 * application is fully initialised, before the welcome screen is shown.
 */
class OpenProjectOnStartup : AppLifecycleListener {
    override fun appStarted() {
        val projectRoot = System.getenv("IDEGYM_PROJECT_ROOT")
        if (projectRoot == null) {
            LOG.info("OpenProjectOnStartup: IDEGYM_PROJECT_ROOT not set, skipping")
            return
        }

        // Skip if a project was already opened via the command line.
        if (ProjectManager.getInstance().openProjects.isNotEmpty()) {
            LOG.info("OpenProjectOnStartup: project already open, skipping")
            return
        }

        LOG.info("OpenProjectOnStartup: opening project at $projectRoot")
        ApplicationManager.getApplication().invokeLater {
            @Suppress("DEPRECATION")
            ProjectManager.getInstance().loadAndOpenProject(projectRoot)
        }
    }
}
