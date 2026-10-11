import json
import pytest
from print_pipeline_core.spec import resolve_spec, violations
from print_pipeline_core.report import summarize


def test_nozzle_bounds_constraints_and_unchanged_baseline():
    base = {'nozzle_diameter': ['0.2'], 'min_layer_height': ['0.05'], 'max_layer_height': ['0.2'],
            'layer_height': '0.1', 'wall_loops': '2', 'sparse_infill_density': '15%', 'brim_type': 'outer_only'}
    spec = resolve_spec({'min_wall_mm': .6, 'locked_keys': ['seam_position'], 'require_brim': True}, base)
    assert spec['bounds']['max_layer_height'] == pytest.approx(.15)
    assert spec['min_wall_loops'] == 3
    assert violations(base, base, spec) == []  # existing below-floor wall choice is preserved
    bad = violations({**base, 'wall_loops': '1', 'layer_height': '.2', 'brim_type': 'no_brim'}, base, spec)
    assert set(bad) == {'wall_loops below minimum', 'layer_height outside resolved printer/spec limits',
                        'require_brim needs an explicit brim type (auto_brim is not a guarantee)'}
    with pytest.raises(ValueError, match='disagrees'):
        resolve_spec({'nozzle_mm': .4}, base)
    with pytest.raises(ValueError, match='Unknown'):
        resolve_spec({'unrecognized': 1}, base)


def test_pareto_and_conservative_report_uses_measured_tradeoffs():
    base = {'wall_loops': 3, 'layer_height': .2}
    rows = [dict(name=name, status=status, settings=changes, metrics=dict(seconds=t, total_grams=g)) for name, status, changes, t, g in
            [('baseline', 'success', {}, 100, 10), ('fast', 'success', {'layer_height': .28}, 60, 11),
             ('light', 'success', {'wall_loops': 2}, 90, 7), ('dominated', 'success', {'wall_loops': 2}, 95, 9),
             ('outside', 'outside', {}, 1, 1), ('invalid', 'success', {}, float('nan'), 1)]]
    report = summarize(rows, base)
    assert report['winners'] == {'fastest': 'fast', 'least_material': 'light', 'conservative': 'baseline'}
    assert report['pareto'] == ['fast', 'light']  # light is faster and lighter than baseline
    assert report['alternatives'][0]['diff']['layer_height'] == {'before': .2, 'after': .28}
    assert 'coarser layers' in report['alternatives'][0]['tradeoffs'][0]
    rows[1]['constraint_violations'] = ['layer height']
    assert summarize(rows, base)['winners']['fastest'] == 'light'


def test_shared_parser_rejects_success_json_without_artifact(tmp_path):
    from bambu_mcp.runner import Runner
    r = Runner.__new__(Runner)
    (tmp_path/'result.json').write_text(json.dumps({'return_code':0,'sliced_plates':[{'id':1,'total_predication':10,'filaments':[{'total_used_g':1}]}]}))
    assert r._cached(tmp_path, 'key', 0) is None
    m = r._cached(tmp_path, 'key', 0, failures=True)
    assert not m.ok and 'invalid sliced artifact' in m.error


def test_per_slot_presets_are_resolved_without_collapsing_filaments():
    from print_pipeline_core.spec import merge_presets
    loaded = [('machine', {'nozzle_diameter':['0.4']}), ('process',{'wall_loops':'3'}),
              ('filament', {'filament_colour':['#00AA00'],'filament_density':['1.24']}),
              ('filament', {'filament_colour':['#FFFFFF'],'filament_density':['1.20']})]
    effective = merge_presets({'wall_loops':'2'},loaded)
    assert effective['wall_loops']=='3'
    assert effective['filament_colour']==['#00AA00','#FFFFFF']
    assert effective['filament_density']==['1.24','1.20']
    assert resolve_spec({},effective)['nozzle_mm']==.4


