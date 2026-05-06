package com.idegym

import com.intellij.ide.impl.ProjectUtil
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.ApplicationStarter
import java.nio.file.Paths

/**
 * Opens a project by path and keeps the IDE alive so the MCP server keeps serving.
 *
 * Registered as the "open" AppStarter command in plugin.xml:
 *   idea.sh open /path/to/project
 *
 * Falls back to IDEGYM_PROJECT_ROOT when no path argument is supplied.
 */
class OpenProjectStarter : ApplicationStarter {

    override val commandName: String = "open"

    override fun main(args: List<String>) {
        val projectPath = args.drop(1).firstOrNull()
            ?: System.getenv("IDEGYM_PROJECT_ROOT")
            ?: error("No project path: pass it as an argument or set IDEGYM_PROJECT_ROOT")

        val path = Paths.get(projectPath).toAbsolutePath().normalize()
        println("[idegym-open-project] Opening project at $path")

        ApplicationManager.getApplication().invokeAndWait {
            @Suppress("UnstableApiUsage")
            ProjectUtil.openOrImport(path, null, true)
        }

        println("[idegym-open-project] Project open — MCP server should now be reachable on port 64342")
        Thread.currentThread().join()
    }
}
