"""Unit tests for parametric logic that does not require a FreeCAD process."""
import importlib
import sys
import types

import pytest


@pytest.fixture
def parametric(monkeypatch):
    fake_app = types.ModuleType("FreeCAD")
    monkeypatch.setitem(sys.modules, "FreeCAD", fake_app)
    sys.modules.pop("parametric", None)
    module = importlib.import_module("parametric")
    yield module
    sys.modules.pop("parametric", None)


def test_translate_rewrites_parameter_tokens(parametric):
    assert parametric._translate("$width / 2 + $wall_2", "Params") == \
        "Params.width / 2 + Params.wall_2"


def test_check_cycles_accepts_acyclic_derived_parameters(parametric):
    parametric._check_cycles([
        {"name": "width", "kind": "input"},
        {"name": "half", "expression": "$width / 2"},
        {"name": "quarter", "expression": "$half / 2"},
    ])


def test_check_cycles_rejects_derived_parameter_cycle(parametric):
    with pytest.raises(ValueError, match="PARAMETER_CYCLE"):
        parametric._check_cycles([
            {"name": "a", "expression": "$b"},
            {"name": "b", "expression": "$a"},
        ])


@pytest.mark.parametrize("width,height", [(1, 1), (4096, 4096), ("640", "480")])
def test_render_dimensions_accept_valid_sizes(parametric, width, height):
    assert parametric._render_dimensions(width, height) == (int(width), int(height))


@pytest.mark.parametrize("width,height,message", [
    (0, 1, "positive"),
    (-1, 1, "positive"),
    (4097, 1, "may not exceed"),
    (4096, 4097, "may not exceed"),
    ("large", 1, "integers"),
])
def test_render_dimensions_reject_invalid_sizes(parametric, width, height, message):
    with pytest.raises(ValueError, match=message):
        parametric._render_dimensions(width, height)


def test_transform_is_a_dependency_but_does_not_consume_visible_base(parametric):
    transform = {"id": "moved", "type": "transform", "base": "box"}
    assert parametric._feature_inputs(transform) == ["box"]
    assert parametric._consumed(transform) == []


def test_dependents_include_derived_and_downstream_transform_nodes(parametric):
    schema = [
        {"name": "width", "kind": "input"},
        {"name": "half_width", "kind": "derived", "expression": "$width / 2"},
    ]
    graph = [
        {"id": "box", "type": "box", "size": {"x": "$half_width"}},
        {"id": "moved", "type": "transform", "base": "box"},
        {"id": "cut", "type": "cut", "base": "moved", "tool": "drill"},
        {"id": "drill", "type": "cylinder", "radius": "$width"},
    ]
    meta = {
        "schema": schema,
        "build_graph": graph,
        "dependency_index": parametric._dependency_index(schema, graph),
    }

    assert parametric._dependents(meta, ["width"]) == ["box", "moved", "cut", "drill"]


def test_dependents_supports_components_created_before_dependency_index(parametric):
    schema = [{"name": "depth", "kind": "input"}]
    graph = [{"id": "box", "type": "box", "size": {"z": "$depth"}}]
    assert parametric._dependents({"schema": schema, "build_graph": graph}, ["depth"]) == ["box"]


def test_create_component_variant_validates_and_copies_values(parametric, monkeypatch):
    container = object()
    meta = {"schema": [
        {"name": "width", "kind": "input"},
        {"name": "area", "kind": "derived"},
    ]}
    saved = []
    monkeypatch.setattr(parametric, "_resolve", lambda component_id: (None, container, meta))
    monkeypatch.setattr(parametric, "_save", lambda target, value: saved.append((target, value.copy())))

    values = {"width": "120 mm"}
    result = parametric.op_create_component_variant("component://Doc/Part", "wide", values)
    values["width"] = "5 mm"

    assert result["values"] == {"width": "120 mm"}
    assert meta["variants"] == {"wide": {"width": "120 mm"}}
    assert saved[0][0] is container


@pytest.mark.parametrize("name,values,message", [
    ("", {}, "non-empty"),
    ("bad-values", [], "must be an object"),
    ("unknown", {"height": 1}, "unknown parameter"),
    ("derived", {"area": 1}, "derived parameter"),
])
def test_create_component_variant_rejects_invalid_overrides(parametric, monkeypatch, name, values, message):
    meta = {"schema": [
        {"name": "width", "kind": "input"},
        {"name": "area", "kind": "derived"},
    ]}
    monkeypatch.setattr(parametric, "_resolve", lambda component_id: (None, object(), meta))
    monkeypatch.setattr(parametric, "_save", lambda target, value: None)

    with pytest.raises(ValueError, match=message):
        parametric.op_create_component_variant("component://Doc/Part", name, values)


