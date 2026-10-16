"""Run with FREECAD_MCP_LIVE=1 against the FreeCAD GUI server."""
import base64
import json
import math
import os
from pathlib import Path
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
        "wires,holes=parametric._loft_wires(loft,parametric._profile_host(loft))\n"
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


def test_annular_loft_keeps_cylindrical_bore_and_tapered_outside(component):
    def ring(fid, outside, x):
        return {"id": fid, "type": "sketch_2d", "role": "construction",
                "frame": {"plane": "YZ", "origin": [x, 0, 0]},
                "geometry": [
                    {"id": "outer", "type": "circle", "radius": outside},
                    {"id": "inner", "type": "circle", "radius": "9.65 mm"},
                ], "constraints": [
                    {"type": "coincident", "first": "outer", "first_pos": 3,
                     "second": -1, "second_pos": 1},
                    {"type": "radius", "geometry": "outer", "value": outside},
                    {"type": "coincident", "first": "inner", "first_pos": 3,
                     "second": -1, "second_pos": 1},
                    {"type": "radius", "geometry": "inner", "value": "9.65 mm"},
                ], "require_fully_constrained": True}

    call("define_component", component_id=component, features=[
        ring("thick", "12.15 mm", "0 mm"),
        ring("thin", "11.15 mm", "$height"),
        {"id": "sleeve", "type": "loft", "loft_profiles": [
            {"profile": "thick"}, {"profile": "thin"}], "ruled": True,
         "role": "output"},
    ])
    values = call("execute", code=(
        "import Part\n"
        "d=App.getDocument('McpDesignTest')\n"
        "s=d.getObject('sleeve').Shape\n"
        "r=sorted(e.Curve.Radius for w in s.slice(App.Vector(1,0,0),3) "
        "for e in w.Edges if isinstance(e.Curve,Part.Circle))\n"
        "result={'valid':s.isValid(),'solids':len(s.Solids),'radii':r,'volume':s.Volume}"
    ))["result"]
    expected = 6 * math.pi * ((12.15**2 + 12.15*11.15 + 11.15**2)/3 - 9.65**2)
    assert values["valid"] and values["solids"] == 1
    assert values["radii"] == pytest.approx([9.65, 11.65], abs=1e-6)
    assert values["volume"] == pytest.approx(expected, rel=1e-6)

    def annulus(pid, outer, x):
        return {"id": pid, "plane": "YZ",
                "frame": {"plane": "YZ", "origin": [x, 0, 0]},
                "composition": [
                    {"operation": "union", "sections": [
                        {"type": "circle", "radius": outer}]},
                    {"operation": "difference", "sections": [
                        {"type": "circle", "radius": "9.65 mm"}]},
                ]}
    call("define_component", component_id=component,
         profile_definitions=[annulus("start", "12.15 mm", "0 mm"),
                              annulus("end", "11.15 mm", "$height")],
         features=[{"id": "sleeve", "type": "loft", "loft_profiles": [
             {"profile_id": "start"}, {"profile_id": "end"}],
             "ruled": True, "role": "output"}])
    inline = call("execute", code=(
        "s=App.getDocument('McpDesignTest').getObject('sleeve').Shape\n"
        "result={'valid':s.isValid(),'solids':len(s.Solids),'volume':s.Volume}"
    ))["result"]
    assert inline["valid"] and inline["solids"] == 1
    assert inline["volume"] == pytest.approx(expected, rel=1e-6)


def test_open_curve_profiles_still_make_surface_loft(component):
    call("execute", code="import importlib,parametric\nimportlib.reload(parametric)")
    call("define_component", component_id=component, features=[
        {"id": "lower", "type": "curve", "role": "construction",
         "curve": {"type": "bspline", "points": [[0, 0, 0], [5, 1, 0], [10, 0, 0]]}},
        {"id": "upper", "type": "curve", "role": "construction",
         "curve": {"type": "bspline", "points": [[0, 0, 5], [5, 1, 5], [10, 0, 5]]}},
        {"id": "surface", "type": "loft", "loft_profiles": [
            {"profile": "lower"}, {"profile": "upper"}], "solid": False},
    ])
    values = call("execute", code=(
        "s=App.getDocument('McpDesignTest').getObject('surface').Shape\n"
        "result={'valid':s.isValid(),'faces':len(s.Faces),'solids':len(s.Solids)}"
    ))["result"]
    assert values == {"valid": True, "faces": 1, "solids": 0}