def test_optimizer_shared_nozzle_limits_and_report():
    from bambu_mcp import optimize
    from bambu_mcp.runner import Variant, Metrics
    from test_unit import FIXTURE
    class R:
        def effective_settings(self, source, v, **kw):
            return {'nozzle_diameter':['.2'], 'layer_height':'.1', 'wall_loops':'2',
                    'sparse_infill_density':'15%', **v.overrides}, {}
        def evaluate(self, source, v, **kw):
            return Metrics(ok=True,time_s=100,filament_g=10)
    # String formatting uses 0.14; independent expected ladder proves nozzle-based candidate generation.
    result = optimize.optimize(R(), FIXTURE, Variant(), optimize.Objective(),
                               stages=['layer_height'], print_spec={}, workers=1)
    assert {float(h['overrides']['layer_height']) for h in result.history}=={.1,.12,.14}
    assert result.report['print_spec']['bounds']['max_layer_height']==pytest.approx(.15)
    assert result.report['winners']['conservative']=='baseline'
    assert result.best.overrides=={}  # equal measured time cannot justify quality loss


def test_spec_lists_do_not_escape_into_other_requests_or_caller():
    settings = {'nozzle_diameter':['.4']}
    caller = {'protected_surfaces':['outer']}
    first = resolve_spec(caller, settings)
    first['locked_keys'].append('layer_height')
    first['protected_surfaces'].append('bottom')
    first['review_requirements'][0]['value'].append('inner')
    assert resolve_spec({}, settings)['locked_keys'] == []
    assert caller['protected_surfaces'] == ['outer']
    assert first['protected_surfaces'] == ['outer', 'bottom']


def test_optimizer_preserves_explicit_objective_requirements_with_partial_spec():
    from bambu_mcp import optimize
    from bambu_mcp.runner import Variant, Metrics
    from test_unit import FIXTURE
    class R:
        def effective_settings(self, source, v, **kw):
            return {'nozzle_diameter':['.6'], 'enable_support':'0', **v.overrides}, {}
        def evaluate(self, source, v, **kw):
            assert v.overrides['enable_support'] == '0'
            return Metrics(ok=True, time_s=100, filament_g=10)
    o = optimize.Objective(functional=False, keep_supports='forbidden', max_time_increase_pct=5,
                           max_material_increase_pct=3)
    result = optimize.optimize(R(), FIXTURE, Variant(), o, stages=['layer_height'],
                               print_spec={}, workers=1)
    assert (o.functional, o.keep_supports, o.max_time_increase_pct, o.max_material_increase_pct) == (False, 'forbidden', 5, 3)
    assert {float(h['overrides']['layer_height']) for h in result.history} == {.3, .36, .42}
    capped = optimize.Objective(max_layer_height=.28, specification=resolve_spec({}, {'nozzle_diameter':['.6']}))
    assert [float(v.overrides['layer_height']) for v in optimize.candidates('layer_height', Variant(), FIXTURE, capped)] == [.28]
    with pytest.raises(ValueError, match='Conflicting'):
        optimize.optimize(R(), FIXTURE, Variant(), optimize.Objective(keep_supports='forbidden'),
                          print_spec={'keep_supports':'required'})


def test_boolean_candidates_use_slicer_setting_encoding():
    from bambu_mcp.pipeline import candidate_specs
    assert [r['settings']['enable_support'] for r in candidate_specs([
        {'name':'yes','settings':{'enable_support':True}},
        {'name':'no','settings':{'enable_support':False}}])] == ['1', '0']


@pytest.mark.parametrize('spec,field', [({'filament_presets':None},'filament_presets'),
    ({'filament_presets':'PLA'},'filament_presets'), ({'machine_preset':1},'machine_preset'),
    ({'process_preset':[]},'process_preset')])
