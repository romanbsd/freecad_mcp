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


def test_generic_profile_tools_are_exposed_without_one_off_keyhole(parametric):
    feature_types = parametric.op_list_feature_types()["feature_types"]
    assert "profile_2d" in feature_types
    assert "profile_extrude" in feature_types
    assert "extrude_cut" in feature_types
    assert "pad" in feature_types
    assert "pocket" in feature_types
    assert "revolve" in feature_types
    assert "sweep" in feature_types
    assert "loft" in feature_types
    assert "keyhole_slot" not in feature_types


def test_profile_tool_schemas_describe_generic_sections(parametric):
    profile = parametric.op_describe_feature_type("profile_2d")
    cut = parametric.op_describe_feature_type("extrude_cut")
    pad = parametric.op_describe_feature_type("pad")
    pocket = parametric.op_describe_feature_type("pocket")

    assert profile["schema"]["one_of"] == [
        "profile_id", "points", "sections", "profile_spec"
    ]
    assert cut["schema"]["required"] == ["id", "base", "length"]
    assert cut["schema"]["one_of"] == [
        "profile", "profile_id", "points", "sections", "profile_spec"
    ]
    assert pad["schema"]["required"] == ["id", "length"]
    assert pocket["schema"]["required"] == ["id", "base", "length"]
    assert profile["schema"]["properties"]["support"]
    assert cut["schema"]["alias_of"] == "pocket"
    assert cut["schema"]["deprecated"] is True


def test_common_feature_schema_uses_one_selector_and_placement_contract(parametric):
    described = parametric.op_describe_feature_type("fillet")
    common = described["common_properties"]

    assert {"support", "placement", "frame", "reversed", "symmetric"} <= set(common)
    assert common["selector"]["properties"]["kind"]["enum"] == [
        "face", "edge", "vertex", "solid"
    ]
    assert described["schema"]["one_of"] == ["selector", "edges"]

    sketch = parametric.op_describe_feature_type("sketch_2d")
    assert sketch["schema"]["required"] == ["id", "geometry"]
    assert "coincident" in sketch["schema"]["properties"]["constraints"]["items"]["type"]


def test_capabilities_advertise_named_references_and_sketches(parametric, monkeypatch):
    fake_freecad = types.SimpleNamespace(Version=lambda: ("1", "1", "1"))
    monkeypatch.setitem(sys.modules, "FreeCAD", fake_freecad)
    capabilities = parametric.op_capabilities()

    assert capabilities["api_version"] == "2.4"
    assert capabilities["named_geometry_references"] is True
    assert capabilities["dynamic_external_geometry"] is True
    assert capabilities["named_datum_frames"] is True
    assert capabilities["manufacturing_features"] == [
        "hole", "shell", "draft", "rib", "face_offset", "replace_face",
    ]
    assert "radius" in capabilities["sketch_constraints"]


def test_common_feature_normalizer_preserves_legacy_spelling(parametric):
    normalized = parametric._normalize_common_feature({
        "id": "part", "type": "box", "position": {"x": "2 mm"},
        "frame": "XY", "reversed": True,
    })

    assert normalized["placement"]["translation"] == {"x": "2 mm"}
    assert normalized["placement"]["frame"] == "XY"
    assert normalized["reversed"] is True

    nested = parametric._normalize_common_feature({
        "id": "part", "type": "pad",
        "placement": {"reversed": True, "symmetric": True},
    })
    assert nested["reversed"] is True
    assert nested["symmetric"] is True


def test_bind_sets_numeric_nested_placement_paths(parametric):
    class Vector:
        x = y = z = 0.0

    class Placement:
        Base = Vector()

    feature = types.SimpleNamespace(Placement=Placement())
    parametric._bind(feature, ".Placement.Base.x", 3, "Params")
    assert feature.Placement.Base.x == 3


def test_quantity_value_uses_native_expression_evaluation(parametric):
    class Host:
        Name = "Params"

        def evalExpression(self, expression):
            assert expression == "Params.width / 2"
            return types.SimpleNamespace(Value=7.0)

    assert parametric._quantity_value("$width / 2", Host()) == 7.0


def test_named_geometry_references_expand_without_mutating_registry(parametric):
    source = [{
        "id": "top", "feature": "panel",
        "selector": {"kind": "face", "where": {"axis": "z", "side": "max"}},
    }]
    references = parametric._geometry_reference_map(source)
    expanded = parametric._expand_geometry_references({
        "id": "sketch", "type": "sketch_2d",
        "placement": {"support": {"reference": "top"}},
    }, references)

    support = expanded["placement"]["support"]
    assert support["feature"] == "panel"
    assert support["reference"] == "top"
    support["selector"]["where"]["normal"] = [0, 0, 1]
    assert "normal" not in references["top"]["selector"]["where"]

    with pytest.raises(ValueError, match="duplicate geometry reference"):
        parametric._geometry_reference_map(source + source)


