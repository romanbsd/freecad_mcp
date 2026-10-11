"""Permanent FreeCAD status-bar control, independent of workbench activation."""
import FreeCADGui as Gui
from mcp_qt import QtCore, QtWidgets


class MCPStatusBar(QtCore.QObject):
    OBJECT_NAME = "FreeCADMCP_StatusButton"
    STATES = {
        "stopped": ("○ MCP", "#888888"),
        "starting": ("◐ MCP", "#e6a23c"),
        "running": ("● MCP", "#67c23a"),
        "stopping": ("◐ MCP", "#e6a23c"),
        "error": ("● MCP", "#f56c6c"),
    }

    def __init__(self, controller):
        self.main_window = Gui.getMainWindow()
        if self.main_window is None:
            raise RuntimeError("FreeCAD main window is unavailable")
        super().__init__(self.main_window)
        self.controller = controller
        self.status_bar = self.main_window.statusBar()
        for existing in self.status_bar.findChildren(QtWidgets.QToolButton, self.OBJECT_NAME):
            self.status_bar.removeWidget(existing)
            existing.setObjectName("")
            existing.hide()
            existing.deleteLater()
        self.button = QtWidgets.QToolButton(self.status_bar)
        self.button.setObjectName(self.OBJECT_NAME)
        self.button.setAutoRaise(True)
        self.button.clicked.connect(self.toggle)
        self.button.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        self.button.customContextMenuRequested.connect(self.show_context_menu)
        self.status_bar.addPermanentWidget(self.button)
        self.controller.state_changed.connect(self.update_state)
        self.update_state(self.controller.state)

    @QtCore.Slot(str)
    def update_state(self, state):
        text, color = self.STATES[state]
        self.button.setText(text)
        self.button.setStyleSheet(f"QToolButton {{ color: {color}; }}")
        tooltip = f"FreeCAD MCP Server: {state}"
        if state == "error" and self.controller.last_error:
            tooltip += "\n" + self.controller.last_error
        self.button.setToolTip(tooltip)
        self.button.setEnabled(state not in ("starting", "stopping"))

    def toggle(self):
        if self.controller.state == "running":
            self.controller.stop()
        elif self.controller.state in ("stopped", "error"):
            self.controller.start()

    def create_menu(self):
        menu = QtWidgets.QMenu(self.button)
        state = self.controller.state
        action = menu.addAction("Stop MCP" if state == "running" else "Start MCP")
        action.setEnabled(state in ("running", "stopped", "error"))
        action.triggered.connect(lambda checked=False: self.toggle())
        restart = menu.addAction("Restart MCP")
        restart.setEnabled(state in ("running", "error"))
        restart.triggered.connect(lambda checked=False: self.controller.restart())
        menu.addSeparator()
        autostart = menu.addAction("Start automatically")
        autostart.setCheckable(True)
        autostart.setChecked(self.controller.autostart)
        autostart.toggled.connect(self.controller.set_autostart)
        return menu

    def show_context_menu(self, position):
        menu = self.create_menu()
        try:
            execute = getattr(menu, "exec", None) or menu.exec_
            execute(self.button.mapToGlobal(position))
        finally:
            menu.deleteLater()

    def cleanup(self):
        if self.button is None:
            return
        self.controller.state_changed.disconnect(self.update_state)
        self.status_bar.removeWidget(self.button)
        self.button.setObjectName("")
        self.button.hide()
        self.button.deleteLater()
        self.button = None
