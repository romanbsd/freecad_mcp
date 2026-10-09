# FreeCAD MCP (Model Control Protocol)

## Overview

The FreeCAD MCP (Model Control Protocol) provides a simplified interface for interacting with FreeCAD through a server-client architecture. This allows users to execute commands and retrieve information about the current FreeCAD document and scene.

https://github.com/user-attachments/assets/5acafa17-4b5b-4fef-9f6c-617e85357d44
## Configuration

To configure the MCP server, you can use a JSON format to specify the server settings. Below is an example configuration:

```json
{
    "mcpServers": {
        "freecad": {
            "command": "C:\\ProgramData\\anaconda3\\python.exe",
            "args": [
                "C:\\Users\\USER\\AppData\\Roaming\\FreeCAD\\Mod\\freecad_mcp\\src\\freecad_bridge.py"
            ]
        }
    }
}
```

### Configuration Details

- **command**: The path to the Python executable that will run the FreeCAD MCP server. This can vary based on your operating system:
  - **Windows**: Typically, it might look like `C:\\ProgramData\\anaconda3\\python.exe` or `C:\\Python39\\python.exe`.
  - **Linux**: It could be `/usr/bin/python3` or the path to your Python installation.
  - **macOS**: Usually, it would be `/usr/local/bin/python3` or the path to your Python installation.

- **args**: An array of arguments to pass to the Python command. The first argument should be the path to the `freecad_bridge.py` script, which is responsible for handling the MCP server logic. Make sure to adjust the path according to your installation.

### Example for Different Operating Systems

#### Windows
```json
{
    "mcpServers": {
        "freecad": {
            "command": "C:\\ProgramData\\anaconda3\\python.exe",
            "args": [
                "C:\\Users\\USER\\AppData\\Roaming\\FreeCAD\\Mod\\freecad_mcp\\src\\freecad_bridge.py"
            ]
        }
    }
}
```

#### Linux
```json
{
    "mcpServers": {
        "freecad": {
            "command": "/usr/bin/python3",
            "args": [
                "/home/USER/.FreeCAD/Mod/freecad_mcp/src/freecad_bridge.py"
            ]
        }
    }
}
```

#### macOS
```json
{
    "mcpServers": {
        "freecad": {
            "command": "/usr/local/bin/python3",
            "args": [
                "/Users/USER/Library/Preferences/FreeCAD/Mod/freecad_mcp/src/freecad_bridge.py"
            ]
        }
    }
}
```

## Features

For image-derived spray-paint templates, see the reusable
[stencil skill](.agents/skills/stencil/SKILL.md) (`$stencil`).

The FreeCAD MCP exposes these tools, plus an MCP resource `freecad://guide/cookbook` with working FreeCAD scripting idioms the agent can read before writing geometry code:

### 1. `execute`

