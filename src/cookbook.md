# FreeCAD MCP cookbook

Use the declarative component tools for reusable models. Use `execute` for
FreeCAD operations that the component graph does not yet express.

Tool results are native MCP objects; do not JSON-decode them a second time.

## Error contract

Failed commands use one envelope:

```json
{
  "status": "error",
  "error": {
    "code": "NO_ACTIVE_VIEW",
    "message": "no active 3D view",
    "recoverable": true
  }
}
```

There is no legacy top-level `message`. Bridge calls raise `FreeCADToolError`
with `code` and `recoverable` attributes. Screenshot validation failures,
including no active view, invalid cameras, unknown views, and missing targets,
all use the error envelope rather than returning a successful result containing
`{"error": ...}`.

## Recommended workflow

1. Call `capabilities`, then discover feature schemas and patterns with
   `describe_feature_type` and `list_patterns`.
2. Build with `build_component`, or use `create_component` followed by
   `define_component`.
3. Inspect with `get_component_graph`, `validate_component`, `check_fit`, and
   `get_screenshot`.
4. Iterate with `set_component_parameters` or `patch_component`.
5. Export all required artifacts with `export_bundle`.

All dimensions should include units, such as `"31.8 mm"` or
`"$width / 2"`.

## Build, validate, and export in one call

`build_component` is the shortest path for a new model:

```json
{
  "document": {"name": "MotorMount", "replace": true},
  "component": {
    "name": "Mount",
    "label": "Parametric Motor Mount",
    "parameters": [
      {
        "name": "width",
        "kind": "input",
        "type": "length",
        "default": "32 mm",
        "min": "20 mm"
      },
      {
        "name": "half_width",
        "kind": "derived",
        "type": "length",
        "expression": "$width / 2"
      }
    ],
    "features": [
      {
        "id": "body",
        "type": "box",
        "size": {"x": "$width", "y": "32 mm", "z": "10 mm"},
        "role": "output"
      }
    ],
    "outputs": ["body"]
  },
  "validate": {
    "profiles": ["geometry_baseline", "fdm"],
    "include_measurements": true
  },
  "exports": {
    "directory": "/tmp/motor_mount",
    "formats": ["FCStd", "STEP", "STL"],
    "per_output": true,
    "assembly": true,
    "overwrite": true,
    "basename": "motor_mount"
  }
}
```

The result includes the component ID, validation findings, and artifact paths,
sizes, and SHA-256 hashes.

## Create and define separately

Use separate calls when the model will be assembled interactively:

```text
create_component(
  document="Mount",
  name="MotorMount",
  parameters=[
    {"name":"width", "type":"length", "default":"32 mm"},
    {"name":"wall", "type":"length", "default":"1.5 mm", "min":"0.8 mm"}
  ]
)
```

Then pass the returned `component_id` to:

```json
{
  "component_id": "component://Mount/MotorMount",
  "features": [
    {
      "id": "outer",
      "type": "box",
      "size": {"x": "$width", "y": "$width", "z": "10 mm"}
    },
    {
      "id": "hole",
      "type": "cylinder",
      "radius": "3 mm",
      "height": "12 mm",
      "axis": "z",
      "position": {"x": "$width / 2", "y": "$width / 2", "z": "-1 mm"},
      "role": "tool"
    },
    {
      "id": "body",
      "type": "cut",
      "base": "outer",
      "tool": "hole",
      "role": "output"
    }
  ],
  "profiles": ["geometry_baseline", "fdm"]
}
```

`define_component` is transactional. A failed build leaves the previous valid
graph intact.

## Declarative features

Core feature types:

- primitives and references: `box`, `cylinder`, `tube`, `cone`, `prism`, `datum_frame`;
- profiles and generated solids: `profile_2d`, `sketch_2d`, `pad`, `pocket`, `revolve`,
  `sweep`, `loft`, `fillet`, `chamfer`;
- relationships: `transform`, `cut`, `union`, `intersection`;
- repetition: `feature_pattern` plus linear/grid/polar/mirror/path aliases;
- assembly: `component_instance`, `mate`;
- organization: `group`;
- reusable expansion: `pattern`.

