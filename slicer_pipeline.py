"""Isolated, non-printing comparisons using a native Bambu Studio project."""
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time
import xml.etree.ElementTree as ET
import zipfile

MAX_ARCHIVE_BYTES = 1024*1024*1024
MAX_ASSEMBLY_OBJECT_VISITS = 10000
MAX_ASSEMBLY_VERTEX_VISITS = 6000000

CORE = 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
PROD = 'http://schemas.microsoft.com/3dmanufacturing/production/2015/06'
ALLOWED_SETTINGS = {
    'layer_height': (0.05, 0.6), 'wall_loops': (1, 20),
    'sparse_infill_density': (0, 100), 'enable_support': None,
    'support_type': ('normal(auto)', 'tree(auto)', 'normal(manual)', 'tree(manual)'),
    'support_threshold_angle': (0, 90), 'support_base_pattern_spacing': (0.1, 20),
    'top_shell_layers': (0, 30), 'top_shell_thickness': (0, 10),
    'top_color_penetration_layers': (0, 30), 'support_on_build_plate_only': None,
}
_INTEGER = {'wall_loops', 'top_shell_layers', 'top_color_penetration_layers'}


def _numeric(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def _validate(candidates):
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 12:
        raise ValueError('candidates must contain 1..12 entries')
    names = {'baseline'}
    result = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) - {'name', 'settings', 'rotation', 'rotation_matrix'}:
            raise ValueError('Unsupported candidate fields')
        name = candidate.get('name')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name) or name in names:
            raise ValueError('Candidate names must be unique safe names, excluding baseline')
        names.add(name)
        settings = candidate.get('settings', {})
        if not isinstance(settings, dict):
            raise ValueError('settings must be a dictionary')
        for key, value in settings.items():
            if key not in ALLOWED_SETTINGS:
                raise ValueError('Unsupported print setting: ' + key)
            constraint = ALLOWED_SETTINGS[key]
            if constraint is None:
                valid = isinstance(value, bool) or isinstance(value, int) and value in (0, 1) or value in ('0', '1')
            elif key == 'support_type':
                valid = value in constraint
            else:
                valid = _numeric(value) and constraint[0] <= value <= constraint[1]
                if key in _INTEGER:
                    valid = valid and int(value) == value
            if not valid:
                raise ValueError('Invalid value for ' + key)
        if 'rotation' in candidate and 'rotation_matrix' in candidate:
            raise ValueError('Specify only one rotation representation')
        matrix = None
        if 'rotation' in candidate:
            angles = candidate['rotation']
            if not isinstance(angles, (list, tuple)) or len(angles) != 3 or not all(_numeric(v) for v in angles):
                raise ValueError('rotation requires three finite angles in degrees')
            x, y, z = (math.radians(v) for v in angles)
            sx, cx, sy, cy, sz, cz = math.sin(x), math.cos(x), math.sin(y), math.cos(y), math.sin(z), math.cos(z)
            matrix = [[cz*cy, cz*sy*sx-sz*cx, cz*sy*cx+sz*sx], [sz*cy, sz*sy*sx+cz*cx, sz*sy*cx-cz*sx], [-sy, cy*sx, cy*cx]]
        elif 'rotation_matrix' in candidate:
            matrix = candidate['rotation_matrix']
            if not isinstance(matrix, (list, tuple)) or len(matrix) != 3 or any(not isinstance(row, (list, tuple)) or len(row) != 3 or not all(_numeric(v) for v in row) for row in matrix):
                raise ValueError('rotation_matrix requires a finite 3x3 matrix')
        if matrix is not None:
            for i in range(3):
                for j in range(3):
                    if abs(sum(matrix[i][k]*matrix[j][k] for k in range(3)) - (i == j)) > 1e-6:
                        raise ValueError('rotation_matrix must be orthonormal')
            det = sum(matrix[0][i]*(matrix[1][(i+1)%3]*matrix[2][(i+2)%3]-matrix[1][(i+2)%3]*matrix[2][(i+1)%3]) for i in range(3))
            if abs(det - 1) > 1e-6:
                raise ValueError('rotation_matrix must not mirror or scale')
        result.append({'name': name, 'settings': dict(settings), 'rotation_matrix': matrix})
    return result


def _identity():
    return [[1.,0.,0.,0.], [0.,1.,0.,0.], [0.,0.,1.,0.], [0.,0.,0.,1.]]


def _transform(text):
    if text is None:
        return _identity()
    values = [float(v) for v in text.split()]
    if len(values) != 12 or not all(math.isfinite(v) for v in values):
        raise ValueError('Invalid model placement')
    return [[values[c*3+r] for c in range(4)] for r in range(3)] + [[0.,0.,0.,1.]]


