# FreeCAD-Backed Parametric Components and Design Rules

## Purpose

Define an MCP extension for reusable parametric components and automated design-rule checks. The extension uses FreeCAD as the authoritative parametric and geometry kernel, while the MCP owns component semantics, rule profiles, validation orchestration, and structured diagnostics.

This document separates capabilities verified as available in FreeCAD from capabilities that must be implemented by the MCP layer.

## Research findings

Research was performed against the connected FreeCAD 1.1.1 runtime and FreeCAD's Python API sources.

| Capability | FreeCAD support | API / implementation basis |
|---|---|---|
| Typed object properties | Native | `obj.addProperty("App::PropertyLength", ...)`, plus Angle, Bool, Enumeration, String, and Link properties. |
| Derived values | Native | `obj.setExpression("InnerWidth", "Width - 2 * WallThickness")`; FreeCAD recomputes dependencies. Spreadsheet aliases are also suitable for named expressions. |
| Custom parametric object | Native extension point | A Python proxy can be assigned to a document object and implement `execute(self, fp)` to regenerate `fp.Shape`. |
| Feature hierarchy | Native | `App::Part`, `App::DocumentObjectGroup`, `Part::Feature`, and `PartDesign` objects provide model-tree structure. |
| Geometry validity | Native | `shape.isValid()` and `shape.check()` are present on `Part.TopoShape`. |
| Boolean operations | Native | `shape.common(other)`, `cut`, `fuse`, and `multiFuse`. |
| Collision evidence | Derived from native APIs | Intersection volume is `shape_a.common(shape_b).Volume`. A zero result alone is not proof of clearance when shapes touch. |
| Clearance measurement | Native | `shape_a.distToShape(shape_b)` returns minimum distance and closest-point information. |
| Mass / envelope measures | Native | `shape.Volume`, `shape.Area`, and `shape.BoundBox` with `XLength`, `YLength`, and `ZLength`. |
| Generic declarative DRC engine | Not native | Must be implemented by this MCP extension. |
| Domain profiles, severity, remediation | Not native | Must be implemented by this MCP extension. |

### Implications

The extension must not replace FreeCAD expressions or geometry operations with a parallel solver. It should declare properties and expressions on FreeCAD objects, invoke FreeCAD recomputation, then evaluate MCP rules over the resulting shapes and values.

## Design principles

- FreeCAD owns geometry, units, expressions, and recomputation.
- The MCP owns the component contract, named feature mapping, rule definitions, profiles, and result schema.
- All rule references use stable component parameter and feature IDs, never face or edge indices.
- Validation is deterministic for a fixed document and parameter set.
- Rebuild and validation are transactional: a failed rebuild cannot replace the last valid component output.
- Rules report errors, warnings, and informational findings; they do not silently mutate user parameters.

## Component model

Each component is a native `App::Part` container.

```text
App::Part — component container
├── App::FeaturePython / parameter host
│   ├── input properties
│   └── derived properties with FreeCAD expressions
├── App::DocumentObjectGroup — generated features
├── App::DocumentObjectGroup — construction / helper geometry
├── App::DocumentObjectGroup — validation metadata
└── App::DocumentObjectGroup — variants
```

The parameter host exposes FreeCAD-native typed properties. The MCP maintains an adjacent JSON metadata record for parameter descriptions, bounds, unit policy, feature IDs, rule profiles, and variants. This metadata is required because FreeCAD property types do not natively represent all MCP-level concerns such as severity and suggested remediation.

## Unified parameter registry

Inputs and computed values share one namespace. Each parameter has a `kind` of `input` or `derived`.

```json
{
  "parameters": [
    {
      "name": "width",
      "kind": "input",
      "type": "length",
      "default": "180 mm",
      "min": "120 mm",
      "description": "External body width"
    },
    {
      "name": "wall_thickness",
      "kind": "input",
      "type": "length",
      "default": "12 mm",
      "min": "6 mm"
    },
    {
      "name": "inner_width",
      "kind": "derived",
      "type": "length",
      "expression": "$width - 2 * $wall_thickness",
      "description": "Usable internal width"
    }
  ]
}
```