Use `list_feature_types` for the server's current list.

### Reusable datum frames

Use a `datum_frame` when several features share a working plane or local
coordinate system:

```json
[
  {
    "id": "mount_frame", "type": "datum_frame",
    "support": {"reference": "top_face"},
    "offsets": {"normal": "$clearance"},
    "rotation": {"axis": "normal", "angle": "30 deg"}
  },
  {
    "id": "boss", "type": "cylinder", "radius": "3 mm",
    "height": "8 mm", "frame_id": "mount_frame"
  }
]
```

Frames may derive from a principal plane, custom normal and x direction,
three points, a planar face support, or another datum via `base_frame`.
Use local `u`, `v`, and `normal` offsets. `frame_id` is accepted by primitives,
sketches, and profile operations.

### Manufacturing operations

Use manufacturing features after a base solid. They consume the base visually,
preserve it as a dependency, and produce an ordinary downstream shape.

```json
[
  {
    "id": "holes", "type": "hole", "base": "plate",
    "diameter": "5 mm", "through_all": true,
    "support": {"reference": "top_face"},
    "positions": [[-15, 0], [15, 0]],
    "head": {"type": "countersink", "diameter": "10 mm", "angle": "90 deg"},
    "thread": {"designation": "M5", "class": "6H"}
  },
  {
    "id": "stiffened", "type": "rib", "base": "plate",
    "support": {"reference": "top_face"},
    "path_points": [[-20, 0], [20, 0]],
    "thickness": "2 mm", "height": "6 mm"
  }
]
```

- `hole`: blind/through holes, multiple local positions, angled drill tips,
  counterbore/counterdrill/countersink heads, and thread metadata.
- `shell`: remove selected faces and hollow with `thickness`; `inward` defaults
  to true.
- `draft`: taper selected faces by `angle` around `neutral_plane`; optionally
  provide one edge with `pull_selector`.
- `rib`: thicken an open `path` or local `path_points` and fuse it to `base`.
- `face_offset`: move selected faces by signed `distance`.
- `replace_face`: replace selected faces with an equal number of
  boundary-compatible faces from `replacement.feature`.

All dimensions accept `$parameter` expressions. Named geometry references work
for supports and selectors. Thread declarations are metadata only; the feature
does not generate helical thread geometry.

### Unified feature patterns

Use `feature_pattern` for shape repetition. Every mode accepts `base`, optional
`frame_id` or planar `support`, plus `reversed`, `symmetric`, and `fuse` fields.

```json
[
  {
    "id": "bolt_circle", "type": "feature_pattern", "mode": "polar",
    "base": "hole_tool", "count": 6, "angle": "360 deg",
    "axis": "normal", "frame_id": "flange_frame"
  },
  {
    "id": "clips", "type": "feature_pattern", "mode": "path",
    "base": "clip", "path": "rim_path", "spacing": "12 mm",
    "orientation": "tangent", "include_end": false
  }
]
```

- `linear`: `count`, `spacing`, and `direction` or `axis`.
- `grid`: `count:{u,v}` and `spacing:{u,v}`, optionally custom local directions.
- `polar`: `count`, total `angle`, `axis`, and optional `center`.
- `mirror`: plane `origin`/`normal`; `keep_original` defaults to true.
- `path`: a wire `path` or `path_points`, distributed by `count` or `spacing`;
  orientation is `fixed` or `tangent`.

The output is a compound unless `fuse=true`. Full polar patterns do not repeat
the 360-degree endpoint. `array` and `grid_array` are deprecated aliases for
linear and grid modes; the explicit `*_pattern` aliases select their named mode.
The macro-expansion feature named `pattern` remains separate.

### Assembly instances and mates

Create reusable assembly parts with `component_instance`, then locate them by
matching source datum frames:

```json
[
  {
    "id": "fixed", "type": "component_instance",
    "component_id": "component://Parts/Bracket", "output": "body"
  },
  {
    "id": "moving", "type": "component_instance",
    "component_id": "component://Parts/Bracket", "output": "body",
    "variant": "wide", "parameters": {"height": "$instance_height"}
  },
  {
    "id": "locate", "type": "mate", "mode": "offset",
    "moving": {"instance": "moving", "frame": "left_mount"},
    "target": {"instance": "fixed", "frame": "right_mount"},
    "offset": {"normal": "$gap"}
  }
]
```

