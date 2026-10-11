"""Reproduce optional-tool measurements without installing them into either MCP runtime.

Run with tools/bambu-mcp/.venv/bin/python; --python identifies an isolated probe
environment, --tweaker-script an installed upstream checkout. Output is exclusive.
"""
import argparse
import hashlib
import itertools
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'tools/bambu-mcp')]
from bambu_mcp.benchmarks import CASES, run_benchmarks
from bambu_mcp.pipeline import compare_project
from bambu_mcp.runner import Runner, Variant
from bambu_mcp.optimize import optimize, Objective
from print_pipeline_core.process import run_bounded
from orientation_candidates import suggest_print_orientations

AXES = [[[1,0,0],[0,0,-1],[0,1,0]], [[0,0,1],[0,1,0],[-1,0,0]], [[0,-1,0],[1,0,0],[0,0,1]]]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', required=True)
    p.add_argument('--python', required=True, help='probe environment Python')
    p.add_argument('--tweaker-script', required=True)
    p.add_argument('--projects', help='existing canonical corpus directory; otherwise generate it')
    a = p.parse_args()
    dest = Path(a.output).expanduser().resolve(); dest.mkdir(parents=True, exist_ok=False)
    python = str(Path(a.python).expanduser().absolute())
    provider = Path(a.tweaker_script).expanduser().resolve(strict=True)
    sources = Path(a.projects).expanduser().resolve(strict=True) if a.projects else dest/'corpus'
    if not a.projects:
        run_benchmarks(sources)

    def probe(script, request, output, *extra):
        directory = dest/(output.stem+'_worker'); directory.mkdir()
        code, error, log, elapsed = run_bounded([python, str(ROOT/'tools/benchmarks'/script), str(request), str(output), *extra], directory, 180)
        if code != 0 or error:
            raise RuntimeError(f'{script}: {error or code}: {log[-2000:]}')
        return json.loads(output.read_text()), elapsed

    request = dest/'geometry_request.json'; request.write_text(json.dumps(CASES))
    geo, geometry_wall = probe('probe_print_geometry.py', request, dest/'geometry.json', str(provider.parent))
    orientation = {}
    for name, data in geo['cases'].items():
        started = time.perf_counter()
        api = suggest_print_orientations(dest/'envelopes'/(name+'.stl'), 'tweaker3-python',
                [python,str(provider)], build_volume=[180]*3, max_candidates=4)
        if api['status'] != 'success':
            raise RuntimeError(f'{name} provider failed: {api}')
        provider_wall = time.perf_counter()-started
        candidates = [{'name':f'axis_{i}', 'rotation_matrix':m} for i,m in enumerate(AXES)]
        for tag, rows in [('tweaker',api['candidates']), ('trimesh',data['trimesh'])]:
            candidates += [{'name':f'{tag}_{i}', 'rotation_matrix':c['rotation_matrix']} for i,c in enumerate(rows)]
        started = time.perf_counter()
        report = compare_project(sources/(name+'.3mf'), dest/name, candidates,
                    workers=2, cache_home=dest/'cache', print_spec={})
        best = {}
        for tag in ('baseline','axis','tweaker','trimesh'):
            rows = [c for c in report['candidates'] if c['name'].startswith(tag) and c['status']=='success']
            best[tag] = min((dict(name=c['name'],seconds=c['metrics']['seconds'],grams=c['metrics']['total_grams']) for c in rows), key=lambda c:c['seconds'], default=None)
        orientation[name] = dict(best=best, provider_wall_seconds=provider_wall,
                slicing_wall_seconds=time.perf_counter()-started, source_sha256=report['source_sha256'],
                source_unchanged=report['source_unchanged'], pareto=report['pareto'],
                candidates=[{k:c.get(k) for k in ('name','status','metrics','constraint_violations','error')} for c in report['candidates']])

    choices = list(itertools.product([10,15], ['grid','gyroid','adaptivecubic'], [.2,.24,.28]))
    search = {}
    for name in ('thin_shell','overhang_bracket','multicolor_assembly'):
        table = []
        for start in (0,12):
            candidates = [dict(name=f'c{i}',settings=dict(sparse_infill_density=d,sparse_infill_pattern=pattern,layer_height=h))
                          for i,(d,pattern,h) in enumerate(choices) if start<=i<start+12]
            report = compare_project(sources/(name+'.3mf'), dest/(name+'_grid'+str(start)), candidates,
                        workers=2, cache_home=dest/'cache', print_spec={})
            table += [dict(choice=choices[int(c['name'][1:])], status=c['status'], metrics=c.get('metrics')) for c in report['candidates'][1:]]
        started = time.perf_counter()
        result = optimize(Runner(home=dest/'greedy_cache'), sources/(name+'.3mf'), Variant(flags={'orient':0},plate=1),
                    Objective(), stages=['infill','layer_height'], max_slices=20, print_spec={}, workers=2)
        search[name] = dict(table=table,greedy=dict(seconds=result.best_metrics.time_s,grams=result.best_metrics.filament_g,
                    slices=1+len(result.history),wall_seconds=time.perf_counter()-started,overrides=result.best.overrides))
    request = dest/'search_request.json'; request.write_text(json.dumps(search))
    tpe, search_wall = probe('probe_print_search.py', request, dest/'search.json')
    record = dict(schema_version=1, estimate_only=True, slicer_version=Runner(home=dest/'cache').version(),
                tweaker_source_sha256=hashlib.sha256((provider.parent/'MeshTweaker.py').read_bytes()).hexdigest(),
                versions=geo['versions'], geometry=geo['cases'], geometry_worker_seconds=geometry_wall,
                search_worker_seconds=search_wall, orientation=orientation, search=tpe)
    (dest/'evaluation.json').write_text(json.dumps(record,indent=2,allow_nan=False))
    print(json.dumps(dict(report=str(dest/'evaluation.json'),cases=list(orientation))))


if __name__ == '__main__':
    main()
