import math
import os

import pytest
from assembly_connectors import connector_pair


def test_round_pair_has_radial_and_axial_clearance_and_independent_materials():
    material = {"name": "Body", "color": "#00FF00"}
    result = connector_pair("peg", "body", "trim", [5, 6, 0], diameter=3,
                            clearance=.2, length=4, lead_in=0, male_material=material)
    features = {f["id"]: f for f in result["features"]}
    assert features["peg_Pin"]["radius"] == 1.5
    assert features["peg_Socket"]["radius"] == 1.7
    assert features["peg_Pin"]["position"]["z"] == -1
    assert features["peg_Socket"]["height"] == pytest.approx(4.21)
    assert features["peg_Male"]["shapes"] == ["body", "peg_Pin"]
    assert features["peg_Female"]["base"] == "trim"
    material["color"] = "#000000"
    assert features["peg_Male"]["material"]["color"] == "#00FF00"


@pytest.mark.parametrize("axis", ["x", "y", "z"])
def test_glue_ledge_socket_is_centered_with_per_side_clearance(axis):
    recipe = connector_pair("ledge", "body", "trim", [5, 6, 7], kind="glue_ledge",
                            axis=axis, width=6, thickness=1.2, clearance=.25)
    assert recipe["lead_in_applied"] is False
    assert "do not apply lead_in" in recipe["notes"][0]
    pin, socket = recipe["features"][:2]
    perpendicular = [d for d in "xyz" if d != axis]
    for dimension in perpendicular:
        assert socket["size"][dimension] - pin["size"][dimension] == pytest.approx(.5)
        assert socket["position"][dimension] - pin["position"][dimension] == pytest.approx(-.25)
    assert socket["size"][axis] == pytest.approx(4.21)


@pytest.mark.parametrize("kwargs", [dict(clearance=-.1), dict(diameter=float("nan")),
                                    dict(position=[True, 0, 0]), dict(lead_in=2),
                                    dict(axis="bad"), dict(prefix="../../bad"),
                                    dict(male_base="trim")])
def test_bad_recipe_rejected(kwargs):
    defaults = dict(prefix="peg", male_base="body", female_base="trim", position=[0,0,0])
    defaults.update(kwargs)
    with pytest.raises(ValueError):
        connector_pair(**defaults)


