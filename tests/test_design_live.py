"""Run with FREECAD_MCP_LIVE=1 against the FreeCAD GUI server."""
import base64
import math
import os
import xml.etree.ElementTree as ET

import pytest
from test_api_v2_live import call, LiveAPIError
from manufacturing_export import read_3mf

pytestmark = pytest.mark.skipif(os.environ.get("FREECAD_MCP_LIVE") != "1",
                              reason="requires running FreeCAD MCP server")


@pytest.fixture
def component():
    previous = call("execute", code="result=App.ActiveDocument.Name if App.ActiveDocument else None")["result"]
    name = "McpDesignTest"
    call("execute", code="if %r in App.listDocuments(): App.closeDocument(%r)" % (name,name))
    cid = call("create_component", document=name, name="Design", parameters=[
        {"name":"height", "type":"length", "default":"6 mm"}])["component_id"]
    yield cid
    call("execute", code="App.closeDocument(%r)\nif %r in App.listDocuments(): App.setActiveDocument(%r)" % (name,previous,previous))


def guided():
    return {"id":"hood", "type":"guided_surface", "guides": {
        "bottom":[[0,0,0],[5,0,0],[10,0,0]],
        "top":[[0,10,0],[5,10,0],[10,10,0]],
        "left":[[0,0,0],[0,5,"$height"],[0,10,0]],
        "right":[[10,0,0],[10,5,"$height"],[10,10,0]]}}


def point_at(feature, u=.5, v=.5):
    return call("execute", code="result=list(App.getDocument('McpDesignTest').getObject(%r).Shape.Faces[0].valueAt(%r,%r))" % (feature,u,v))["result"]


def test_guided_patch_boundaries_curvature_rebuild_rollback_and_reopen(component, tmp_path):
    call("define_component", component_id=component, features=[guided()])
    assert point_at("hood") == pytest.approx([5,5,3])
    for u in [0,.25,.75,1]:
        for v in [0,1]:
            assert point_at("hood",u,v) == pytest.approx([10*u,10*v,0])
    for u in [0,.25,.75,1]:
        assert point_at("hood",u,.25) == pytest.approx([10*u,2.5,2.25])
    inspected = call("inspect_surface", component_id=component, feature_id="hood",
                     samples=3, stripe_axis=[0,1,0], stripe_count=11)
    center = inspected["faces"][0]["samples"][4]
    assert center["point"] == pytest.approx([5,5,3])
    assert sorted(center["principal_curvatures"]) == pytest.approx([-.24,0])
    assert center["normal"] == pytest.approx([0,0,1])
    assert {p["stripe"] for p in inspected["faces"][0]["samples"]} == {0,1}
    preview = call("preview_parameter_update", component_id=component, values={"height":"10 mm"})
    assert preview["committed"] is False
    assert point_at("hood")[2] == pytest.approx(3)
    call("set_component_parameters", component_id=component, values={"height":"10 mm"})
    assert point_at("hood")[2] == pytest.approx(5)
    rejected = tmp_path / "unprintable.3mf"
    rejected.write_bytes(b"previous export")
    with pytest.raises((LiveAPIError, RuntimeError), match="valid solid"):
        call("export_component",component_id=component,path=str(rejected),format="3MF")
    assert rejected.read_bytes() == b"previous export"
    wrong = guided()
    wrong["guides"]["left"][0] = [1,0,0]
    with pytest.raises((LiveAPIError, RuntimeError), match="(invalid|failed|corners)"):
        call("define_component", component_id=component, features=[wrong])
    assert point_at("hood")[2] == pytest.approx(5)
    wrong = guided()
    wrong["continuity"] = "G2"
    with pytest.raises((LiveAPIError, RuntimeError), match="(invalid|failed|G0)"):
        call("define_component", component_id=component, features=[wrong])
    assert point_at("hood")[2] == pytest.approx(5)
    path = str(tmp_path / "McpDesignTest.FCStd")
    call("export_component", component_id=component, path=path)
    call("execute", code="App.closeDocument('McpDesignTest')\nApp.openDocument(%r)" % path)
    call("set_component_parameters", component_id=component, values={"height":"8 mm"})
    assert point_at("hood")[2] == pytest.approx(4)