Implementation mapping:

```python
host.addProperty("App::PropertyLength", "Width", "Inputs")
host.addProperty("App::PropertyLength", "WallThickness", "Inputs")
host.addProperty("App::PropertyLength", "InnerWidth", "Derived")
host.setExpression("InnerWidth", "Width - 2 * WallThickness")
```

Input parameters may be updated. Derived parameters are read-only through MCP calls. The MCP must reject cycles before calling `setExpression`, retain original expressions, and return evaluated values after `doc.recompute()`.

## Feature contract

Each generated feature has:

- A stable component-local ID, e.g. `front_wall`.
- A unique FreeCAD object name.
- A human-readable label.
- A role, such as `output`, `construction`, `tool`, or `inspection`.
- Optional material and display properties.
- A stable mapping to its resulting `Part.TopoShape`.

The MCP uses this mapping for validation. Rules may address whole objects only in v1; face/edge selectors are explicitly out of scope because topological naming can change after regeneration.

## API

### `create_component`

Creates the `App::Part`, parameter host, typed properties, expression bindings, and metadata record.

```json
{
  "document": "BirdHouse",
  "name": "BudgerigarHouse",
  "label": "Parametric Budgerigar House",
  "parameters": [
    {"name": "width", "kind": "input", "type": "length", "default": "180 mm"},
    {"name": "wall_thickness", "kind": "input", "type": "length", "default": "12 mm"},
    {
      "name": "inner_width",
      "kind": "derived",
      "type": "length",
      "expression": "$width - 2 * $wall_thickness"
    }
  ]
}
```

### `define_component`

Stores a declarative feature graph and creates or updates generated FreeCAD features. The MCP compiles graph expressions into FreeCAD property expressions where possible; it uses the FeaturePython proxy only for operations that require procedural shape construction.

### `set_component_parameters`

Sets only `input` parameters, recomputes the document, and validates the affected component.

```json
{
  "component_id": "component://BirdHouse/BudgerigarHouse",
  "values": {"width": "200 mm", "entrance_diameter": "42 mm"},
  "validate": true
}
```

The response includes changed input values, evaluated derived values, generated feature IDs, and validation findings. Attempts to set `derived` values return `PARAMETER_READ_ONLY`.

### `validate_component`

Evaluates baseline rules plus selected profiles and custom component rules.

```json
{
  "component_id": "component://BirdHouse/BudgerigarHouse",
  "profiles": ["geometry_baseline", "cnc_plywood"],
  "rule_ids": ["perch_connection"],
  "include_measurements": false
}
```

### `get_component`

Returns schema metadata, input values, derived expressions and evaluated values, generated feature state, current profile selection, and last validation result.

## Rule engine

The rule engine runs after a successful `doc.recompute()`. It receives a resolved component context:

```text
component metadata
→ typed parameter values and derived values
→ named output FreeCAD objects
→ Part.TopoShape measurements
→ structured findings
```

### Rule families

| Family | Native API used | Rule examples |
|---|---|---|
| Parameter | Property values and expression status | required, min/max, enum, no cyclic derived parameters |
| Shape integrity | `isValid()`, `check()`, solid count | `geometry_valid`, `requires_solid` |
| Intersection | `common()` and resulting `Volume` | `must_intersect`, `must_not_intersect`, `minimum_overlap` |
| Clearance | `distToShape()` | `minimum_clearance`, `maximum_gap` |
| Envelope | `BoundBox`, `Volume`, `Area` | maximum dimensions, minimum cavity dimensions |
| Feature-specific | named parameters and feature roles | minimum wall thickness, minimum hole diameter, roof overhang |
| Process profile | combinations of the above | CNC minimum tool radius, FDM wall thickness, sheet-stock thickness |
| Domain profile | combinations of the above | birdhouse entrance range and cleanout access |

