"""Persistent-namespace behaviour of handle_execute (no FreeCAD process).

Stubs FreeCAD/FreeCADGui/PySide so freecad_mcp imports, then drives
handle_execute with no active document (doc=None path) to verify that user
variables persist across calls, `result` is per-call, and reset clears state.
Run: python3 -m pytest tests/test_execute_state.py -q
"""
import importlib
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture
def fcmod(monkeypatch):
    """Import freecad_mcp with FreeCAD/FreeCADGui/PySide stubbed out."""
    fake_fc = types.ModuleType("FreeCAD")
    fake_fc.ActiveDocument = None
    fake_fc.Console = types.SimpleNamespace(PrintError=lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "FreeCAD", fake_fc)
    monkeypatch.setitem(sys.modules, "FreeCADGui", types.ModuleType("FreeCADGui"))
    pyside = types.ModuleType("PySide")
    pyside.QtCore = types.ModuleType("PySide.QtCore")
    pyside.QtGui = types.ModuleType("PySide.QtGui")
    monkeypatch.setitem(sys.modules, "PySide", pyside)
    monkeypatch.setitem(sys.modules, "PySide.QtCore", pyside.QtCore)
    monkeypatch.setitem(sys.modules, "PySide.QtGui", pyside.QtGui)
    sys.modules.pop("freecad_mcp", None)
    mod = importlib.import_module("freecad_mcp")
    yield mod
    sys.modules.pop("freecad_mcp", None)


@pytest.fixture
def server(fcmod):
    srv = fcmod.FreeCADMCPServer.__new__(fcmod.FreeCADMCPServer)
    srv._ns = {}
    srv._recompute_errors = lambda: []
    return srv


def test_fit_dims_noop_when_within_bound(fcmod):
    assert fcmod._fit_dims(800, 600, 1000) == (800, 600)
    assert fcmod._fit_dims(800, 600, None) == (800, 600)


def test_fit_dims_scales_down_keeping_aspect(fcmod):
    assert fcmod._fit_dims(1024, 768, 512) == (512, 384)
    w, h = fcmod._fit_dims(2000, 1000, 200)
    assert max(w, h) == 200 and (w, h) == (200, 100)


def test_variables_persist_across_calls(server):
    assert server.handle_execute("a = 41")["command_result"] == "success"
    r = server.handle_execute("result = a + 1")
    assert r["command_result"] == "success"
    assert r["result"] == 42


def test_result_does_not_leak_into_next_call(server):
    server.handle_execute("result = 99")
    r = server.handle_execute("b = 1")  # sets no result
    assert r["command_result"] == "success"
    assert "result" not in r


def test_reset_clears_state(server):
    server.handle_execute("a = 5")
    r = server.handle_execute("result = a", reset=True)  # `a` should be gone
    assert r["command_result"] == "error"
    assert "name 'a'" in r.get("error", "") or "NameError" in r.get("traceback", "")
