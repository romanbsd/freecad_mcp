"""Read-only optional validation evaluation against existing native projects/artifacts."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from print_pipeline_core.assembly import _read_project
from print_pipeline_core.artifacts import read_artifacts
from print_pipeline_core.validation import validate_file
from print_pipeline_core.process import run_bounded

CASES = ('thin_shell', 'narrow_footprint', 'overhang_bracket', 'fitted_connector', 'multicolor_assembly')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--projects', required=True, help='canonical benchmark corpus directory')
    parser.add_argument('--python', required=True, help='isolated lib3mf/gcodeparser/nlopt environment')
    parser.add_argument('--output', required=True, help='new evidence directory')
    parser.add_argument('--additional-project', action='append', default=[])
    args = parser.parse_args()
    output = Path(args.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    projects = Path(args.projects).expanduser().resolve(strict=True)
    python = str(Path(args.python).expanduser().absolute())
    paths = [projects/(case+'.3mf') for case in CASES] + [Path(p).expanduser().resolve(strict=True) for p in args.additional_project]
    report = dict(schema_version=1, validations=[], toolpath_profiles=[], defects=[], decisions={})
    for path in paths:
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        result = validate_file(path, python_executable=python)
        report['validations'].append(dict(file=path.name, result=result,
                                         source_unchanged=before == hashlib.sha256(path.read_bytes()).hexdigest()))
    for case in CASES:
        artifact = projects/case/'baseline'/'sliced.3mf'
        metrics = read_artifacts(artifact, artifact.parent)
        report['toolpath_profiles'].append(dict(case=case, profiles=metrics['toolpath_profiles']))
    source = projects/'narrow_footprint.3mf'
    with zipfile.ZipFile(source) as z:
        entries = {i.filename:z.read(i) for i in z.infolist()}
    for kind in ('invalid_index', 'open_mesh'):
        copied = dict(entries)
        for name, data in copied.items():
            if name.endswith('.model'):
                if kind == 'invalid_index':
                    changed, count = re.subn(rb'(<triangle\b[^>]*\bv1=")[0-9]+', rb'\g<1>999999', data, count=1)
                else:
                    changed, count = re.subn(rb'<triangle\b[^>]*/>', b'', data, count=1)
                if count:
                    copied[name] = changed
                    break
        else:
            raise ValueError('No triangle available for mutation')
        mutation = output/(kind+'.3mf')
        with zipfile.ZipFile(mutation, 'w') as z:
            for name, data in copied.items():
                z.writestr(name, data)
        try:
            _read_project(mutation)
            current = 'accepted'
        except ValueError:
            current = 'rejected'
        report['defects'].append(dict(mutation=kind, native_assembly_reader=current,
                                     lib3mf=validate_file(mutation, python_executable=python)))
    work = output/'numeric_probe'
    work.mkdir()
    code, error, log, elapsed = run_bounded([python, str(ROOT/'tools/benchmarks/probe_print_validation.py')], work, 30)
    if code or error:
        raise RuntimeError(f'Optional probe failed: {error or code}: {log[-1000:]}')
    report['optional_probe'] = json.loads((work/'slicer.log').read_text())
    report['decisions'] = dict(lib3mf='adopt optional read-only advisory validator; strict mode cannot gate Bambu files',
        gcodeparser='reject Bambu motion interpretation; use bounded native comment diagnostics instead',
        nlopt='defer CAD optimizer: numeric smoke matches a direct bound choice and proves no real CAD benefit',
        orca='defer integration: no measured print calibration results or installed plugin host',
        cgal='not executed; no demonstrated self-intersection or remeshing failure requiring another geometry adapter')
    (output/'evaluation.json').write_text(json.dumps(report, indent=2, allow_nan=False))
    print(json.dumps(dict(evidence=str(output/'evaluation.json'), cases=len(paths))))


if __name__ == '__main__':
    main()
