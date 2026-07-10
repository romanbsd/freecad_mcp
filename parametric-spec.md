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

- Arbitrary Sketcher constraint solving
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

Every feature accepts the same common fields: `placement`, `frame`, `support`,
`reversed`, and `symmetric`. `position` remains a compatibility spelling for
`placement.translation`. A placement can contain `translation`, `rotation`, a
named principal-plane `frame`, or a custom frame with `origin`,
`x_direction`, and `normal`.
`support` derives that frame from an earlier feature and a face selector for
profiles, primitives, and other shape features. `reversed` flips the local
normal; `symmetric` centers axial features around the frame origin. Native
feature placements are refreshed after parameter changes.

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

### Reusable profiles and profile operations

Reusable 2D profiles can be placed on a principal plane or attached to a
planar face. Profile sections support `rectangle`, `circle`, `polygon`, and
`slot`; overlapping sections are fused into one face.

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
