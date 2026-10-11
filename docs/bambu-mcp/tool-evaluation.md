# Optional tool evaluation

Measured with Bambu Studio 02.08.02.61 on 2026-10-11. The input corpus contains
an open shell, narrow tall footprint, supported bracket, pin/socket, and two-color
assembly. These are synthetic regression parts, not the full Mainline body and
not physical-print validation. No user CAD geometry or source project was changed.

## Decisions

| Tool | Evidence | Decision |
|---|---|---|
| Tweaker-3 Python API | Extended search exposes several poses in one invocation. On the bracket, the first pose predicts 819 s / 4.76 g; another gives 814 s / 4.76 g and a third 1157 s / 4.60 g. | Adopt an explicit optional `tweaker3-python` backend. The improvement is candidate diversity and a material/time alternative, not a large speed gain over axis rotations. |
| Trimesh stable poses | Best times beat simple axis candidates by 0–0.92% across the five cases. Narrow footprint: 323 versus 326 s. It also returns reasonable baseline poses for the multicolor case, where Tweaker proposals are rejected. | Do not add a runtime dependency. Keep the probe for future complex geometry; this corpus does not establish enough incremental benefit. |
| Optuna multi-objective TPE | Three seeds, equal 8/8/9 evaluation budgets on shell/bracket/multicolor. Shell and multicolor tie staged search; bracket weighted-score changes range from -0.34% to +0.70%. | Do not integrate a second search controller yet. The current staged search is adequate on this small setting space. |
| Shapely | Probe exercises buffered projected footprints. This prototype uses raw envelope coordinates, not final plate placement; its fit flag is illustrative and is not a production prefilter. | Defer until irregular keep-outs or packing become actual requirements. |
| SciPy | Used by the Trimesh pose experiment. No standalone continuous-refinement advantage was demonstrated. | Keep evaluation-only. Existing stl-fit remains optional. |
| PyMeshLab / MeshFix / Manifold | No mesh repair or Boolean failure was established in the measured corpus. FreeCAD remains the CAD geometry authority. | Not installed or benchmarked; defer a repair fallback until there is a concrete failing input and a geometry-deviation acceptance limit. |
| Gmsh / CalculiX | No part loads, boundary conditions, or calibrated printed-material properties were supplied. | Not benchmarked; defer structural optimization rather than infer strength from generic assumptions. |
| libnest2d | Current comparison deliberately preserves aligned assembly placement. Packing separate parts is a different operation. | Not benchmarked; defer until a separate-part layout workload is specified. |

The orientation experiment actually sliced baseline, three axis rotations, up to
four Tweaker poses and four Trimesh poses per case. Candidate matrices were
validated against the original STL; the native project retained its meshes,
color assignments, relative placement and source identity. All four Tweaker
poses made the multicolor case much worse and were rejected by measured budgets.
Two connector poses (one axis rotation and one Tweaker pose) failed slicing. These outcomes are retained in the evidence;
the heuristic is not treated as authority.

The search experiment measured an 18-combination real-slicer table for each of
three cases (density 10/15%, grid/gyroid/adaptivecubic, layers .20/.24/.28 mm),
then replayed seeded Optuna proposals against those observations. Baseline counts
against the budget; duplicate proposals reuse observations. TPE runtime excludes
the initial response-table slicing cost. This compares settings at fixed poses,
not general joint orientation/CAD optimization. The table also permits checking
against the exhaustive best; the remaining opportunity is small on these cases.
The staged budget counts its actual baseline and history requests, which may
include redundant effective settings; TPE counts distinct setting combinations.
This gives TPE the benefit of deduplication rather than overstating the staged
optimizer's efficiency.

[`tool-evaluation.json`](tool-evaluation.json) stores versions, source hashes,
candidate statuses/metrics, Pareto sets and search outcomes. Individual run
directories with complete sliced artifacts and logs are produced by the harness.

## Use the adopted adapter

Install upstream Tweaker-3 and NumPy in a separate environment. No optional
geometry/search dependency is imported by either MCP runtime.

```python
suggest_print_orientations(
    stl_path="/path/whole_assembly.stl", backend="tweaker3-python",
    executable=["/path/env/bin/python", "/path/Tweaker-3/Tweaker.py"],
    build_volume=[256, 256, 256], max_candidates=4, seed=42
)
```

Feed selected `rotation_matrix` values into `compare_print_candidates` with the
original native project. Retain its automatic baseline and all print-spec locks
and budgets. Scores only rank suggestions; actual sliced results choose winners.
The API worker uses extended support-volume search, bounds execution and output,
seeds NumPy, removes duplicate matrices and independently checks original-size
rotated bounds. `samples` is unused for this backend. Favored surfaces still need
geometry/preview review; this batch does not certify them. The old CLI backend
keeps its behavior.

## Reproduce

The harness runs in the owned Bambu environment and invokes heavy probes in an
explicit separate environment. It neither downloads providers nor installs tools.
Pin the upstream checkout; this run used Tweaker commit
`d444290a732b8b72d10a701feed4e36a5284897d` and records the source-file SHA256.

```bash
uv venv --python 3.12 /tmp/print-tool-probes
uv pip install --python /tmp/print-tool-probes/bin/python -r tools/benchmarks/requirements.txt
tools/bambu-mcp/.venv/bin/python tools/benchmark_print_tools.py \
  --output /tmp/print-tools-001 \
  --python /tmp/print-tool-probes/bin/python \
  --tweaker-script /path/Tweaker-3/Tweaker.py
```

