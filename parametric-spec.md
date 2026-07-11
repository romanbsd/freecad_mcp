# Spec: Parametric Component Workflow for FreeCAD MCP

## Goal

Provide a higher-level workflow for creating and editing reusable parametric CAD components without requiring users to embed full Python scripts in every request.

The workflow must let an agent create a named component, define typed parameters, generate a structured FreeCAD feature tree, update parameters incrementally, validate the result, and export it.

## Scope

Initial target: simple mechanical and enclosure-style assemblies composed from:

- Boxes, cylinders, cones, prisms
- Transforms
- Boolean union, cut, and intersection
- Repeated features
- Groups / assemblies
- Materials and display properties

Out of scope for v1:

- Full PartDesign history editing
- Multi-document assembly constraints
- FEM, CAM, or rendering workflows

## Core concepts

| Concept | Description |
|---|---|
| Component | A reusable, named parametric model stored in a FreeCAD document. |
| Parameter | A typed, editable input with a default value, range, units, and description. |
| Feature | A generated geometric element, such as a wall panel, entrance cutout, or perch. |
| Build graph | Declarative dependency graph from parameters to features. |
| Validation rule | A constraint evaluated after rebuild, such as minimum wall thickness or no collisions. |
| Variant | A named saved set of parameter overrides. |

## MCP API

### `create_component`

Creates a component container and its parameter schema.

```json
{
  "document": "BirdHouse",
  "name": "BudgerigarHouse",
  "label": "Parametric Budgerigar House",
  "parameters": [
    {
      "name": "width",
      "type": "length",
      "default": "180 mm",
      "min": "120 mm",
      "description": "External body width"
    },
    {
      "name": "wall_thickness",
      "type": "length",
      "default": "12 mm",
      "min": "6 mm"
    },
    {
      "name": "entrance_diameter",
      "type": "length",
      "default": "40 mm",
      "min": "35 mm",
      "max": "45 mm"
    }
  ]
}
```

Returns:

```json
{
  "component_id": "component://BirdHouse/BudgerigarHouse",
  "object_name": "BudgerigarHouse",
  "parameters_created": 3
}
```

### `define_component`

Adds or replaces the declarative build graph.

```json
{
  "component_id": "component://BirdHouse/BudgerigarHouse",
  "features": [
    {
      "id": "base",
      "type": "box",
      "label": "Floor panel",
      "size": {
        "x": "$width",
        "y": "$depth",
        "z": "$wall_thickness"
      },
      "material": "Exterior plywood"
    },
    {
      "id": "front_wall",
      "type": "box",
      "size": {
        "x": "$inner_width",
        "y": "$wall_thickness",
        "z": "$body_height"
      },
      "position": {
        "x": "$wall_thickness",
        "y": "0 mm",
        "z": "$wall_thickness"
      }
    },
    {
      "id": "entrance_hole",
      "type": "cylinder",
      "radius": "$entrance_diameter / 2",
      "height": "$wall_thickness + 2 mm",
      "axis": "y",
      "position": {
        "x": "$width / 2",
        "y": "-1 mm",
        "z": "$entrance_height"
      }
    },
    {
      "id": "front_with_entrance",
      "type": "cut",
      "base": "front_wall",
      "tool": "entrance_hole"
    }
  ]
}
```

Expressions use `$parameter_name`, arithmetic, and FreeCAD-compatible units.

### Common placement and selectors

Every feature accepts the same common fields: `placement`, `frame`, `frame_id`,
`support`, `reversed`, and `symmetric`. `position` remains a compatibility spelling for
`placement.translation`. A placement can contain `translation`, `rotation`, a
named principal-plane `frame`, or a custom frame with `origin`,
`x_direction`, and `normal`.
`support` derives that frame from an earlier feature and a face selector for
profiles, primitives, and other shape features. `reversed` flips the local
normal; `symmetric` centers axial features around the frame origin. Native
feature placements are refreshed after parameter changes.

### Datum frames

`datum_frame` adds a reusable local coordinate system to the feature graph.
Consumers reference an earlier datum with `frame_id`, so dependency-scoped
rebuilds update downstream primitives, sketches, and profile operations.

```json
{
  "id": "mount_frame",
  "type": "datum_frame",
  "support": {"reference": "panel_top"},
  "offsets": {"u": "10 mm", "v": "5 mm", "normal": "$clearance"},
  "rotation": {"axis": "normal", "angle": "$mount_angle"}
}
```

