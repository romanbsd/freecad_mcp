# Curved design and colored printing (API 2.11)

The declarative component graph now supports rounded profiles, reusable 3D
curves, surface patches, and colored 3MF parts. These features use the existing
parameter host, dependency rebuilds, preview/rollback and FCStd persistence.
Restart FreeCAD and the MCP bridge to load and discover the new tools.

## Curves and filled profiles

`curve` creates a 3D edge for inspection, sweeps, or open surface lofts:

```json
{"id":"roof_line","type":"curve","role":"construction","curve":{
  "type":"bspline","mode":"interpolate",
  "points":[[0,0,0],[50,0,"$roof_height"],[100,0,0]],
  "tangents":[[1,0,1],null,[1,0,-1]]
}}
```

`bezier` uses `poles`. `bspline` defaults to interpolation and supports optional
`parameters`, `periodic` and one tangent or null per point. In `mode:"poles"`,
it accepts `degree`, `poles`, optional `knots`/`multiplicities`, and `weights`.
Custom `knots` and `multiplicities` must be supplied together, with finite,
strictly increasing knots and positive integer multiplicities consistent with
the pole count and degree; invalid declarations fail before kernel construction.
Without knots, it builds an open uniform clamped spline. Custom periodic pole
splines require suitable explicit knots and multiplicities. Default
interpolation has C2 interior continuity; tangent constraints or periodic
interpolation may reduce that to C1, as in FreeCAD's native interpolator.

Sketch geometry accepts the same `bezier` and `bspline` declarations, using
local 2D points. Beziers are converted to native sketch B-splines.

For filled profiles, use a `curved` section with ordered, connected segments:

```json
{"id":"rounded_profile","type":"profile_2d","sections":[{
  "type":"curved","curves":[
    {"type":"bezier","poles":[[0,0],[50,"$rise"],[100,0]]},
    {"type":"line","points":[[100,0],[0,0]]}
  ]
}]}
```

The wire must close explicitly; open or invalid profiles fail the transaction.
These profiles work with pad, pocket, revolve, sweep, and loft. Spline tangents
in profiles are local 2D directions; frame translation does not affect them.

## Surfaces and aligned lofts

`surface_patch` takes a rectangular, u-major `grid` of 3D coordinates.
`mode:"interpolate"` passes through the grid points using FreeCAD's B-spline
surface interpolator. `mode:"poles"` treats the grid as a tensor Bezier control
net, represented as a clamped B-spline surface. Coordinates accept parameters.

`guided_surface` creates a Coons patch from four Bezier pole arrays:

```json
{"id":"hood","type":"guided_surface","guides":{
  "bottom":[[0,0,0],[50,0,0],[100,0,0]],
  "top":[[0,80,0],[50,80,0],[100,80,0]],
  "left":[[0,0,0],[0,40,"$rise"],[0,80,0]],
  "right":[[100,0,0],[100,40,"$rise"],[100,80,0]]
}}
```

Bottom/top run in increasing u; left/right run in increasing v. Opposite guides
need equal pole counts and matching corners. All four boundary curves are
reproduced exactly. The patch guarantees **G0 position continuity only**.
Automatic G1/G2 matching to neighboring faces is not implemented; requests for
other `continuity` values fail. Interior pole rows let designers control slopes,
but should be checked before claiming smooth joins. Patches are faces, not
printable solids; construct and validate a closed solid before 3MF export.

Loft entries accept `reverse` and zero-based `seam_edge` to align section
orientation and start edges explicitly. `solid:false` builds a surface loft;
existing lofts continue to default to solid. Seam shifts require closed wires.

## Inspection

- `inspect_surface(component_id, feature_id, samples, stripe_axis, stripe_count)`
  returns sampled positions, normals and principal curvatures in 1/mm. Samples
  outside trimmed faces are omitted. Reflection stripes project a reflected
  +Z viewing ray onto the specified axis; this is a sampled directional
  diagnostic, not a full environment zebra renderer.
- `cross_sections(component_id, plane, offsets, feature_id, samples)` returns
  discretized intersection curves. XY offsets are z, XZ offsets y, YZ offsets
  x. Offsets accept dimensions/parameter expressions. No hit returns no curves.
- `reference_overlay(component_id, image_path, plane, model_points, pixel_points,
  feature_id, samples, opacity, reflection_stripes)` returns a self-contained
  SVG with embedded PNG and orthographic model edges. Two corresponding 2D
  anchors define uniform scale and rotation; projected model y points up and
  pixel y points down. This is a calibrated edge overlay, not an occluded
  silhouette or automatic blueprint trace. Optional stripes appear as face
  sample markers. All geometry uses document coordinates, including part
  container placement. Inspection does not alter visibility or the camera.