def test_apply_graph_operations_upserts_removes_and_sets_outputs(parametric):
    graph = [
        {"id": "base", "type": "box"},
        {"id": "hole", "type": "cylinder"},
        {"id": "body", "type": "cut", "base": "base", "tool": "hole"},
    ]
    result = parametric._apply_graph_operations(graph, [
        {"op": "remove", "id": "body"},
        {"op": "upsert", "feature": {
            "id": "base", "type": "box", "size": {"x": "20 mm"},
        }},
        {"op": "upsert", "feature": {
            "id": "cap", "type": "box", "role": "output",
        }},
        {"op": "set_output", "ids": ["base", "cap"]},
    ], outputs=["body"])

    assert result["added"] == ["cap"]
    assert result["changed"] == ["base"]
    assert result["removed"] == ["body"]
    assert result["outputs"] == ["base", "cap"]
    assert [f["id"] for f in result["graph"]] == ["base", "hole", "cap"]


def test_apply_graph_operations_rejects_dangling_reference(parametric):
    with pytest.raises(ValueError, match="missing inputs"):
        parametric._apply_graph_operations([
            {"id": "body", "type": "cut", "base": "base", "tool": "hole"},
        ], [{"op": "upsert", "feature": {"id": "base", "type": "box"}}])


def test_patch_component_rejects_stale_revision_without_building(parametric, monkeypatch):
    meta = {"revision": 4, "build_graph": [], "outputs": []}
    monkeypatch.setattr(
        parametric, "_resolve",
        lambda component_id: (object(), object(), meta),
    )
    with pytest.raises(ValueError, match="STALE_COMPONENT_REVISION"):
        parametric.op_patch_component(
            "component://Doc/Part", [], expected_revision=3
        )


def test_structural_dependents_rebuild_only_changed_branch(parametric):
    graph = [
        {"id": "base", "type": "box"},
        {"id": "hole", "type": "cylinder"},
        {"id": "body", "type": "cut", "base": "base", "tool": "hole"},
        {"id": "independent", "type": "box"},
    ]
    impacted = parametric._structural_dependents(graph, graph, ["hole"])
    assert impacted == {"hole", "body"}


def test_describe_feature_type_returns_machine_readable_requirements(parametric):
    schema = parametric.op_describe_feature_type("tube")
    assert schema["feature_type"] == "tube"
    assert schema["schema"]["required"] == [
        "id", "outer_radius", "inner_radius", "height"
    ]


def test_edge_selector_matches_axis_aligned_edges(parametric):
    class Bounds:
        XLength, YLength, ZLength = 0.0, 0.0, 12.0

    class Edge:
        BoundBox = Bounds()
        Length = 12.0

    assert parametric._edge_matches(Edge(), {"parallel_to": "z"})
    assert not parametric._edge_matches(Edge(), {"parallel_to": "x"})


def test_lego_stud_grid_pattern_expands_to_seed_and_grid(parametric):
    from patterns import expand_patterns
    features = expand_patterns([{
        "id": "studs", "type": "pattern", "pattern": "lego.stud_grid",
        "parameters": {
            "count_x": 4, "count_y": 4, "pitch": "8 mm",
            "diameter": "4.8 mm", "height": "1.8 mm",
            "origin": {"x": "3.9 mm", "y": "3.9 mm", "z": "28.8 mm"},
        },
    }])
    assert [f["id"] for f in features] == ["studs__seed", "studs"]
    assert features[1]["type"] == "grid_array"
    assert features[1]["count_x"] == 4 and features[1]["count_y"] == 4


def test_lego_underside_pattern_contains_tubes_and_raised_ribs(parametric):
    from patterns import expand_patterns
    features = expand_patterns([{
        "id": "underside", "type": "pattern",
        "pattern": "lego.brick_underside",
        "parameters": {
            "base": "shell", "width": "31.8 mm", "length": "31.8 mm",
            "wall": "1.5 mm", "depth": "2.7 mm",
            "tube_od": "6.5 mm", "tube_id": "4.85 mm",
            "pitch": "8 mm", "count_x": 4, "count_y": 4,
        },
    }])
    by_id = {f["id"]: f for f in features}
    assert by_id["underside__tube_seed"]["type"] == "tube"
    assert by_id["underside__tubes"]["count_x"] == 3
    assert by_id["underside__h_ribs"]["type"] == "grid_array"
    assert by_id["underside__v_ribs"]["type"] == "grid_array"
    assert by_id["underside"]["tags"] == ["lego_underside"]


