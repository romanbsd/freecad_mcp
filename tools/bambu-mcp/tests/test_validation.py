import hashlib
import io
import os
import re
import zipfile

import pytest
from bambu_mcp.benchmarks import make_project
from print_pipeline_core.toolpath import comment_profile
from print_pipeline_core.validation import validate_file


def test_comments_preserve_unknowns_and_do_not_parse_motion():
    profile = comment_profile(io.BytesIO(b'; LAYER_HEIGHT: 0.2\n; FEATURE: Support\nM620.1 E F523 T210\n'
                                        b'; FEATURE: Outer wall\n; LAYER_HEIGHT: 0.4\n; LAYER_HEIGHT: nan\n'))
    assert profile['complete']
    assert profile['feature_blocks'] == {'Support': 1, 'Outer wall': 1}
    assert profile['declared_layer_heights_mm'] == {'0.2': 1, '0.4': 1}
    assert profile['malformed_height_comments'] == 1
    empty = comment_profile(io.BytesIO(b'G1 X1 E2\n'))
    assert empty['min_declared_layer_height_mm'] is None
    assert empty['feature_blocks'] == {}


def test_comment_scan_is_bounded_and_reports_partial_results():
    text = b'; FEATURE: Support\n; FEATURE: Outer wall\n'
    result = comment_profile(io.BytesIO(text), max_bytes=20)
    assert not result['complete'] and result['feature_blocks'] == {'Support': 1}
    assert not comment_profile(io.BytesIO(b';' + b'x' * 70000))['complete']


def test_validator_worker_errors_and_duplicate_archive(tmp_path):
    project = make_project(tmp_path/'cube.3mf', 'narrow_footprint', {})
    # A valid interpreter with a deliberately failing entry script exercises error propagation.
    fake = tmp_path/'python'
    fake.write_text('#!/bin/sh\nexit 7\n')
    fake.chmod(0o755)
    assert validate_file(project, python_executable=fake)['error'] == 'worker exit 7'
    with zipfile.ZipFile(project, 'a') as z:
        with pytest.warns(UserWarning):
            z.writestr('3D/3dmodel.model', 'duplicate')
    with pytest.raises(ValueError, match='Duplicate'):
        validate_file(project)


@pytest.mark.skipif(not os.environ.get('PRINT_VALIDATION_PYTHON'), reason='explicit optional lib3mf environment required')
def test_real_lib3mf_detects_index_and_open_mesh_without_rewriting(tmp_path):
    python = os.environ['PRINT_VALIDATION_PYTHON']
    source = make_project(tmp_path/'cube.3mf', 'narrow_footprint', {})
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    result = validate_file(source, python_executable=python)
    assert result['geometry_valid'] and result['read_only']
    assert result['source_sha256'] == before == hashlib.sha256(source.read_bytes()).hexdigest()
    with zipfile.ZipFile(source) as z:
        original = {i.filename: z.read(i) for i in z.infolist()}
    for mutation in ('invalid_index', 'open_mesh'):
        entries = dict(original)
        model = entries['3D/3dmodel.model']
        if mutation == 'invalid_index':
            model, count = re.subn(rb'(<triangle\b[^>]*\bv1=")[0-9]+', rb'\g<1>999999', model, count=1)
        else:
            model, count = re.subn(rb'<triangle\b[^>]*/>', b'', model, count=1)
        assert count == 1
        entries['3D/3dmodel.model'] = model
        path = tmp_path/(mutation+'.3mf')
        with zipfile.ZipFile(path, 'w') as z:
            for name, data in entries.items():
                z.writestr(name, data)
        result = validate_file(path, python_executable=python)
        assert result['status'] == 'failed' and not result['geometry_valid'], result


def test_project_validate_inspects_source_not_working_overrides(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from bambu_mcp import server
    from print_pipeline_core import validation
    source = tmp_path/'source.3mf'
    session = SimpleNamespace(source=source, variant={'unrelated': 'working'})
    monkeypatch.setattr(server, 'sess', lambda sid: session)
    monkeypatch.setattr(validation, 'validate_file', lambda path: {'source': str(path)})
    assert server.project_validate('test')['source'] == str(source)