- `rigid` aligns frame origins and axes.
- `coincident` aligns origins and opposes normals unless `opposed=false`.
- `offset` adds local target-frame `u`, `v`, and `normal` displacement.
- `angle` rotates around the target-frame normal.

Each moving instance may have one mate. Cycles and duplicate positional mates
fail before geometry is changed. Instances snapshot source geometry and datum
frames, so assemblies work before source documents are saved and survive FCStd
reopen. Call `refresh_assembly` after changing a source component; use
`get_assembly_status` to inspect source signatures and mate residuals.

### Named geometry references

Name semantic selectors once at component definition time:

```json
{
  "geometry_references": [
    {
      "id": "top_face",
      "feature": "panel",
      "selector": {
        "kind": "face",
        "where": {"axis": "z", "side": "max", "planar": true},
        "expect": "one"
      }
    },
    {
      "id": "front_edge",
      "feature": "panel",
      "selector": {
        "kind": "edge",
        "where": {"parallel_to": "x", "near": [20, 0, 0]},
        "expect": "one"
      }
    }
  ]
}
```

Use `{"reference":"top_face"}` as a support or selector. Call
`list_geometry_references(resolve=true)` to audit all matches and
`resolve_geometry_reference` to inspect one current match. Each reference
captures a geometric baseline when the component is defined. Resolution reports
`stable` when the original topology index still matches and `remapped` when the
same geometry moved to another index. `ambiguous` and `missing` references stop
the rebuild transaction instead of allowing a different face or edge to be
selected silently.

For unusually large topology changes, tune matching on the reference itself:

```json
{
  "id": "front_edge",
  "feature": "panel",
  "selector": {"kind": "edge", "index": 1, "expect": "one"},
  "tracking": {"max_distance": 1.5, "ambiguity_tolerance": 0.01}
}
```

### Native constrained sketches

`sketch_2d` builds an editable `Sketcher::SketchObject`. Give geometry and
constraints stable IDs, then use the sketch as a profile:

```json
[
  {
    "id": "hole_sketch",
    "type": "sketch_2d",
    "role": "construction",
    "placement": {"support": {"reference": "top_face"}},
    "geometry": [
      {"id": "hole", "type": "circle", "center": [0, 0], "radius": "3 mm"}
    ],
    "constraints": [
      {"id": "diameter", "type": "diameter", "geometry": "hole", "value": "$hole_diameter"}
    ]
  },
  {
    "id": "cut", "type": "pocket", "base": "panel",
    "profile": "hole_sketch", "length": "$panel_thickness + 2 mm",
    "reversed": true
  }
]
```

Use dimensional constraints, rather than parameterized seed coordinates, for
geometry that must update. `require_fully_constrained=true` rejects remaining
degrees of freedom. Conflicting, redundant, and malformed constraints fail the
transaction. `external_geometry` accepts named edge or vertex references and
adds their IDs to the same geometry map used by constraints.
Those selectors are resolved again after parameter changes. If the matched
topological index changes, links and declarative constraints are rebuilt
transactionally; `remapped_external_geometry` reports the affected sketches.
Use `get_sketch_status(component_id, feature_id)` to inspect degrees of freedom,
solver findings, and all named maps without raw Python.

### Tube

```json
{
  "id": "clutch_tube",
  "type": "tube",
  "outer_radius": "3.25 mm",
  "inner_radius": "2.425 mm",
  "height": "2.7 mm",
  "axis": "z",
  "position": {"x": "7.9 mm", "y": "7.9 mm", "z": "0 mm"}
}
```

### Two-axis grid

```json
{
  "id": "tube_grid",
  "type": "grid_array",
  "base": "clutch_tube",
  "count_x": 3,
  "count_y": 3,
  "spacing_x": "8 mm",
  "spacing_y": "8 mm"
}
```

Boolean and array features reference earlier graph nodes by ID.

### Reusable profiles and unified operations

