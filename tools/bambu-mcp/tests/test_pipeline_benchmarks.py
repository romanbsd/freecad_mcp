import json
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile
import pytest
from bambu_mcp.benchmarks import CASES, CORE, make_project, run_benchmarks


def test_generated_geometry_and_filament_assignment_are_substantive(tmp_path):
    shell = make_project(tmp_path/'shell.3mf', 'thin_shell', {})
    with zipfile.ZipFile(shell) as z:
        root = ET.fromstring(z.read('3D/3dmodel.model'))
        points = [tuple(float(v.get(k)) for k in ('x','y','z')) for v in root.iter('{%s}vertex'%CORE)]
        assert [max(v[k] for v in points)-min(v[k] for v in points) for k in range(3)] == [60,40,20]
        assert len(root.findall('{%s}resources/{%s}object/{%s}mesh'%(CORE,CORE,CORE))) == 5
    colored = make_project(tmp_path/'color.3mf','multicolor_assembly',{})
    with zipfile.ZipFile(colored) as z:
        parts = ET.fromstring(z.read('Metadata/model_settings.config'))
        assert [m.get('value') for m in parts.findall('object/part/metadata') if m.get('key')=='extruder'] == ['1','1','2']
    connector = CASES['fitted_connector']
    assert connector[1][3] == 4
    socket_width = connector[4][0] - (connector[3][0] + connector[3][3])
    assert (socket_width - connector[1][3]) / 2 == pytest.approx(.3)


@pytest.mark.slow
def test_representative_native_projects_are_actually_sliced(tmp_path):
    report = run_benchmarks(tmp_path/'benchmarks')
    assert {r['case'] for r in report['cases']} == set(CASES)
    for row in report['cases']:
        result = row['report']
        base = result['candidates'][0]
        assert base['metrics']['seconds'] > 0 and base['metrics']['total_grams'] > 0
        assert base['metrics']['outside'] is False
        assert base['metrics']['layers'] > 0
        profile = base['metrics']['toolpath_profiles'][0]
        assert profile['complete'] and profile['declared_layer_heights_mm']
        assert result['source_unchanged']
        fastest = next(c for c in result['candidates'] if c['name']==result['winners']['fastest'])
        assert fastest['metrics']['seconds'] <= base['metrics']['seconds']
        for candidate in result['candidates'][1:]:
            assert candidate['status']=='success'
            raw=json.loads((Path(candidate['directory'])/'result.json').read_text())
            if candidate['name']=='coarser':
                assert raw['layer_height']==pytest.approx(.28,abs=1e-6)
            else:
                assert raw['sparse_infill_density']==pytest.approx(10)
        if row['case']=='overhang_bracket':
            assert base['metrics']['feature_seconds']['Support']>0
            assert profile['feature_blocks']['Support'] > 0
        if row['case']=='multicolor_assembly':
            assert profile['feature_blocks']['Prime tower'] > 0
            raw = json.loads((Path(base['directory'])/'result.json').read_text())
            filaments = raw['sliced_plates'][0]['filaments']
            assert len([f for f in filaments if f['total_used_g']>0]) == 2
            assert base['metrics']['filament_changes'] > 0


@pytest.mark.slow
def test_boolean_support_values_reach_actual_sliced_settings(tmp_path):
    from bambu_mcp.pipeline import compare_project
    run_benchmarks(tmp_path/'corpus', cases=['narrow_footprint'])
    report = compare_project(tmp_path/'corpus/narrow_footprint.3mf', tmp_path/'bool',
        [{'name':'on','settings':{'enable_support':True}},
         {'name':'off','settings':{'enable_support':False}}], cache_home=tmp_path/'cache')
    for candidate, expected in zip(report['candidates'][1:], ('1','0')):
        assert candidate['status'] == 'success', candidate
        with zipfile.ZipFile(candidate['output_path']) as package:
            settings = json.loads(package.read('Metadata/project_settings.config'))
        assert settings['enable_support'] == expected