def test_bbox_clearances_report_containment_and_minimum(parametric):
    result = parametric._bbox_clearances(
        (0, 0, 0, 20, 30, 10),
        (2, 3, 1, 18, 25, 9),
    )
    assert result["contained"] is True
    assert result["minimum"] == 1
    assert result["directions"]["+y"] == 5


def test_bbox_clearances_report_escape(parametric):
    result = parametric._bbox_clearances(
        (0, 0, 0, 10, 10, 10),
        (-1, 1, 1, 9, 9, 9),
    )
    assert result["contained"] is False
    assert result["directions"]["-x"] == -1


def test_bound_violations_detects_out_of_range(parametric):
    schema = [
        {"name": "width", "min": "10 mm", "max": "100 mm"},
        {"name": "count", "min": 1, "max": 8},
    ]
    assert parametric._bound_violations(schema, {"width": "5 mm"})
    assert parametric._bound_violations(schema, {"count": 9})
    assert parametric._bound_violations(schema, {"width": "50 mm", "count": 4}) == []
    # unknown / unbounded / non-numeric params are skipped, not flagged
    assert parametric._bound_violations(schema, {"other": "x", "width": "abc"}) == []


def test_array_requires_explicit_count(parametric):
    with pytest.raises(ValueError, match="requires 'count'"):
        parametric._require_array_count({"id": "a", "base": "seed"})
    parametric._require_array_count({"id": "a", "base": "seed", "count": 3})
    parametric._require_array_count({"id": "a", "base": "seed", "count_x": 2})


def test_profile_points_requires_at_least_three(parametric):
    with pytest.raises(ValueError, match="at least 3 points"):
        parametric._profile_points({"id": "p", "points": [[0, 0], [1, 0]]})
    assert parametric._profile_points(
        {"id": "p", "points": [[0, 0], [1, 0], [1, 1]]}
    ) == [[0, 0], [1, 0], [1, 1]]


def test_evaluate_reports_unknown_rule_type(parametric):
    import design_rules as DR
    ctx = {"params": {}, "features": {}, "graph": [], "tolerance": "0.01 mm"}
    findings, passed = DR.evaluate(ctx, [{"id": "oops", "type": "no_such_rule"}])
    assert passed == 0
    assert len(findings) == 1
    assert findings[0]["severity"] == "error"
    assert "unknown rule type" in findings[0]["message"]


def test_expand_profile_unknown_returns_none(parametric):
    import design_rules as DR
    ctx = {"params": {}, "features": {}, "graph": [], "tolerance": "0.01 mm"}
    version, rules = DR.expand_profile("geomtry_baseline", ctx)  # typo
    assert version is None
    assert rules == []


def test_create_component_rejects_duplicate_id(parametric):
    # Guard runs before any FreeCAD object creation, so a tiny fake suffices.
    class _Obj:
        mcp_component_id = "component://doc/Widget"

    class _Doc:
        Objects = [_Obj()]

    parametric.App.listDocuments = lambda: ["doc"]
    parametric.App.getDocument = lambda name: _Doc()
    with pytest.raises(ValueError, match="already exists"):
        parametric.op_create_component("doc", "Widget")


def test_edge_treatment_proxy_round_trips_mode(parametric):
    # FreeCAD restores a saved Proxy via __setstate__/loads; the chamfer/fillet
    # mode must survive or a reopened document applies the wrong operation.
    proxy = parametric._EdgeTreatmentProxy("chamfer")
    restored = parametric._EdgeTreatmentProxy.__new__(parametric._EdgeTreatmentProxy)
    restored.__setstate__(proxy.__getstate__())
    assert restored.mode == "chamfer"


def test_edge_treatment_proxy_defaults_mode_on_empty_state(parametric):
    restored = parametric._EdgeTreatmentProxy.__new__(parametric._EdgeTreatmentProxy)
    restored.__setstate__(None)
    assert restored.mode == "fillet"


def test_profile_extrusion_proxy_serialization_is_stable(parametric):
    proxy = parametric._ProfileExtrusionProxy()
    assert proxy.__getstate__() is None
    proxy.__setstate__(proxy.__getstate__())  # must not raise


def test_fdm_profile_expands_manufacturing_checks(parametric):
    import design_rules
    ctx = {
        "params": {"wall_thickness": {"kind": "input"}},
        "features": {}, "graph": [],
    }
    version, rules = design_rules.expand_profile("fdm", ctx, {
        "min_wall_thickness": "1.2 mm",
        "nozzle_diameter": "0.6 mm",
    })
    assert version == 1
    assert any(r["id"] == "fdm_min_wall:wall_thickness" and
               r["minimum"] == "1.2 mm" for r in rules)
    assert any("0.6 mm" in r.get("message", "") for r in rules)