Profiles can use polygon points or `rectangle`, `circle`, `polygon`, and
`slot` sections. Definitions are geometry-only and create no document objects:

```json
{
  "profile_definitions": [
    {
      "id": "hole",
      "sections": [{"type": "circle", "radius": "$hole_radius"}]
    }
  ],
  "features": [{
    "id": "panel_with_hole",
    "type": "pocket",
    "base": "panel",
    "profile_id": "hole",
    "length": "$panel_thickness + 2 mm",
    "placement": {
      "support": {
        "feature": "panel",
        "selector": {
          "kind": "face",
          "where": {"axis": "z", "side": "max"}
        }
      },
      "reversed": true
    }
  }]
}
```

Use `profile_id` for reusable definitions and `profile` for graph features such
as `profile_2d`. `pad`, `pocket`, `revolve`, `sweep`, and `loft` share this
resolver and the common support/frame/direction contract. `profile_extrude`
and `extrude_cut` are deprecated schema aliases of `pad` and `pocket`.

The same `placement` envelope applies to primitives and other shape features.
With a face support, `reversed` flips the local normal and `symmetric` centers
an axial feature around the support frame. Parameter updates refresh native
feature placements as their supports change.

### Common selectors

All subelements share `{kind, index|indices|where, expect}`. This selector finds
vertical edges for a fillet:

### Profile extrusion and geometric edge selectors

```json
[
  {
    "id": "profile",
    "type": "pad",
    "points": [[0, 0], [20, 0], [20, 8], [0, 8]],
    "length": "$body_height",
    "axis": "z"
  },
  {
    "id": "rounded",
    "type": "fillet",
    "base": "profile",
    "radius": "1 mm",
    "selector": {
      "kind": "edge",
      "where": {"parallel_to": "z"}
    },
    "role": "output"
  }
]
```

`chamfer` uses `size` instead of `radius`. Selectors cover faces, edges,
vertices, and solids, with index selection and geometric predicates including
axis/side, normal, length, radius, area, volume, point, and tolerance.

## Reusable patterns

Patterns expand into ordinary graph features. They are inspectable and can be
patched after expansion.

Discover them with:

```text
list_patterns()
```

Preview generated features without building:

```json
{
  "feature": {
    "id": "studs",
    "type": "pattern",
    "pattern": "lego.stud_grid",
    "version": "1",
    "parameters": {
      "count_x": 4,
      "count_y": 4,
      "pitch": "8 mm",
      "diameter": "4.8 mm",
      "height": "1.8 mm",
      "origin": {"x": "3.9 mm", "y": "3.9 mm", "z": "9.6 mm"}
    }
  }
}
```

### Standard-style LEGO underside

The underside pattern hollows a base and adds clutch tubes plus raised ribs:

```json
{
  "id": "underside_pattern",
  "type": "pattern",
  "pattern": "lego.brick_underside",
  "version": "1",
  "parameters": {
    "base": "shell",
    "output": "underside",
    "width": "31.8 mm",
    "length": "31.8 mm",
    "wall": "1.5 mm",
    "depth": "2.7 mm",
    "tube_od": "6.5 mm",
    "tube_id": "4.85 mm",
    "pitch": "8 mm",
    "count_x": 4,
    "count_y": 4,
    "tube_origin_x": "7.9 mm",
    "tube_origin_y": "7.9 mm",
    "rib_thickness": "0.75 mm",
    "rib_z": "1.9 mm"
  }
}
```

This is nominal 3D-printable compatibility geometry, not a claim of official
LEGO manufacturing tolerances.

## Inspect and patch the graph

Retrieve a compact graph:

```text
get_component_graph(component_id, detail="summary")
```

Use `detail="full"` when preparing edits. The result contains a `revision`.

Dry-run a patch:

```json
{
  "component_id": "component://Mount/MotorMount",
  "expected_revision": 3,
  "dry_run": true,
  "operations": [
    {
      "op": "upsert",
      "feature": {
        "id": "rear_cap",
        "type": "box",
        "size": {"x": "34 mm", "y": "2 mm", "z": "20 mm"},
        "role": "output"
      }
    },
    {"op": "set_output", "ids": ["body", "rear_cap"]}
  ]
}
```

