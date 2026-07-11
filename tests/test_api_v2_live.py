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


class LiveAPIError(RuntimeError):
    def __init__(self, error):
        super().__init__(error.get("message", "FreeCAD command failed"))
        self.code = error.get("code")
        self.recoverable = error.get("recoverable", False)


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
        raise LiveAPIError(response.get("error") or {})
    result = response.get("result", response)
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(result["error"])
    return result


def _assert_manufacturing_workflow():
    call("execute", code=(
        "import FreeCAD as App\n"
        "if 'ApiManufacturingLive' in App.listDocuments():\n"
        "    App.closeDocument('ApiManufacturingLive')"
    ))
    created = call(
        "create_component", document="ApiManufacturingLive", name="ManufacturingPart",
        parameters=[
            {"name": "width", "type": "length", "default": "30 mm"},
            {"name": "hole_d", "type": "length", "default": "4 mm"},
            {"name": "wall", "type": "length", "default": "1.5 mm"},
            {"name": "draft_angle", "type": "angle", "default": "5 deg"},
        ],
    )
    component_id = created["component_id"]
    call(
        "define_component", component_id=component_id,
        geometry_references=[
            {"id": "top", "feature": "panel", "selector": {
                "kind": "face", "where": {"axis": "z", "side": "max"},
                "expect": "one",
            }},
            {"id": "bottom", "feature": "panel", "selector": {
                "kind": "face", "where": {"axis": "z", "side": "min"},
                "expect": "one",
            }},
        ],
        features=[
            {"id": "panel", "type": "box", "size": {
                "x": "$width", "y": "20 mm", "z": "8 mm",
            }},
            {"id": "replacement", "type": "box", "role": "construction", "size": {
                "x": "$width", "y": "20 mm", "z": "8 mm",
            }},
            {"id": "drilled", "type": "hole", "base": "panel",
             "diameter": "$hole_d", "through_all": True,
             "support": {"reference": "top"}, "positions": [[-6, 0], [6, 0]],
             "head": {"type": "counterbore", "diameter": "7 mm", "depth": "2 mm"},
             "thread": {"designation": "M4", "class": "6H"}},
            {"id": "shelled", "type": "shell", "base": "panel",
             "thickness": "$wall", "selector": {"reference": "top"}},
            {"id": "offset", "type": "face_offset", "base": "panel",
             "distance": "2 mm", "selector": {"reference": "top"}},
            {"id": "ribbed", "type": "rib", "base": "panel",
             "thickness": "2 mm", "height": "3 mm",
             "support": {"reference": "top"}, "path_points": [[-8, 0], [8, 0]]},
            {"id": "drafted", "type": "draft", "base": "panel",
             "angle": "$draft_angle", "selector": {"kind": "face", "indices": [1, 2, 3, 4]},
             "neutral_plane": {"reference": "bottom"},
             "pull_selector": {"kind": "edge", "index": 1}},
            {"id": "replaced", "type": "replace_face", "base": "panel",
             "selector": {"reference": "top"}, "replacement": {
                 "feature": "replacement", "selector": {
                     "kind": "face", "where": {"axis": "z", "side": "max"},
                     "expect": "one",
                 },
             }},
        ],
    )
    feature_ids = ["drilled", "shelled", "offset", "ribbed", "drafted", "replaced"]
    before = call("execute", code=(
        "d=App.getDocument('ApiManufacturingLive')\n"
        "ids=%r\n"
        "result={fid:{'valid':d.getObject(fid).Shape.isValid(),"
        "'volume':d.getObject(fid).Shape.Volume} for fid in ids}"
    ) % feature_ids)["result"]
    assert all(item["valid"] and item["volume"] > 0 for item in before.values())

    updated = call("set_component_parameters", component_id=component_id, values={
        "width": "36 mm", "hole_d": "5 mm", "wall": "2 mm",
        "draft_angle": "8 deg",
    })
    assert set(feature_ids) <= set(updated["regenerated"])
    assert len(updated["geometry_references"]) == 2
    assert all(
        item["tracking"]["status"] in ("stable", "remapped")
        for item in updated["geometry_references"]
    )
    after = call("execute", code=(
        "d=App.getDocument('ApiManufacturingLive')\n"
        "ids=%r\n"
        "result={fid:{'valid':d.getObject(fid).Shape.isValid(),"
        "'volume':d.getObject(fid).Shape.Volume} for fid in ids}"
    ) % feature_ids)["result"]
    assert all(item["valid"] for item in after.values())
    assert all(after[fid]["volume"] != before[fid]["volume"] for fid in feature_ids)


