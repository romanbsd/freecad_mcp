"""Evaluation-only parser compatibility and numerical optimizer smoke, not a CAD benchmark."""
import importlib.metadata
import json
import nlopt
from pathlib import Path
from gcodeparser import parse_gcode_lines

calls = 0


def volume(x, gradient):
    global calls
    calls += 1
    t = x[0]
    return 60*40*20 - (60-2*t)*(40-2*t)*(20-t)


opt = nlopt.opt(nlopt.LN_BOBYQA, 1)
opt.set_lower_bounds([1.2])
opt.set_upper_bounds([3])
opt.set_min_objective(volume)
opt.set_maxeval(30)
opt.set_xtol_abs([1e-6])
thickness = float(opt.optimize([2.0])[0])
evaluation_calls = calls
solver_volume = volume([thickness], None)
direct_volume = volume([1.2], None)
lines = list(parse_gcode_lines('M620.1 E F523 T210'))
Path('probe.json').write_text(json.dumps(dict(versions={p:importlib.metadata.version(p) for p in ('gcodeparser','nlopt','lib3mf')},
    gcodeparser=dict(input='M620.1 E F523 T210', parsed_commands=[list(line.command) for line in lines],
                    compatible=False, reason='Decimal M command becomes M620 and a spurious T210 tool-change command'),
    nlopt=dict(status='numerical_smoke_only', calls=evaluation_calls, thickness_mm=thickness,
               volume_mm3=solver_volume, direct_bound_volume_mm3=direct_volume,
               incremental_gain_percent=(direct_volume-solver_volume)/direct_volume*100,
               limitations='Analytical hollow-box objective with a hypothetical thickness floor; no FreeCAD geometry, loads, physical prints or slicer optimization.')), allow_nan=False))
