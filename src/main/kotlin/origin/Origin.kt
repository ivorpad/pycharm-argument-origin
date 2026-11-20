package origin

import com.google.gson.Gson
import com.google.gson.JsonParser
import com.intellij.openapi.actionSystem.ActionUpdateThread
import com.intellij.openapi.actionSystem.AnActionEvent
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.PathManager
import com.intellij.openapi.project.DumbAware
import com.intellij.openapi.project.Project
import com.intellij.openapi.project.DumbAwareAction
import com.intellij.openapi.ui.InputValidator
import com.intellij.openapi.ui.Messages
import com.intellij.openapi.wm.ToolWindow
import com.intellij.openapi.wm.ToolWindowFactory
import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.ui.content.ContentFactory
import com.intellij.openapi.util.Disposer
import com.intellij.xdebugger.XDebugSession
import com.intellij.xdebugger.XDebugSessionListener
import com.intellij.xdebugger.XDebuggerManager
import com.intellij.xdebugger.XDebuggerManagerListener
import com.intellij.xdebugger.XDebugProcess
import com.intellij.xdebugger.evaluation.XDebuggerEvaluator
import com.intellij.xdebugger.frame.XValue
import com.intellij.xdebugger.impl.breakpoints.XExpressionImpl
import com.intellij.xdebugger.impl.ui.DebuggerUIUtil
import com.intellij.xdebugger.impl.ui.tree.XDebuggerTree
import com.intellij.xdebugger.impl.ui.tree.nodes.WatchNode
import com.intellij.xdebugger.impl.ui.tree.nodes.WatchesRootNode
import com.intellij.xdebugger.impl.ui.tree.actions.XDebuggerTreeActionBase
import com.intellij.xdebugger.impl.ui.tree.nodes.XValueNodeImpl
import com.jetbrains.python.debugger.PyDebugValue
import com.jetbrains.python.debugger.PyStackFrame
import java.nio.file.Files
import java.nio.file.Path

internal const val windowName = "Argument Origin"
private const val retry = "No recording for this run.\n\nStart a fresh Debug session for a local Python script using pydevd and CPython 3.12–3.15. If this configuration sets ARG_ORIGIN_RECORD=0, clear it first."

class OriginWindow : ToolWindowFactory, DumbAware {
    override fun createToolWindowContent(project: Project, toolWindow: ToolWindow) {
        val pane = ReportPane(project)
        project.messageBus.connect(toolWindow.disposable).subscribe(XDebuggerManager.TOPIC, object : XDebuggerManagerListener {
            override fun currentSessionChanged(previousSession: XDebugSession?, currentSession: XDebugSession?) {
                ApplicationManager.getApplication().invokeLater {
                    if (!project.isDisposed && pane.session !== XDebuggerManager.getInstance(project).currentSession) pane.clearReport()
                }
            }
            override fun processStopped(debugProcess: XDebugProcess) {
                ApplicationManager.getApplication().invokeLater {
                    if (!project.isDisposed && pane.session === debugProcess.session) pane.clearReport()
                }
            }
        })
        val content = ContentFactory.getInstance().createContent(pane, "", false)
        toolWindow.contentManager.addContent(content)
    }
}

private fun display(session: XDebugSession, report: Report) {
    val project = session.project
    ApplicationManager.getApplication().invokeLater {
        if (project.isDisposed || XDebuggerManager.getInstance(project).currentSession !== session) return@invokeLater
        val window = ToolWindowManager.getInstance(project).getToolWindow(windowName) ?: return@invokeLater
        window.show {
            (window.contentManager.getContent(0)?.component as? ReportPane)?.let { pane ->
                pane.session = session
                pane.render(report)
            }
        }
    }
}

// The backend finishes writing the hit before asking pydevd to suspend.
private class HitReport(private val session: XDebugSession, val file: Path) : XDebugSessionListener {
    private val lifetime = Disposer.newDisposable("Argument Origin watch")
    private var lastHit = -1L
    init {
        Disposer.register(session.project, lifetime)
        Disposer.register(lifetime) { Files.deleteIfExists(file) }
        session.addSessionListener(this, lifetime)
    }
    override fun sessionPaused() {
        ApplicationManager.getApplication().executeOnPooledThread { readHit() }
    }
    @Synchronized private fun readHit() {
        try {
            val text = Files.readString(file)
            if (text.isEmpty()) return
            val root = JsonParser.parseString(text).asJsonObject
            if (root.get("closed")?.asBoolean == true) {
                close()
                return
            }
            val id = root.get("hit")?.asLong ?: 0L
            if (id > lastHit) {
                lastHit = id
                display(session, decode(root))
            }
        } catch (_: java.nio.file.NoSuchFileException) {
            // The session ended while this notification was queued.
        }
    }
    override fun sessionStopped() = close()
    fun close() = Disposer.dispose(lifetime)
}

private val localIdentifier = Regex("[_\\p{L}][_\\p{L}\\p{M}\\p{N}]*")

private fun localName(node: XValueNodeImpl): String? {
    val name = if (node is WatchNode) node.expression.expression.trim() else {
        val value = node.valueContainer as? PyDebugValue ?: return null
        if (value.parent != null || value.name != node.name) return null
        node.name
    }
    return name?.takeIf(localIdentifier::matches)
}

abstract class BindingAction(private val operation: String) : XDebuggerTreeActionBase(), DumbAware {
    override fun isEnabled(node: XValueNodeImpl, e: AnActionEvent) = localName(node) != null

