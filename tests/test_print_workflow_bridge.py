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


def test_shared_print_spec_reaches_geometric_preflight(monkeypatch):
    async def fake(payload):
        assert payload['type']=='inspect_printability'
        assert payload['params']['min_wall']==1.2
        return {'status':'success','result':{'min_observed_wall_mm':.7,'findings':[{'kind':'thin_wall','actual_mm':.7,'threshold_mm':1.2}]}}
    monkeypatch.setattr(fb,'send_to_freecad',fake)
    result=asyncio.run(fb.inspect_printability('component://D/C',print_spec={'min_wall_mm':1.2}))
    assert result['findings'][0]['threshold_mm']==1.2
    assert result['min_observed_wall_mm']==.7


def test_bridge_owned_backend_uses_real_json_worker(project,slicer,tmp_path,monkeypatch):
    text=slicer.read_text().replace("mode=folder.name", "mode='normal'")
    text=text.replace("print('warning: preset missing')", "(folder/'result.json').write_text(json.dumps({'return_code':0,'sliced_plates':[{'id':1,'total_predication':seconds,'filaments':[{'total_used_g':42.5}]}]}))")
    slicer.write_text(text)
    monkeypatch.setenv('BAMBU_MCP_HOME',str(tmp_path/'cache'))
    result=asyncio.run(fb.compare_print_candidates(str(project),str(tmp_path/'result'),
        [{'name':'coarse','settings':{'layer_height':.28}}],str(slicer),backend='bambu',print_spec={'nozzle_mm':.4},workers=2))
    assert result['backend']=='bambu'
    assert result['winners']['fastest']=='coarse'
    assert result['candidates'][1]['metrics']['seconds']==80
    assert result['candidates'][1]['metrics']['total_grams']==42.5
    assert result['source_unchanged'] is True