@pytest.mark.skipif(os.environ.get("FREECAD_MCP_LIVE") != "1", reason="requires test FreeCAD server")
def test_connector_public_api_native_geometry_color_reopen_and_local_seating(tmp_path):
    from test_api_v2_live import call
    name = "McpConnectorTest"
    cid = call("create_component", document=name, name="Connectors", parameters=[
        {"name": "width", "type": "length", "default": "10 mm"}])["component_id"]
    try:
        graph = [
            {"id": "body", "type": "box", "size": {"x": "$width", "y": 10, "z": 2},
             "position": {"z": -2}, "material": {"name": "Green", "color": "#00FF00"}},
            {"id": "trim", "type": "box", "size": {"x": 10, "y": 10, "z": 6},
             "material": {"name": "White", "color": "#FFFFFF"}},
        ]
        call("define_component", component_id=cid, features=graph)
        before = call("get_component", component_id=cid)
        recipe = call("create_connector_features", component_id=cid, prefix="peg",
                      male_base="body", female_base="trim", position=[5,5,0], lead_in=0)
        assert recipe["committed"] is False
        assert call("get_component", component_id=cid) == before
        call("patch_component", component_id=cid, operations=recipe["operations"],
             expected_revision=before["revision"])
        code = "d=App.getDocument(%r)\na=d.getObject('peg_Male')\nb=d.getObject('peg_Female')\nresult={'male':a.Shape.Volume,'female':b.Shape.Volume,'interference':a.Shape.common(b.Shape).Volume,'colors':[a.McpPrintColor,b.McpPrintColor]}" % name
        actual = call("execute", code=code)["result"]
        assert actual["male"] == pytest.approx(200 + math.pi*1.5**2*4)
        assert actual["female"] == pytest.approx(600 - math.pi*1.7**2*4.2)
        assert actual["interference"] < 1e-8
        assert actual["colors"] == ["#00FF00FF", "#FFFFFFFF"]
        from manufacturing_export import read_3mf
        exported = str(tmp_path / "Connector.3mf")
        call("export_component", component_id=cid, path=exported, format="3MF")
        parts = read_3mf(exported)
        assert len(parts) == 2
        assert {p["color"] for p in parts} == {"#00FF00FF", "#FFFFFFFF"}
        seated = call("inspect_attachment", component_id=cid, feature_id="peg_Male",
                      target_id="peg_Female", anchor=[5,5,0], radius=3)
        assert seated["seated"] is True
        absent = call("inspect_attachment", component_id=cid, feature_id="peg_Male",
                     target_id="peg_Female", anchor=[100,100,0], radius=3)
        assert absent["seated"] is False
        assert absent["status"] == "NO_LOCAL_GEOMETRY"
        assert set(absent) == set(seated)
        assert absent["tolerance_mm"] == .05
        path = str(tmp_path / (name + ".FCStd"))
        call("export_component", component_id=cid, path=path)
        call("execute", code="App.closeDocument(%r)\nApp.openDocument(%r)" % (name,path))
        call("set_component_parameters", component_id=cid, values={"width": "12 mm"})
        rebuilt = call("execute", code=code)["result"]
        assert rebuilt["male"] == pytest.approx(240 + math.pi*1.5**2*4)
        assert rebuilt["colors"] == actual["colors"]
    finally:
        call("execute", code="if %r in App.listDocuments(): App.closeDocument(%r)" % (name, name))


@pytest.mark.skipif(os.environ.get("FREECAD_MCP_LIVE") != "1", reason="requires test FreeCAD server")
def test_native_attachment_ignores_contact_elsewhere_and_ledge_has_clearance():
    from test_api_v2_live import call
    code = """import Part
from assembly_connectors import inspect_attachment
body=Part.makeBox(20,10,2)
trim=Part.makeBox(4,10,2,App.Vector(0,0,2)).fuse(Part.makeBox(20,10,1,App.Vector(0,0,4)))
result={'global_gap':body.distToShape(trim)[0], 'seat':inspect_attachment(body,trim,[15,5,3],4)}
"""
    actual = call("execute", code=code)["result"]
    assert actual["global_gap"] == pytest.approx(0)
    assert actual["seat"]["status"] == "GAP"
    assert actual["seat"]["gap_mm"] == pytest.approx(2)
    assert actual["seat"]["seated"] is False
    name = "McpLedgeTest"
    cid = call("create_component", document=name, name="Connectors")["component_id"]
    try:
        call("define_component", component_id=cid, features=[
            {"id":"body", "type":"box", "size":{"x":10,"y":10,"z":2},"position":{"z":-2}},
            {"id":"trim", "type":"box", "size":{"x":10,"y":10,"z":6}},
        ])
        recipe = call("create_connector_features", component_id=cid, prefix="ledge",
                      male_base="body", female_base="trim", position=[5,5,0], kind="glue_ledge")
        call("patch_component", component_id=cid, operations=recipe["operations"])
        code = "d=App.getDocument(%r)\na=d.getObject('ledge_Male').Shape\nb=d.getObject('ledge_Female').Shape\nresult={'male':a.Volume,'female':b.Volume,'interference':a.common(b).Volume}" % name
        actual = call("execute", code=code)["result"]
        assert actual["male"] == pytest.approx(200 + 6*1.2*4)
        assert actual["female"] == pytest.approx(600 - 6.4*1.6*4.2)
        assert actual["interference"] < 1e-8
    finally:
        call("execute", code="if %r in App.listDocuments(): App.closeDocument(%r)" % (name,name))