    override fun perform(node: XValueNodeImpl, nodeName: String, e: AnActionEvent) {
        val session = DebuggerUIUtil.getSession(e) ?: return
        runBinding(session, localName(node) ?: return, operation)
    }
}

// New Watch is available even in an empty Watches tree. Offer the same entry point for a local rebinding.
class WatchWhenChanged : DumbAwareAction() {
    override fun getActionUpdateThread() = ActionUpdateThread.EDT

    override fun update(e: AnActionEvent) {
        val session = DebuggerUIUtil.getSession(e)
        val nodes = XDebuggerTreeActionBase.getSelectedNodes(e.dataContext)
        e.presentation.isEnabled = session?.isPaused == true && session.currentStackFrame is PyStackFrame &&
            (nodes.isEmpty() || nodes.singleOrNull()?.let(::localName) != null)
    }

    override fun actionPerformed(e: AnActionEvent) {
        val session = DebuggerUIUtil.getSession(e) ?: return
        val frame = session.currentStackFrame as? PyStackFrame ?: return
        if (!session.isPaused) return
        val view = DebuggerUIUtil.getWatchesView(e) ?: return
        val tree = XDebuggerTree.getTree(e) ?: return
        val nodes = XDebuggerTreeActionBase.getSelectedNodes(e.dataContext)
        val name = if (nodes.isEmpty()) {
            Messages.showInputDialog(session.project, "Local variable in the suspended frame:",
                "Pause on Value Changes", null, "", object : InputValidator {
                    override fun checkInput(inputString: String) = localIdentifier.matches(inputString.trim())
                    override fun canClose(inputString: String) = checkInput(inputString)
                })?.trim() ?: return
        } else nodes.singleOrNull()?.let(::localName) ?: return
        if (!session.isPaused || session.currentStackFrame !== frame) return
        runBinding(session, name, "watch") {
            val root = tree.root as? WatchesRootNode ?: return@runBinding
            if (root.watchChildren.none { it.expression.expression.trim() == name }) {
                view.addWatchExpression(XExpressionImpl.fromText(name), -1, true)
            }
        }
    }
}

private fun runBinding(session: XDebugSession, nodeName: String, operation: String, onArmed: (() -> Unit)? = null) {
    val frame = session.currentStackFrame as? PyStackFrame
    val id = frame?.frameId?.toLongOrNull()
    val evaluator = frame?.evaluator
    if (evaluator == null || id == null) {
        display(session, Report(nodeName, "Select a local frame in the PyCharm pydevd debugger."))
        return
    }
    // Results can exceed pydevd's string presentation limit. Only the local
    // debuggee writes this already-created private exchange file.
    val exchange = Files.createTempFile(Path.of(PathManager.getSystemPath()), "argument-origin-", ".json")
    val hit = if (operation == "watch") HitReport(session, Files.createTempFile(exchange.parent, "argument-hit-", ".json")) else null
    val watchFile = hit?.let { Gson().toJson(it.file.toString()) } ?: "None"
    val quotedPath = Gson().toJson(exchange.toString())
    val variable = Gson().toJson(nodeName)
    val expression = "__import__('sys').modules.get('argument_origin') and " +
        "__import__('sys').modules['argument_origin'].export($id, $variable, '$operation', $quotedPath, $watchFile)"
    evaluator.evaluate(expression, object : XDebuggerEvaluator.XEvaluationCallback {
        override fun evaluated(result: XValue) {
            ApplicationManager.getApplication().executeOnPooledThread {
                val report = try {
                    if (Files.size(exchange) == 0L) Report(nodeName, retry)
                    else decode(Files.readString(exchange))
                } catch (ex: Exception) {
                    Report(nodeName, "Could not read the recorder response (${ex.javaClass.simpleName}).")
                } finally {
                    Files.deleteIfExists(exchange)
                }
                if (hit != null && !report.note.startsWith("Armed")) hit.close()
                if (onArmed != null && report.note.startsWith("Armed")) {
                    ApplicationManager.getApplication().invokeLater {
                        if (!session.project.isDisposed && session.isPaused && session.currentStackFrame === frame) onArmed()
                    }
                }
                display(session, report)
            }
        }
        override fun errorOccurred(errorMessage: String) {
            hit?.close()
            Files.deleteIfExists(exchange)
            display(session, Report(nodeName, "Debugger evaluation failed. $errorMessage"))
        }
    }, frame.sourcePosition)
}

private fun decode(text: String): Report = decode(JsonParser.parseString(text).asJsonObject)

private fun decode(root: com.google.gson.JsonObject): Report {
    val rows = root.getAsJsonArray("rows").map {
        val r = it.asJsonObject
        fun loc(key: String): Location? {
            val p = r.get(key)?.takeIf { p -> p.isJsonObject }?.asJsonObject ?: return null
            return Location(p["path"].asString, p["line"].asInt)
        }
        fun field(key: String) = r.get(key)?.takeUnless { v -> v.isJsonNull }?.asString
        if (!r.has("action")) return Report(root["title"].asString, "Start a new recording to use the updated bindings view.")
        Row(r["name"].asString, r["function"].asString, r["action"].asString, field("value"), field("previous"),
            r["forwarded"].asBoolean, r["edge"].asString, r["description"].asString, r["frame"].asString,
            r["caller"].asString, r["time"].asString, r["thread"].asString, field("task"), r["lines"].asString,
            loc("location"), loc("call"))
    }
    return Report(root["title"].asString, root["note"].asString, rows)
}

class Trace : BindingAction("trace")
class Watch : BindingAction("watch")
class Unwatch : BindingAction("unwatch")
class History : BindingAction("history")