def test_loft_rejects_two_disjoint_loops_as_a_hole(component):
    call("execute", code="import importlib,parametric\nimportlib.reload(parametric)")
    before = call("get_component_graph", component_id=component)["revision"]
    def profile(fid, x):
        return {"id": fid, "type": "sketch_2d", "role": "construction",
                "frame": {"plane": "YZ", "origin": [x, 0, 0]},
                "geometry": [
                    {"id": "first", "type": "circle", "center": [0, 0],
                     "radius": "5 mm"},
                    {"id": "second", "type": "circle", "center": [20, 0],
                     "radius": "2 mm"}]}
    with pytest.raises(LiveAPIError, match="invalid features"):
        call("define_component", component_id=component, features=[
            profile("a", "0 mm"), profile("b", "5 mm"),
            {"id": "loft", "type": "loft",
             "loft_profiles": [{"profile": "a"}, {"profile": "b"}]}])
    assert call("get_component_graph", component_id=component)["revision"] == before


def test_native_partdesign_annular_loft_rebuilds_after_reopen(tmp_path):
    name = "McpNativeAnnularTest"
    call("execute", code="if %r in App.listDocuments(): App.closeDocument(%r)" % (name, name))
    parameters = [
        {"name": "RodDiameter", "default": "19 mm"},
        {"name": "Clearance", "default": "0.15 mm"},
        {"name": "Length", "default": "33.5 mm"},
        {"name": "WallThick", "default": "2.5 mm"},
        {"name": "WallThin", "default": "1.5 mm"},
        {"name": "InnerRadius", "expression": "RodDiameter / 2 + Clearance"},
        {"name": "OuterThick", "expression": "InnerRadius + WallThick"},
        {"name": "OuterThin", "expression": "InnerRadius + WallThin"},
    ]
    def profile(fid, outer, x):
        return {"id": fid, "type": "sketch_2d",
                "frame": {"plane": "YZ", "origin": [x, 0, 0]},
                "geometry": [
                    {"id": "outer", "type": "circle", "radius": outer},
                    {"id": "inner", "type": "circle", "radius": "$InnerRadius"},
                ], "constraints": [
                    {"type": "coincident", "first": "outer", "first_pos": 3,
                     "second": -1, "second_pos": 1},
                    {"type": "radius", "geometry": "outer", "value": outer},
                    {"type": "coincident", "first": "inner", "first_pos": 3,
                     "second": -1, "second_pos": 1},
                    {"type": "radius", "geometry": "inner", "value": "$InnerRadius"},
                ]}
    spec = {"document": name, "body": "Sleeve", "parameters": parameters,
            "features": [profile("Thick", "$OuterThick", "0 mm"),
                         profile("Thin", "$OuterThin", "$Length"),
                         {"id": "Shell", "type": "loft",
                          "loft_profiles": ["Thick", "Thin"], "ruled": True}],
            "save_path": str(tmp_path / "native_annular.FCStd")}
    code = ("import json,importlib,sys\n"
            "sys.path.insert(0,%r)\n" % str(Path(__file__).resolve().parents[1]) +
            "import native_partdesign\n"
            "importlib.reload(native_partdesign)\n"
            "spec=json.loads(%r)\n" % json.dumps(spec) +
            "built=native_partdesign.build_native_partdesign(**spec)\n"
            "d=App.getDocument(spec['document'])\n"
            "before=d.getObject('Shell').Shape.Volume\n"
            "d.getObject('Parameters').set('B6','2 mm')\n"
            "d.recompute()\n"
            "after=d.getObject('Shell').Shape.Volume\n"
            "d.save()\nApp.closeDocument(d.Name)\n"
            "d=App.openDocument(spec['save_path'])\n"
            "s=d.getObject('Shell').Shape\n"
            "result={'built':built,'before':before,'after':after,'reopened':s.isValid(),"
            "'solids':len(s.Solids),'volume':s.Volume,'dof':[d.getObject(n).DoF for n in ('Thick','Thin')],"
            "'types':[d.getObject(n).TypeId for n in ('Sleeve','Shell','Parameters')]}\n"
            "App.closeDocument(d.Name)")
    values = call("execute", code=code)["result"]
    assert values["built"]["solid_count"] == 1
    assert values["before"] < values["after"]
    assert values["reopened"] and values["solids"] == 1
    assert values["volume"] == pytest.approx(values["after"])
    assert values["dof"] == [0, 0]
    assert values["types"] == ["PartDesign::Body", "PartDesign::AdditiveLoft",
                                "Spreadsheet::Sheet"]