## Colored 3MF

Declare separate output features for body, bumpers, grille and lenses, with
`material:{"name":"Chrome plastic","color":"#C0C0C0"}`. Existing string
material names continue to work. Colors accept #RRGGBB or #RRGGBBAA and persist
on native objects; exports fall back to each object's display color.

`export_component(..., format:"3MF")` and `export_bundle(..., formats:["3MF"])`
write named mesh parts plus a single Core assembly, sharing color materials.
Geometry stays in document coordinates; assembly export preserves alignment.
Per-output export creates separate files. A variant export restores the source
parameters afterward, as for other formats.

Every part must be a valid solid. Native meshing uses 0.05 mm linear and
0.15 rad angular deflection and rejects non-solid/self-intersecting meshes.
The serializer checks finite vertices, triangle indices/area, closed oriented
edge topology and enclosed volume, then reads the ZIP/XML back and compares
geometry/colors before atomically replacing the target. Contacts between parts
are allowed; collision clearance and printer-specific filament-slot assignment
are separate decisions. Core colors do not constitute a printer project or
guarantee a slicer's automatic filament assignments.

## Print-aware workflow (API 2.11)

The following tools supplement the declarative FDM parameter rules with actual
geometry checks and measured slicer results. They never start a printer job.
All lengths are millimeters. Inspection coordinates and attachment anchors are
in document coordinates; connector positions use the component build graph's
local coordinates. Inspection samples cannot certify a whole solid.

### Geometric preflight and assembly seating

`inspect_printability(component_id, feature_id=None, min_wall=0.8,
samples_per_face=5, build_direction=None, overhang_angle=45, max_samples=2000)`
casts inward rays from trimmed face samples and measures the first material
segment. It reports `min_observed_wall_mm`, thin-wall findings with actual and
required dimensions, unresolved samples, truncation, sampled overhangs, and
connected solid clusters. Touching solids belong to the same cluster. Floating
means a cluster does not reach the plane through the model's lowest point in
the chosen build direction. The tool derives this plane; it does not know the
printer's absolute plate position.

`build_direction` is the upward vector in document coordinates, default +Z.
Overhangs use sampled outward normals; they do not predict bridges or support
volume. UV samples are not area weighted. Thin walls between sample locations
can be missed; unresolved or capped analysis produces WARN rather than PASS.
Open geometry is rejected. This tool does not change `validate_component`'s
parameter-based FDM rules or replace final slicer preview.

`inspect_attachment(component_id, feature_id, target_id, anchor, radius=5,
tolerance=0.05)` crops both solids around a spherical anchor and measures local
gap and intersection volume. A distant contact cannot prove this intended
attachment is seated. An empty crop returns `NO_LOCAL_GEOMETRY`; retention,
glue area and access still need design review.

### Connector recipes

`create_connector_features(component_id, prefix, male_base, female_base,
position, axis="z", kind="pin_socket", diameter=3, length=4, clearance=0.2,
embed=1, bottom_clearance=0.2, lead_in=0.3, width=6, thickness=1.2)` returns a
recipe without modifying the component. `position` is the socket entry plane;
positive `axis` (x/y/z) points into the receiver. Clearance is **per side**.
`pin_socket` creates an embedded round pin, tip lead-in and matching pocket.
`glue_ledge` creates a rectangular tongue/pocket using width and thickness;
`lead_in` does not alter rectangular ledges. Both inherit the bases' materials.

Apply the returned `operations` with `patch_component`, using the current
`expected_revision` and first `dry_run:true` for a preview. The operations replace
the two base outputs with connected male/female outputs and preserve other
outputs. Verify embedding, remaining receiver wall and local seating, then print
a clearance coupon before committing to a large assembly. The recipe does not
guarantee sufficient wall thickness or a physical press fit.

### Optional orientation candidates

`backend="tweaker3-python"` optionally returns multiple ranked poses from one
upstream API invocation. Use `[python, /checkout/Tweaker.py]` as the executable;
the existing `tweaker3` CLI adapter is preserved. See
[`tool-evaluation.md`](docs/bambu-mcp/tool-evaluation.md) for measured adoption decisions.