def _multiply(a, b):
    return [[sum(a[r][k]*b[k][c] for k in range(4)) for c in range(4)] for r in range(4)]


def _point(matrix, xyz):
    return [sum(matrix[r][c]*xyz[c] for c in range(3))+matrix[r][3] for r in range(3)]


def _rotate_project(entries, matrix):
    # Keep component and mesh bytes intact; transform the one build item as an assembly.
    trees = {}
    def tree(path):
        if path not in trees:
            trees[path] = ET.fromstring(entries[path])
        return trees[path]
    root = tree('3D/3dmodel.model')
    if root.get('unit', 'millimeter') != 'millimeter':
        raise ValueError('Orientation requires millimeter units')
    items = root.findall('{%s}build/{%s}item' % (CORE, CORE))
    if len(items) != 1:
        raise ValueError('Orientation requires one whole assembly build item')
    bounds = [[math.inf]*3, [-math.inf]*3]
    object_visits = vertex_visits = 0
    def visit(path, oid, placement, ancestors):
        nonlocal object_visits, vertex_visits
        object_visits += 1
        if object_visits > MAX_ASSEMBLY_OBJECT_VISITS:
            raise ValueError(f'Assembly exceeds {MAX_ASSEMBLY_OBJECT_VISITS} object visits')
        key = (path, oid)
        if key in ancestors or len(ancestors) > 64:
            raise ValueError('Cyclic or excessively nested model components')
        obj = tree(path).find("{%s}resources/{%s}object[@id='%s']" % (CORE, CORE, oid))
        if obj is None:
            raise ValueError('Missing component object')
        for vertex in obj.iterfind('{%s}mesh/{%s}vertices/{%s}vertex' % (CORE, CORE, CORE)):
            vertex_visits += 1
            if vertex_visits > MAX_ASSEMBLY_VERTEX_VISITS:
                raise ValueError(f'Assembly exceeds {MAX_ASSEMBLY_VERTEX_VISITS} vertex visits')
            p = _point(placement, [float(vertex.get(k)) for k in ('x','y','z')])
            if not all(math.isfinite(v) for v in p):
                raise ValueError('Nonfinite mesh vertex')
            for i in range(3):
                bounds[0][i] = min(bounds[0][i], p[i]); bounds[1][i] = max(bounds[1][i], p[i])
        for comp in obj.iterfind('{%s}components/{%s}component' % (CORE, CORE)):
            child = comp.get('{%s}path' % PROD, path).lstrip('/')
            visit(child, comp.get('objectid'), _multiply(placement, _transform(comp.get('transform'))), ancestors | {key})
    item = items[0]
    original = _transform(item.get('transform'))
    visit('3D/3dmodel.model', item.get('objectid'), original, set())
    if not all(math.isfinite(v) for row in bounds for v in row):
        raise ValueError('Assembly has no vertices')
    center = [(a+b)/2 for a,b in zip(*bounds)]
    rotation = [list(row)+[0.] for row in matrix]+[[0.,0.,0.,1.]]
    for i in range(3):
        rotation[i][3] = center[i]-sum(matrix[i][j]*center[j] for j in range(3))
    # Measure exact rotated vertices, rather than bounding-box corners.
    transformed = _multiply(rotation, original)
    bounds = [[math.inf]*3, [-math.inf]*3]
    visit('3D/3dmodel.model', item.get('objectid'), transformed, set())
    transformed[2][3] -= bounds[0][2]
    item.set('transform', ' '.join(format(transformed[r][c], '.17g') for c in range(4) for r in range(3)))
    # Preserve original namespace prefixes (requiredextensions refers to p).
    ET.register_namespace('', CORE); ET.register_namespace('p', PROD)
    ET.register_namespace('BambuStudio', 'http://schemas.bambulab.com/package/2021')
    entries['3D/3dmodel.model'] = ET.tostring(root, encoding='utf-8', xml_declaration=True)


def _read_project(path):
    """Read only native millimeter projects containing one assembly build item."""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len({i.filename for i in infos}) != len(infos) or sum(i.file_size for i in infos) > MAX_ARCHIVE_BYTES:
                raise ValueError('Duplicate or oversized 3MF entries')
            entries = {i.filename: archive.read(i) for i in infos}
        for key in ('Metadata/project_settings.config', 'Metadata/model_settings.config', '3D/3dmodel.model'):
            if key not in entries:
                raise ValueError('Input must be a native Bambu project with filament/part settings')
        settings = json.loads(entries['Metadata/project_settings.config'])
        if not isinstance(settings, dict):
            raise ValueError('Invalid project settings')
        ET.fromstring(entries['Metadata/model_settings.config'])
        # Validate every referenced object and placement even for an unchanged baseline.
        try:
            _rotate_project(dict(entries), [[1,0,0],[0,1,0],[0,0,1]])
        except (KeyError, TypeError, ET.ParseError, OverflowError) as exc:
            raise ValueError('Invalid native assembly: ' + str(exc)) from exc
        return entries, settings
    except (zipfile.BadZipFile, ET.ParseError) as exc:
        raise ValueError("Invalid native Bambu project: " + str(exc)) from exc


