# FreeCAD MCP cookbook

Use the declarative component tools for reusable models. Use `execute` for
FreeCAD operations that the component graph does not yet express.

Tool results are native MCP objects; do not JSON-decode them a second time.

## Recommended workflow

1. Discover available features and patterns with `list_feature_types` and
   `list_patterns`.
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

- primitives: `box`, `cylinder`, `tube`, `cone`, `prism`;
- relationships: `transform`, `cut`, `union`, `intersection`;
- repetition: `array`, `grid_array`;
- organization: `group`;
- reusable expansion: `pattern`.

Use `list_feature_types` for the server's current list.

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

Structural patches currently reuse the transactional graph builder. Parameter
changes remain natively incremental:

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
restored after capture. A quaternion camera may be supplied as
`camera={"quaternion":[x,y,z,w]}`.

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

## Raw `execute` fallback

The execution namespace already contains `App`, `Gui`, and `doc`. Import other
modules explicitly. Each call is one undo transaction and recomputes the
document automatically.

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
an index-based fillet:

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