def test_curved_pad_and_bspline_tangent_geometry(component):
    call("define_component", component_id=component, features=[
        {"id":"profile", "type":"profile_2d", "role":"construction", "sections":[
            {"type":"curved", "curves":[
                {"type":"bezier", "poles":[[0,0],[5,"$height"],[10,0]]},
                {"type":"line", "points":[[10,0],[0,0]]}]}]},
        {"id":"pad", "type":"pad", "profile":"profile", "length":"2 mm"},
        {"id":"spline", "type":"curve", "role":"construction", "curve":{
            "type":"bspline", "points":[[0,0,0],[5,3,0],[10,0,0]],
            "parameters":[0,.5,1], "tangents":[[1,1,0],None,[1,-1,0]]}},
        {"id":"poles", "type":"curve", "role":"construction", "curve":{
            "type":"bspline", "mode":"poles", "degree":2,
            "poles":[[0,0,0],[5,6,0],[10,0,0]]}},
        {"id":"weighted", "type":"curve", "role":"construction", "curve":{
            "type":"bspline", "mode":"poles", "degree":2,
            "poles":[[0,0,0],[5,6,0],[10,0,0]], "weights":[1,.5,1],
            "knots":[0,1], "multiplicities":[3,3]}},
        {"id":"periodic", "type":"curve", "role":"construction", "curve":{
            "type":"bspline", "mode":"poles", "degree":2, "periodic":True,
            "poles":[[0,0,0],[10,0,0],[10,10,0],[0,10,0]],
            "knots":[0,.25,.5,.75,1], "multiplicities":[1,1,1,1,1]}},
    ])
    values = call("execute", code="d=App.getDocument('McpDesignTest')\nc=d.getObject('spline').Shape.Edges[0].Curve\np=d.getObject('poles').Shape.Edges[0].Curve\nresult={'volume':d.getObject('pad').Shape.Volume,'middle':list(c.value(.5)),'tangent':list(c.tangent(0)[0]),'poles_middle':list(p.value(.5))}")["result"]
    assert values["volume"] == pytest.approx(40, abs=1e-6)
    assert values["middle"] == pytest.approx([5,3,0])
    assert values["tangent"] == pytest.approx([1/math.sqrt(2),1/math.sqrt(2),0])
    assert values["poles_middle"] == pytest.approx([5,3,0])
    weighted = call("execute",code="result=list(App.getDocument('McpDesignTest').getObject('weighted').Shape.Edges[0].Curve.value(.5))")["result"]
    assert weighted == pytest.approx([5,2,0])
    periodic = call("execute",code="result=App.getDocument('McpDesignTest').getObject('periodic').Shape.Edges[0].isClosed()")["result"]
    assert periodic is True
    call("set_component_parameters", component_id=component, values={"height":"9 mm"})
    volume = call("execute", code="result=App.getDocument('McpDesignTest').getObject('pad').Shape.Volume")["result"]
    assert volume == pytest.approx(60, abs=1e-6)


def test_sections_overlay_and_native_multicolor_roundtrip(component, tmp_path):
    call("define_component", component_id=component, features=[
        {"id":"body", "type":"box", "size":{"x":10,"y":8,"z":6},
         "material":{"name":"Body plastic", "color":"#880022"}},
        {"id":"bumper", "type":"box", "size":{"x":2,"y":8,"z":2},
         "position":{"x":12}, "material":{"color":"#C0C0C0"}},
    ])
    cut = call("cross_sections", component_id=component, feature_id="body", plane="XY", offsets=[3,20], samples=5)
    assert cut["sections"][1]["curves"] == []
    pts = [p for c in cut["sections"][0]["curves"] for p in c]
    assert all(p[2] == pytest.approx(3) for p in pts)
    assert (min(p[0] for p in pts),max(p[0] for p in pts),min(p[1] for p in pts),max(p[1] for p in pts)) == pytest.approx((0,10,0,8))
    # Real one-pixel PNG; calibration is independent of image resolution.
    png = tmp_path / "reference.png"
    png.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="))
    overlay = call("reference_overlay", component_id=component, feature_id="body",
                   image_path=str(png), plane="XY", model_points=[[0,0],[10,0]], pixel_points=[[10,100],[110,100]], samples=3)
    root = ET.fromstring(overlay["svg"])
    ns = {"s":"http://www.w3.org/2000/svg"}
    assert overlay["pixels_per_mm"] == 10
    projected = [list(map(float,p.split(','))) for line in root.findall("s:polyline",ns) for p in line.get("points").split()]
    assert [10,20] in projected and [110,100] in projected
    assert root.find("s:image",ns).get("href").startswith("data:image/png;base64,")
    call("execute", code="App.getDocument('McpDesignTest').getObject('Design').Placement.Base=App.Vector(20,0,0)\nApp.getDocument('McpDesignTest').recompute()")
    path = str(tmp_path / "body.3mf")
    result = call("export_component", component_id=component, path=path, format="3MF")
    parts = read_3mf(path)
    assert [p["name"] for p in parts] == ["body","bumper"]
    assert [p["color"] for p in parts] == ["#880022FF","#C0C0C0FF"]
    assert min(p[0] for p in parts[0]["vertices"]) == pytest.approx(20)
    assert max(p[0] for p in parts[1]["vertices"]) == pytest.approx(34)
    imported = call("execute", code="import Mesh\nm=Mesh.Mesh(%r)\nresult={'facets':m.CountFacets,'volume':m.Volume,'solid':m.isSolid()}" % path)["result"]
    assert imported["facets"] == sum(p["triangles"] for p in result["parts"])
    assert imported["volume"] == pytest.approx(512)
    assert imported["solid"] is True
    bundle = call("export_bundle", component_id=component, directory=str(tmp_path), formats=["3MF"], basename="bundle")
    assert len(bundle["artifacts"]) == 3
    assert len(read_3mf(str(tmp_path / "bundle.3mf"))) == 2