def _positive(value, field, strictly_positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('Invalid ' + field)
    value = float(value)
    if not math.isfinite(value) or value < 0 or strictly_positive and value == 0:
        raise ValueError('Invalid ' + field)
    return value


def _parse_result(path, directory):
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len({i.filename for i in infos}) != len(infos) or sum(i.file_size for i in infos) > MAX_ARCHIVE_BYTES:
            raise ValueError('Duplicate or oversized sliced 3MF entries')
        if archive.getinfo('Metadata/slice_info.config').file_size > 1024*1024:
            raise ValueError('Oversized slicing metadata')
        root = ET.fromstring(archive.read('Metadata/slice_info.config'))
        version = next((v.get('value') for v in root.findall('header/header_item') if v.get('key') == 'X-BBL-Client-Version'), None)
        plates = root.findall('plate')
        if len(plates) != 1:
            raise ValueError('Expected exactly one sliced plate')
        metadata = {m.get('key'):m.get('value') for m in plates[0].findall('metadata')}
        outside = metadata.get('outside')
        if outside not in ('true','false'):
            raise ValueError('Missing or invalid plate-boundary status')
        result = {'seconds': _positive(float(metadata['prediction']), 'prediction', True), 'total_grams': _positive(float(metadata['weight']), 'weight', True), 'outside': outside == 'true',
                  'filament_changes': None, 'layers': None, 'model_grams': None, 'support_grams': None, 'purge_grams': None, 'tower_grams': None, 'feature_seconds': None, 'slicer_version': version}
        gcode = next((n for n in archive.namelist() if re.fullmatch(r'Metadata/plate_\d+\.gcode', n)), None)
        if not gcode or archive.getinfo(gcode).file_size == 0:
            raise ValueError('Missing or empty sliced G-code artifact')
        with archive.open(gcode) as stream:
            header = stream.read(65536).decode('utf-8', errors='replace')
        for field, pattern in [('layers', r';\s*total layer number\s*:\s*(\d+)'), ('filament_changes', r';\s*total filament change(?: times)?\s*[:=]\s*(\d+)')]:
            match = re.search(pattern, header, re.I)
            if match:
                result[field] = int(match.group(1))
    raw_path = directory / 'result.json'
    if raw_path.exists():
        if raw_path.stat().st_size > 16*1024*1024:
            raise ValueError('Oversized slicer result document')
        raw = json.loads(raw_path.read_text())
        if not isinstance(raw, dict):
            raise ValueError('Invalid slicer result document')
        if isinstance(raw.get('return_code'), bool) or raw.get('return_code') != 0:
            raise ValueError('Slicer result reports failure')
        sliced = raw.get('sliced_plates')
        if not isinstance(sliced, list) or len(sliced) != 1:
            raise ValueError('Invalid sliced_plates')
        plate = sliced[0]
        if not isinstance(plate, dict):
            raise ValueError('Invalid sliced plate')
        if 'filament_change_times' in plate:
            value = _positive(plate['filament_change_times'], 'filament changes')
            if int(value) != value:
                raise ValueError('Invalid filament changes')
            result['filament_changes'] = int(value)
        features = plate.get('feature_type_times')
        if features is not None:
            if not isinstance(features, dict):
                raise ValueError('Invalid feature time breakdown')
            result['feature_seconds'] = {k:_positive(v, 'feature time') for k,v in features.items()}
        result['warning_message'] = plate.get('warning_message', '')
    # main_used_g includes support; do not falsely label it model-only mass.
    return result


MAX_LOG_BYTES = 4*1024*1024


def _run_slicer(argv, directory, timeout):
    """POSIX-only bounded pipe capture, including a deadline after parent exit."""
    started = time.monotonic()
    process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    head, tail, total, error = b'', b'', 0, None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        with (directory/'slicer.log').open('wb') as log:
            while selector.get_map() or process.poll() is None:
                remaining = timeout-(time.monotonic()-started)
                if remaining <= 0:
                    error = 'timeout'; break
                events = selector.select(min(remaining, 0.1))
                for key, _ in events:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj); continue
                    head = (head+chunk)[:16384]
                    tail = (tail+chunk)[-16384:]
                    allowed = max(0, MAX_LOG_BYTES-total)
                    log.write(chunk[:allowed])
                    total += len(chunk)
                    if total > MAX_LOG_BYTES:
                        error = 'log_limit'; break
                if error:
                    break
    finally:
        if error or process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait()
        selector.close()
        process.stdout.close()
    captured = head if total <= 16384 else head+b'\n[log truncated; tail follows]\n'+tail
    return process.returncode, error, captured.decode('utf-8', errors='replace'), time.monotonic()-started