def test_invalid_presets_rejected_before_loader(tmp_path, monkeypatch, spec, field):
    from bambu_mcp import pipeline
    from bambu_mcp.benchmarks import make_project
    source = make_project(tmp_path/'native.3mf', 'narrow_footprint', {})
    def forbidden(*a, **kw):
        pytest.fail('Preset loading must not run for malformed preset fields')
    monkeypatch.setattr(pipeline, 'Runner', forbidden)
    with pytest.raises(ValueError, match=field):
        pipeline.compare_project(source, tmp_path/'out', [{'name':'same'}], print_spec=spec)
    assert not (tmp_path/'out').exists()


def test_reported_default_layer_cap_matches_enforced_cap():
    from bambu_mcp.optimize import Objective, _violations
    assert _violations({'layer_height':'0.4'}, Objective()) == ['layer_height=0.4 above max 0.28']


def test_compare_uses_strict_artifacts_and_preserves_corrupt_cache(tmp_path, monkeypatch):
    import zipfile
    from bambu_mcp import server
    from bambu_mcp.runner import Runner
    r = Runner.__new__(Runner)
    r.home = tmp_path
    monkeypatch.setattr(server, 'runner', lambda: r)
    ids = ['a'*24, 'b'*24]
    for rid, outside in zip(ids, ['false','true']):
        directory = tmp_path/'runs'/rid
        directory.mkdir(parents=True)
        # JSON predictions deliberately differ: compare must use the sliced artifact.
        (directory/'result.json').write_text(json.dumps({'return_code':0, 'sliced_plates':[
            {'id':1, 'total_predication':10, 'filaments':[{'total_used_g':1}]}]}))
        xml = '<config><plate><metadata key="prediction" value="100"/><metadata key="weight" value="4"/>' + f'<metadata key="outside" value="{outside}"/></plate></config>'
        with zipfile.ZipFile(directory/'out.3mf', 'w') as z:
            z.writestr('Metadata/slice_info.config', xml)
            z.writestr('Metadata/plate_1.gcode', '; FEATURE: Support\n; LAYER_HEIGHT: 0.2\nG1 E1\n')
    rows = server.compare(ids)
    assert rows[0].ok and rows[0].time_s == 100 and rows[0].filament_g == 4
    assert not rows[1].ok and rows[1].outside
    assert rows[1].toolpath_profiles[0]['feature_blocks'] == {'Support':1}
    raw = tmp_path/'runs'/ids[0]/'result.json'
    raw.write_text('{broken')
    with pytest.raises(ValueError, match='unknown run'):
        server.compare([ids[0]])
    assert raw.read_text() == '{broken'  # read-only compare must not invalidate the cache


def test_prototype_still_honors_explicit_minimum_and_locked_assembly():
    base = {'nozzle_diameter':['.4'],'wall_loops':4,'enable_support':'1','brim_type':'outer_only'}
    spec = resolve_spec({'functional':False,'min_wall_loops':3,'locked_objects':[2],
                         'keep_supports':'required'},base)
    errors = violations({**base,'wall_loops':2,'enable_support':'0'},base,spec,rotation=True)
    assert set(errors)=={'wall_loops below minimum','enable_support conflicts with keep_supports',
                         'whole-assembly rotation conflicts with locked_objects'}


def test_artifact_multi_plate_selection_and_unknown_buckets(tmp_path):
    import zipfile
    from print_pipeline_core.artifacts import read_artifacts
    xml = '<config>'+''.join(f'<plate><metadata key="index" value="{i}"/><metadata key="prediction" value="{t}"/><metadata key="weight" value="{g}"/><metadata key="outside" value="false"/></plate>' for i,t,g in [(1,100,4),(2,50,2)])+'</config>'
    p=tmp_path/'out.3mf'
    with zipfile.ZipFile(p,'w') as z:
        z.writestr('Metadata/slice_info.config',xml)
        z.writestr('Metadata/plate_1.gcode','; total layer number: 20\nG1 E1')
        z.writestr('Metadata/plate_2.gcode','; total layer number: 10\nG1 E1')
    all_plates = read_artifacts(p,tmp_path,strict_single_plate=False)
    selected = read_artifacts(p,tmp_path,plate=2,strict_single_plate=False)
    assert (all_plates['seconds'],all_plates['total_grams'],all_plates['layers'])==(150,6,30)
    assert (selected['seconds'],selected['total_grams'],selected['layers'])==(50,2,10)
    assert all_plates['support_grams'] is None
    with pytest.raises(ValueError,match='exactly one'):
        read_artifacts(p,tmp_path)