def test_interpolated_patch_sketch_splines_and_aligned_surface_loft(component):
    square = [[0,0],[10,0],[10,10],[0,10]]
    call("define_component", component_id=component, features=[
        {"id":"plane", "type":"surface_patch", "grid":[
            [[x,y,x+y] for y in (0,5,10)] for x in (0,5,10)], "role":"construction"},
        {"id":"sketch", "type":"sketch_2d", "role":"construction", "geometry":[
            {"id":"arch", "type":"Bezier", "poles":[[0,0],[5,6],[10,0]]},
            {"id":"ridge", "type":"BSpline", "points":[[0,0],[5,4],[10,0]]}]},
        {"id":"a", "type":"profile_2d", "points":square, "role":"construction"},
        {"id":"b", "type":"profile_2d", "points":square, "position":{"z":5}, "role":"construction"},
        {"id":"loft", "type":"loft", "loft_profiles":[
            {"profile":"a","seam_edge":1,"reverse":True},
            {"profile":"b","seam_edge":1,"reverse":True}], "solid":False},
    ])
    values = call("execute", code=(
        "import Part, parametric\nd=App.getDocument('McpDesignTest')\n"
        "face=d.getObject('plane').Shape.Faces[0]\n"
        "sk=d.getObject('sketch')\nloft=d.getObject('loft')\n"
        "wires=parametric._loft_wires(loft,parametric._profile_host(loft))\n"
        "result={'distances':[Part.Vertex(App.Vector(x,y,x+y)).distToShape(face)[0] for x in (0,5,10) for y in (0,5,10)],"
        "'normal':list(face.normalAt(.5,.5)),'curve_middle':list(sk.Geometry[0].value(.5)),"
        "'curve_types':[c.TypeId for c in sk.Geometry],'area':loft.Shape.Area,'solids':len(loft.Shape.Solids),"
        "'first_edge':list(wires[0].Edges[0].Vertexes[0].Point),'orientation':wires[0].Orientation}"
    ))["result"]
    assert values["distances"] == pytest.approx([0]*9,abs=1e-7)
    assert values["normal"] == pytest.approx([-1/math.sqrt(3),-1/math.sqrt(3),1/math.sqrt(3)])
    assert values["curve_middle"] == pytest.approx([5,3,0])
    assert values["curve_types"] == ["Part::GeomBSplineCurve"]*2
    assert values["area"] == pytest.approx(200)
    assert values["solids"] == 0
    assert values["first_edge"] == pytest.approx([10,0,0])
    assert values["orientation"] == "Reversed"
    before = call("get_component_graph",component_id=component)
    with pytest.raises((LiveAPIError,RuntimeError),match="(invalid|failed|seam)"):
        call("patch_component",component_id=component,operations=[{"op":"upsert","feature":{
            "id":"loft","type":"loft","loft_profiles":[{"profile":"a","seam_edge":99},{"profile":"b"}]}}])
    after = call("get_component_graph",component_id=component)
    assert after["revision"] == before["revision"]