def compare_print_candidates(project_path, output_dir, candidates, slicer_path=None, timeout=300):
    """Compare baseline plus candidates. No scale, printing, or arbitrary CLI arguments.

    Input is a native Bambu 3MF with one assembly build item in millimeters,
    even for settings-only comparisons. Source bytes are never modified. Metrics
    are slicer estimates, not physical validation. Missing mass breakdowns are null.
    """
    if os.name != 'posix':
        raise ValueError('Slicer subprocess backend currently requires POSIX process groups')
    validated = _validate(candidates)
    if not _numeric(timeout) or not 0 < timeout <= 3600:
        raise ValueError('timeout must be finite and within (0,3600] seconds')
    source = Path(project_path).expanduser().resolve(strict=True)
    # Parse and hash the same immutable snapshot, even if the caller saves a
    # newer project while this comparison is being prepared.
    with source.open('rb') as handle:
        source_bytes = handle.read(MAX_ARCHIVE_BYTES + 1)
    if len(source_bytes) > MAX_ARCHIVE_BYTES:
        raise ValueError('Oversized input 3MF file')
    digest = hashlib.sha256(source_bytes).hexdigest()
    entries, settings = _read_project(io.BytesIO(source_bytes))
    executable = Path(slicer_path or '/Applications/BambuStudio.app/Contents/MacOS/BambuStudio').expanduser().resolve(strict=True)
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError('Slicer must be an executable file')
    prepared = []
    for candidate in [{'name':'baseline','settings':{},'rotation_matrix':None}] + validated:
        copied = dict(entries)
        if candidate['rotation_matrix'] is not None:
            _rotate_project(copied, candidate['rotation_matrix'])
        prepared.append((candidate, copied))
    destination = Path(output_dir).expanduser().resolve()
    # mkdir is deliberately exclusive; never reuse stale results or overwrite files.
    destination.mkdir(parents=True, exist_ok=False)
    records = []
    for candidate, copied in prepared:
        directory = destination / candidate['name']; directory.mkdir()
        if candidate['settings']:
            modified = dict(settings)
            for key,value in candidate['settings'].items():
                modified[key] = str(int(value)) if isinstance(value, bool) else str(value)
                if key == 'sparse_infill_density':
                    modified[key] += '%'
            copied['Metadata/project_settings.config'] = json.dumps(modified).encode()
        record = dict(candidate, status='failed', directory=str(directory), started_at=datetime.now(timezone.utc).isoformat())
        records.append(record)
        try:
            input_path = directory / 'input.3mf'
            with zipfile.ZipFile(input_path, 'w', zipfile.ZIP_DEFLATED) as archive:
                for name, data in copied.items():
                    archive.writestr(name, data)
            output_path = directory / 'sliced.3mf'
            argv = [str(executable), '--debug', '2', '--slice', '1', '--export-3mf', output_path.name, '--outputdir', str(directory), str(input_path)]
            record['input_sha256'] = hashlib.sha256(input_path.read_bytes()).hexdigest()
            record['argv'] = argv; record['input_path'] = str(input_path); record['output_path'] = str(output_path)
            code, failure, log, elapsed = _run_slicer(argv, directory, timeout)
            record.update(return_code=code, log=log, elapsed_seconds=elapsed)
            if failure:
                record['error_type'] = failure
                continue
            if code != 0:
                record['error_type'] = 'exit'; continue
            if not output_path.is_file():
                record['error_type'] = 'missing_output'; continue
            metrics = _parse_result(output_path, directory)
            record['metrics'] = metrics
            record['status'] = 'outside' if metrics['outside'] else 'success'
            if metrics['outside']:
                record['error_type'] = 'outside_plate'
        except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile, ET.ParseError, OverflowError) as exc:
            record['error_type'] = 'malformed_output' if 'output_path' in record else 'invalid_project'
            record['error'] = str(exc)
    successful = [r for r in records if r['status'] == 'success']
    best = min(successful, key=lambda r:r['metrics']['seconds']) if successful else None
    report = {'source_path': str(source), 'source_sha256': digest, 'slicer_path': str(executable), 'candidates': records,
              'best_candidate': best['name'] if best else None, 'estimate_only': True,
              'created_at': datetime.now(timezone.utc).isoformat(), 'timeout_seconds': timeout,
              'source_unchanged': hashlib.sha256(source.read_bytes()).hexdigest() == digest}
    (destination/'comparison.json').write_text(json.dumps(report, indent=2))
    return report