A datum can start from `plane` plus `origin`, from `normal` and `x_direction`,
from three points (`origin`, `x_point`, `plane_point`), from a planar-face
`support`, or from an earlier `base_frame`. Offsets use the local `u`, `v`, and
`normal` axes. Rotation accepts a local axis name or a vector. Datum objects
are hidden construction features but remain inspectable in FreeCAD.

### Manufacturing features

Manufacturing operations are dependency-tracked graph features. Their selectors
and dimensions are re-evaluated after parameter changes, and each operation
returns a normal shape feature for later booleans, dress-ups, and exports.

`hole` cuts one or more holes normal to a planar support face. Positions are
local `[u, v]` coordinates in that face frame. It supports blind and through
holes, flat or angled drill tips, counterbores, counterdrills, countersinks,
and thread metadata:

```json
{
  "id": "mounting_holes",
  "type": "hole",
  "base": "plate",
  "diameter": "$clearance_diameter",
  "through_all": true,
  "support": {"reference": "plate_top"},
  "positions": [[-12, 0], [12, 0]],
  "head": {"type": "counterbore", "diameter": "8 mm", "depth": "3 mm"},
  "thread": {"designation": "M5", "class": "6H"}
}
```

Thread data is manufacturing metadata; modeled helical threads are deliberately
not generated. `shell` removes selected faces and applies an inward or outward
wall thickness. `face_offset` moves selected faces by a signed distance.
`draft` tapers selected faces around one neutral face; `pull_selector` may name
an edge, otherwise a parallel edge is inferred from the neutral-face normal.
`rib` thickens an open path and fuses it to its base using a support or datum
frame. `replace_face` substitutes boundary-compatible faces from another graph
feature and attempts to heal the resulting solid.

### Unified feature patterns

`feature_pattern` repeats one graph feature with a common schema. `mode` is one
of `linear`, `grid`, `polar`, `mirror`, or `path`. Results are compounds by
default; set `fuse=true` to union touching copies. The original `base` is
included as the first copy and becomes visually consumed by the pattern.

```json
{
  "id": "vent_rows",
  "type": "feature_pattern",
  "mode": "grid",
  "base": "vent",
  "count": {"u": 4, "v": 3},
  "spacing": {"u": "$pitch", "v": "$row_spacing"},
  "frame_id": "panel_frame",
  "symmetric": true
}
```

Linear patterns use `count`, `spacing`, and `direction`. Grid patterns use
`count` and `spacing` objects with local `u`/`v` values, with optional
`direction_u` and `direction_v`. Polar patterns use `count`, `angle`, `axis`,
and an optional `center`; a full 360-degree pattern avoids duplicating its end
copy. Mirror patterns use a plane `origin` and `normal`, defaulting to the datum
frame origin and normal, and retain the original unless `keep_original=false`.
Path patterns accept a wire feature or `path_points`, plus either `count` or
`spacing`; `orientation=tangent` follows the path direction while `fixed`
preserves the seed orientation.

`frame_id` or a planar `support` supplies local axes and origins to every mode.
`reversed` flips the primary linear/grid direction, polar sweep, or path
traversal. `symmetric` centers linear/grid offsets and partial polar sweeps.
`array` and `grid_array` remain
deprecated compatibility aliases; `linear_pattern`, `polar_pattern`,
`mirror_pattern`, and `path_pattern` are concise mode aliases. The separate
`pattern` feature still expands reusable domain macros such as LEGO patterns.

### Component instances and mates

`component_instance` places one or more outputs from an existing component into
an assembly graph. Instances are persistent shape snapshots rather than
`App::Link` objects, so they work with unsaved source documents and remain
self-contained when the assembly is reopened.

```json
{
  "id": "left_bracket",
  "type": "component_instance",
  "component_id": "component://BracketParts/WallBracket",
  "output": "body",
  "grounded": true,
  "variant": "heavy_duty",
  "parameters": {"depth": "$bracket_depth"},
  "position": {"x": "40 mm"}
}
```

Use `output` or `outputs` to select source graph outputs. A named source
`variant` and per-instance `parameters` are evaluated transactionally while the
snapshot is captured; the source component is restored immediately. Overrides
may reference assembly parameters. Same-document instances are supported, but
same-document parameter overrides are rejected because FreeCAD cannot nest the
required source transaction.

