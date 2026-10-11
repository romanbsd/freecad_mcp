"""Workbench-independent GUI startup for the FreeCAD MCP addon."""
# FreeCAD puts the addon directory on sys.path before executing this script.
# InitGui.py is exec'd, so __file__ is not reliably the addon script's path.
from mcp_qt import QtCore
import mcp_bootstrap

QtCore.QTimer.singleShot(0, mcp_bootstrap.initialize)