def test_native_partdesign_pad_pocket_revolve_and_groove():
    def circle(fid, radius, z):
        return {"id": fid, "type": "sketch_2d",
                "frame": {"plane": "XY", "origin": [0, 0, z]},
                "geometry": [{"id": "c", "type": "circle", "radius": radius}],
                "constraints": [
                    {"type": "coincident", "first": "c", "first_pos": 3,
                     "second": -1, "second_pos": 1},
                    {"type": "radius", "geometry": "c", "value": radius}]}

    def rectangle(fid, xa, xb, ya, yb):
        points = [(xa, ya), (xb, ya), (xb, yb), (xa, yb)]
        return {"id": fid, "type": "sketch_2d",
                "geometry": [{"id": f"line_{i}", "type": "line",
                              "start": points[i], "end": points[(i + 1) % 4]}
                             for i in range(4)],
                "constraints": [{"type": "block", "geometry": f"line_{i}"}
                                for i in range(4)]}

    recipes = [
        {"document": "McpNativePadPocket", "body": "Body", "parameters": [],
         "features": [
             circle("Base", "5 mm", "0 mm"),
             {"id": "Pad", "type": "pad", "profile": "Base", "length": "5 mm"},
             circle("Bore", "2 mm", "5 mm"),
             {"id": "Pocket", "type": "pocket", "profile": "Bore", "length": "5 mm"}]},
        {"document": "McpNativeRevolveGroove", "body": "Body", "parameters": [],
         "features": [
             rectangle("Section", 0, 5, 2, 5),
             {"id": "Revolve", "type": "revolve", "profile": "Section",
              "axis": "horizontal"},
             rectangle("GrooveSection", 1, 2, 4, 5.5),
             {"id": "Groove", "type": "groove", "profile": "GrooveSection",
              "axis": "horizontal"}]},
    ]
    code = (
        "import json,importlib,sys\n"
        "sys.path.insert(0,%r)\n" % str(Path(__file__).resolve().parents[1]) +
        "import native_partdesign\n"
        "importlib.reload(native_partdesign)\n"
        "recipes=json.loads(%r)\n" % json.dumps(recipes) +
        "result=[]\n"
        "for spec in recipes:\n"
        " if spec['document'] in App.listDocuments(): App.closeDocument(spec['document'])\n"
        " built=native_partdesign.build_native_partdesign(**spec)\n"
        " d=App.getDocument(spec['document'])\n"
        " result.append({'built':built,'volume':d.getObject(built['output']).Shape.Volume})\n"
        " App.closeDocument(d.Name)"
    )
    values = call("execute", code=code)["result"]
    assert [row["built"]["solid_count"] for row in values] == [1, 1]
    assert values[0]["volume"] == pytest.approx(math.pi * (25 - 4) * 5)
    assert values[1]["volume"] == pytest.approx(
        math.pi * (25 - 4) * 5 - math.pi * (25 - 16))


def test_native_partdesign_rejects_missing_parameter_and_length():
    code = (
        "import importlib,sys\n"
        "sys.path.insert(0,%r)\n" % str(Path(__file__).resolve().parents[1]) +
        "import native_partdesign\n"
        "importlib.reload(native_partdesign)\n"
        "result=[]\n"
        "for parameters,features in ["
        "([{'name':'Missing','expression':None}],[{'id':'Pad','type':'pad'}]),"
        "([],[{'id':'Pad','type':'pad'}]),"
        "([],[{'id':'Cut','type':'pocket','length':'1 mm'}])]:\n"
        " try: native_partdesign._validate_spec('Probe','Body',parameters,features)\n"
        " except ValueError as error: result.append(str(error))"
    )
    errors = call("execute", code=code)["result"]
    assert errors == ["parameter 'Missing' needs a default or expression",
                      "feature 'Pad' needs a length",
                      "native Part Design body needs an additive feature"]