`mate` deterministically places one moving instance frame onto a target instance
frame or local assembly feature:

```json
{
  "id": "attach_bracket",
  "type": "mate",
  "mode": "offset",
  "moving": {"instance": "left_bracket", "frame": "wall_mount"},
  "target": {"instance": "cabinet", "frame": "left_socket"},
  "offset": {"u": "0 mm", "v": "0 mm", "normal": "$clearance"}
}
```

Full-pose modes are `rigid`, `coincident`, `offset`, and `angle`. Coincident
mates oppose frame normals by default; set `opposed=false` to preserve
orientation. Offsets use target-frame `u`, `v`, and `normal` axes. `angle`
rotates around the target normal.

Partial-DOF modes are:

- `revolute`: fixes the frame origin and axis, leaving rotation about the axis;
- `slider`: fixes orientation and transverse position, leaving axial travel;
- `cylindrical`: leaves axial travel and rotation;
- `planar`: fixes normal distance and alignment, leaving two translations and
  in-plane rotation.

Set `grounded=true` on an instance to hold its placement fixed. A moving
instance may have several mates. The deterministic solver iteratively projects
each constrained coordinate, accepts redundant consistent constraints, and
reports incompatible residuals as `overconstrained`. Dependency cycles and
self-mates remain invalid.

`get_assembly_status` reports instance source revisions/signatures, placements,
mate residual vectors, constraint rank, remaining degrees of freedom, and
`fully_constrained`, `underconstrained`, or `overconstrained` solver status.
Constraint rank is calculated from the numerical assembly Jacobian rather than
the raw number of mate equations. `refresh_assembly` recaptures source outputs
and datum frames, solves the mate graph, and returns the updated status and
assembly bounds.

Faces, edges, vertices, and solids use one selector shape:

```json
{
  "kind": "face",
  "where": {
    "axis": "z",
    "side": "max",
    "planar": true,
    "tolerance": "0.01 mm"
  },
  "expect": "one"
}
```

Selectors also accept 1-based `index`/`indices`. Geometric predicates include
`normal`, `parallel_to`, `length`, `radius`, `area`, `volume`, and `point` as
appropriate for the selected kind. Legacy `face`, `face_index`, and `edges`
forms remain accepted.

### Named geometry references

`geometry_references` assigns stable names to semantic selector recipes. At the
first successful definition, the resolver captures a geometric signature for
every selected face, edge, vertex, or solid. Later rebuilds verify the original
subelement and deterministically remap it when FreeCAD changes topology order.