### Rule definition schema

```json
{
  "id": "perch_connection",
  "type": "must_intersect",
  "severity": "error",
  "features": ["perch", "front_wall"],
  "minimum_overlap": "6 mm",
  "message": "The perch must enter the front wall by at least 6 mm."
}
```

```json
{
  "id": "minimum_roof_overhang",
  "type": "parameter_minimum",
  "severity": "warning",
  "parameter": "roof_overhang",
  "minimum": "10 mm"
}
```

### Collision semantics

The engine must not label every intersecting pair a collision. Each rule declares intended behavior:

- `must_not_intersect`: `common().Volume` must be below tolerance.
- `must_intersect`: intersection volume must be greater than tolerance.
- `minimum_overlap`: intersection must meet a declared depth or volume threshold.
- `minimum_clearance`: `distToShape()` must be at least the declared distance.
- `touching_allowed`: zero distance is permitted but positive intersection volume is not.

Tolerances are explicit and use document units. Default geometry tolerance must be configurable and included in every result.

## Built-in profiles

Profiles are versioned metadata bundles, not hard-coded product assumptions.

### `geometry_baseline`

- All output shapes pass `isValid()`.
- All output features produce at least one solid when marked `requires_solid`.
- Boolean tools intersect their intended bases.
- Parameter graph has no expression cycles.

### `cnc_plywood`

- Minimum wall thickness.
- Allowed stock thickness list.
- Minimum hole diameter.
- Minimum inside corner radius equal to the configured cutter radius.
- Optional material-edge distance for holes and fasteners.

### `budgerigar_nest_box`

- Entrance diameter configurable within the species-specific range.
- Minimum internal width and depth.
- Minimum entrance-to-floor distance.
- Roof must be tagged `removable` or `hinged`.
- Configured minimum roof overhang.

The profile provides defaults only. User-supplied values override it, and the result records the active profile version and resolved thresholds.

## Transaction and failure behavior

1. Snapshot current component metadata and generated output object shapes.
2. Validate MCP-level parameter input and rule configuration.
3. Apply input properties and expressions.
4. Call `doc.recompute()`.
5. Detect recompute errors and invalid required output shapes.
6. If rebuild fails, restore the prior snapshot and emit an error result.
7. If rebuild succeeds, commit the new state and run validation.

Warnings never roll back geometry. Errors in geometry generation or expression resolution do.

## Result schema

```json
{
  "component_id": "component://BirdHouse/BudgerigarHouse",
  "build_status": "success",
  "validation_status": "warning",
  "tolerance": "0.01 mm",
  "summary": {"errors": 0, "warnings": 1, "passed": 12},
  "findings": [
    {
      "id": "minimum_roof_overhang",
      "severity": "warning",
      "rule": "parameter_minimum",
      "parameter": "roof_overhang",
      "actual": "8 mm",
      "required": "10 mm",
      "message": "Roof overhang is below the configured minimum.",
      "suggested_parameter_change": {"roof_overhang": "10 mm"}
    }
  ]
}
```

## Non-goals for v1

- Inferring design intent from arbitrary unlabelled FreeCAD documents.
- Reliably persisting references to arbitrary face and edge indices across topological changes.
- Replacing workbench-specific CAM, FEM, or sheet-metal validation.
- Claiming biological, legal, or safety compliance without an explicit, versioned external rule source.

## Acceptance criteria

- `create_component` creates FreeCAD-native typed properties and expression-backed derived values.
- A change to an input parameter recomputes dependent features through FreeCAD's dependency system.
- `validate_component` supports validity, intersection, clearance, envelope, and parameter rules with stable named references.
- Every validation finding has severity, rule ID, stable target, measured/expected value where applicable, and actionable text.
- The engine distinguishes intended joins from unintended part collisions.
- Failed regeneration retains the last valid component geometry and metadata.
- Results remain compact by default; topology and raw measurement detail are opt-in.