def _assert_feature_pattern_workflow():
    call("execute", code=(
        "import FreeCAD as App\n"
        "if 'ApiPatternLive' in App.listDocuments():\n"
        "    App.closeDocument('ApiPatternLive')"
    ))
    created = call(
        "create_component", document="ApiPatternLive", name="Patterns",
        parameters=[
            {"name": "linear_count", "type": "int", "default": 3},
            {"name": "spacing", "type": "length", "default": "6 mm"},
            {"name": "path_count", "type": "int", "default": 5},
        ],
    )
    component_id = created["component_id"]
    call("define_component", component_id=component_id, features=[
        {"id": "seed", "type": "box", "size": {
            "x": "4 mm", "y": "3 mm", "z": "2 mm",
        }, "position": {"x": "5 mm"}},
        {"id": "frame", "type": "datum_frame", "plane": "XY",
         "rotation": {"axis": "normal", "angle": "90 deg"}},
        {"id": "linear", "type": "feature_pattern", "mode": "linear",
         "base": "seed", "count": "$linear_count", "spacing": "$spacing",
         "direction": "u", "frame_id": "frame", "symmetric": True},
        {"id": "grid", "type": "grid_array", "base": "seed",
         "count_x": 2, "count_y": 3,
         "spacing_x": "7 mm", "spacing_y": "5 mm"},
        {"id": "polar", "type": "polar_pattern", "base": "seed",
         "count": 4, "angle": "360 deg", "axis": "z", "center": [0, 0, 0]},
        {"id": "mirrored", "type": "mirror_pattern", "base": "seed",
         "origin": [0, 0, 0], "normal": [1, 0, 0]},
        {"id": "path", "type": "path_pattern", "base": "seed",
         "path_points": [[0, 0, 0], [12, 0, 0], [12, 10, 0]],
         "count": "$path_count", "orientation": "tangent"},
    ])
    expected = {"linear": 3, "grid": 6, "polar": 4, "mirrored": 2, "path": 5}
    before = call("execute", code=(
        "d=App.getDocument('ApiPatternLive')\n"
        "expected=%r\n"
        "result={fid:{'valid':d.getObject(fid).Shape.isValid(),"
        "'solids':len(d.getObject(fid).Shape.Solids)} for fid in expected}"
    ) % expected)["result"]
    assert all(before[fid]["valid"] for fid in expected)
    assert {fid: before[fid]["solids"] for fid in expected} == expected

    updated = call("set_component_parameters", component_id=component_id, values={
        "linear_count": 4, "spacing": "8 mm", "path_count": 6,
    })
    assert updated["regenerated"] == ["linear", "path"]
    after = call("execute", code=(
        "d=App.getDocument('ApiPatternLive')\n"
        "result={fid:len(d.getObject(fid).Shape.Solids) for fid in ('linear','path')}"
    ))["result"]
    assert after == {"linear": 4, "path": 6}


