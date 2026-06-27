"""Live smoke test for the revisioned FreeCAD MCP component API.

Prerequisite: FreeCAD is running with the workbench server started on port 9876.
This file is intentionally excluded from the normal unit-test suite by requiring
the FREECAD_MCP_LIVE=1 environment variable.
"""
import json
import os
import socket
import tempfile

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("FREECAD_MCP_LIVE") != "1",
    reason="set FREECAD_MCP_LIVE=1 with a running FreeCAD MCP server",
)

HOST, PORT = "localhost", 9876


def _recv_exactly(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("FreeCAD closed the connection")
        data.extend(chunk)
    return bytes(data)


def call(command, **params):
    payload = json.dumps({"type": command, "params": params}).encode()
    with socket.create_connection((HOST, PORT), timeout=60) as sock:
        sock.sendall(len(payload).to_bytes(4, "big") + payload)
        size = int.from_bytes(_recv_exactly(sock, 4), "big")
        response = json.loads(_recv_exactly(sock, size))
    if response.get("status") == "error":
        raise RuntimeError(response.get("message"))
    result = response.get("result", response)
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(result["error"])
    return result


def test_v2_component_workflow():
    call(
        "execute",
        code=(
            "import FreeCAD as App, importlib, parametric, freecad_mcp, types\n"
            "_panel=freecad_mcp._panel\n"
            "_server=_panel.server if _panel else None\n"
            "importlib.reload(parametric)\n"
            "importlib.reload(freecad_mcp)\n"
            "if _server:\n"
            "    _server.handle_get_screenshot=types.MethodType(\n"
            "        freecad_mcp.FreeCADMCPServer.handle_get_screenshot,_server)\n"
            "freecad_mcp._panel=_panel\n"
            "if 'ApiV2Live' in App.listDocuments():\n"
            "    App.closeDocument('ApiV2Live')"
        ),
    )

    feature_types = call("list_feature_types")["feature_types"]
    assert {"tube", "grid_array", "pattern"}.issubset(feature_types)
    capabilities = call("capabilities")
    assert capabilities["api_version"] == "2.0"
    assert capabilities["structural_patch_mode"] == "dependency_scoped"
    tube_schema = call("describe_feature_type", feature_type="tube")
    assert "outer_radius" in tube_schema["schema"]["required"]
    patterns = {item["name"] for item in call("list_patterns")["patterns"]}
    assert {"lego.stud_grid", "lego.brick_underside"}.issubset(patterns)

    expanded = call("expand_pattern", feature={
        "id": "studs",
        "type": "pattern",
        "pattern": "lego.stud_grid",
        "parameters": {
            "count_x": 2, "count_y": 2, "pitch": "8 mm",
            "diameter": "4.8 mm", "height": "1.8 mm",
        },
    })
    assert [feature["id"] for feature in expanded["features"]] == [
        "studs__seed", "studs",
    ]

    export_dir = tempfile.mkdtemp(prefix="freecad_mcp_v2_")
    built = call(
        "build_component",
        document={"name": "ApiV2Live", "replace": True},
        component={
            "name": "FitFixture",
            "features": [
                {
                    "id": "cavity", "type": "box",
                    "size": {"x": "10 mm", "y": "10 mm", "z": "10 mm"},
                    "role": "inspection",
                },
                {
                    "id": "insert", "type": "box",
                    "size": {"x": "8 mm", "y": "8 mm", "z": "8 mm"},
                    "position": {"x": "1 mm", "y": "1 mm", "z": "1 mm"},
                    "role": "output",
                },
                {
                    "id": "profile", "type": "profile_extrude",
                    "points": [[0, 0], [6, 0], [6, 6], [0, 6]],
                    "length": "4 mm",
                    "position": {"x": "20 mm", "y": "0 mm", "z": "0 mm"},
                    "role": "construction",
                },
                {
                    "id": "rounded_profile", "type": "fillet",
                    "base": "profile", "radius": "0.5 mm",
                    "edges": {"parallel_to": "z"},
                    "role": "inspection",
                },
            ],
            "outputs": ["insert"],
        },
        validate={"profiles": ["geometry_baseline", "fdm"]},
        exports={
            "directory": export_dir, "formats": ["STEP"],
            "per_output": True, "assembly": True, "basename": "fixture",
        },
    )
    component_id = built["component_id"]
    assert built["validation"]["validation_status"] == "ok"
    assert all(item["bytes"] > 0 for item in built["exports"]["artifacts"])

    graph = call("get_component_graph", component_id=component_id, detail="full")
    assert graph["revision"] == 1
    assert {feature["type"] for feature in graph["features"]} >= {
        "profile_extrude", "fillet"
    }
    before_visibility = call(
        "execute",
        code=(
            "result={name:App.ActiveDocument.getObject(name).ViewObject.Visibility "
            "for name in ('cavity','insert')}"
        ),
    )["result"]
    screenshot = call(
        "get_screenshot",
        width=256,
        height=256,
        view="iso",
        fit=True,
        targets=["insert"],
        transparent=[],
        temporary=True,
    )
    assert screenshot["width"] == 256
    assert screenshot["visible_objects"] == ["insert"]
    assert screenshot["axis_convention"]["z"] == "up"
    vector_view = call(
        "get_screenshot",
        width=128,
        height=128,
        view="current",
        fit=True,
        targets=["insert"],
        camera={"direction": [1, 1, -1], "up": [0, 0, 1]},
        temporary=True,
    )
    assert vector_view["view"] == "vector"
    contact_sheet = call(
        "get_screenshot",
        width=128,
        height=128,
        fit=True,
        targets=["insert"],
        views=["front", "rear", "top", "bottom"],
        temporary=True,
    )
    assert contact_sheet["view"] == "contact_sheet"
    assert contact_sheet["views"] == ["front", "rear", "top", "bottom"]
    assert contact_sheet["width"] == 512
    after_visibility = call(
        "execute",
        code=(
            "result={name:App.ActiveDocument.getObject(name).ViewObject.Visibility "
            "for name in ('cavity','insert')}"
        ),
    )["result"]
    assert after_visibility == before_visibility

    dry_run = call(
        "patch_component",
        component_id=component_id,
        expected_revision=1,
        dry_run=True,
        operations=[{
            "op": "upsert",
            "feature": {
                "id": "retainer", "type": "box",
                "size": {"x": "1 mm", "y": "10 mm", "z": "10 mm"},
                "position": {"x": "9 mm", "y": "0 mm", "z": "0 mm"},
                "role": "inspection",
            },
        }],
    )
    assert dry_run["added"] == ["retainer"]

    patched = call(
        "patch_component",
        component_id=component_id,
        expected_revision=1,
        operations=[{
            "op": "upsert",
            "feature": {
                "id": "retainer", "type": "box",
                "size": {"x": "1 mm", "y": "10 mm", "z": "10 mm"},
                "position": {"x": "9 mm", "y": "0 mm", "z": "0 mm"},
                "role": "inspection",
            },
        }],
    )
    assert patched["revision"] == 2
    assert patched["regenerated"] == ["retainer"]

    fit = call(
        "check_fit",
        component_id=component_id,
        container="cavity",
        insert="insert",
        retainers=["retainer"],
        probe_steps=20,
    )
    assert fit["contained"] is True
    assert fit["clearance_mm"]["minimum"] == 1.0
    assert fit["blocked_translation"]["+x"] is True