def test_named_selector_supplies_default_base_and_external_dependency(parametric):
    references = parametric._geometry_reference_map([{
        "id": "vertical_edges", "feature": "panel",
        "selector": {"kind": "edge", "where": {"parallel_to": "z"}},
    }])
    fillet = parametric._expand_geometry_references({
        "id": "rounded", "type": "fillet", "selector": {"reference": "vertical_edges"},
    }, references)
    sketch = parametric._expand_geometry_references({
        "id": "guide", "type": "sketch_2d",
        "external_geometry": [{"reference": "vertical_edges"}],
    }, references)

    assert fillet["base"] == "panel"
    assert parametric._feature_inputs(sketch) == ["panel"]


def test_datum_frames_participate_in_graph_dependencies(parametric):
    datum = {
        "id": "offset", "type": "datum_frame", "base_frame": "root",
        "support": {"feature": "panel"},
    }
    consumer = {
        "id": "boss", "type": "cylinder",
        "placement": {"frame_id": "offset"},
    }

    assert parametric._feature_inputs(datum) == ["root", "panel"]
    assert parametric._feature_inputs(consumer) == ["offset"]
    assert "datum_frame" in parametric.op_list_feature_types()["feature_types"]
    assert parametric.op_describe_feature_type("datum_frame")["schema"]["properties"]["offsets"]


def test_manufacturing_features_are_described_and_track_inputs(parametric):
    feature_types = parametric.op_list_feature_types()["feature_types"]
    assert {"hole", "shell", "draft", "rib", "face_offset", "replace_face"} \
        <= set(feature_types)
    assert parametric.op_describe_feature_type("hole")["schema"]["required"] == [
        "id", "base", "diameter", "support",
    ]
    assert parametric._feature_inputs({
        "id": "rib", "type": "rib", "base": "body", "path": "spine",
    }) == ["body", "spine"]
    assert parametric._feature_inputs({
        "id": "replaced", "type": "replace_face", "base": "body",
        "replacement": {"feature": "surface"},
    }) == ["body", "surface"]


def test_manufacturing_nested_selectors_expand_named_references(parametric):
    references = parametric._geometry_reference_map([
        {"id": "bottom", "feature": "body", "selector": {
            "kind": "face", "where": {"axis": "z", "side": "min"},
        }},
        {"id": "replacement", "feature": "surface", "selector": {
            "kind": "face", "index": 1,
        }},
    ])
    draft = parametric._expand_geometry_references({
        "id": "draft", "type": "draft", "base": "body",
        "neutral_plane": {"reference": "bottom"},
    }, references)
    replaced = parametric._expand_geometry_references({
        "id": "replace", "type": "replace_face", "base": "body",
        "replacement": {"reference": "replacement"},
    }, references)

    assert draft["neutral_plane"]["feature"] == "body"
    assert draft["neutral_plane"]["selector"]["where"]["axis"] == "z"
    assert replaced["replacement"]["feature"] == "surface"
    assert replaced["replacement_selector"]["index"] == 1


def test_refresh_sketch_external_geometry_rebuilds_constraints(parametric, monkeypatch):
    source = types.SimpleNamespace(Name="Panel")

    class Sketch:
        Name = "Guide"
        GeometryMap = '{"line": 0, "edge": -3}'
        ExternalMap = '{"edge": {"feature": "panel", "subelement": "Edge1"}}'
        ExternalSpec = "[]"
        ExternalGeometry = [object()]

        def __init__(self):
            self.constraints = [object(), object()]
            self.added = []

        @property
        def ConstraintCount(self):
            return len(self.constraints)

        def delConstraint(self, index):
            self.constraints.pop(index)

        def delExternal(self, index):
            self.ExternalGeometry.pop(index)

        def addExternal(self, object_name, subelement):
            self.ExternalGeometry.append((object_name, subelement))
            self.added.append((object_name, subelement))

    sketch = Sketch()
    doc = types.SimpleNamespace(
        getObject=lambda name: {"Panel": source, "Guide": sketch}.get(name)
    )
    meta = {
        "id_map": {"panel": "Panel", "guide": "Guide"},
        "build_graph": [{
            "id": "guide", "type": "sketch_2d",
            "external_geometry": [{"id": "edge", "feature": "panel"}],
            "constraints": [{"id": "on_edge", "type": "point_on_object"}],
        }],
    }
    monkeypatch.setattr(parametric, "_resolve_sketch_external_geometry", lambda *args: [{
        "id": "edge", "feature": "panel", "object": source,
        "subelement": "Edge4",
    }])
    captured = {}

    def rebuild(_sketch, specs, geometry_map, host):
        captured["geometry_map"] = dict(geometry_map)
        _sketch.constraints.append(object())
        return {"on_edge": 0}

    monkeypatch.setattr(parametric, "_build_sketch_constraints", rebuild)
    monkeypatch.setattr(parametric, "_check_sketch_solver", lambda *args: None)

    remapped = parametric._refresh_sketch_external_geometry(
        doc, meta, ["guide"], object()
    )

    assert remapped == ["guide"]
    assert sketch.added == [("Panel", "Edge4")]
    assert captured["geometry_map"] == {"line": 0, "edge": -3}
    assert sketch.ConstraintMap == '{"on_edge": 0}'
    assert '"Edge4"' in sketch.ExternalMap