- **Description**: Executes Python inside the running FreeCAD instance. The namespace has `App` (FreeCAD), `Gui` (FreeCADGui) and `doc` (active document).
- **Returning data**: assign to `result` or `print()` — both are captured and returned (`result` and `stdout`).
- **Safety/freshness**: the action runs inside one undo transaction (revert with a single Ctrl-Z; aborted automatically on error) and the document is recomputed afterwards. Objects left in an error state come back as `recompute_errors`.
- **`return_context`** (default `False`): when `True`, also returns a document summary:
  - Document properties (name, filename, object count)
  - Per-object info (name, label, type, visibility, placement, and shape type/volume/area when present) — best-effort per object; one that fails to introspect reports an `error` field instead of aborting the whole dump
  - View/camera state (best-effort — `error` note if the Coin/pivy bindings aren't loaded)

### 2. `get_screenshot`

- **Description**: Captures the active 3D view as a PNG and returns it as an image so the model can see the model. Params: `width`/`height` (default 1024×768), `view` (one of `iso`, `front`, `rear`, `top`, `bottom`, `left`, `right`, or `current`; default `iso`), and `fit` (zoom-to-fit before capture, default `True`).

### 3. `list_objects`

- **Description**: Lists every object in the active document (name, label, type) — a cheap overview.

### 4. `get_object`

- **Description**: Full detail for one object (by `name`): all properties, validity/state, and shape bounding box + topology counts (vertexes/edges/faces/solids) when present.

### 5. `export`

- **Description**: Exports objects (`names`) to a file at `path`; the format is chosen by extension — `.step`/`.stp`, `.iges`/`.igs`, `.brep`/`.brp`, or `.stl`.

### 6. `list_types` / `describe_type`

- **`list_types(filter="")`**: creatable object TypeIds (what you can pass to `addObject`), optionally substring-filtered (e.g. `Part::`).
- **`describe_type(type_id)`**: property schema for a type — each property's type, group, doc string and enum options. Lets the agent learn the API before writing code, version-correct from the running instance.

### 7. `measure`

- **Description**: With one object, returns volume/area/center-of-mass/bbox; with two, the minimum distance between them and the closest points.

### 8. `get_selection` / `set_selection`

- **Description**: Read or replace the current FreeCAD selection (objects and sub-elements like `Edge1`/`Face2`, for targeting fillets/chamfers). GUI-only.

### 9. `get_subelements`

- **Description**: Returns a shape's edges (`Edge1..N` with curve type, length, center) and faces (`Face1..N` with area, center), or a Sketcher sketch's `Geometry` and `Constraints`. Use the edge/face names to fillet/chamfer by index or to `set_selection`.

## Parametric components

A higher-level declarative layer (see `parametric-spec.md`) for building reusable parametric models without embedding Python. It is a thin wrapper over FreeCAD's native expression + dependency engine, so unit math, derived values, incremental rebuild, and dependency tracking are FreeCAD's own.

A component is an `App::Part` container with a typed **parameter registry** (each parameter is either `input` — concrete with `default`/`min`/`max`/`enum` — or `derived` — read-only with an `expression`) and groups of generated features. Tools:

- **`create_component(document, name, label, parameters)`** — container + parameter registry.
- **`define_component(component_id, features, rules, profiles)`** — the build graph. Feature types: `box`, `cylinder`, `cone`, `prism`, `transform`, `cut`, `union`, `intersection`, `array`, `group`. Sizes/positions are expressions over `$param` with units (`"$width / 2"`, `"$wall_thickness + 2 mm"`); booleans/arrays reference other features by id. Each feature may set a `role` (`output`/`construction`/`tool`/`inspection`, routing it to the right group) and `tags`. Optional `rules` (custom design-rule definitions) and default `profiles` are stored with the component. A build that produces invalid geometry is rolled back, leaving the prior model intact.
- **`set_component_parameters(component_id, values, rebuild, validate)`** — update inputs; rebuilds only affected features; returns changed/regenerated/validation/bbox. Derived params are read-only.
- **`get_component(component_id)`** — registry + current values + dependency summary + variants + last validation.
- **`create_component_variant(component_id, name, values)`** — a named override set.
- **`validate_component(component_id, profiles, rule_ids, include_measurements, tolerance)`** — a declarative design-rule-check engine (read-only). Runs built-in **profiles** (`geometry_baseline`, `cnc_plywood`) plus the component's custom rules (filter with `rule_ids`) — domain-specific checks (e.g. a nest-box's entrance range and removable roof) are expressed as custom rules, not baked-in profiles. Rule types span parameter (`required`/`parameter_minimum`/`parameter_maximum`/`enum`), shape (`geometry_valid`/`requires_solid`), intersection (`must_intersect`/`must_not_intersect`/`minimum_overlap`/`touching_allowed`), clearance (`minimum_clearance`/`maximum_gap`), and envelope. Returns `build_status`, `validation_status`, the `tolerance` used (document units, configurable), a `summary` count, and `findings` carrying `severity`, `actual`/`required`, `message`, and `suggested_parameter_change`.
- **`render_component(component_id, view, section, hide_features, width, height)`** — image, optional cross-section (`{"plane":"XZ","offset":"90 mm"}`).
- **`export_component(component_id, path, format, variant)`** — `FCStd`, `STEP`, `STL`, `IGES`, `BREP`.

### Example Usage

The server speaks a length-prefixed framing protocol: each message (both
directions) is a 4-byte big-endian length followed by that many bytes of
UTF-8 JSON. A minimal client:

```python
import socket
import json

def _recv(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            raise ConnectionError("closed early")
        buf += chunk
    return bytes(buf)

def call(command):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect(('localhost', 9876))
        payload = json.dumps(command).encode('utf-8')
        s.sendall(len(payload).to_bytes(4, 'big') + payload)
        length = int.from_bytes(_recv(s, 4), 'big')
        return json.loads(_recv(s, length).decode('utf-8'))

# Execute code; return a value via `result` and read the document context back
print(call({
    "type": "execute",
    "params": {
        "code": (
            "doc = App.ActiveDocument or App.newDocument()\n"
            "box = doc.addObject('Part::Box', 'MyBox'); box.Length = 20\n"
            "result = box.Shape.Volume\n"
        ),
        "return_context": True,
    },
}))

# Capture the 3D view (image_base64 in the response)
print(call({"type": "get_screenshot", "params": {"width": 800, "height": 600}}).keys())
```

See `test_e2e.py` for a runnable version of this against a live FreeCAD.

## Installation

1. Clone the repository or download the files.
2. Place the `freecad_mcp` directory in your FreeCAD modules directory:
   - Windows: `%APPDATA%/FreeCAD/Mod/`
   - Linux: `~/.FreeCAD/Mod/`
   - macOS: `~/Library/Preferences/FreeCAD/Mod/`
3. Restart FreeCAD and select the "FreeCAD MCP" workbench from the workbench selector.

## Contributing

Feel free to contribute by submitting issues or pull requests. Your feedback and contributions are welcome!

## License

This project is licensed under the MIT License. See the LICENSE file for details.
# Incremental parametric API

The MCP server supports compact, revisioned component workflows in addition to raw
FreeCAD Python execution:

