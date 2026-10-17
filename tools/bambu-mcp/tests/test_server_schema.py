"""Guards the MCP surface: every tool carries hints plus input/output JSON schemas with descriptions."""
import asyncio

import pytest
from fastmcp import Client
from pydantic import ValidationError

from bambu_mcp.server import mcp
from bambu_mcp.schemas import VariantSpec

READ_ONLY = {"studio_info", "presets_list", "parameters_catalog", "project_inspect", "project_validate", "compare"}


@pytest.fixture(scope="module")
def tools():
    async def go():
        async with Client(mcp) as c:
            return {t.name: t for t in await c.list_tools()}
    return asyncio.run(go())


def test_expected_tools_present(tools):
    assert set(tools) >= READ_ONLY | {"project_open", "settings_set", "settings_reset", "object_transform", "orient",
                                      "arrange", "slice", "evaluate_batch", "optimize", "export", "open_in_studio"}


def test_every_tool_has_hints_description_and_schemas(tools):
    for name, t in tools.items():
        a = t.annotations
        assert t.description and len(t.description) > 20, name
        assert a is not None and a.title, name
        assert a.read_only_hint is (name in READ_ONLY), name
        assert a.open_world_hint is not None and a.idempotent_hint is not None, name
        assert t.input_schema["type"] == "object", name
        assert t.output_schema and (t.output_schema.get("properties") or t.output_schema.get("additionalProperties")), name
        for prop, spec in t.input_schema.get("properties", {}).items():
            assert spec.get("description"), f"{name}.{prop} has no description"


def test_destructive_only_where_it_overwrites(tools):
    destructive = {n for n, t in tools.items() if t.annotations.destructive_hint}
    assert destructive == {"export"}


def test_bounded_inputs(tools):
    props = tools["evaluate_batch"].input_schema["properties"]
    assert props["variants"]["maxItems"] == 32 and props["workers"]["maximum"] == 8
    assert tools["optimize"].input_schema["properties"]["keep_supports"]["enum"] == ["auto", "required", "forbidden"]


def test_batch_variant_schema_rejects_unknown_and_empty_changes(tools):
    variants_schema = tools["evaluate_batch"].input_schema["properties"]["variants"]
    variant_schema = variants_schema["items"]
    assert variants_schema["examples"] == [[{"overrides": {"layer_height": 0.24}}]]
    assert variant_schema["additionalProperties"] is False
    assert variant_schema["properties"]["flags"]["additionalProperties"] is False
    assert "overrides" in variant_schema["properties"]
    for payload in ({"settings": {"layer_height": 0.24}}, {}, {"flags": {"unknown": True}},
                    {"object_overrides": {"2": {}}}):
        with pytest.raises(ValidationError):
            VariantSpec.model_validate(payload)
    assert VariantSpec.model_validate({"overrides": {"layer_height": 0.24}}).overrides["layer_height"] == 0.24