def _assert_assembly_workflow():
    call("execute", code=(
        "import FreeCAD as App\n"
        "for name in ('ApiAssemblySource','ApiAssemblyLive','ApiAssemblyConflict'):\n"
        "    if name in App.listDocuments(): App.closeDocument(name)"
    ))
    source = call(
        "create_component", document="ApiAssemblySource", name="Bracket",
        parameters=[{"name": "width", "type": "length", "default": "20 mm"}],
    )
    source_id = source["component_id"]
    call("define_component", component_id=source_id, features=[
        {"id": "body", "type": "box", "size": {
            "x": "$width", "y": "10 mm", "z": "4 mm",
        }},
        {"id": "left", "type": "datum_frame", "plane": "YZ",
         "origin": [0, 5, 2]},
        {"id": "right", "type": "datum_frame", "plane": "YZ",
         "origin": ["$width", 5, 2]},
    ])
    call("create_component_variant", component_id=source_id,
         name="wide", values={"width": "30 mm"})

    assembly = call(
        "create_component", document="ApiAssemblyLive", name="Assembly",
        parameters=[
            {"name": "gap", "type": "length", "default": "5 mm"},
            {"name": "instance_width", "type": "length", "default": "24 mm"},
            {"name": "mate_angle", "type": "angle", "default": "30 deg"},
        ],
    )
    assembly_id = assembly["component_id"]
    call("define_component", component_id=assembly_id, features=[
        {"id": "fixed", "type": "component_instance",
         "component_id": source_id, "output": "body", "grounded": True,
         "position": {"x": "10 mm"}},
        {"id": "offset_part", "type": "component_instance",
         "component_id": source_id, "output": "body", "variant": "wide"},
        {"id": "angle_part", "type": "component_instance",
         "component_id": source_id, "output": "body",
         "parameters": {"width": "$instance_width"}},
        {"id": "rigid_part", "type": "component_instance",
         "component_id": source_id, "output": "body"},
        {"id": "coincident_part", "type": "component_instance",
         "component_id": source_id, "output": "body"},
        {"id": "revolute_part", "type": "component_instance",
         "component_id": source_id, "output": "body"},
        {"id": "multi_part", "type": "component_instance",
         "component_id": source_id, "output": "body"},
        {"id": "offset_mate", "type": "mate", "mode": "offset",
         "moving": {"instance": "offset_part", "frame": "left"},
         "target": {"instance": "fixed", "frame": "right"},
         "offset": {"normal": "$gap"}},
        {"id": "angle_mate", "type": "mate", "mode": "angle",
         "moving": {"instance": "angle_part", "frame": "left"},
         "target": {"instance": "fixed", "frame": "right"},
         "angle": "$mate_angle"},
        {"id": "rigid_mate", "type": "mate", "mode": "rigid",
         "moving": {"instance": "rigid_part", "frame": "left"},
         "target": {"instance": "fixed", "frame": "right"}},
        {"id": "coincident_mate", "type": "mate", "mode": "coincident",
         "moving": {"instance": "coincident_part", "frame": "left"},
         "target": {"instance": "fixed", "frame": "right"}},
        {"id": "revolute_mate", "type": "mate", "mode": "revolute",
         "moving": {"instance": "revolute_part", "frame": "left"},
         "target": {"instance": "fixed", "frame": "right"}},
        {"id": "slider_mate", "type": "mate", "mode": "slider",
         "moving": {"instance": "multi_part", "frame": "left"},
         "target": {"instance": "fixed", "frame": "right"}},
        {"id": "planar_mate", "type": "mate", "mode": "planar",
         "moving": {"instance": "multi_part", "frame": "left"},
         "target": {"instance": "fixed", "frame": "right"}},
    ])
    status = call("get_assembly_status", component_id=assembly_id)
    assert status["satisfied"] is True
    assert status["solver_status"] == "underconstrained"
    assert status["degrees_of_freedom"] == 1
    assert len(status["instances"]) == 7
    assert {mate["mode"] for mate in status["mates"]} == {
        "rigid", "coincident", "offset", "angle", "revolute", "slider", "planar",
    }
    states = {item["id"]: item for item in status["instances"]}
    assert states["fixed"]["grounded"] is True
    assert states["revolute_part"]["degrees_of_freedom"] == 1
    assert states["multi_part"]["constraint_count"] == 2
    assert states["multi_part"]["degrees_of_freedom"] == 0

    conflict = call(
        "create_component", document="ApiAssemblyConflict", name="Conflict",
    )
    conflict_id = conflict["component_id"]
    call("define_component", component_id=conflict_id, features=[
        {"id": "base", "type": "component_instance", "component_id": source_id,
         "output": "body", "grounded": True},
        {"id": "moving", "type": "component_instance", "component_id": source_id,
         "output": "body"},
        {"id": "mate_a", "type": "mate", "mode": "rigid",
         "moving": {"instance": "moving", "frame": "left"},
         "target": {"instance": "base", "frame": "right"}},
        {"id": "mate_b", "type": "mate", "mode": "rigid",
         "moving": {"instance": "moving", "frame": "left"},
         "target": {"instance": "base", "frame": "right"},
         "offset": {"normal": "3 mm"}},
    ])
    conflict_status = call("get_assembly_status", component_id=conflict_id)
    assert conflict_status["satisfied"] is False
    assert conflict_status["overconstrained"] is True
    assert conflict_status["solver_status"] == "overconstrained"
    assert sum(not mate["satisfied"] for mate in conflict_status["mates"]) >= 1

    updated = call("set_component_parameters", component_id=assembly_id, values={
        "gap": "7 mm", "instance_width": "26 mm", "mate_angle": "45 deg",
    })
    assert {"angle_part", "offset_mate", "angle_mate"} <= set(updated["regenerated"])
    call("set_component_parameters", component_id=source_id, values={"width": "22 mm"})
    refreshed = call("refresh_assembly", component_id=assembly_id)
    assert refreshed["satisfied"] is True
    assert len(refreshed["refreshed_instances"]) == 7
    assert len(refreshed["refreshed_mates"]) == 7
    inspected = call("execute", code=(
        "d=App.getDocument('ApiAssemblyLive')\n"
        "result={'fixed_volume':d.getObject('fixed').Shape.Volume,"
        "'rigid_x':d.getObject('rigid_part').Placement.Base.x,"
        "'offset_x':d.getObject('offset_part').Placement.Base.x}"
    ))["result"]
    assert abs(inspected["fixed_volume"] - 880.0) < 1e-6
    assert abs(inspected["rigid_x"] - 32.0) < 1e-6
    assert abs(inspected["offset_x"] - 39.0) < 1e-6

    path = os.path.join(tempfile.mkdtemp(prefix="freecad_mcp_assembly_"),
                        "ApiAssemblyLive.FCStd")
    call("execute", code="App.getDocument('ApiAssemblyLive').saveAs(%r)" % path)
    call("execute", code="App.closeDocument('ApiAssemblyLive')")
    call("execute", code="App.openDocument(%r)" % path)
    reopened = call("get_assembly_status", component_id=assembly_id)
    assert reopened["satisfied"] is True
    assert all(instance["last_refresh"] == "ok" for instance in reopened["instances"])
    assert call("refresh_assembly", component_id=assembly_id)["satisfied"] is True


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
            "    _server.execute_command=types.MethodType(\n"
            "        freecad_mcp.FreeCADMCPServer.execute_command,_server)\n"
            "    _server._component_handlers=types.MethodType(\n"
            "        freecad_mcp.FreeCADMCPServer._component_handlers,_server)\n"
            "freecad_mcp._panel=_panel\n"
            "if 'ApiV2Live' in App.listDocuments():\n"
            "    App.closeDocument('ApiV2Live')"
        ),
    )

    feature_types = call("list_feature_types")["feature_types"]
    assert {
        "tube", "grid_array", "pattern", "datum_frame", "hole", "shell",
        "draft", "rib", "face_offset", "replace_face", "feature_pattern",
        "linear_pattern", "polar_pattern", "mirror_pattern", "path_pattern",
        "component_instance", "mate",
    }.issubset(feature_types)
    capabilities = call("capabilities")
    assert capabilities["api_version"] == "2.9"
    assert capabilities["structural_patch_mode"] == "dependency_scoped"
    assert capabilities["manufacturing_features"] == [
        "hole", "shell", "draft", "rib", "face_offset", "replace_face",
    ]
    assert capabilities["feature_pattern_modes"] == [
        "linear", "grid", "polar", "mirror", "path",
    ]
    assert capabilities["mate_modes"] == [
        "rigid", "coincident", "offset", "angle",
        "revolute", "slider", "cylindrical", "planar",
    ]
    tube_schema = call("describe_feature_type", feature_type="tube")
    assert "outer_radius" in tube_schema["schema"]["required"]
    with pytest.raises(LiveAPIError) as unknown:
        call("definitely_not_a_command")
    assert unknown.value.code == "UNKNOWN_COMMAND"
    assert unknown.value.recoverable is True
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
    assert dry_run["valid"] is True
    assert dry_run["committed"] is False
    assert "retainer" in dry_run["after"]["measurements"]
    unchanged = call(
        "execute",
        code=(
            "d=App.getDocument('ApiV2Live')\n"
            "result={'retainer':d.getObject('retainer') is not None}"
        ),
    )["result"]
    assert unchanged["retainer"] is False
    assert call("get_component_graph", component_id=component_id)["revision"] == 1

    stored_validation = call(
        "get_component", component_id=component_id
    )["validation"]
    validated_patch = call(
        "validate_patch", component_id=component_id, expected_revision=1,
        operations=[{
            "op": "upsert",
            "feature": {
                "id": "preview_brace", "type": "box",
                "size": {"x": "2 mm", "y": "2 mm", "z": "12 mm"},
                "role": "inspection",
            },
        }],
    )
    assert validated_patch["valid"] is True
    assert validated_patch["committed"] is False
    assert validated_patch["validation"]["validation_status"] == "ok"
    assert "preview_brace" in validated_patch["after"]["measurements"]
    assert call("execute", code=(
        "result=App.getDocument('ApiV2Live').getObject('preview_brace') is None"
    ))["result"] is True
    assert call("get_component", component_id=component_id)["validation"] == \
        stored_validation

    diagnostics = call(
        "get_feature_diagnostics", component_id=component_id,
        feature_id="insert",
    )
    assert diagnostics["status"] == "ok"
    assert diagnostics["features"][0]["shape"]["valid"] is True

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
    _assert_manufacturing_workflow()
    _assert_feature_pattern_workflow()
    _assert_assembly_workflow()


