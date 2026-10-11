"""GUI-thread lifecycle control for the existing FreeCAD TCP service."""
import traceback

import FreeCAD as App
from mcp_qt import QtCore

PARAM_PATH = "User parameter:BaseApp/Preferences/Mod/FreeCADMCP"


class MCPController(QtCore.QObject):
    state_changed = QtCore.Signal(str)

    def __init__(self, parent=None, server_factory=None):
        super().__init__(parent)
        self._state = "stopped"
        self.last_error = ""
        self.server = None
        self._server_factory = server_factory
        self._params = App.ParamGet(PARAM_PATH)
        self._closing = False
        self._monitor = QtCore.QTimer(self)
        self._monitor.setInterval(250)
        self._monitor.timeout.connect(self._check_health)

    @property
    def state(self):
        return self._state

    @property
    def autostart(self):
        return self._params.GetBool("AutoStart", False)

    def set_autostart(self, enabled):
        self._params.SetBool("AutoStart", bool(enabled))

    def _set_state(self, state):
        self._state = state
        self.state_changed.emit(state)

    def start(self):
        if self._closing or self.state not in ("stopped", "error"):
            return
        self.last_error = ""
        self._set_state("starting")
        QtCore.QTimer.singleShot(0, self._start)

    def _start(self):
        if self._closing or self.state != "starting":
            return
        try:
            factory = self._server_factory
            if factory is None:
                from freecad_mcp import FreeCADMCPServer
                factory = FreeCADMCPServer
            self.server = factory(
                host=self._params.GetString("BindAddress", "127.0.0.1"),
                port=self._params.GetInt("Port", 9876),
            )
            self.server.on_error = self._failure
            self.server.start()
            if (not self.server.running or self.server.socket is None
                    or self.server.timer is None or not self.server.timer.isActive()):
                raise RuntimeError("MCP listener did not become ready")
            self._monitor.start()
            self._set_state("running")
        except Exception as error:
            self._failure(str(error))

    def _failure(self, reason):
        self._monitor.stop()
        self.last_error = reason
        App.Console.PrintError("FreeCAD MCP: " + reason + "\n" + traceback.format_exc())
        try:
            self._dispose_server()
        except Exception:
            App.Console.PrintError(traceback.format_exc())
        self._set_state("error")

    def _dispose_server(self):
        if self.server is not None:
            self.server.stop()
            self.server = None

    def _check_health(self):
        if self.state == "running" and (
            self.server is None or not self.server.running
            or self.server.socket is None or self.server.socket.fileno() < 0
            or self.server.timer is None or not self.server.timer.isActive()
        ):
            self._failure("MCP listener stopped unexpectedly")

    def stop(self):
        if self.state in ("stopped", "starting", "stopping"):
            return
        self._set_state("stopping")
        QtCore.QTimer.singleShot(0, self._stop)

    def _stop(self, restart=False):
        try:
            self._monitor.stop()
            self._dispose_server()
            self._set_state("stopped")
            if restart:
                self.start()
        except Exception as error:
            self._failure(str(error))

    def restart(self):
        if self._closing or self.state not in ("running", "error"):
            return
        self._set_state("stopping")
        QtCore.QTimer.singleShot(0, lambda: self._stop(restart=True))

    def shutdown(self):
        # aboutToQuit cannot depend on another event-loop turn.
        self._closing = True
        self._stop()
