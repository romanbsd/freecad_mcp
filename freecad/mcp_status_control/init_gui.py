"""Support loaders that only run namespaced GUI init for <other/> addons."""
from pathlib import Path
import sys

_addon_dir = str(Path(__file__).resolve().parents[2])
if _addon_dir not in sys.path:
    sys.path.insert(0, _addon_dir)

from mcp_qt import QtCore
import mcp_bootstrap

QtCore.QTimer.singleShot(0, mcp_bootstrap.initialize)
