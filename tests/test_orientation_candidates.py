import hashlib
from pathlib import Path
import struct
import sys

import pytest

import orientation_candidates as oc


@pytest.fixture
def envelope(tmp_path):
    path = tmp_path / 'car.stl'
    path.write_text('solid car\nfacet normal 0 0 1\nouter loop\nvertex 2 3 4\nvertex 32 3 4\nvertex 2 8 6\nendloop\nendfacet\nendsolid car\n')
    return path


def cli(tmp_path, content):
    script = tmp_path / 'fake_tweaker.py'
    script.write_text(content)
    return [sys.executable, str(script)]


def matrix_cli(tmp_path, matrix='0 -1 0\n1 0 0\n0 0 1'):
    return cli(tmp_path, "import sys\nfrom pathlib import Path\np=Path(sys.argv[sys.argv.index('-i')+1])\nassert p.name == 'envelope.stl'\nassert '-r' in sys.argv and '-x' in sys.argv\np.write_text('backend changed only its copy')\nprint(" + repr('Rotation matrix:\n' + matrix + '\nUnprintability: 0.125') + ")\n")


def test_tweaker_transposes_native_row_matrix_rechecks_original_mesh(envelope, tmp_path):
    before = hashlib.sha256(envelope.read_bytes()).hexdigest()
    result = oc.suggest_print_orientations(envelope, 'tweaker3', matrix_cli(tmp_path), build_volume=[5, 30, 2])
    assert result['status'] == 'success'
    candidate = result['candidates'][0]
    assert candidate['rotation_matrix'] == [[0, 1, 0], [-1, 0, 0], [0, 0, 1]]
    assert candidate['bounds_mm'] == {'min': [3, -32, 4], 'max': [8, -2, 6]}
    assert candidate['extents_mm'] == [5, 30, 2]
    assert candidate['translation_to_origin_mm'] == [-3, 32, -4]
    assert candidate['heuristic_score'] == .125
    assert before == hashlib.sha256(envelope.read_bytes()).hexdigest()
    assert sorted(p.name for p in tmp_path.iterdir()) == ['car.stl', 'fake_tweaker.py']


def test_candidate_does_not_fit_axis_order(envelope, tmp_path):
    result = oc.suggest_print_orientations(envelope, 'tweaker3', matrix_cli(tmp_path), build_volume=[30, 5, 2])
    assert result['status'] == 'no_fit_found'
    assert result['candidates'][0]['fits_build_volume'] is False


@pytest.mark.parametrize('matrix', ['2 0 0\n0 1 0\n0 0 1', '-1 0 0\n0 1 0\n0 0 1', 'nan 0 0\n0 1 0\n0 0 1', '1 0\n0 1\n0 0'])
def test_backend_rejects_scale_mirror_nonfinite_and_malformed(envelope, tmp_path, matrix):
    result = oc.suggest_print_orientations(envelope, 'tweaker3', matrix_cli(tmp_path, matrix))
    assert result['status'] == 'error'
    assert result['candidates'] == []


def test_optional_backend_unavailable(envelope):
    assert oc.suggest_print_orientations(envelope, 'tweaker3')['status'] == 'unavailable'
    assert oc.suggest_print_orientations(envelope, 'tweaker3', '/nonexistent/tool')['status'] == 'unavailable'


def test_timeout_and_output_limit(envelope, tmp_path):
    cmd = cli(tmp_path, 'import time\ntime.sleep(20)\n')
    assert oc.suggest_print_orientations(envelope, 'tweaker3', cmd, timeout=.1)['status'] == 'timeout'
    cmd = cli(tmp_path, "print('x' * 300000)\n")
    assert 'output limit' in oc.suggest_print_orientations(envelope, 'tweaker3', cmd)['error']


def test_missing_result_and_exit_failure(envelope, tmp_path):
    assert oc.suggest_print_orientations(envelope, 'tweaker3', cli(tmp_path, "print('no matrix')"))['status'] == 'error'
    assert 'exit 3' in oc.suggest_print_orientations(envelope, 'tweaker3', cli(tmp_path, 'raise SystemExit(3)'))['error']