Supported patch operations:

- `{"op":"upsert","feature":{...}}`
- `{"op":"remove","id":"feature_id"}`
- `{"op":"set_output","ids":["body","cap"]}`

Apply the same patch with `dry_run=false`. A stale `expected_revision` is
rejected rather than overwriting another edit.

Structural patches rebuild only the changed feature and its downstream
dependents. Parameter changes remain natively incremental:

```text
set_component_parameters(
  component_id,
  values={"width":"40 mm"},
  rebuild=true,
  validate=true
)
```

Derived parameters are read-only.

## Roles and outputs

Give every graph feature an appropriate role:

- `output`: printable/exportable result;
- `construction`: intermediate geometry;
- `tool`: cutting or boolean tool;
- `inspection`: fit envelope or reference geometry.

Declare final output IDs explicitly when a component contains several printable
bodies, such as a mount and removable cap.

## Validation

Built-in profiles:

- `geometry_baseline`: parameter bounds, cycles, valid output solids, and
  intersecting cut tools;
- `cnc_plywood`: plywood-oriented thickness and stock checks;
- `fdm`: minimum wall warnings plus nozzle and clearance guidance.

```text
validate_component(
  component_id,
  profiles=["geometry_baseline", "fdm"],
  include_measurements=true
)
```

Domain-specific requirements should be custom rules:

```json
{
  "id": "cap_join",
  "type": "must_intersect",
  "severity": "error",
  "features": ["cap_clip", "latch_pocket"],
  "minimum_overlap": "0.5 mm",
  "message": "cap clips must engage the brick"
}
```

Findings include severity, actual and required values, and suggested parameter
changes when available.

## Fit and retention checks

Model the available space and inserted part as inspection features, then call:

```json
{
  "component_id": "component://Mount/MotorMount",
  "container": "motor_cavity",
  "insert": "motor_envelope",
  "retainers": ["rear_cap"],
  "probe_steps": 24,
  "tolerance": 0.01
}
```

`check_fit` reports:

- containment;
- per-axis and minimum bounding-box clearance;
- interference volume with retainers;
- blocked and unrestrained translation directions.

Retention paths are sampled, so increase `probe_steps` for thin clips or small
features.

## Screenshots without persistent view changes

Capture the whole active document:

```text
get_screenshot(view="iso", fit=true)
```

Isolate selected objects and temporarily make references transparent:

```text
get_screenshot(
  view="rear",
  targets=["MotorMountBrick", "RearSnapCap", "MotorEnvelope"],
  transparent=["MotorMountBrick"],
  temporary=true
)
```

With `temporary=true`, camera orientation, visibility, and transparency are
restored after capture. Set an explicit camera basis with:

```text
camera={"direction":[1,1,-1], "up":[0,0,1]}
```

A quaternion may instead be supplied as
`camera={"quaternion":[x,y,z,w]}`.

Create a horizontal contact sheet in one call:

```text
get_screenshot(
  targets=["MotorMountBrick"],
  views=["iso","front","rear","bottom"],
  width=320,
  height=240,
  temporary=true
)
```

The returned image width is the tile width multiplied by the number of views.

For cheaper images on quick shape checks, cap the longest side with `max_dim`
and/or render edges only with `wireframe=true` (the draw style is restored after
capture):

```text
get_screenshot(view="iso", max_dim=400, wireframe=true)
```

## Export bundles

```text
export_bundle(
  component_id,
  directory="/tmp/mount",
  formats=["FCStd","STEP","STL"],
  per_output=true,
  assembly=true,
  overwrite=true,
  basename="motor_mount"
)
```

`FCStd` is exported for the assembly/document. STEP, STL, IGES, and BREP can
also be emitted per output. Results include paths, byte sizes, and SHA-256
hashes.

## Direct object tools (no `execute` needed)

By-name mutation tools that avoid writing a script for common edits. They pair
with the read tools (`list_objects`, `get_object`, `get_subelements`):

- `transform(object, translate=[dx,dy,dz], rotate={"axis":[..],"angle":deg},
  center, relative=true, copy_to)` — move/rotate via `Placement`;
  `relative=true` is a "move by", `copy_to` operates on a named copy.