def test_proxy_survives_save_reload(tmp_path):
    """FeaturePython proxies must survive save/reopen and keep recomputing."""
    doc_name = "ProxyReload"
    # openDocument names the reopened doc after the file's basename, and the
    # component_id embeds the doc name — so the file must be <doc_name>.FCStd
    # for the component to resolve after a reload.
    path = str(tmp_path / (doc_name + ".FCStd"))

    call("execute", code=(
        "import FreeCAD as App\n"
        "if %r in App.listDocuments():\n"
        "    App.closeDocument(%r)" % (doc_name, doc_name)
    ))

    built = call(
        "build_component",
        document={"name": doc_name, "replace": True},
        component={
            "name": "Block",
            "parameters": [{"name": "cham", "type": "length", "default": "3 mm"}],
            "features": [
                {"id": "body", "type": "box",
                 "size": {"x": "40 mm", "y": "40 mm", "z": "40 mm"}},
                {"id": "ch", "type": "chamfer", "base": "body",
                 "size": "$cham", "edges": {"parallel_to": "z"}, "role": "output"},
                {"id": "patterned", "type": "feature_pattern", "mode": "linear",
                 "base": "body", "count": 2, "spacing": "$cham + 45 mm",
                 "axis": "x", "role": "inspection"},
            ],
            "outputs": ["ch"],
        },
    )
    component_id = built["component_id"]
    ch_name = built["defined"]["object_names"]["ch"]
    pattern_name = built["defined"]["object_names"]["patterned"]

    def chamfer_volume():
        return call("execute", code=(
            "result = App.getDocument(%r).getObject(%r).Shape.Volume"
            % (doc_name, ch_name)
        ))["result"]

    before = chamfer_volume()
    pattern_before = call("execute", code=(
        "result = App.getDocument(%r).getObject(%r).Shape.BoundBox.XLength"
        % (doc_name, pattern_name)
    ))["result"]

    preview = call(
        "preview_parameter_update", component_id=component_id,
        values={"cham": "8 mm"}, rebuild=True, validate=True,
    )
    assert preview["valid"] is True
    assert preview["committed"] is False
    assert preview["before"]["parameters"]["cham"] != \
        preview["after"]["parameters"]["cham"]
    assert preview["before"]["volume"] != preview["after"]["volume"]
    assert abs(chamfer_volume() - before) < 1e-6
    assert call("get_component", component_id=component_id)["validation"] is None

    explanation = call(
        "explain_rebuild", component_id=component_id,
        parameters={"cham": "8 mm"},
    )
    assert {"ch", "patterned"} <= set(explanation["affected"])
    invalid_preview = call(
        "preview_parameter_update", component_id=component_id,
        values={"unknown_parameter": 1},
    )
    assert invalid_preview["valid"] is False
    assert invalid_preview["errors"][0]["code"] == "PREVIEW_FAILED"

    # round-trip through disk: this is where a lost Proxy would surface
    call("execute", code="App.getDocument(%r).saveAs(%r)" % (doc_name, path))
    call("execute", code="App.closeDocument(%r)" % doc_name)
    call("execute", code="App.openDocument(%r)" % path)

    # changing the bound param must drive the restored chamfer proxy to recompute
    call("set_component_parameters", component_id=component_id,
         values={"cham": "8 mm"}, rebuild=True)
    after = chamfer_volume()
    pattern_after = call("execute", code=(
        "result = App.getDocument(%r).getObject(%r).Shape.BoundBox.XLength"
        % (doc_name, pattern_name)
    ))["result"]

    # a bigger chamfer cuts away more material -> strictly smaller volume
    assert after < before - 1.0, (before, after)
    assert pattern_after > pattern_before + 4.9, (pattern_before, pattern_after)
