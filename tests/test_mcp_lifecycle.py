"""Lifecycle/UI regressions with real Qt and loopback sockets, no FreeCAD process.

Run with a Python providing PySide6/PySide2 (e.g. FreeCAD's bundled Python):
    QT_QPA_PLATFORM=offscreen python tests/test_mcp_lifecycle.py
"""
import importlib
import json
import runpy
from pathlib import Path
import socket
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from PySide6 import QtCore, QtWidgets
except ImportError:
    try:
        from PySide2 import QtCore, QtWidgets
    except ImportError:
        QtCore = QtWidgets = None


class Parameters:
    def __init__(self):
        self.values = {}

    def GetBool(self, key, default):
        return self.values.get(key, default)

    GetInt = GetString = GetBool

    def SetBool(self, key, value):
        self.values[key] = value


@unittest.skipIf(QtCore is None, "real Qt bindings required")
class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def setUp(self):
        self.params = Parameters()
        self.window = QtWidgets.QMainWindow()
        fc = types.SimpleNamespace(
            ParamGet=lambda path: self.params,
            Console=types.SimpleNamespace(PrintError=lambda *a: None, PrintMessage=lambda *a: None),
        )
        self.gui = types.SimpleNamespace(getMainWindow=lambda: self.window)
        self.modules = ("freecad_mcp", "mcp_controller", "mcp_statusbar", "mcp_bootstrap", "mcp_qt")
        self.saved = {key: sys.modules.pop(key, None) for key in self.modules}
        self.stub_patch = patch.dict(sys.modules, {"FreeCAD": fc, "FreeCADGui": self.gui})
        self.stub_patch.start()
        self.backend = importlib.import_module("freecad_mcp")
        self.controller_module = importlib.import_module("mcp_controller")
        self.bootstrap = importlib.import_module("mcp_bootstrap")
        self.controller = self.controller_module.MCPController(self.window)
        # Reserve an ephemeral port to avoid interference with the user's listener.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.params.values["Port"] = probe.getsockname()[1]
        self.states = []
        self.controller.state_changed.connect(self.states.append)

    def pump(self):
        for _ in range(5):
            self.app.processEvents()

    def tearDown(self):
        self.bootstrap.shutdown()
        self.controller.shutdown()
        self.window.close()
        self.window.deleteLater()
        self.pump()
        self.stub_patch.stop()
        for key in self.modules:
            sys.modules.pop(key, None)
            if self.saved[key] is not None:
                sys.modules[key] = self.saved[key]

    def start(self):
        self.controller.start()
        self.assertEqual(self.controller.state, "starting")
        self.pump()
        self.assertEqual(self.controller.state, "running")

    def assert_released(self):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", self.params.values["Port"]))

    def test_readiness_repeated_start_stop_and_protocol(self):
        for _ in range(3):
            self.start()
            server = self.controller.server
            self.controller.start()
            self.assertIs(self.controller.server, server)
            with socket.create_connection(("127.0.0.1", self.params.values["Port"])) as client:
                request = json.dumps({"type": "execute", "params": {"code": "result = 42"}}).encode()
                # Replace dispatch only: framing and timer listener are real.
                server.execute_command = lambda command: {"answer": 42}
                client.sendall(len(request).to_bytes(4, "big") + request)
                server._process_server()
                client.settimeout(1)
                length = int.from_bytes(client.recv(4), "big")
                self.assertEqual(json.loads(client.recv(length)), {"answer": 42})
            self.controller.stop()
            self.assertEqual(self.controller.state, "stopping")
            self.pump()
            self.assertEqual(self.controller.state, "stopped")
            self.controller.stop()
            self.assert_released()
        self.assertEqual(self.states[:4], ["starting", "running", "stopping", "stopped"])

    def test_port_conflict_error_retry(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", self.params.values["Port"]))
            occupied.listen()
            self.controller.start()
            self.pump()
            self.assertEqual(self.controller.state, "error")
            self.assertTrue(self.controller.last_error)
            self.assertIsNone(self.controller.server)
        self.start()

    def test_restart_replaces_server(self):
        self.start()
        old = self.controller.server
        self.controller.restart()
        self.pump()
        self.assertEqual(self.controller.state, "running")
        self.assertIsNot(self.controller.server, old)
        self.assertIsNone(old.socket)

    def test_listener_failure_and_external_stop(self):
        self.start()
        self.controller.server.socket.close()
        self.controller.server._process_server()
        self.assertEqual(self.controller.state, "error")
        self.start()
        self.controller.server.stop()
        self.controller._check_health()
        self.assertEqual(self.controller.state, "error")
        self.assert_released()

    def test_client_disconnect_does_not_stop_server(self):
        self.start()
        with socket.create_connection(("127.0.0.1", self.params.values["Port"])):
            self.controller.server._process_server()
        self.controller.server._process_server()
        self.assertEqual(self.controller.state, "running")

    def test_shutdown_cancels_pending_start_and_restart(self):
        self.controller.start()
        self.controller.shutdown()
        self.pump()
        self.assertEqual(self.controller.state, "stopped")
        self.assertIsNone(self.controller.server)
        self.assert_released()

    def test_shutdown_releases_client_and_listener(self):
        self.start()
        with socket.create_connection(("127.0.0.1", self.params.values["Port"])) as client:
            self.controller.server._process_server()
            self.controller.restart()
            self.controller.shutdown()
            self.pump()
            client.settimeout(1)
            self.assertEqual(client.recv(1), b"")
        self.assertEqual(self.controller.state, "stopped")
        self.assert_released()

    def test_status_menu_toggle_and_duplicate_cleanup(self):
        ui_module = importlib.import_module("mcp_statusbar")
        ui = ui_module.MCPStatusBar(self.controller)
        self.assertEqual(ui.button.text(), "○ MCP")
        menu = ui.create_menu()
        self.assertEqual([a.text() for a in menu.actions()], ["Start MCP", "Restart MCP", "", "Start automatically"])
        self.assertFalse(menu.actions()[1].isEnabled())
        menu.actions()[-1].setChecked(True)
        self.assertTrue(self.controller.autostart)
        ui.button.click()
        self.assertFalse(ui.button.isEnabled())
        self.controller.stop()  # transition guard
        self.controller.restart()
        self.pump()
        self.assertEqual(ui.button.text(), "● MCP")
        self.assertTrue(ui.button.isEnabled())
        ui.cleanup()
        ui2 = ui_module.MCPStatusBar(self.controller)
        stale = QtWidgets.QToolButton(self.window.statusBar())
        stale.setObjectName(ui_module.MCPStatusBar.OBJECT_NAME)
        ui2.cleanup()
        ui3 = ui_module.MCPStatusBar(self.controller)
        self.assertEqual(len(self.window.statusBar().findChildren(QtWidgets.QToolButton, ui_module.MCPStatusBar.OBJECT_NAME)), 1)
        self.controller._failure("test failure")
        self.assertIn("test failure", ui3.button.toolTip())
        self.assertIn("#f56c6c", ui3.button.styleSheet())
        ui3.cleanup()

    def test_bootstrap_singleton_reload_and_autostart(self):
        self.assertFalse(self.controller.autostart)
        self.controller.set_autostart(True)
        controller, ui = self.bootstrap.initialize()
        self.pump()
        self.assertEqual(controller.state, "running")
        importlib.reload(self.bootstrap)
        self.assertEqual(self.bootstrap.initialize(), (controller, ui))
        self.bootstrap.shutdown()
        self.assert_released()
        self.assertTrue(self.controller_module.MCPController().autostart)

    def test_both_gui_entrypoints_share_session_without_file_global(self):
        root = Path(__file__).resolve().parents[1]
        # Traditional loader execs the script without supplying __file__.
        exec(compile((root / "InitGui.py").read_text(), str(root / "InitGui.py"), "exec"), {})
        runpy.run_path(str(root / "freecad/mcp_status_control/init_gui.py"))
        self.pump()
        session = self.bootstrap.initialize()
        self.assertEqual(session[0].state, "stopped")
        self.assertEqual(len(self.window.statusBar().findChildren(
            QtWidgets.QToolButton, session[1].OBJECT_NAME)), 1)

    def test_missing_server_dependency_is_retryable(self):
        self.controller._server_factory = lambda **kw: (_ for _ in ()).throw(ImportError("missing dependency"))
        self.controller.start()
        self.pump()
        self.assertEqual(self.controller.state, "error")
        self.assertIn("missing dependency", self.controller.last_error)
        self.controller._server_factory = None
        self.start()


if __name__ == "__main__":
    unittest.main()