def install_stl_fit_fake(tmp_path, matrix=None):
    # Independent fake of the inspected upstream signatures; no provider install.
    (tmp_path / 'numpy.py').write_text('''
class Random:
    def seed(self, seed):
        assert seed == 42
random = Random()
def array(values): return values
def sort(values): return sorted(values)
''')
    (tmp_path / 'stl_fit.py').write_text('raise RuntimeError("CLI must never run: it would scale the model")')
    native = matrix or [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    (tmp_path / 'stl_fit_lib.py').write_text('''
class Matrix:
    def tolist(self): return MATRIX
class Rotation:
    def as_matrix(self): return Matrix()
def load_and_prepare(stl_path):
    assert stl_path.endswith('envelope.stl')
    return [[0, 0, 0]], object()
def coarse_search(hull_verts, build_dims, num_samples, batch_size):
    assert num_samples == 5000
    assert build_dims == [20.0] * 3
    return [(0.1, Rotation(), [1, 1, 1])], []
def refinement_phase(hull_verts, build_dims, top_k_results):
    return (0.1, Rotation(), [1, 1, 1])
def select_diverse(results, n=10): return []
def refine(hull_verts, build_dims_sorted, initial_rotation):
    return (0.1, initial_rotation, [1, 1, 1])
'''.replace('MATRIX', repr(native)))
    return [sys.executable, str(tmp_path / 'stl_fit.py')]


def test_stl_fit_library_avoids_scaling_and_revalidates_claimed_bounds(envelope, tmp_path):
    before = envelope.read_bytes()
    result = oc.suggest_print_orientations(envelope, 'stl-fit', install_stl_fit_fake(tmp_path), build_volume=[20]*3)
    assert result['status'] == 'no_fit_found'  # upstream lies: score .1, extents [1,1,1]
    assert result['candidates'][0]['extents_mm'] == [30, 5, 2]
    assert envelope.read_bytes() == before


def test_stl_fit_rejects_incompatible_runtime_signature(envelope, tmp_path):
    command = install_stl_fit_fake(tmp_path)
    path = tmp_path / 'stl_fit_lib.py'
    path.write_text(path.read_text().replace('def load_and_prepare(stl_path):', 'def load_and_prepare(stl_path, new_arg):'))
    result = oc.suggest_print_orientations(envelope, 'stl-fit', command, build_volume=[20]*3)
    assert result['status'] == 'error'
    assert 'Unsupported stl-fit API signature' in result['error']


def test_stl_fit_matrix_validation(envelope, tmp_path):
    cmd = install_stl_fit_fake(tmp_path, [[-1, 0, 0], [0, 1, 0], [0, 0, 1]])
    result = oc.suggest_print_orientations(envelope, 'stl-fit', cmd, build_volume=[20]*3)
    assert result['status'] == 'error'
    assert 'mirroring' in result['error']


def test_binary_stl_header_starting_solid_is_binary(tmp_path):
    path = tmp_path / 'binary.stl'
    path.write_bytes(b'solid' + bytes(75) + struct.pack('<I12fH', 1, 0, 0, 1, 0, 0, 0, 30, 0, 0, 0, 5, 2, 0))
    assert oc._vertices(path) == [(0, 0, 0), (30, 0, 0), (0, 5, 2)]


@pytest.mark.parametrize('kwargs', [dict(build_volume=[20, 20, 19]), dict(samples=0), dict(seed=-1), dict(timeout=float('nan')), dict(max_candidates=100), dict(executable=['python', '-m', 'stl_fit'])])
def test_invalid_configuration(envelope, kwargs):
    with pytest.raises(ValueError):
        oc.suggest_print_orientations(envelope, 'stl-fit', **kwargs)


def test_invalid_mesh(envelope):
    envelope.write_text('solid malformed\nvertex nan 0 0\nendsolid malformed')
    with pytest.raises(ValueError):
        oc.suggest_print_orientations(envelope, 'tweaker3')


def test_cli_rounding_reconstructed_as_proper_rotation():
    matrix = oc._rotation([[.707107, -.707107, 0], [.707107, .707107, 0], [0, 0, 1]])
    assert sum(x*x for x in matrix[0]) == pytest.approx(1, abs=1e-14)
    assert sum(matrix[0][i]*matrix[1][i] for i in range(3)) == pytest.approx(0, abs=1e-14)


def test_timeout_kills_descendant_with_inherited_output(envelope, tmp_path):
    import os
    import time
    if os.name != 'posix':
        pytest.skip('process group cleanup is a POSIX guarantee')
    marker = tmp_path / 'descendant-survived'
    child = "import time;from pathlib import Path;time.sleep(.8);Path(" + repr(str(marker)) + ").write_text('survived')"
    cmd = cli(tmp_path, "import subprocess,sys\nsubprocess.Popen([sys.executable,'-c'," + repr(child) + "])\n")
    result = oc.suggest_print_orientations(envelope, 'tweaker3', cmd, timeout=.15)
    assert result['status'] == 'timeout'
    time.sleep(.9)
    assert not marker.exists()


def test_configured_interpreter_symlink_preserves_venv(envelope, tmp_path, monkeypatch):
    interpreter = tmp_path / 'venv-python'
    interpreter.symlink_to(sys.executable)
    def capture(argv, cwd, timeout):
        assert argv[0] == str(interpreter.absolute())
        return 'Rotation matrix:\n1 0 0\n0 1 0\n0 0 1\n'
    monkeypatch.setattr(oc, '_run', capture)
    result = oc.suggest_print_orientations(envelope, 'tweaker3', [str(interpreter)])
    assert result['status'] == 'success'


@pytest.mark.parametrize('backend, variable', [('tweaker3', 'FREECAD_TWEAKER_ARGV'), ('stl-fit', 'FREECAD_STL_FIT_ARGV')])
def test_actual_optional_orientation_providers(tmp_path, backend, variable):
    """Opt-in conformance: env variables contain JSON command argv arrays.

    Uses native binary STL, not a provider-generated mesh; no tool installs here.
    Run with configured isolated dependency environments to verify real APIs.
    """
    import json
    import os
    if not os.environ.get(variable):
        pytest.skip(f'{variable} not configured for optional-provider conformance')
    vertices = [(x, y, z) for x in (-15., 15.) for y in (-5., 5.) for z in (-2.5, 2.5)]
    triangles = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5),
                 (0, 4, 5), (0, 5, 1), (2, 3, 7), (2, 7, 6),
                 (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    data = bytes(80) + struct.pack('<I', len(triangles))
    for triangle in triangles:
        data += struct.pack('<12fH', 0, 0, 0, *(v for i in triangle for v in vertices[i]), 0)
    path = tmp_path / 'native_box.stl'
    path.write_bytes(data)
    argv = json.loads(os.environ[variable])
    for size, status in [(40, 'success'), (2, 'no_fit_found')]:
        result = oc.suggest_print_orientations(path, backend, argv, build_volume=[size]*3, samples=1000, timeout=120)
        assert result['status'] == status, result
        assert path.read_bytes() == data
        assert result['candidates']
        for candidate in result['candidates']:
            matrix = candidate['rotation_matrix']
            # Independent original vertices, column-vector convention and bounds.
            rotated = [[sum(matrix[i][j]*v[j] for j in range(3)) for i in range(3)] for v in vertices]
            extents = [max(v[i] for v in rotated)-min(v[i] for v in rotated) for i in range(3)]
            assert candidate['extents_mm'] == pytest.approx(extents)
            # A proper rigid rotation preserves the box's edge length.
            assert sum((rotated[4][i]-rotated[0][i])**2 for i in range(3)) == pytest.approx(900, abs=1e-9)
        assert sorted(p.name for p in tmp_path.iterdir()) == ['native_box.stl']


def test_windows_reader_rejected_before_process_launch(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError('must not launch an unsupported pipe reader')
    with monkeypatch.context() as scoped:
        scoped.setattr(oc.os, 'name', 'nt')
        scoped.setattr(oc.subprocess, 'Popen', unexpected)
        with pytest.raises(ValueError, match='POSIX'):
            oc._run(['unused'], tmp_path, 1)


@pytest.mark.parametrize('binary', [False, True])
def test_vertex_budget_checked_before_materializing_large_envelope(envelope, monkeypatch, binary):
    if binary:
        envelope.write_bytes(bytes(80) + struct.pack('<I12fH', 1, 0,0,1, 0,0,0, 30,0,0, 0,5,2, 0))
    monkeypatch.setattr(oc, 'MAX_VERTICES', 2)
    with pytest.raises(ValueError, match='STL exceeds 2 envelope vertices; export a coarser envelope'):
        oc._vertices(envelope)


def test_input_byte_budget_checked_by_bounded_read(envelope, monkeypatch):
    monkeypatch.setattr(oc, 'MAX_STL_BYTES', 10)
    with pytest.raises(ValueError, match='STL exceeds 10 bytes; export a coarser envelope'):
        oc._vertices(envelope)
