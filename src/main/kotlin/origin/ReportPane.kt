package origin

import com.intellij.icons.AllIcons
import com.intellij.openapi.fileEditor.OpenFileDescriptor
import com.intellij.openapi.project.Project
import com.intellij.openapi.vfs.LocalFileSystem
import com.intellij.ui.AppUIUtil
import com.intellij.ui.ClickListener
import com.intellij.ui.ColoredTreeCellRenderer
import com.intellij.ui.SimpleTextAttributes
import com.intellij.ui.TreeSpeedSearch
import com.intellij.ui.components.JBLabel
import com.intellij.ui.components.JBScrollPane
import com.intellij.ui.treeStructure.Tree
import com.intellij.util.ui.JBUI
import com.intellij.xdebugger.XDebugSession
import java.awt.BorderLayout
import java.awt.Cursor
import java.awt.event.ActionEvent
import java.awt.event.MouseAdapter
import java.awt.event.MouseEvent
import javax.swing.AbstractAction
import javax.swing.JMenuItem
import javax.swing.JPanel
import javax.swing.JPopupMenu
import javax.swing.JTree
import javax.swing.JTextArea
import javax.swing.KeyStroke
import javax.swing.SwingUtilities
import javax.swing.tree.DefaultMutableTreeNode
import javax.swing.tree.DefaultTreeModel
import javax.swing.tree.TreePath

internal data class Location(val path: String, val line: Int) {
    override fun toString() = "${path.substringAfterLast('/')}:$line"
}

internal data class Row(val name: String, val function: String, val action: String, val value: String?,
                        val previous: String?, val forwarded: Boolean, val edge: String, val description: String,
                        val frame: String, val caller: String, val time: String, val thread: String, val task: String?,
                        val lines: String, val location: Location?, val call: Location?) {
    override fun toString() = "$name in $function()"
}
internal data class Report(val title: String, val note: String, val rows: List<Row> = emptyList())
private data class Detail(val text: String, val target: Location? = null) {
    override fun toString() = text
}

// Populate a binding's metadata only when the user inspects it.
private class Binding(val row: Row) : DefaultMutableTreeNode(row) {
    override fun isLeaf() = false
    override fun getChildCount(): Int {
        if (children == null) {
            fun detail(text: String, target: Location? = null) {
                // insert() avoids add() asking for childCount during initialization.
                insert(DefaultMutableTreeNode(Detail(text, target), false), super.getChildCount())
            }
            detail("${row.action} in ${row.function}() · ${row.edge}")
            detail(row.description)
            row.location?.let { detail("Source", it) }
            detail("Called from ${row.caller}", row.call)
            detail("Frame #${row.frame} · thread ${row.thread}" + (row.task?.let { " · task $it" } ?: ""))
            detail(row.time)
            if (row.lines.isNotEmpty()) detail("Recent lines  ${row.lines}")
        }
        return super.getChildCount()
    }
}

internal class ReportPane(private val project: Project) : JPanel(BorderLayout()) {
    var session: XDebugSession? = null
    private val title = JBLabel()
    private val tree = Tree()
    private val note = JTextArea().apply {
        isEditable = false
        isOpaque = false
        lineWrap = true
        wrapStyleWord = true
        font = title.font
        foreground = title.foreground
        border = JBUI.Borders.empty(4, 8)
    }
    private val content = JBScrollPane(tree)
    private val renderer = object : ColoredTreeCellRenderer() {
        override fun customizeCellRenderer(tree: JTree, value: Any?, selected: Boolean, expanded: Boolean,
                                           leaf: Boolean, rowIndex: Int, hasFocus: Boolean) {
            val node = value as DefaultMutableTreeNode
            when (val item = node.userObject) {
                is Row -> {
                    icon = AllIcons.Debugger.Db_primitive
                    append(item.name, SimpleTextAttributes.REGULAR_ATTRIBUTES)
                    if (item.previous != null) append(" = ${item.previous} → ${item.value ?: "(deleted)"}")
                    else append(" = ${item.value ?: "(deleted)"}")
                    append("   ${item.function}()", SimpleTextAttributes.GRAYED_ATTRIBUTES)
                    if (item.edge != "observed") append(" · ${item.edge}", SimpleTextAttributes.GRAYED_ATTRIBUTES)
                    item.location?.let { append("   $it", SimpleTextAttributes.LINK_PLAIN_ATTRIBUTES, it) }
                }
                is Detail -> {
                    append(item.text, SimpleTextAttributes.GRAYED_ATTRIBUTES)
                    item.target?.let { append("   $it", SimpleTextAttributes.LINK_PLAIN_ATTRIBUTES, it) }
                }
                else -> {
                    icon = AllIcons.Debugger.Frame
                    append(item.toString())
                }
            }
        }
    }

