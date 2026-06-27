"""Bridge-level unit checks that don't need a running FreeCAD.

Catches regressions in how the bridge tools unwrap the server's response
envelope ({"status":"success","result":{...}}). Run with a python that has
`mcp`:  .venv/bin/python tests/test_bridge.py
"""
import asyncio
import base64
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import freecad_bridge as fb
from mcp.server.fastmcp import Image

_PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 16).decode()


def test_get_screenshot_unwraps_envelope():
    # Server wraps handler results; get_screenshot must look inside "result".
    async def fake(cmd):
        return {"status": "success",
                "result": {"image_base64": _PNG, "width": 1, "height": 1, "view": "iso"}}
    fb.send_to_freecad = fake
    img = asyncio.run(fb.get_screenshot())
    assert isinstance(img, Image), type(img)


def test_get_screenshot_reports_error():
    async def fake(cmd):
        return {"status": "error", "error": {
            "code": "NO_ACTIVE_VIEW",
            "message": "no active 3D view to capture",
            "recoverable": True,
        }}
    fb.send_to_freecad = fake
    try:
        asyncio.run(fb.get_screenshot())
    except fb.FreeCADToolError as e:
        assert "no active 3D view" in str(e), e
        assert e.code == "NO_ACTIVE_VIEW"
        assert e.recoverable is True
    else:
        raise AssertionError("expected RuntimeError on error envelope")


def test_native_result_is_unwrapped_without_json_string():
    async def fake(cmd):
        return {"status": "success", "result": {"objects": [{"name": "Box"}]}}
    fb.send_to_freecad = fake
    result = asyncio.run(fb.list_objects())
    assert result == {"objects": [{"name": "Box"}]}
    assert not isinstance(result, str)


def test_handler_error_inside_success_envelope_is_raised():
    async def fake(cmd):
        return {"status": "success", "result": {"error": "no active document"}}
    fb.send_to_freecad = fake
    try:
        asyncio.run(fb.list_objects())
    except RuntimeError as e:
        assert "no active document" in str(e)
    else:
        raise AssertionError("expected nested handler error to be raised")


if __name__ == "__main__":
    test_get_screenshot_unwraps_envelope()
    test_get_screenshot_reports_error()
    test_native_result_is_unwrapped_without_json_string()
    test_handler_error_inside_success_envelope_is_raised()
    print("OK: get_screenshot unwraps the result envelope and surfaces errors")
