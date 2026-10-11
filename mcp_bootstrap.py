"""Initialize once per FreeCAD GUI session, including repeated addon loads."""
import traceback

import FreeCAD as App
import FreeCADGui as Gui
from mcp_qt import QtCore, QtWidgets
from mcp_controller import MCPController
from mcp_statusbar import MCPStatusBar

_SESSION_KEY = "_freecad_mcp_session"
_attempts = 0


def initialize():
    global _attempts
    existing = getattr(Gui, _SESSION_KEY, None)
    if existing is not None:
        return existing
    main_window = Gui.getMainWindow()
    if main_window is None:
        _attempts += 1
        if _attempts <= 50:
            QtCore.QTimer.singleShot(100, initialize)
        else:
            App.Console.PrintError("FreeCAD MCP: main window unavailable after startup\n")
        return None
    try:
        controller = MCPController(main_window)
        statusbar = MCPStatusBar(controller)
        session = (controller, statusbar)
        setattr(Gui, _SESSION_KEY, session)
        setattr(Gui, _SESSION_KEY + "_shutdown", shutdown)
        QtWidgets.QApplication.instance().aboutToQuit.connect(shutdown)
        if controller.autostart:
            controller.start()
        return session
    except Exception:
        App.Console.PrintError("FreeCAD MCP initialization failed:\n" + traceback.format_exc())
        return None


def shutdown():
    session = getattr(Gui, _SESSION_KEY, None)
    if session is None:
        return
    controller, statusbar = session
    controller.shutdown()
    statusbar.cleanup()
    controller.deleteLater()
    statusbar.deleteLater()
    delattr(Gui, _SESSION_KEY)
    try:
        callback = getattr(Gui, _SESSION_KEY + "_shutdown", shutdown)
        QtWidgets.QApplication.instance().aboutToQuit.disconnect(callback)
        delattr(Gui, _SESSION_KEY + "_shutdown")
    except (RuntimeError, TypeError):
        pass