def test_identical_nozzle_arrays_and_auto_width_are_explicitly_supported():
    spec=resolve_spec({'min_wall_mm':.8},{'nozzle_diameter':['.4','.4'],'line_width':'0'})
    assert spec['nozzle_mm']==.4
    assert spec['min_wall_loops']==2
    assert spec['bounds']['wall_width_source']=='nozzle_proxy'
    with pytest.raises(ValueError,match='Mixed'):
        resolve_spec({}, {'nozzle_diameter':['.2','.4']})
    assert resolve_spec(spec, {'nozzle_diameter':['.4']})['bounds']==spec['bounds']


def test_bounded_execution_stops_child_holding_pipe_after_parent_exit(tmp_path):
    import os, sys, time
    from print_pipeline_core.process import run_bounded
    child = 'import time,pathlib; p=pathlib.Path("tick"); i=0\nwhile True:\n p.write_text(str(i)); i+=1; time.sleep(.02)'
    script = 'import subprocess,sys,pathlib; p=subprocess.Popen([sys.executable,"-c",' + repr(child) + ']); pathlib.Path("child.pid").write_text(str(p.pid))'
    _, error, _, elapsed = run_bounded([sys.executable,'-c',script],tmp_path,.8)
    assert error=='timeout' and elapsed<3
    pid=int((tmp_path/'child.pid').read_text())
    try:
        # Independent observable work must stop, regardless of orphan/zombie reaping timing.
        time.sleep(.05)
        before=(tmp_path/'tick').read_text()
        time.sleep(.1)
        assert (tmp_path/'tick').read_text()==before
        assert int(before)>0
    finally:
        try:os.kill(pid,9)
        except ProcessLookupError:pass


@pytest.mark.parametrize("cleanup_enabled", [True, False])
def test_worker_timeout_terminates_separately_grouped_slicer(tmp_path, cleanup_enabled):
    import os, sys, time
    from print_pipeline_core.process import run_bounded
    child='import pathlib,time,os; pathlib.Path("nested.pid").write_text(str(os.getpid())); p=pathlib.Path("nested.tick"); i=0\nwhile True:\n p.write_text(str(i)); i+=1; time.sleep(.02)'
    worker='from concurrent.futures import ThreadPoolExecutor; from pathlib import Path; import sys; from bambu_mcp.pipeline import install_cancellation; from print_pipeline_core.process import run_bounded; install_cancellation()\nwith ThreadPoolExecutor(1) as pool: pool.submit(run_bounded,[sys.executable,"-c",'+repr(child)+'],Path.cwd(),30).result()'
    if not cleanup_enabled:
        worker = worker.replace('install_cancellation()\n', 'install_cancellation(); import signal; signal.signal(signal.SIGTERM,signal.SIG_DFL)\n')
    _,error,_,elapsed=run_bounded([sys.executable,'-c',worker],tmp_path,1)
    assert error=='timeout' and elapsed<3
    pid=int((tmp_path/'nested.pid').read_text())
    try:
        time.sleep(.05)
        before=(tmp_path/'nested.tick').read_text()
        time.sleep(.1)
        assert int(before)>0
        assert ((tmp_path/'nested.tick').read_text()==before) is cleanup_enabled
    finally:
        try:os.kill(pid,9)
        except ProcessLookupError:pass