- `fillet(object, edges, radius, chamfer=false, copy_to)` — round/chamfer edges
  by the 1-based indices from `get_subelements`.
- `boolean(op, objects, name, keep=false)` — `cut`/`fuse`/`common` on named
  objects into one new object.
- `duplicate(object, count, translate, rotate, center, name, combine)` — copy or
  linear (`translate`) / polar (`rotate`+`center`) array; `combine` =
  `"compound"`/`"fuse"` for one object, else separate objects.
- `set_property(object, properties={...})` — set data props (e.g. a `Part::Box`
  `Length`) and recompute; ideal for iterating parametric primitives.
- `measure(a, b)` — also returns `overlap_volume` / `clash`, so a fit check is
  one call (no probe-solid script).
- `get_object(name, compact=true)` — identity + shape summary without the full
  property dump.
- `get_screenshot(..., max_dim=N, wireframe=true)` — cap the longest side and/or
  render edges-only for cheaper images.

## Raw `execute` fallback

The execution namespace contains `App`, `Gui`, `doc`, and `mcp` (helpers below).
Import other modules explicitly. Each call is one undo transaction and recomputes
automatically.

Variables persist across `execute` calls — define parameters and helpers once,
then reference them later instead of re-declaring (saves tokens, avoids drift).
Pass `reset=true` to clear the namespace.

`mcp` provides small generic helpers: `mcp.add(name, shape)` (idempotent
get-or-create `Part::Feature` + assign `Shape`), `mcp.fuse(shapes)`,
`mcp.cut(base, *tools)`, `mcp.common(shapes)`, `mcp.grid(nx, ny, pitch, inset)`,
and `mcp.box`/`mcp.cyl`/`mcp.vec` (= `Part.makeBox`/`makeCylinder`/`App.Vector`).

```python
# params defined in an earlier call are still here; mcp.add replaces the
# get-or-create + .Shape boilerplate
body = mcp.cut(mcp.box(32, 24, 10), mcp.cyl(3, 12, mcp.vec(16, 12, -1)))
mcp.add("Plate", body)
```

```python
import Part
doc = App.ActiveDocument or App.newDocument("Part")

box = doc.addObject("Part::Box", "Box")
box.Length, box.Width, box.Height = 10, 20, 5

hole = doc.addObject("Part::Cylinder", "Hole")
hole.Radius, hole.Height = 3, 7

cut = doc.addObject("Part::Cut", "Body")
cut.Base, cut.Tool = box, hole

result = {
    "valid": cut.isValid(),
    "bbox": tuple(cut.Shape.BoundBox),
    "volume": cut.Shape.Volume,
}
```

Return data by assigning to `result` or calling `print()`.

### PartDesign sketch and pad

```python
import Part, Sketcher
body = doc.addObject("PartDesign::Body", "Body")
sketch = body.newObject("Sketcher::SketchObject", "Sketch")
sketch.AttachmentSupport = [(doc.XY_Plane, "")]
sketch.MapMode = "FlatFace"

points = [
    App.Vector(0, 0, 0), App.Vector(10, 0, 0),
    App.Vector(10, 6, 0), App.Vector(0, 6, 0),
]
for start, end in zip(points, points[1:] + points[:1]):
    sketch.addGeometry(Part.LineSegment(start, end), False)

pad = body.newObject("PartDesign::Pad", "Pad")
pad.Profile = sketch
pad.Length = 5
```

### Target edges by inspection

Use `get_subelements` to retrieve stable geometric information before applying
an index-based fillet (or just call the `fillet` tool, which does this directly):

```python
fillet = doc.addObject("Part::Fillet", "Fillet")
fillet.Base = box
fillet.Edges = [(3, 1.5, 1.5), (5, 1.5, 1.5)]
box.Visibility = False
```

`get_selection` also returns user-selected `EdgeN` and `FaceN` names.

### Raw export

```python
import Part
Part.export([cut], "/tmp/out.step")
cut.Shape.exportStl("/tmp/out.stl")
```

Prefer `export_bundle` for component models.
