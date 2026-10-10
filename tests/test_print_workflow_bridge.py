import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import pytest
import freecad_bridge as fb
from test_slicer_pipeline import project, slicer
from test_orientation_candidates import envelope, matrix_cli


@pytest.mark.parametrize("tool,arguments,kind", [
    (fb.inspect_printability, {"component_id":"component://D/C", "min_wall":1.2,
                              "max_samples":50, "build_direction":[0,1,0]}, "inspect_printability"),
    (fb.inspect_attachment, {"component_id":"component://D/C", "feature_id":"bumper",
                            "target_id":"body", "anchor":[1,2,3]}, "inspect_attachment"),
    (fb.create_connector_features, {"component_id":"component://D/C", "prefix":"peg",
                                    "male_base":"body", "female_base":"trim",
                                    "position":[0,0,0], "clearance":.3}, "create_connector_features"),
])
def test_print_tools_forward_contract(monkeypatch, tool, arguments, kind):
    async def fake(payload):
        assert payload["type"] == kind
        assert all(payload["params"][k] == v for k,v in arguments.items())
        return {"status":"success", "result":{"findings":[{"actual_mm":.5,"threshold_mm":1.2}]}}
    monkeypatch.setattr(fb, "send_to_freecad", fake)
    assert asyncio.run(tool(**arguments))["findings"][0]["actual_mm"] == .5


def test_local_orientation_matrix_reaches_real_compare_entrypoint(envelope, project, slicer, tmp_path):
    suggested = asyncio.run(fb.suggest_print_orientations(
        str(envelope), "tweaker3", matrix_cli(tmp_path), build_volume=[256,256,256]))
    matrix = suggested["candidates"][0]["rotation_matrix"]
    compared = asyncio.run(fb.compare_print_candidates(str(project), str(tmp_path/"comparison"),
        [{"name":"oriented", "rotation_matrix":matrix, "settings":{"layer_height":.28}}], str(slicer)))
    assert compared["best_candidate"] == "oriented"
    assert compared["candidates"][1]["metrics"]["seconds"] == 80
    assert compared["candidates"][1]["rotation_matrix"] == matrix
    assert compared["source_unchanged"] is True
