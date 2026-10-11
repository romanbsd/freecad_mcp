# Unified print comparison workflow

The FreeCAD bridge and standalone Bambu MCP share a versioned print specification,
assembly validation, rigid transforms, bounded POSIX execution, artifact checks,
and winner reporting. `tools/bambu-mcp/print_pipeline_core` is dependency-free MIT
code bundled in the standalone package. The root `print_pipeline_core` facade
loads that same implementation for the bridge; there is no second copy.

The bridge's existing comparison call defaults to `backend="legacy"` for
compatibility. Select `backend="bambu"` for preset selection, cached results and
parallel slicing. The bridge and standalone server use the same FastMCP 4 workspace environment.
Comparison runs in a bounded JSON subprocess using the current interpreter;
`backend_python` can identify a deliberately separate installation. The bridge
also mounts the standalone MCP tools under `bambu_` names. Install both with
`uv sync --locked --extra validation` from the repository root.

```python
spec = {
    "schema_version": 1,
    "machine_preset": "Bambu Lab A1 mini 0.4 nozzle",
    "process_preset": "0.20mm Standard @BBL A1M",
    "filament_presets": ["Bambu PLA Basic @BBL A1M"],
    "functional": True,
    "min_wall_mm": 0.8,
    "max_layer_height": 0.28,
    "locked_keys": ["seam_position"],
    "max_time_increase_pct": 10,
    "max_material_increase_pct": 10
}
# Supply a native Bambu project with one aligned whole-assembly build item.
report = compare_print_candidates(
    project_path="/path/body.3mf", output_dir="/path/comparison_001",
    candidates=[
        {"name": "coarser", "settings": {"layer_height": 0.28}},
        {"name": "leaner", "settings": {"sparse_infill_density": 10}}
    ], backend="bambu", print_spec=spec, workers=2
)
```

Inputs must also carry Bambu Studio Application metadata. Merely adding a
project-settings file to a generic 3MF does not make it native; unsupported
inputs are rejected before invoking the slicer.

The standalone MCP exposes the same workflow as `compare_project`. Session
`optimize` accepts `print_spec` too; selected presets must match `project_open`.
It returns its existing best variant plus a `report` with additional alternatives.
Omitted specification fields preserve the optimizer's functional/support arguments.
Time and material caps use the stricter requirement when both interfaces specify
them; conflicting required/forbidden support requests fail. The optimizer uses
0.28 mm as its legacy default maximum without a specification, and resolved
nozzle bounds with one. An explicit `max_layer_height` still caps that bound.
Bridge `inspect_printability(print_spec=spec)` uses `min_wall_mm` as its sampled
wall threshold when present.

## Requirements and their limits

| Fields | Behavior |
|---|---|
| `machine_preset`, `process_preset`, `filament_presets` | Fully flattened presets; comparison snapshots their contents before slicing. Filament arrays stay ordered by slot. Legacy backend requires embedded presets. |
| `nozzle_mm`, `max_layer_height` | Nozzle comes from resolved machine settings; explicit nozzle must agree. Layer bounds use machine min/max, cap maximum at 75% of nozzle, and apply the requested maximum. Missing nozzle requires explicit `nozzle_mm`. Mixed nozzle sizes are rejected. |
| `functional`, `min_wall_loops`, `min_top_layers`, `min_bottom_layers`, `min_infill_pct` | Functional defaults: 2 walls, 3 top, 2 bottom, 10% infill; lightning excluded. Prototype defaults: 1 wall, 1 top/bottom, 5% infill. Explicit minima apply in either mode. |
| `min_wall_mm` | Derives a wall-loop floor from resolved `line_width`, or explicitly labeled nozzle-width proxy. Existing geometric preflight uses the actual millimeter threshold. Loop count does not prove wall thickness. |
| `locked_keys`, `locked_objects` | Settings cannot change; any object lock prevents whole-assembly rotation. Session optimization rejects global auto-orient with locks. |
| `keep_supports` | `auto`, `required`, `forbidden`; explicit requirements are checked globally and per object. |
| `require_brim` | Requires an explicit outer/inner/combined/ears brim; `auto_brim` does not guarantee brim generation. |
| `max_time_increase_pct`, `max_material_increase_pct` | Measured limits relative to a valid baseline. With a specification, an invalid baseline prevents candidate ranking. |
| `dimensional_tolerance_mm`, `protected_surfaces` | Carried as explicit `review_requirements`; require geometry, preview or measured-part evidence. They are not automatically certified. |

Unchanged baseline numeric floors and layer settings remain the user's own
choice. Explicit support/brim requirements and locks still apply. Specifications
with unknown fields fail; resolved specifications can be reused, with derived
bounds recomputed. An unresolved preset fails before the comparison output
folder is created.

## Results

`comparison.json` contains immutable source identity, resolved settings and
specification, slicer version, candidate settings and transforms, run IDs,
rejection reasons, artifact paths, and measured metrics. It never modifies the
input project. Candidates preserve component meshes, colors and relative
placement. No automatic XY rearrangement or scale-down is performed.

Both backends exclude failed, malformed, out-of-plate and constraint-invalid
results. Complete G-code and finite positive time/mass artifacts are required;
a success flag alone is insufficient. Unavailable support/model/purge mass
remains null. Unknown layer/change counts remain null rather than zero.

- `winners.fastest`: lowest predicted time.
- `winners.least_material`: lowest predicted grams.
- `winners.conservative`: fewest flagged quality-changing settings, then time.
  It may correctly select the baseline; this is a review heuristic, not a strength certificate.
- `pareto`: candidates not dominated in both time and material.
- `alternatives`: unique winners with before/after setting differences and trade-offs.

Session optimization and the owned comparison backend resolve presets once and
slice against their content-addressed snapshots. Inspection uses the same
resolver with explicit partial-resolution errors. Constraint checks and bounded
provider/slicer execution share their underlying implementation.

The owned backend uses deterministic candidate packages for
cache identity, and evaluates candidate processes in parallel after the baseline.
Both backends bound logs and execution time and stop POSIX process groups on
failure. The owned timeout range is 1–3600 seconds. Windows bounded execution is
not supported in this batch.

## Representative benchmarks

```bash
cd tools/bambu-mcp
.venv/bin/python -m bambu_mcp.benchmarks --output /tmp/print-benchmark-001
.venv/bin/python -m pytest -m slow
```

The corpus generates geometry, canonicalizes each project through the real slicer
with explicit presets, then compares and actually slices a 2 mm open shell, 6×8 mm tall footprint,
supported overhang bracket, square pin/socket with 0.3 mm per-side clearance,
and two-color assembly. Each case compares baseline, coarser layers and lower
infill. The multicolor check requires two consumed filament slots and actual
filament changes; the bracket check requires measured support extrusion time.
Reports record the installed slicer version. The presets target A1 mini 0.4 mm
with PLA Basic; the second slot uses an explicit white PLA preset. Wipe-tower
placement is configured inside this printer's plate.

These are reproducible synthetic regression cases, not physical print validation
or proof that a particular user's part will succeed.

Optional-tool adoption and reproducible measurements are documented in
[`tool-evaluation.md`](tool-evaluation.md).
