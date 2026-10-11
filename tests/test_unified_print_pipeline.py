import json
from pathlib import Path
import sys
import pytest
import slicer_pipeline as sp
from test_slicer_pipeline import project, slicer
from print_pipeline_core.spec import resolve_spec


def test_legacy_spec_blocks_weak_candidate_and_reports_winners(project, slicer, tmp_path):
    report = sp.compare_print_candidates(project, tmp_path/'results', [
        {'name':'fast','settings':{'layer_height':.28}}, {'name':'weak','settings':{'wall_loops':1}}], slicer,
        print_spec={'nozzle_mm':.4})
    assert report['winners']['fastest'] == 'fast'
    assert report['winners']['conservative'] == 'baseline'
    weak = report['candidates'][2]
    assert weak['status'] == 'rejected' and 'wall_loops below minimum' in weak['constraint_violations']
    assert not (tmp_path/'results/weak/invoked').exists()
    assert report['print_spec']['bounds']['max_layer_height'] == pytest.approx(.3)


def test_owned_bridge_calls_real_worker_and_preserves_assembly(project, slicer, tmp_path, monkeypatch):
    # Independent fake slicer emits complete artifacts and result.json expected by real cache Runner.
    text = slicer.read_text().replace("mode=folder.name", "mode='normal'")
    text = text.replace("print('warning: preset missing')", "(folder/'result.json').write_text(json.dumps({'return_code':0,'sliced_plates':[{'id':1,'total_predication':seconds,'filaments':[{'total_used_g':42.5}]}]}))")
    slicer.write_text(text)
    monkeypatch.setenv('BAMBU_MCP_HOME', str(tmp_path/'cache'))
    python = Path(__file__).resolve().parents[1]/'tools/bambu-mcp/.venv/bin/python'
    assert python.is_file(), 'Install tools/bambu-mcp to run the cross-process test'
    report = sp.compare_print_candidates(project, tmp_path/'results', [{'name':'fast','settings':{'layer_height':.28}}],
                                        slicer, backend='bambu', print_spec={'nozzle_mm':.4}, backend_python=python)
    assert report['backend'] == 'bambu' and report['winners']['fastest'] == 'fast'
    assert report['candidates'][1]['metrics']['seconds'] == 80
    assert report['source_unchanged'] is True
    import zipfile
    with zipfile.ZipFile(project) as a,zipfile.ZipFile(report['candidates'][1]['input_path']) as b:
        assert a.read('3D/3dmodel.model') == b.read('3D/3dmodel.model')
        assert a.read('Metadata/model_settings.config') == b.read('Metadata/model_settings.config')
    # Same request in a different output directory must reuse both successful cached runs.
    repeated = sp.compare_print_candidates(project, tmp_path/'again', [{'name':'fast','settings':{'layer_height':.28}}],
                                           slicer, backend='bambu', print_spec={'nozzle_mm':.4}, backend_python=python)
    assert [r['run_id'] for r in repeated['candidates']] == [r['run_id'] for r in report['candidates']]
    assert repeated['winners'] == report['winners']


def test_legacy_and_owned_share_constraint_rejections(project, slicer, tmp_path, monkeypatch):
    # The root and standalone entry points must implement the same specification contract.
    text = slicer.read_text().replace("mode=folder.name", "mode='normal'")
    text = text.replace("print('warning: preset missing')", "(folder/'result.json').write_text(json.dumps({'return_code':0,'sliced_plates':[{'id':1,'total_predication':seconds,'filaments':[{'total_used_g':42.5}]}]}))")
    slicer.write_text(text)
    monkeypatch.setenv('BAMBU_MCP_HOME',str(tmp_path/'cache'))
    candidates=[{'name':'weak','settings':{'wall_loops':1}}, {'name':'locked','rotation':[90,0,0]}]
    spec={'nozzle_mm':.4,'locked_objects':[2]}
    legacy = sp.compare_print_candidates(project,tmp_path/'legacy',candidates,slicer,print_spec=spec)
    owned = sp.compare_print_candidates(project,tmp_path/'owned',candidates,slicer,print_spec=spec,backend='bambu')
    assert legacy['winners']==owned['winners']=={'fastest':'baseline','least_material':'baseline','conservative':'baseline'}
    for result in (legacy,owned):
        assert result['candidates'][1]['constraint_violations']==['wall_loops below minimum', 'object 2: wall_loops below minimum']
        assert 'locked_objects' in result['candidates'][2]['constraint_violations'][0]
        assert result['candidates'][1]['status']=='rejected'
        assert result['candidates'][2]['status']=='rejected'
        assert result['candidates'][0]['metrics']['seconds']==100