Without `--projects`, the harness generates and canonicalizes all five cases
through Bambu first. To reuse a canonical corpus, supply `--projects DIR` with
the five `<case>.3mf` files. Output directories must be new. The corpus uses
A1 mini 0.4 mm / PLA presets; installing the extra libraries alone does not prove
that an arbitrary user design will benefit.

## Correctness repairs included

The evaluation was preceded by fixes for boolean serialization (True/False now
reach both project patch and CLI as 1/0), objective/spec requirement merging,
larger-nozzle layer ladders, and independent specification lists. A real-slicer
regression checks exported support settings. Connector-clearance assertions now
derive dimensions from the fixture rather than comparing constants.

## Second round: independent validation and diagnostics

Evaluated on 2026-10-11 with lib3mf 2.5.0, GcodeParser 0.3.0 and NLopt 2.11.0.
Evidence is in [`validation-evaluation.json`](validation-evaluation.json).

| Tool | Trial and result | Decision |
|---|---|---|
| lib3mf | Read all five native benchmark projects, the 25-mesh Mainline baseline and the mount bracket without rewriting any source. All meshes pass its validity/manifold checks. Strict reading rejects vendor metadata; permissive reading returns 8 warnings per corpus/Mainline project and 11 on the bracket. Injected invalid triangle indices and missing triangles are accepted by the current assembly reader but detected by lib3mf. | Adopt an optional `project_validate` MCP tool and `[validation]` extra. Advisory geometry inspection runs in an isolated, bounded subprocess. Do not use strict compliance as a Bambu admission gate. |
| GcodeParser | `parse_gcode_lines('M620.1 E F523 T210')` emits `M620` plus a spurious `T210` tool change. This misinterprets the Bambu decimal command. | Reject this parser for Bambu motion/tool-change analysis. Add dependency-free comment diagnostics instead. |
| Native comment diagnostics | Read actual sliced corpus outputs. Identify support/support-interface, prime-tower and declared extrusion-height blocks. Initial scans of shell/bracket/multicolor artifacts cost about 10–35 ms. | Adopt in shared artifact metrics, owned slice metrics and comparison reports. Explicitly mark truncated scans; never infer mass, execution or strength from comments. |
| NLopt | Numerical BOBYQA smoke on an analytical hollow-box volume: 12 evaluations choose the same 1.2 mm hypothetical thickness floor as a direct bound choice, giving 0% incremental gain. This is not a FreeCAD or slicer benchmark. | No CAD optimizer integration. A meaningful workload needs permissible geometry changes, protected interfaces and an objective/constraints that cannot be solved by choosing a bound. |
| CGAL | Capability review only; not installed or executed. No representative self-intersection/remeshing failure was demonstrated. lib3mf manifold checks do not establish absence of self-intersections. | Defer an additional geometry adapter pending a failing input and a geometry-deviation acceptance limit. |
| Orca calibration / Python plugins | Documentation/availability review only. No Orca host is installed locally, and no measured physical calibration results were supplied. The documented plugin system requires nightly builds or releases newer than 2.4.2. | Defer integration and automatic preset compensation. Tolerance coupons and calibrated printer/nozzle/filament records remain useful future work, requiring physical measurements. |

The lib3mf worker takes approximately 60–90 ms including startup on the small
projects and 4.2 s on Mainline in the recorded run. It snapshots the input,
checks archive size/duplicates, bounds execution to 30 seconds, caps mesh/triangle
inspection and reports the source hash. Strict and permissive results are
separate. Vendor warnings remain visible rather than being discarded.

`toolpath_profiles` contain a maximum 64 MiB comment scan per plate. Feature
counts are blocks, not toolpath length or material mass. `LAYER_HEIGHT` comments
may describe combined infill/support extrusion thickness; the observed 0.4 mm
comments on nominal 0.2 mm profiles must not be used as a nominal-layer limit
violation. Missing comments yield empty distributions and null bounds; incomplete
scans remain marked `complete=false`. These diagnostics never affect ranking.

Reproduce the read-only round against the canonical corpus from the first harness:

```bash
tools/bambu-mcp/.venv/bin/python tools/benchmark_print_validation.py \
  --projects /path/canonical-corpus --python /path/probe-env/bin/python \
  --output /tmp/print-validation-001 --additional-project /path/native-project.3mf
PRINT_VALIDATION_PYTHON=/path/probe-env/bin/python \
  tools/bambu-mcp/.venv/bin/python -m pytest tools/bambu-mcp/tests/test_validation.py
```

Upstream references: [lib3mf Python bindings](https://github.com/3MFConsortium/lib3mf_python),
[GcodeParser](https://github.com/AndyEveritt/GcodeParser),
[NLopt algorithms](https://nlopt.readthedocs.io/en/stable/NLopt_Algorithms/),
[CGAL mesh processing](https://doc.cgal.org/latest/Polygon_mesh_processing/),
[Orca tolerance calibration](https://github.com/OrcaSlicer/OrcaSlicer/wiki/tolerance_calib),
and [Orca plugin development](https://github.com/OrcaSlicer/OrcaSlicer/wiki/plugin_development).
