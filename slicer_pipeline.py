"""Isolated, non-printing comparisons using a native Bambu Studio project."""
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET
import zipfile

MAX_ARCHIVE_BYTES = 1024*1024*1024
MAX_ASSEMBLY_OBJECT_VISITS = 10000
MAX_ASSEMBLY_VERTEX_VISITS = 6000000

CORE = 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
PROD = 'http://schemas.microsoft.com/3dmanufacturing/production/2015/06'
from print_pipeline_core.candidates import ALLOWED_SETTINGS, _validate, _numeric
from print_pipeline_core import assembly as _assembly
from print_pipeline_core.artifacts import read_artifacts
from print_pipeline_core.process import run_bounded

_identity = _assembly._identity
_transform = _assembly._transform
_multiply = _assembly._multiply
_point = _assembly._point


def _rotate_project(entries, matrix):
    return _assembly._rotate_project(entries, matrix, MAX_ASSEMBLY_OBJECT_VISITS, MAX_ASSEMBLY_VERTEX_VISITS)


def _read_project(path):
    return _assembly._read_project(path, MAX_ARCHIVE_BYTES, MAX_ASSEMBLY_OBJECT_VISITS, MAX_ASSEMBLY_VERTEX_VISITS)


def _positive(value, field, strictly_positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('Invalid ' + field)
    value = float(value)
    if not math.isfinite(value) or value < 0 or strictly_positive and value == 0:
        raise ValueError('Invalid ' + field)
    return value


def _parse_result(path, directory):
    return read_artifacts(path, directory, MAX_ARCHIVE_BYTES)


MAX_LOG_BYTES = 4*1024*1024


def _run_slicer(argv, directory, timeout):
    return run_bounded(argv, directory, timeout, MAX_LOG_BYTES)


def compare_print_candidates(project_path, output_dir, candidates, slicer_path=None, timeout=300,
                             backend='legacy', print_spec=None, workers=2, backend_python=None):
    """Compare baseline plus candidates. No scale, printing, or arbitrary CLI arguments.

    Input is a native Bambu 3MF with one assembly build item in millimeters,
    even for settings-only comparisons. Source bytes are never modified. Metrics
    are slicer estimates, not physical validation. Missing mass breakdowns are null.
    """
    if backend not in ('legacy', 'bambu'):
        raise ValueError('backend must be legacy or bambu')
    if backend == 'bambu':
        return _compare_owned(project_path, output_dir, candidates, slicer_path, timeout,
                              print_spec, workers, backend_python)
    from print_pipeline_core.spec import resolve_spec, violations, metric_violations
    from print_pipeline_core.report import summarize
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
    spec = resolve_spec(print_spec, settings) if print_spec is not None else None
    if spec and (spec['machine_preset'] or spec['process_preset'] or spec['filament_presets']):
        raise ValueError('Preset selection requires backend=bambu')
    per_object = ET.fromstring(entries['Metadata/model_settings.config'])
    object_settings = {o.get('id'): {m.get('key'): m.get('value') for m in o.findall('metadata')}
                       for o in per_object.findall('object')}
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
        normalized = {k: str(int(v)) if isinstance(v, bool) else v for k, v in candidate['settings'].items()}
        effective = {**settings, **normalized}
        record['effective_settings'] = effective
        record['constraint_violations'] = []
        if spec:
            bad = violations(effective, settings, spec, candidate['rotation_matrix'] is not None)
            for oid, stored in object_settings.items():
                bad += [f'object {oid}: {x}' for x in violations({**effective, **stored}, {**settings, **stored}, spec)]
            record['constraint_violations'] = bad
            if bad:
                record.update(status='rejected', error_type='constraints')
                continue
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
    if spec:
        baseline = records[0]
        for row in records[1:]:
            if row['status'] == 'success':
                if baseline['status'] != 'success':
                    row.update(status='rejected', error_type='baseline_unavailable')
                else:
                    row['constraint_violations'] += metric_violations(row['metrics'], baseline['metrics'], spec)
                    if row['constraint_violations']:
                        row.update(status='rejected', error_type='constraints')
    successful = [r for r in records if r['status'] == 'success']
    best = min(successful, key=lambda r:r['metrics']['seconds']) if successful else None
    report = {'source_path': str(source), 'source_sha256': digest, 'slicer_path': str(executable), 'candidates': records,
              'best_candidate': best['name'] if best else None, 'estimate_only': True,
              'created_at': datetime.now(timezone.utc).isoformat(), 'timeout_seconds': timeout,
              'source_unchanged': hashlib.sha256(source.read_bytes()).hexdigest() == digest}
    report.update(schema_version=1, backend='legacy', print_spec=spec, baseline_settings=settings,
                  **summarize(records, settings))
    (destination/'comparison.json').write_text(json.dumps(report, indent=2))
    return report


def _compare_owned(project_path, output_dir, candidates, slicer_path, timeout, print_spec, workers, backend_python):
    """Explicit JSON process boundary: no Bambu dependencies in FreeCAD's Python runtime."""
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 12:
        raise ValueError('candidates must contain 1..12 entries')
    if not _numeric(timeout) or not 1 <= timeout <= 3600:
        raise ValueError('Bambu backend timeout must be finite and within 1..3600 seconds')
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 8:
        raise ValueError('workers must be in 1..8')
    # Preserve the venv interpreter symlink: resolving it would select the base Python environment.
    python = Path(backend_python or Path(__file__).resolve().parent / 'tools/bambu-mcp/.venv/bin/python').expanduser().absolute()
    if not python.is_file():
        raise ValueError('Install tools/bambu-mcp or supply backend_python')
    request = dict(project_path=str(Path(project_path).expanduser().resolve()),
                   output_dir=str(Path(output_dir).expanduser().resolve()), candidates=candidates,
                   slicer_path=str(Path(slicer_path).expanduser().resolve()) if slicer_path else None, timeout=timeout,
                   print_spec=print_spec, workers=workers)
    # Includes baseline plus bounded waves, worker startup, preset resolution and artifact validation.
    limit = timeout * (1 + math.ceil(len(candidates) / workers)) + 60
    with tempfile.TemporaryDirectory(prefix='freecad-bambu-') as td:
        directory = Path(td)
        req, response = directory/'request.json', directory/'response.json'
        req.write_text(json.dumps(request, allow_nan=False))
        code, failure, log, _ = run_bounded([str(python), '-m', 'bambu_mcp.pipeline',
                                                  '--request', str(req), '--response', str(response)], directory, limit)
        if failure or code or not response.is_file():
            raise RuntimeError(f'Bambu backend {failure or "failed"}: {log[-2000:]}')
        if response.stat().st_size > 16*1024*1024:
            raise ValueError('Oversized Bambu backend report')
        result = json.loads(response.read_text())
        if not result['ok']:
            raise ValueError(result['error'])
        return result['report']
