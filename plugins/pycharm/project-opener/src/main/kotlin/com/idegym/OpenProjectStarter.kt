package com.idegym

import com.intellij.ide.impl.ProjectUtil
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.ApplicationStarter
import java.nio.file.Paths

/**
 * AppStarter that opens a project by path and keeps the IDE process alive so the
 * built-in MCP server continues to serve requests.
 *
 * Registered as the "open" command in plugin.xml, invoked by the PyCharm launcher as:
 *   pycharm.sh open /path/to/project
 *
 * Falls back to IDEGYM_PROJECT_ROOT if no path argument is supplied.
 *
 * Note: in PyCharm CE, ApplicationStarter.main() runs on the EDT. invokeAndWait
 * is safe here — when called from the EDT it executes the block in-place (no deadlock).
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