`suggest_print_orientations(stl_path, backend, executable=None,
build_volume=None, timeout=120, seed=42, samples=5000, max_candidates=4)` runs
on the bridge host, outside FreeCAD's GUI thread. Supply an STL envelope of the
**entire aligned assembly**, in its original coordinates. The source is copied
to an isolated working directory and remains unchanged.
The external-tool runner requires POSIX process groups. Envelopes are limited
to 16 MiB and 300,000 triangle vertices; export a coarser envelope for large
models. This limits local bounds-processing cost as well as input memory.
The `timeout` controls the external subprocess, rather than local parsing.

Supported external providers are [Tweaker-3](https://github.com/ChristophSchranz/Tweaker-3)
and [stl-fit](https://github.com/khromov/stl-fit). Install these separately with
their upstream dependencies; they are not vendored or installed automatically.
`executable` is an argv prefix, for example `["/path/venv/bin/python",
"/path/Tweaker-3/Tweaker.py"]` or `["/path/venv/bin/python",
"/path/stl-fit/stl_fit.py"]`. A Tweaker executable on PATH may also be used.
The stl-fit adapter calls its library search directly, bypassing the upstream
CLI's scale-down loop. It currently accepts cubic build volumes only.

Candidates contain proper rigid `rotation_matrix` values using column vectors,
independently measured bounds, `fits_build_volume`, and heuristic scores. No scaling or
reflection is accepted. `no_fit_found` is a heuristic outcome, not proof that no
orientation exists. Scores are not print-time estimates. Candidates must still
be arranged and sliced with all parts sharing the same rotation.

### Measured slicer comparison

`compare_print_candidates(project_path, output_dir, candidates,
slicer_path=None, timeout=300)` also runs on the bridge host. The CLI runner
currently requires POSIX process groups. Input must be a
**native Bambu Studio project** containing printer, process and filament
configuration, with one whole assembly build item in millimeters. Multiple
build items and other units are rejected even for settings-only comparisons.
Save that project from Bambu Studio after assigning filament
slots; a generic colored Core 3MF export alone is insufficient.

Use a new, nonexistent output directory. One unchanged baseline is sliced
first, followed by 1–12 named candidates. Supported settings are bounded layer
height, walls, infill, top layers/thickness/color penetration and support
options; unsupported keys fail before slicing. Each candidate may supply Euler
`rotation:[rx,ry,rz]` in degrees or a `rotation_matrix` from the orientation tool.
Assembly traversal has shared object and vertex budgets across its bounds
passes, rejecting exponentially repeated subassemblies before slicing.
Whole-assembly rotation preserves component colors and relative placements,
then lifts the lowest vertex to the build plane. It does not arrange models in
XY or guarantee that a rotated model fits the plate.

```json
{
  "project_path":"/path/Body_Bambu.3mf",
  "output_dir":"/path/comparison_001",
  "candidates":[
    {"name":"coarser","settings":{"layer_height":0.28,"wall_loops":3,"sparse_infill_density":10}},
    {"name":"rotated","rotation":[90,0,0],"settings":{"layer_height":0.28}}
  ]
}
```

Each candidate gets its own input project, sliced project and bounded log.
`comparison.json` records source hashes, slicer version, elapsed print seconds,
total filament grams, plate status and available layer/change counts. Support,
model or purge mass is null when the slicer does not expose it separately.
Failed, incomplete and out-of-bounds results are excluded from ranking; the
fastest valid result becomes `best_candidate`. Review its sliced preview before
printing. The input project remains unchanged. Processes have time/output
limits and their child processes are stopped on timeout.

## Tests

```sh
.venv/bin/python -m pytest -q
FREECAD_MCP_LIVE=1 .venv/bin/python -m pytest tests/test_design_live.py tests/test_api_v2_live.py -q
```

Live tests require the FreeCAD GUI MCP server on localhost:9876. New geometry
tests compare analytical profile volumes, interpolated points, guide boundaries,
curvature and tangents; exercise rollback and reopen/rebuild; and verify section
coordinates, calibration, seam alignment, colors and native 3MF reimport.
The serializer tests independently inspect ZIP/XML and cover atomic failure.

### Unified comparison backend and print specification

`compare_print_candidates` now also accepts `backend="bambu"`, `print_spec`,
`workers`, and `backend_python`. The compatibility default remains `legacy`.
The owned backend adds fully resolved presets, immutable preset snapshots, cached
parallel slicing, and the same whole-assembly and strict artifact validation.
Both backends return fastest, least-material and conservative alternatives with
setting differences and trade-offs. `inspect_printability(print_spec=...)` uses
`min_wall_mm` as its sampled threshold. See the [versioned contract and
benchmarks](docs/bambu-mcp/unified-pipeline.md) for enforced constraints and the
geometry/preview requirements that remain explicitly unverified.