```json
{
  "geometry_references": [
    {
      "id": "panel_top",
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

Use `{ "reference": "panel_top" }` wherever a support is accepted and
`{ "reference": "front_edge" }` as a selector or sketch external geometry.
`list_geometry_references` returns the registry; `resolve_geometry_reference`
returns its current object, subelement matches, remap distances, and one of
`stable`, `remapped`, `ambiguous`, `missing`, or legacy `untracked`. Ambiguous and missing mappings
fail feature rebuilds transactionally rather than silently selecting different
geometry. Optional reference-level `tracking.max_distance` and
`tracking.ambiguity_tolerance` values tune matching; generated signatures and
baseline subelement names are persisted in component metadata. Components
created before API 2.7 capture baselines automatically on their next parameter
update.

### Constrained sketches

`sketch_2d` creates a native editable `Sketcher::SketchObject`. Geometry items
have stable IDs and support lines, circles, arcs, points, and construction
geometry. Constraints refer to those IDs:

```json
{
  "id": "cut_sketch",
  "type": "sketch_2d",
  "role": "construction",
  "placement": {"support": {"reference": "panel_top"}},
  "geometry": [
    {"id": "bottom", "type": "line", "start": [-6, -4], "end": [6, -4]},
    {"id": "right", "type": "line", "start": [6, -4], "end": [6, 4]}
  ],
  "constraints": [
    {
      "type": "coincident", "first": "bottom", "first_pos": 2,
      "second": "right", "second_pos": 1
    },
    {"type": "horizontal", "geometry": "bottom"},
    {"id": "width", "type": "distance", "geometry": "bottom", "value": "$cut_width"}
  ]
}
```

Supported constraints include coincident, horizontal, vertical, parallel,
perpendicular, equal, concentric, tangent, point-on-object, symmetric, block,
distance, distance-X/Y, radius, diameter, and angle constraints. Dimensional
constraint expressions are bound to component parameters and update natively.
Set `require_fully_constrained` to reject sketches with remaining degrees of
freedom. Conflicting, redundant, and malformed constraints fail transactionally.
`get_sketch_status` returns degrees of freedom, solver findings, and the named
geometry, constraint, and external-geometry maps.

`external_geometry` accepts named edge or vertex references. The resulting
external IDs can be used by constraints alongside local geometry IDs. Closed
sketches can be passed directly to profile operations with `profile`.
External selectors are re-evaluated after parameter changes. If a semantic
edge or vertex moves to another topological index, links and declarative
constraints are rebuilt in one transaction. `set_component_parameters`
reports those sketches in `remapped_external_geometry`.

### Reusable profiles and profile operations

Reusable 2D profiles can be placed on a principal plane or attached to a
planar face. Profile sections support `rectangle`, `circle`, `polygon`, and
`slot`; overlapping sections are fused into one face. Circles accept `radius`
or `diameter`; slots accept `radius` or full `width`.

```json
{
  "profile_definitions": [
    {
      "id": "mounting_hole",
      "sections": [
        {"type": "circle", "center": [0, 0], "radius": "3 mm"}
      ]
    }
  ],
  "features": [
    {
      "id": "front_with_hole",
      "type": "pocket",
      "base": "front_wall",
      "profile_id": "mounting_hole",
      "length": "$wall_thickness + 2 mm",
      "placement": {
        "support": {
          "feature": "front_wall",
          "selector": {
            "kind": "face",
            "where": {"axis": "y", "side": "min"}
          },
          "u_offset": "20 mm",
          "v_offset": "35 mm"
        },
        "reversed": true
      }
    }
  ]
}
```

`support.face_index` selects an exact 1-based FreeCAD face when topology is
stable. `support.face.normal` or `support.face.axis` with `side` (`min` or
`max`) is more descriptive and survives many dimension changes. Local profile
coordinates are centered on the selected face's bounds; `u_offset`, `v_offset`, and
`offset` move that local frame along its two axes and normal.

`pad` extrudes a profile and optionally fuses it with `base`. `pocket` cuts the
extrusion from `base`. Both accept inline geometry, a visible `profile` feature
id, or an invisible reusable `profile_id` definition,
infer the extrusion direction from a face support, and support `reversed` and
`symmetric` booleans. When a pocket support omits `feature`, its `base` is used.

`revolve`, `sweep`, and `loft` use the same profile resolver. A sweep accepts
`path` or `path_points`; a loft accepts ordered `loft_profiles` entries using
`profile_id` or `profile`. Any profile operation can combine with `base` using
`boolean: "fuse" | "cut" | "common"`.

`profile_extrude` is a deprecated schema alias of `pad`; `extrude_cut` is a
deprecated alias of `pocket`. Both remain executable for existing graphs.

### `set_component_parameters`

Applies incremental parameter updates and rebuilds only affected features.

```json
{
  "component_id": "component://BirdHouse/BudgerigarHouse",
  "values": {
    "width": "200 mm",
    "entrance_diameter": "42 mm"
  },
  "rebuild": true,
  "validate": true
}
```

Returns changed parameters, regenerated features, validation result, and bounding box.

### Transactional preview and diagnostics

`preview_parameter_update` runs the same parameter assignment, dependency
refresh, topology remapping, assembly solve, recompute, and optional design-rule
validation as `set_component_parameters`, then aborts the FreeCAD transaction.
Its result includes `before` and `after` parameter values, bounds, volume, area,
per-feature measurements, reference status, assembly status, warnings, and
fatal errors. `committed` is always false.

`validate_patch` applies graph operations through the real dependency-scoped
builder, validates resulting shapes and design rules, captures the same
before/after analysis, and aborts. Transform edits that require the full builder
are also previewed transactionally. Neither document revision nor stored
validation state changes.

`explain_rebuild` is read-only and does not recompute. Given parameter names,
feature IDs, or patch operations, it reports affected features in graph order,
upstream reasons, topology references, sketches, instances, mates, and outputs.

`get_feature_diagnostics` returns normalized diagnostics for one feature or the
whole graph: FreeCAD object state, shape validity and measurements, expression
bindings, topology-reference health, sketch constraints, instance DOF, and mate
residuals. Findings use machine-readable `errors` and `warnings` arrays.

### `get_component`

Returns the component schema, current parameter values, dependency graph summary, variants, and latest validation status.

### `create_component_variant`

Stores a reusable parameter override set.

```json
{
  "component_id": "component://BirdHouse/BudgerigarHouse",
  "name": "OutdoorLarge",
  "values": {
    "width": "200 mm",
    "depth": "200 mm",
    "roof_overhang": "20 mm"
  }
}
```

### `validate_component`

Runs structural and geometric validation.

```json
{
  "component_id": "component://BirdHouse/BudgerigarHouse",
  "rules": ["geometry_valid", "no_collisions", "minimum_wall_thickness"]
}
```

### `render_component`

Produces targeted visual verification.

```json
{
  "component_id": "component://BirdHouse/BudgerigarHouse",
  "view": "front",
  "section": {
    "plane": "XZ",
    "offset": "90 mm"
  },
  "hide_features": ["right_wall"],
  "width": 900,
  "height": 700
}
```

### `export_component`

Exports the component or selected variant.

```json
{
  "component_id": "component://BirdHouse/BudgerigarHouse",
  "variant": "OutdoorLarge",
  "format": "FCStd",
  "path": "/outputs/budgerigar-house.FCStd"
}
```

Supported formats: `FCStd`, `STEP`, `STL`, `IGES`, `BREP`.

## Component representation

A component is represented in FreeCAD as:

```text
App::Part (component container)
├── App::FeaturePython (parameter host)
├── App::DocumentObjectGroup (generated features)
│   ├── Part::Feature (base)
│   ├── Part::Feature (walls)
│   ├── Part::Feature (roof)
│   └── Part::Feature (perch)
├── App::DocumentObjectGroup (construction geometry)
├── App::DocumentObjectGroup (validation results)
└── App::DocumentObjectGroup (variants)
```

The parameter host exposes native FreeCAD properties, such as `App::PropertyLength`, `App::PropertyAngle`, `App::PropertyBool`, and `App::PropertyString`.

## Rebuild behavior

1. Validate parameter types, units, ranges, and expression dependencies.
2. Identify the affected subgraph.
3. Regenerate only affected features.
4. Preserve feature names and labels when possible.
5. Recompute the document.
6. Run requested validation rules.
7. Return a compact result; detailed object context is opt-in.

A failed rebuild must be transactional: retain the last valid geometry and return precise errors.

## Validation rules

Built-in rules:

- `geometry_valid`: all generated shapes are valid solids.
- `no_collisions`: independent solid features do not intersect unless explicitly allowed.
- `minimum_wall_thickness`: checks configured wall thickness thresholds.
- `contained_tools`: boolean-cut tools intersect their targets as expected.
- `parameter_ranges`: validates type, units, bounds, and enums.
- `manufacturable`: optional checks for unsupported features, minimum hole size, and overly thin details.
- `enclosure_access`: optional rule requiring a removable or openable service panel.

Rules produce structured findings:

```json
{
  "status": "warning",
  "rule": "minimum_wall_thickness",
  "feature": "front_with_entrance",
  "message": "Wall thickness is 5 mm; configured minimum is 6 mm.",
  "suggested_fix": "Set wall_thickness to at least 6 mm."
}
```

## Example: budgerigar house parameter schema

```json
{
  "width": "180 mm",
  "depth": "180 mm",
  "body_height": "250 mm",
  "wall_thickness": "12 mm",
  "roof_thickness": "18 mm",
  "roof_overhang": "12 mm",
  "entrance_diameter": "40 mm",
  "entrance_height": "195 mm",
  "perch_diameter": "12 mm",
  "perch_length": "58 mm"
}
```

Derived expressions:

```text
inner_width = width - 2 * wall_thickness
inner_depth = depth - 2 * wall_thickness
overall_width = width + 2 * roof_overhang
overall_depth = depth + 2 * roof_overhang
overall_height = wall_thickness + body_height + roof_thickness
```

## Acceptance criteria

- An agent can create and fully define the budgerigar house using fewer than five MCP calls.
- Updating `entrance_diameter` rebuilds only the entrance-related front-wall geometry.
- Updating `width` updates all dependent walls, roof, perch position, and derived dimensions.
- The component appears as a meaningful, editable hierarchy in FreeCAD’s model tree.
- A failed expression or invalid geometry leaves the prior valid model untouched.
- A front, isometric, and sectional render can be produced without custom Python.
- Export produces a valid native FreeCAD file and at least one interchange format.