    init {
        title.border = JBUI.Borders.empty(3, 8)
        add(title, BorderLayout.NORTH)
        tree.isRootVisible = false
        tree.showsRootHandles = true
        tree.cellRenderer = renderer
        TreeSpeedSearch.installOn(tree)
        val mouse = object : MouseAdapter() {
            override fun mouseMoved(e: MouseEvent) {
                tree.cursor = if (linkAt(e) != null) Cursor.getPredefinedCursor(Cursor.HAND_CURSOR) else Cursor.getDefaultCursor()
            }
            override fun mousePressed(e: MouseEvent) = popup(e)
            override fun mouseReleased(e: MouseEvent) = popup(e)
        }
        tree.addMouseListener(mouse)
        tree.addMouseMotionListener(mouse)
        object : ClickListener() {
            override fun onClick(event: MouseEvent, clickCount: Int): Boolean {
                if (!SwingUtilities.isLeftMouseButton(event) || clickCount != 1) return false
                val location = linkAt(event) ?: return false
                open(location)
                return true
            }
        }.installOn(tree)
        for (key in listOf("ENTER", "F4")) tree.getInputMap().put(KeyStroke.getKeyStroke(key), "origin.source")
        tree.actionMap.put("origin.source", object : AbstractAction() {
            override fun actionPerformed(e: ActionEvent?) {
                val path = tree.selectionPath ?: return
                val loc = source(path)
                if (loc != null) open(loc)
                else if (tree.isExpanded(path)) tree.collapsePath(path) else tree.expandPath(path)
            }
        })
        add(content, BorderLayout.CENTER)
        clearReport()
    }

    private fun linkAt(e: MouseEvent): Location? {
        val path = tree.getPathForLocation(e.x, e.y) ?: return null
        val bounds = tree.getPathBounds(path) ?: return null
        val index = tree.getRowForPath(path)
        AppUIUtil.targetToDevice(renderer, tree)
        renderer.getTreeCellRendererComponent(tree, path.lastPathComponent, false, false,
            tree.model.isLeaf(path.lastPathComponent), index, false)
        return renderer.getFragmentTagAt(e.x - bounds.x) as? Location
    }

    private fun source(path: TreePath): Location? = when (val item = (path.lastPathComponent as DefaultMutableTreeNode).userObject) {
        is Row -> item.location
        is Detail -> item.target
        else -> null
    }

    private fun popup(e: MouseEvent) {
        if (!e.isPopupTrigger) return
        val path = tree.getPathForLocation(e.x, e.y) ?: return
        val loc = source(path) ?: return
        tree.selectionPath = path
        val menu = JPopupMenu()
        fun item(text: String, target: Location) {
            menu.add(JMenuItem(text).apply { addActionListener { open(target) } })
        }
        item("Jump to Source", loc)
        ((path.lastPathComponent as DefaultMutableTreeNode).userObject as? Row)?.call?.let { item("Jump to Caller", it) }
        menu.show(tree, e.x, e.y)
    }

    private fun open(location: Location) {
        val file = LocalFileSystem.getInstance().findFileByPath(location.path) ?: return
        OpenFileDescriptor(project, file, location.line - 1, 0).navigate(true)
    }

    fun clearReport() {
        session = null
        render(Report(windowName, "Select a local in a recorded debug session."))
    }

    fun render(report: Report) {
        val rows = report.rows
        title.text = report.title + if (rows.size > 1) "   ·   ${rows.size} bindings" else ""
        val root = DefaultMutableTreeNode()
        // A long chain of direct forwards is one expandable run. Keep its
        // endpoints and every transformation visible in chronological order.
        var i = 0
        while (i < rows.size) {
            var end = i
            while (end < rows.size && rows[end].forwarded) end++
            if (end - i >= 5) {
                root.add(Binding(rows[i]))
                val group = DefaultMutableTreeNode("${end-i-2} calls forwarded the value unchanged")
                for (j in i+1 until end-1) group.add(Binding(rows[j]))
                root.add(group)
                root.add(Binding(rows[end-1]))
                i = end
            } else {
                root.add(Binding(rows[i++]))
            }
        }
        tree.model = DefaultTreeModel(root)
        tree.expandPath(TreePath(root))
        val warning = report.note.substringAfter("Earliest retained producer first.", report.note).trim()
        remove(note)
        if (rows.isEmpty()) {
            content.setViewportView(note)
            note.text = report.note
            note.caretPosition = 0
        } else {
            if (content.viewport.view !== tree) content.setViewportView(tree)
            if (warning.isNotEmpty()) {
                note.text = warning
                add(note, BorderLayout.SOUTH)
            }
        }
        revalidate()
        repaint()
    }
}