def test_get_sketch_status_returns_solver_and_named_maps(parametric, monkeypatch):
    sketch = types.SimpleNamespace(
        Name="Sketch", GeometryCount=2, ConstraintCount=3, DoF=0,
        FullyConstrained=True, ConflictingConstraints=[], RedundantConstraints=[],
        MalformedConstraints=[], GeometryMap='{"line": 0}',
        ConstraintMap='{"width": 2}', ExternalMap='{}',
        isDerivedFrom=lambda type_id: type_id == "Sketcher::SketchObject",
    )
    doc = types.SimpleNamespace(getObject=lambda name: sketch if name == "Sketch" else None)
    meta = {"id_map": {"profile": "Sketch"}}
    monkeypatch.setattr(parametric, "_resolve", lambda component_id: (doc, object(), meta))

    status = parametric.op_get_sketch_status("component://Doc/Part", "profile")
    assert status["fully_constrained"] is True
    assert status["dof"] == 0
    assert status["geometry_map"] == {"line": 0}


def test_reusable_profiles_resolve_without_graph_objects(parametric):
    definitions = parametric._profile_definition_map([{
        "id": "mount_hole",
        "sections": [{"type": "circle", "radius": "$radius"}],
    }])

    assert list(definitions) == ["mount_hole"]
    resolved = parametric._resolve_profile_spec(
        {"profile_id": "mount_hole", "plane": "XZ"}, definitions
    )
    assert resolved["plane"] == "XZ"
    assert resolved["sections"][0]["radius"] == "$radius"

    with pytest.raises(ValueError, match="duplicate profile"):
        parametric._profile_definition_map([
            {"id": "same", "points": [[0, 0], [1, 0], [1, 1]]},
            {"id": "same", "points": [[0, 0], [2, 0], [2, 2]]},
        ])


def test_profile_definition_parameters_feed_dependency_index(parametric):
    definitions = parametric._profile_definition_map([{
        "id": "mount_hole",
        "sections": [{"type": "circle", "radius": "$radius"}],
    }])
    index = parametric._dependency_index(
        [{"name": "radius"}],
        [{"id": "hole", "type": "pad", "profile_id": "mount_hole"}],
        definitions,
    )

    assert index["parameter_features"]["radius"] == ["hole"]


def test_sweep_and_loft_dependencies_use_graph_references_only(parametric):
    assert parametric._feature_inputs({
        "id": "rail", "type": "sweep", "profile_id": "section", "path": "curve",
    }) == ["curve"]
    assert parametric._feature_inputs({
        "id": "transition", "type": "loft",
        "loft_profiles": [
            {"profile_id": "small"}, {"profile": "large_feature"},
        ],
    }) == ["large_feature"]


def test_extrude_cut_depends_on_base_and_optional_profile(parametric):
    assert parametric._feature_inputs({
        "id": "cut", "type": "extrude_cut", "base": "panel", "profile": "hole",
    }) == ["panel", "hole"]


def test_face_support_is_a_dependency_without_duplicates(parametric):
    assert parametric._feature_inputs({
        "id": "hole", "type": "profile_2d",
        "support": {"feature": "panel", "face": {"axis": "z", "side": "max"}},
    }) == ["panel"]
    assert parametric._feature_inputs({
        "id": "cut", "type": "pocket", "base": "panel",
        "support": {"feature": "panel", "face_index": 6},
    }) == ["panel"]


def test_support_feature_id_accepts_shorthand_and_default(parametric):
    assert parametric._support_feature_id("panel") == "panel"
    assert parametric._support_feature_id({"feature": "panel"}) == "panel"
    assert parametric._support_feature_id({"face_index": 6}, "base") == "base"
    assert parametric._support_feature_id(None, "base") is None
    assert parametric._consumed({
        "id": "cut", "type": "extrude_cut", "base": "panel", "profile": "hole",
    }) == ["panel", "hole"]


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
    for proxy_type in (
        parametric._ProfileExtrusionProxy,
        parametric._ManufacturingFeatureProxy,
        parametric._LinkedShapeProxy,
    ):
        proxy = proxy_type()
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
