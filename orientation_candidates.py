"""Optional external orientation heuristics; never change or scale the source mesh.

Matrices use column vectors: p_print = rotation_matrix @ p_model. Apply the
same rotation to every part of a native multipart project, then arrange/slice.
Backend results are suggestions, not printability or slicer cost measurements.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import struct
import subprocess
import tempfile
import time

OUTPUT_LIMIT = 256 * 1024
MAX_STL_BYTES = 16 * 1024 * 1024
MAX_VERTICES = 300000
SOURCES = {
    "tweaker3": "https://github.com/ChristophSchranz/Tweaker-3",
    "tweaker3-python": "https://github.com/ChristophSchranz/Tweaker-3",
    "stl-fit": "https://github.com/khromov/stl-fit",
}

# Isolated provider worker: no optional/GPL dependency is imported by the bridge.
_TWEAKER_PY_HELPER = '''
import sys, json, contextlib
if sys.argv[1]: sys.path.insert(0, sys.argv[1])
import numpy as np
from FileHandler import FileHandler
from MeshTweaker import Tweak
np.random.seed(int(sys.argv[4]))
with contextlib.redirect_stdout(sys.stderr):
    objects = FileHandler().load_mesh(sys.argv[2])
    if len(objects) != 1: raise ValueError('Envelope must contain one flattened assembly mesh')
    content = next(iter(objects.values()))['mesh']
    tweak = Tweak(content, extended_mode=True, verbose=False, min_volume=True)
    rows = []
    for result in tweak.all_orientations:
        # Tweaker uses row vectors; bridge matrices use column vectors.
        matrix = np.asarray(tweak.euler(result[:3])[2]).T
        if any(np.allclose(matrix, r['rotation_matrix'], atol=1e-6, rtol=0) for r in rows): continue
        rows.append(dict(rotation_matrix=matrix.tolist(), heuristic_score=float(result[6])))
        if len(rows) >= int(sys.argv[3]): break
print('FREECAD_ORIENTATION_JSON=' + json.dumps(rows, allow_nan=False))
'''

# Uses upstream library signatures, intentionally bypassing stl_fit.py's
# iterative scale-down loop. Nothing from the upstream implementation is vendored.
_STL_FIT_HELPER = r'''
import contextlib, inspect, json, sys
if sys.argv[1]:
    sys.path.insert(0, sys.argv[1])
import numpy as np
import stl_fit_lib as lib
expected = {
    'load_and_prepare': ['stl_path'],
    'coarse_search': ['hull_verts', 'build_dims', 'num_samples', 'batch_size'],
    'refinement_phase': ['hull_verts', 'build_dims', 'top_k_results'],
    'select_diverse': ['results', 'n'],
    'refine': ['hull_verts', 'build_dims_sorted', 'initial_rotation'],
}
for name, parameters in expected.items():
    if list(inspect.signature(getattr(lib, name)).parameters) != parameters:
        raise RuntimeError('Unsupported stl-fit API signature: ' + name)
np.random.seed(int(sys.argv[4]))
dims = np.array([float(sys.argv[3])] * 3)
count = int(sys.argv[6])
with contextlib.redirect_stdout(sys.stderr):
    vertices, mesh = lib.load_and_prepare(sys.argv[2])
    # Bound each projection batch to approximately 64 MiB.
    batch = max(1, min(5000, (64 * 1024 * 1024) // (max(1, len(vertices)) * 3 * 8)))
    top, fits = lib.coarse_search(vertices, dims, int(sys.argv[5]), batch)
    if not top:
        raise RuntimeError('stl-fit returned no search results')
    best = lib.refinement_phase(vertices, dims, top)
    candidates = [best]
    for score, rotation, extents in lib.select_diverse(fits, n=max(0, count - 1)) if count > 1 else []:
        candidates.append(lib.refine(vertices, np.sort(dims), rotation))
result = [{'rotation_matrix': rotation.as_matrix().tolist(), 'heuristic_score': float(score)}
          for score, rotation, extents in candidates]
print('FREECAD_ORIENTATION_JSON=' + json.dumps(result, allow_nan=False))
'''


def _number(value, name, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")
    return float(value)


def _vertices(path):
    if path.suffix.lower() != ".stl":
        raise ValueError("Orientation envelope must be an STL file in millimeters")
    with path.open('rb') as stream:
        data = stream.read(MAX_STL_BYTES + 1)
    if len(data) > MAX_STL_BYTES:
        raise ValueError(f"STL exceeds {MAX_STL_BYTES} bytes; export a coarser envelope")
    vertices = []
    if len(data) >= 84 and 84 + 50 * struct.unpack_from('<I', data, 80)[0] == len(data):
        if 3 * struct.unpack_from('<I', data, 80)[0] > MAX_VERTICES:
            raise ValueError(f"STL exceeds {MAX_VERTICES} envelope vertices; export a coarser envelope")
        for offset in range(84, len(data), 50):
            values = struct.unpack_from('<9f', data, offset + 12)
            vertices.extend([values[i:i+3] for i in (0, 3, 6)])
    else:
        try:
            source = data.decode('ascii')
        except UnicodeDecodeError as exc:
            raise ValueError("Malformed binary or ASCII STL") from exc
        for line in source.splitlines():
            if line.strip().startswith('vertex'):
                fields = line.split()
                if len(fields) != 4 or fields[0] != 'vertex':
                    raise ValueError("Malformed STL vertex")
                if len(vertices) >= MAX_VERTICES:
                    raise ValueError(f"STL exceeds {MAX_VERTICES} envelope vertices; export a coarser envelope")
                try:
                    vertices.append(tuple(float(x) for x in fields[1:]))
                except ValueError as exc:
                    raise ValueError("Malformed STL vertex") from exc
        if not source.lstrip().startswith('solid') or not re.search(r'\bendsolid\b', source):
            raise ValueError("Malformed ASCII STL")
    if not vertices or len(vertices) % 3 or any(not math.isfinite(x) for v in vertices for x in v):
        raise ValueError("STL must contain finite triangle vertices")
    return vertices


def _rotation(matrix):
    if not isinstance(matrix, (list, tuple)) or len(matrix) != 3:
        raise ValueError("Backend matrix must be 3 by 3")
    rows = []
    for row in matrix:
        if not isinstance(row, (list, tuple)) or len(row) != 3:
            raise ValueError("Backend matrix must be 3 by 3")
        rows.append([_number(x, 'matrix element') for x in row])
    # Tweaker prints six decimals; accommodate only its rounding error.
    for i in range(3):
        for j in range(3):
            if abs(sum(rows[i][k] * rows[j][k] for k in range(3)) - (i == j)) > 1e-5:
                raise ValueError("Backend matrix is not orthonormal; scaling/shear prohibited")
    a, b, c = rows
    determinant = a[0]*(b[1]*c[2]-b[2]*c[1])-a[1]*(b[0]*c[2]-b[2]*c[0])+a[2]*(b[0]*c[1]-b[1]*c[0])
    if abs(determinant - 1) > 1e-5:
        raise ValueError("Backend matrix must be a proper rotation; mirroring prohibited")
    # Reconstruct a proper rotation after validating; printed six-decimal
    # matrices otherwise carry tiny scale/shear errors into assembly transforms.
    first_norm = math.sqrt(sum(x*x for x in rows[0]))
    first = [x / first_norm for x in rows[0]]
    projection = sum(first[i] * rows[1][i] for i in range(3))
    second = [rows[1][i] - projection * first[i] for i in range(3)]
    second_norm = math.sqrt(sum(x*x for x in second))
    second = [x / second_norm for x in second]
    third = [first[1]*second[2]-first[2]*second[1],
             first[2]*second[0]-first[0]*second[2],
             first[0]*second[1]-first[1]*second[0]]
    return [first, second, third]


def _kill(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def _run(argv, cwd, timeout):
    if os.name != 'posix':
        raise ValueError('Orientation subprocess backend currently requires POSIX process groups')
    process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               start_new_session=True, shell=False)
    output = bytearray()
    started = time.monotonic()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("Orientation backend timed out")
                for key, _ in selector.select(min(remaining, .1)):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(chunk)
                        if len(output) > OUTPUT_LIMIT:
                            raise ValueError("Orientation backend exceeded output limit")
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("Orientation backend timed out")
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError("Orientation backend timed out") from exc
        if process.returncode:
            raise RuntimeError(f"Orientation backend failed (exit {process.returncode}): " + output.decode(errors='replace')[-2000:])
        return output.decode(errors='replace')
    except BaseException:
        _kill(process)
        raise
    finally:
        process.stdout.close()


def suggest_print_orientations(stl_path, backend, executable=None,
                               build_volume=None, timeout=120, seed=42,
                               samples=5000, max_candidates=4):
    """Suggest rotations using an explicitly configured optional external tool.

    tweaker3: executable is a command or argv prefix (e.g. [python, Tweaker.py]).
    tweaker3-python: [python, /checkout/Tweaker.py] exposes multiple ranked poses;
    [python] uses installed upstream top-level FileHandler/MeshTweaker modules.
    stl-fit: executable is [python, /checkout/stl_fit.py], or [python] with the
    upstream stl_fit_lib installed. Only cubic envelopes are supported by this
    adapter; search failure is not proof that fitting is mathematically impossible.
    Returns status, candidates and limitations. Invalid caller inputs raise
    ValueError; unavailable/failed optional backends return structured status.
    """
    if backend not in SOURCES:
        raise ValueError("backend must be tweaker3, tweaker3-python or stl-fit")
    volume = list(build_volume if build_volume is not None else [256, 256, 256])
    if len(volume) != 3:
        raise ValueError("build_volume must contain three dimensions")
    volume = [_number(x, 'build volume dimension', True) for x in volume]
    timeout = _number(timeout, 'timeout', True)
    for value, name, limit in ((samples, 'samples', 1000000), (max_candidates, 'max_candidates', 32)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= limit:
            raise ValueError(f"{name} must be an integer between 1 and {limit}")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= 2**32-1:
        raise ValueError("seed must be an unsigned 32-bit integer")
    if backend == 'stl-fit' and max(volume) - min(volume) > 1e-9:
        raise ValueError("stl-fit adapter supports cubic build volumes only")
    path = Path(stl_path).expanduser().resolve(strict=True)
    original_vertices = _vertices(path)
    result = dict(status='unavailable', backend=backend, source=str(path),
                  provenance={'upstream': SOURCES[backend]}, candidates=[],
                  matrix_convention='column_vectors', source_modified=False,
                  limitations=['Heuristics do not measure print time or guarantee optimal orientation.',
                               'Bounds exclude supports, brim, prime tower and printer keep-out regions.',
                               'Apply one rotation to the entire native multipart project; preserve colors and parts.'])
    if executable is None:
        result['error'] = 'Configure an installed backend; no tools are downloaded or installed automatically.'
        return result
    argv = [executable] if isinstance(executable, str) else list(executable)
    if not argv or any(not isinstance(arg, str) or not arg or '\x00' in arg for arg in argv):
        raise ValueError("executable must be a nonempty command argv list")
    # Resolve relative configured script/executable paths before changing cwd.
    for i, arg in enumerate(argv):
        if Path(arg).is_file():
            # Preserve interpreter symlinks: resolving venv/bin/python would
            # select the base interpreter and lose its installed dependencies.
            argv[i] = os.path.abspath(arg)
    if backend in ('stl-fit', 'tweaker3-python'):
        if len(argv) not in (1, 2) or (len(argv) == 2 and not argv[1].endswith('.py')):
            raise ValueError(backend + " requires [python] or [python, path/to/provider.py]")
        module_dir = str(Path(argv[1]).parent) if len(argv) == 2 else ''
        if backend == 'stl-fit':
            result['limitations'].append('Cubic build volume only; upstream CLI auto-scaling is bypassed.')
        else:
            result['limitations'].append('samples is unused by Tweaker; the API uses its extended search with a seeded NumPy RNG.')
    else:
        result['limitations'].append('Tweaker returns one heuristic orientation; seed/samples are not supported by its CLI.')
    try:
        with tempfile.TemporaryDirectory(prefix='freecad-orientation-') as directory:
            copied = Path(directory) / 'envelope.stl'
            shutil.copyfile(path, copied)
            if backend == 'tweaker3':
                text = _run(argv + ['-i', str(copied), '-r', '-x'], directory, timeout)
                match = re.search(r'Rotation matrix:\s*\n\s*([^\n]+)\n\s*([^\n]+)\n\s*([^\n]+)', text)
                if not match:
                    raise ValueError("Tweaker output lacks a supported rotation matrix")
                native = [[float(x) for x in row.split()] for row in match.groups()]
                if any(len(row) != 3 for row in native):
                    raise ValueError("Tweaker output contains a malformed matrix")
                # Upstream FileHandler rotates row vectors with @ matrix.
                matrix = [list(row) for row in zip(*native)]
                score = re.search(r'Unprintability:\s*([^\s]+)', text)
                raw_candidates = [{'rotation_matrix': matrix}]
                if score:
                    raw_candidates[0]['heuristic_score'] = float(score.group(1))
            else:
                helper = Path(directory) / 'orientation_helper.py'
                helper.write_text(_STL_FIT_HELPER if backend == 'stl-fit' else _TWEAKER_PY_HELPER)
                args = [str(volume[0]), str(seed), str(samples), str(max_candidates)] if backend == 'stl-fit' else [str(max_candidates), str(seed)]
                text = _run([argv[0], str(helper), module_dir, str(copied), *args], directory, timeout)
                lines = [line for line in text.splitlines() if line.startswith('FREECAD_ORIENTATION_JSON=')]
                if len(lines) != 1:
                    raise ValueError(backend + " helper returned no unique candidate JSON")
                raw_candidates = json.loads(lines[0].split('=', 1)[1])
            if not isinstance(raw_candidates, list) or not raw_candidates or len(raw_candidates) > max_candidates:
                raise ValueError("Backend returned an invalid candidate count")
            for raw in raw_candidates:
                matrix = _rotation(raw['rotation_matrix'])
                if any(all(abs(matrix[i][j] - c['rotation_matrix'][i][j]) < 1e-8
                           for i in range(3) for j in range(3)) for c in result['candidates']):
                    continue
                lower, upper = [math.inf]*3, [-math.inf]*3
                for vertex in original_vertices:
                    for i in range(3):
                        value = sum(matrix[i][j] * vertex[j] for j in range(3))
                        lower[i] = min(lower[i], value)
                        upper[i] = max(upper[i], value)
                extents = [upper[i]-lower[i] for i in range(3)]
                candidate = dict(rotation_matrix=matrix, bounds_mm={'min': lower, 'max': upper},
                                 extents_mm=extents, fits_build_volume=all(extents[i] <= volume[i] + 1e-6 for i in range(3)),
                                 translation_to_origin_mm=[-x for x in lower])
                if 'heuristic_score' in raw:
                    candidate['heuristic_score'] = _number(raw['heuristic_score'], 'heuristic score')
                result['candidates'].append(candidate)
            result['status'] = 'success' if any(c['fits_build_volume'] for c in result['candidates']) else 'no_fit_found'
            return result
    except FileNotFoundError as exc:
        result['error'] = str(exc)
    except TimeoutError as exc:
        result.update(status='timeout', error=str(exc), candidates=[])
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as exc:
        result.update(status='error', error=str(exc), candidates=[])
    return result
