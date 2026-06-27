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
