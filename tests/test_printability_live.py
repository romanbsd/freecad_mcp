"""Native BRep regression checks against an explicitly enabled FreeCAD server."""
import os
import pytest
from test_api_v2_live import call

NATIVE = os.environ.get("FREECAD_MCP_NATIVE") == "1"
pytestmark = pytest.mark.skipif(not NATIVE and os.environ.get("FREECAD_MCP_LIVE") != "1",
                              reason="requires native FreeCAD Python or running MCP server")


@pytest.fixture(autouse=True)
def restore_document():
    if NATIVE:
        yield
        return
    previous = call("execute", code="result=App.ActiveDocument.Name if App.ActiveDocument else None")["result"]
    yield
    call("execute", code="if 'McpPrintabilityTest' in App.listDocuments(): App.closeDocument('McpPrintabilityTest')\n"
         + "if %r in App.listDocuments(): App.setActiveDocument(%r)" % (previous, previous))


def native(expression, **settings):
    if NATIVE:
        import FreeCAD as App
        import Part
        from printability import analyze_shape
        return analyze_shape(eval(expression, {"Part": Part, "App": App}), **settings)
    # Build a component, insert a transient native fixture, and exercise public API.
    call("execute", code="if 'McpPrintabilityTest' in App.listDocuments(): App.closeDocument('McpPrintabilityTest')")
    cid = call("create_component", document="McpPrintabilityTest", name="Preflight")['component_id']
    call("define_component", component_id=cid, features=[
        {"id": "panel", "type": "box", "size": {"x": "10 mm", "y": "10 mm", "z": "1 mm"}}])
    call("execute", code=(
        "import Part, parametric\n"
        "doc, container, meta = parametric._resolve(" + repr(cid) + ")\n"
        "target = doc.addObject('Part::Feature', 'NativeFixture')\n"
        "target.Shape = " + expression + "\n"
        "meta['id_map']['fixture'] = target.Name\n"
        "parametric._save(container, meta)\n"
        "doc.recompute()\n"))
    return call("inspect_printability", component_id=cid, feature_id="fixture", **settings)


def test_box_actual_wall_and_threshold():
    report = native("Part.makeBox(10,10,1)", min_wall=.8)
    assert report["status"] == "PASS"
    assert report["min_observed_wall_mm"] == pytest.approx(1, abs=1e-5)
    thin = native("Part.makeBox(10,10,.5)", min_wall=.8)
    assert thin["status"] == "WARN"
    assert thin["min_observed_wall_mm"] == pytest.approx(.5, abs=1e-5)
    assert any(f["kind"] == "thin_wall" and f["actual_mm"] < .8 for f in thin["findings"])


def test_curved_shell_finds_thin_region_without_counting_cavity():
    # Eccentric spherical cavity: wall varies from .4 to 1.6 mm.
    report = native("Part.makeSphere(10).cut(Part.makeSphere(9, App.Vector(.6,0,0)))",
                    min_wall=.8, samples_per_face=49)
    assert report["min_observed_wall_mm"] < .8
    assert report["min_observed_wall_mm"] >= .39
    assert any(f["kind"] == "thin_wall" for f in report["findings"])


def test_orientation_changes_overhang_screening():
    expression = "Part.makeBox(2,2,10).fuse(Part.makeBox(10,2,2,App.Vector(0,0,8)))"
    upright = native(expression, build_direction=[0,0,1], samples_per_face=9)
    side = native(expression, build_direction=[0,1,0], samples_per_face=9)
    assert upright["sampling"]["overhang_samples"] > side["sampling"]["overhang_samples"]


def test_budget_cannot_claim_complete_pass():
    report = native("Part.makeBox(10,10,1)", max_samples=1)
    assert report["status"] == "WARN"
    assert report["sampling"]["truncated"] is True
    assert report["sampling"]["attempted"] == 1


def test_invalid_open_shape_rejected():
    with pytest.raises((RuntimeError, ValueError), match="closed solid"):
        native("Part.makePlane(10,10)")


def test_disconnected_floating_solids_are_reported():
    report = native("Part.makeCompound([Part.makeBox(2,2,1),Part.makeBox(2,2,1,App.Vector(5,0,3))])")
    assert any(f["kind"] == "disconnected_solids" for f in report["findings"])
    floating = [f for f in report["findings"] if f["kind"] == "floating_cluster"]
    assert len(floating) == 1
    assert floating[0]["clearance_mm"] == pytest.approx(3)


def test_unresolved_normal_cannot_pass(monkeypatch):
    if not NATIVE:
        pytest.skip("fault injection is only available in native process")
    import printability
    monkeypatch.setattr(printability, "_wall_at", lambda *args: (None, None, "ambiguous normal"))
    report = native("Part.makeBox(10,10,1)")
    assert report["status"] == "WARN"
    assert report["sampling"]["unresolved"] == report["sampling"]["attempted"]
    assert report["min_observed_wall_mm"] is None


def test_mixed_open_geometry_rejected():
    with pytest.raises((RuntimeError, ValueError), match="closed solids"):
        native("Part.makeCompound([Part.makeBox(2,2,1),Part.makeLine(App.Vector(5,0,0),App.Vector(6,0,0))])")


def test_curved_floating_clearance_uses_surface_extrema():
    report = native("Part.makeCompound([Part.makeBox(2,2,1),Part.makeSphere(1,App.Vector(5,0,3))])",
                    build_direction=[0,0,1])
    floating = [f for f in report["findings"] if f["kind"] == "floating_cluster"]
    assert floating[0]["clearance_mm"] == pytest.approx(2, abs=1e-5)


def test_touching_multicolor_parts_are_one_grounded_cluster():
    report = native("Part.makeCompound([Part.makeBox(2,2,1),Part.makeBox(2,2,1,App.Vector(0,0,1))])")
    assert report["connectivity"] == {"complete": True, "clusters": 1}
    assert not any(f["kind"] in ("disconnected_solids", "floating_cluster") for f in report["findings"])


def test_connectivity_budget_reports_incomplete():
    report = native("Part.makeCompound([Part.makeBox(1,1,1,App.Vector(2*i,0,0)) for i in range(65)])",max_samples=1)
    assert report["connectivity"]["complete"] is False
    assert any(f["kind"] == "incomplete_connectivity" for f in report["findings"])