- `build_component` creates, defines, validates, and exports in one request.
- `get_component_graph` returns a summary or editable full graph with its revision.
- `patch_component` supports dry runs and optimistic concurrency.
- `list_feature_types`, `list_patterns`, and `expand_pattern` provide on-demand schema
  discovery.
- `check_fit` reports containment, axis clearances, interference, and unrestrained
  translations.
- `export_bundle` writes assembly and per-output artifacts in several formats.

Declarative features include `tube` and `grid_array`. Version 1 patterns include
`lego.stud_grid` and `lego.brick_underside`; patterns expand into ordinary graph nodes
so they remain inspectable and patchable.

## API 2.9 capabilities

The current server exposes a substantially broader FreeCAD surface than the original
script-only bridge. Call `capabilities` to discover the running FreeCAD version,
supported feature types, patterns, validation profiles, and response format; use
`describe_feature_type` for the machine-readable schema of any declarative feature.

### Direct document tools

In addition to `execute`, `get_screenshot`, and document inspection, the bridge provides
focused operations that avoid a one-off Python script for common edits:

- `transform` moves or rotates an object, optionally into a copy.
- `set_property` updates scalar, string, boolean, and enum data properties and recomputes.
- `fillet` and `boolean` apply edge treatments and cut/fuse/common operations.
- `duplicate` creates single copies or linear/polar arrays, optionally as a compound or fuse.
- `measure` reports volume, area, center of mass, bounding boxes, minimum distance, closest
  points, and overlap/clash volume.
- `get_selection` / `set_selection` work with the GUI selection, while `get_subelements`
  exposes indexed edges, faces, sketch geometry, and constraints for precise targeting.

`get_screenshot` now supports standard and current camera views, target visibility,
temporary transparency, custom camera/view sequences, dimension caps, and wireframe
captures. `execute` keeps a persistent helper namespace, runs as one undo transaction,
recomputes by default, and can return hardened document/object/view context plus
recompute errors.

### Declarative modeling

Components are backed by FreeCAD's native expression and dependency engine. The feature
catalog includes:

- primitives and structure: `box`, `cylinder`, `tube`, `cone`, `prism`, `datum_frame`,
  `transform`, booleans, `group`, and linked `component_instance` features;
- profiles and solid operations: `profile_2d`, `sketch_2d`, `profile_extrude`, `pad`,
  `pocket`, `extrude_cut`, `revolve`, `sweep`, and `loft`;
- manufacturing features: `hole`, `shell`, `draft`, `rib`, `face_offset`,
  `replace_face`, `fillet`, and `chamfer`;
- patterns: unified linear, grid, polar, mirror, and path patterns, with legacy aliases
  (`array`, `grid_array`, `linear_pattern`, `polar_pattern`, `mirror_pattern`, and
  `path_pattern`). Use `list_patterns` and `expand_pattern` to inspect or expand
  server-owned patterns into ordinary editable graph nodes.

Features share common placement, frame, support, selector, role, and tag fields. Reusable
geometry-only `profile_definitions` and named datum frames can be referenced without
creating unnecessary document objects. `build_component` combines creation, definition,
validation, and optional export in one request.

### Validation, references, and diagnostics

Components can carry custom design rules and use the built-in `geometry_baseline`,
`cnc_plywood`, and `fdm` profiles. `validate_component` is read-only and returns build
and validation status, severity-ranked findings, actual/required values, optional
measurements, and suggested parameter changes. `check_fit` verifies containment,
clearance, retainer interference, and sampled unrestrained translations.

Named face, edge, vertex, and solid references are tracked across rebuilds. Use
`list_geometry_references` and `resolve_geometry_reference` to inspect stable,
remapped, ambiguous, missing, or untracked references. Native constrained sketches
report solver status, degrees of freedom, constraint errors, and named geometry maps via
`get_sketch_status`.

The component workflow is revisioned and dependency-scoped:

- `patch_component` applies optimistic-concurrency structural edits;
- `preview_parameter_update` and `validate_patch` run the real rebuild path and abort
  before committing changes;
- `explain_rebuild` reports parameter and structural impact without rebuilding;
- `get_feature_diagnostics` reports feature, shape, expression, reference, sketch, and
  mate health.

Preview operations do not overwrite the component's stored validation result. Use
`get_component_graph(detail="full")` when an editable graph is needed, and
`get_component` for the compact registry, current values, variants, dependency summary,
and latest committed validation.

### Assemblies and outputs

Assemblies support grounded instances and deterministic multi-constraint mates, including
rigid/coincident/offset/angle plus `revolute`, `slider`, `cylindrical`, and `planar`
modes. `get_assembly_status` reports constraint count/rank, degrees of freedom, solver
state, residuals, and bounds; `refresh_assembly` refreshes source instances and reapplies
mates in dependency order.

Use `create_component_variant` for named parameter overrides, `render_component` for
standard views or optional plane/offset cross-sections, `export_component` for FCStd,
STEP, STL, IGES, or BREP, and `export_bundle` for multi-format, per-output, and assembly
exports.

Bridge tools return native MCP objects rather than JSON encoded inside strings. Clients
that parsed the old string response should remove that extra JSON decoding step.
