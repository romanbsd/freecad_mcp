import json
import os
from pathlib import Path
import pytest
import slicer_pipeline as sp
from test_slicer_pipeline import project, slicer


@pytest.fixture
def owned_python():
    configured = os.environ.get('FREECAD_BAMBU_BACKEND_PYTHON')
    python = Path(configured) if configured else Path(__file__).resolve().parents[1]/'tools/bambu-mcp/.venv/bin/python'
    if not python.is_file():
        if configured:
            pytest.fail('Configured FREECAD_BAMBU_BACKEND_PYTHON is not a file')
        pytest.skip('Install tools/bambu-mcp to run the cross-process test')
    return python


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


def test_legacy_noop_boolean_preserves_locked_setting(project, slicer, tmp_path):
    import zipfile
    with zipfile.ZipFile(project) as z:
        entries = {i.filename:z.read(i) for i in z.infolist()}
    settings = json.loads(entries['Metadata/project_settings.config'])
    settings['enable_support'] = '1'
    entries['Metadata/project_settings.config'] = json.dumps(settings).encode()
    with zipfile.ZipFile(project, 'w') as z:
        for name, data in entries.items():
            z.writestr(name, data)
    report = sp.compare_print_candidates(project, tmp_path/'boolean',
        [{'name':'same', 'settings':{'enable_support':True}}], slicer,
        print_spec={'nozzle_mm':.4, 'locked_keys':['enable_support']})
    row = report['candidates'][1]
    assert row['status'] == 'success' and row['constraint_violations'] == []
    assert row['effective_settings']['enable_support'] == '1'
    assert all(item['diff'] == {} for item in report['alternatives'])


@pytest.mark.parametrize('candidates', [None, 4, 'abc', [], [{}]*13])
def test_owned_candidate_count_rejected_before_worker_or_source_access(tmp_path, candidates):
    with pytest.raises(ValueError, match='candidates must contain 1..12 entries'):
        sp.compare_print_candidates(tmp_path/'missing.3mf', tmp_path/'out', candidates, backend='bambu')
    assert not (tmp_path/'out').exists()


def test_owned_bridge_calls_real_worker_and_preserves_assembly(project, slicer, tmp_path, monkeypatch, owned_python):
    # Independent fake slicer emits complete artifacts and result.json expected by real cache Runner.
    text = slicer.read_text().replace("mode=folder.name", "mode='normal'")
    text = text.replace("print('warning: preset missing')", "(folder/'result.json').write_text(json.dumps({'return_code':0,'sliced_plates':[{'id':1,'total_predication':seconds,'filaments':[{'total_used_g':42.5}]}]}))")
    slicer.write_text(text)
    monkeypatch.setenv('BAMBU_MCP_HOME', str(tmp_path/'cache'))
    python = owned_python
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


def test_legacy_and_owned_share_constraint_rejections(project, slicer, tmp_path, monkeypatch, owned_python):
    # The root and standalone entry points must implement the same specification contract.
    text = slicer.read_text().replace("mode=folder.name", "mode='normal'")
    text = text.replace("print('warning: preset missing')", "(folder/'result.json').write_text(json.dumps({'return_code':0,'sliced_plates':[{'id':1,'total_predication':seconds,'filaments':[{'total_used_g':42.5}]}]}))")
    slicer.write_text(text)
    monkeypatch.setenv('BAMBU_MCP_HOME',str(tmp_path/'cache'))
    candidates=[{'name':'weak','settings':{'wall_loops':1}}, {'name':'locked','rotation':[90,0,0]}]
    spec={'nozzle_mm':.4,'locked_objects':[2]}
    legacy = sp.compare_print_candidates(project,tmp_path/'legacy',candidates,slicer,print_spec=spec)
    owned = sp.compare_print_candidates(project,tmp_path/'owned',candidates,slicer,print_spec=spec,backend='bambu',backend_python=owned_python)
    assert legacy['winners']==owned['winners']=={'fastest':'baseline','least_material':'baseline','conservative':'baseline'}
    for result in (legacy,owned):
        assert result['candidates'][1]['constraint_violations']==['wall_loops below minimum', 'object 2: wall_loops below minimum']
        assert 'locked_objects' in result['candidates'][2]['constraint_violations'][0]
        assert result['candidates'][1]['status']=='rejected'
        assert result['candidates'][2]['status']=='rejected'
        assert result['candidates'][0]['metrics']['seconds']==100
