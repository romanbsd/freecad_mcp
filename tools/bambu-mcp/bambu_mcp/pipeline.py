"""Owned native-project comparison backend; JSON boundary with the FreeCAD bridge."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import signal
import zipfile
import tempfile
import xml.etree.ElementTree as ET

from .catalog import validate
from .runner import Runner, Variant
from print_pipeline_core.assembly import _read_project, _rotate_project
from print_pipeline_core.artifacts import read_artifacts
from print_pipeline_core.candidates import _validate, ALLOWED_SETTINGS
from print_pipeline_core.spec import resolve_spec, violations, metric_violations, merge_presets
from print_pipeline_core.report import summarize

MAX_BYTES = 1024**3


def candidate_specs(candidates):
    """Share rigid-transform validation and support both maintained setting catalogs."""
    rows = []
    for c in candidates:
        if not isinstance(c, dict) or set(c) - {'name', 'settings', 'rotation', 'rotation_matrix'}:
            raise ValueError('Invalid comparison candidate fields')
        settings = c.get('settings', {})
        if not isinstance(settings, dict):
            raise ValueError('settings must be a dictionary')
        legacy = {k: v for k, v in settings.items() if k in ALLOWED_SETTINGS}
        newer = {k: validate(k, v) for k, v in settings.items() if k not in ALLOWED_SETTINGS}
        row = _validate([{**c, 'settings': legacy}])[0]
        row['settings'].update(newer)
        row['settings'] = {k: str(int(v)) if isinstance(v, bool) else v for k, v in row['settings'].items()}
        rows.append(row)
    if not 1 <= len(rows) <= 12 or len({x['name'] for x in rows}) != len(rows):
        raise ValueError('Need 1..12 uniquely named candidates')
    return rows


def _write_package(path, entries):
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)


def compare_project(project_path, output_dir, candidates, slicer_path=None, timeout=300,
                    print_spec=None, workers=2, cache_home=None):
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 8:
        raise ValueError('workers must be in 1..8')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 3600:
        raise ValueError('timeout must be within 1..3600 seconds')
    cands = candidate_specs(candidates)
    source = Path(project_path).expanduser().resolve(strict=True)
    with source.open('rb') as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Oversized input 3MF file')
    digest = hashlib.sha256(raw).hexdigest()
    entries, stored = _read_project(io.BytesIO(raw))
    r = Runner(binary=slicer_path, **({'home': Path(cache_home)} if cache_home else {}))
    requested = print_spec if print_spec is not None else {}
    if not isinstance(requested, dict):
        raise ValueError('print_spec must be a dictionary')
    # Load every requested preset before publishing any output directory.
    selected = [('machine', requested.get('machine_preset', '')), ('process', requested.get('process_preset', ''))]
    selected += [('filament', f) for f in requested.get('filament_presets', [])]
    loaded = [(kind, r._load_preset(kind, name)) for kind, name in selected if name]
    effective = merge_presets(stored, loaded)
    # No spec retains compatibility while still using strict artifacts and Pareto reports.
    spec = resolve_spec(requested, effective) if print_spec is not None else None
    object_xml = ET.fromstring(entries['Metadata/model_settings.config'])
    per = {o.get('id'): {m.get('key'): m.get('value') for m in o.findall('metadata') if m.get('key')}
           for o in object_xml.findall('object')}
    prepared = []
    for candidate in [{'name': 'baseline', 'settings': {}, 'rotation_matrix': None}] + cands:
        copied = dict(entries)
        settings = dict(effective)
        settings.update(candidate['settings'])
        for k in ('sparse_infill_density',):
            if k in candidate['settings']:
                settings[k] = str(candidate['settings'][k]).rstrip('%') + '%'
        bad = violations(settings, effective, spec, candidate['rotation_matrix'] is not None) if spec else []
        if spec:
            for oid, obj in per.items():
                bad += [f'object {oid}: {x}' for x in violations({**settings, **obj}, {**effective, **obj}, spec)]
        if candidate['rotation_matrix'] is not None:
            _rotate_project(copied, candidate['rotation_matrix'])
        # CLI handles preset loading; project patch preserves assembly and part colors.
        if candidate['settings']:
            patched = {**stored, **candidate['settings']}
            if 'sparse_infill_density' in candidate['settings']:
                patched['sparse_infill_density'] = settings['sparse_infill_density']
            copied['Metadata/project_settings.config'] = json.dumps(patched).encode()
        prepared.append((candidate, copied, settings, bad))
    destination = Path(output_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=False)
    # Immutable, content-addressed preset snapshots prevent mid-run edits and allow cache reuse.
    preset_paths = {'machine': None, 'process': None, 'filament': []}
    for kind, data in loaded:
        encoded = json.dumps(data, sort_keys=True).encode()
        snapshots = r.home / 'preset_snapshots'; snapshots.mkdir(exist_ok=True)
        p = snapshots / (hashlib.sha256(encoded).hexdigest() + '.json')
        if not p.exists() or p.read_bytes() != encoded:
            with tempfile.NamedTemporaryFile(dir=snapshots, delete=False) as handle:
                handle.write(encoded)
                temporary = Path(handle.name)
            temporary.replace(p)
        if kind == 'filament':
            preset_paths[kind].append(str(p))
        else:
            preset_paths[kind] = str(p)
    with ThreadPoolExecutor(max_workers=1) as warmup:
        version = warmup.submit(r.version).result()

    def evaluate(prepared_row):
        c, copied, settings, bad = prepared_row
        directory = destination / c['name']; directory.mkdir()
        path = directory / 'input.3mf'
        if c['name'] == 'baseline':
            path.write_bytes(raw)
        else:
            _write_package(path, copied)
        row = dict(c, effective_settings=settings, constraint_violations=bad, status='rejected' if bad else 'failed',
                   input_path=str(path), directory=str(directory), started_at=datetime.now(timezone.utc).isoformat())
        if bad:
            row['error_type'] = 'constraints'
            return row
        # Global CLI overrides are required to win over chosen process presets.
        overrides = {k: str(v) for k, v in c['settings'].items()}
        if 'sparse_infill_density' in overrides:
            overrides['sparse_infill_density'] = settings['sparse_infill_density']
        m = r.evaluate(path, Variant(overrides=overrides, flags={'orient': 0}, plate=1),
                       machine=preset_paths['machine'], process=preset_paths['process'],
                       filaments=preset_paths['filament'], timeout_s=int(timeout))
        row['run_id'] = m.run_id
        row['input_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        if not m.ok:
            row['error'] = m.error
            row['error_type'] = 'outside_plate' if m.outside else 'slice_failed'
            if m.outside:
                row['status'] = 'outside'
            return row
        try:
            cache = Path(m.out_3mf).parent
            row['metrics'] = read_artifacts(m.out_3mf, cache)
            shutil.copy(m.out_3mf, directory / 'sliced.3mf')
            row['output_path'] = str(directory / 'sliced.3mf')
            for filename in ('result.json', 'cmd.json', 'slicer.log'):
                if (cache / filename).is_file():
                    shutil.copy(cache / filename, directory / filename)
            row['status'] = 'outside' if row['metrics']['outside'] else 'success'
            if row['metrics']['outside']:
                row['error_type'] = 'outside_plate'
        except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile, ET.ParseError) as exc:
            row['error_type'], row['error'] = 'malformed_output', str(exc)
        return row

    with ThreadPoolExecutor(max_workers=workers) as pool:
        baseline = pool.submit(evaluate, prepared[0]).result()
        records = [baseline] + list(pool.map(evaluate, prepared[1:]))
    if spec:
        base_metrics = baseline.get('metrics') if baseline['status'] == 'success' else None
        for row in records[1:]:
            if row['status'] == 'success':
                row['constraint_violations'] += metric_violations(row['metrics'], base_metrics, spec)
                if row['constraint_violations']:
                    row['status'], row['error_type'] = 'rejected', 'constraints'
        # A failed/rejected baseline leaves relative budgets unverified: do not rank candidates.
        if baseline['status'] != 'success':
            for row in records[1:]:
                if row['status'] == 'success':
                    row['status'], row['error_type'] = 'rejected', 'baseline_unavailable'
    choices = summarize(records, effective)
    report = dict(schema_version=1, backend='bambu', source_path=str(source), source_sha256=digest,
                  slicer_path=r.binary, slicer_version=version, print_spec=spec,
                  baseline_settings=effective, candidates=records, best_candidate=choices['winners']['fastest'],
                  estimate_only=True, source_unchanged=hashlib.sha256(source.read_bytes()).hexdigest() == digest,
                  created_at=datetime.now(timezone.utc).isoformat(), **choices)
    (destination / 'comparison.json').write_text(json.dumps(report, indent=2, allow_nan=False))
    return report


def install_cancellation():
    from print_pipeline_core.process import terminate_active_process_groups
    def stop(signum, frame):
        terminate_active_process_groups()
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)


def main():
    install_cancellation()
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True)
    parser.add_argument('--response', required=True)
    args = parser.parse_args()
    try:
        p = Path(args.request)
        if p.stat().st_size > 1024*1024:
            raise ValueError('Oversized comparison request')
        report = compare_project(**json.loads(p.read_text()))
        result = {'ok': True, 'report': report}
    except Exception as exc:
        result = {'ok': False, 'error': f'{type(exc).__name__}: {exc}'}
    Path(args.response).write_text(json.dumps(result, allow_nan=False))


if __name__ == '__main__':
    main()
