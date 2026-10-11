# bambu-mcp

MCP server that drives the **Bambu Studio CLI** to cut print time and filament use. No Bambu Studio changes
needed. Maintained in `freecad_mcp/tools/bambu-mcp` as a standalone server alongside FreeCAD MCP.
See the [design and rationale](../../docs/bambu-mcp/optimizer-spec.md).

## Install & register

```bash
cd tools/bambu-mcp
uv venv --python 3.12 && uv pip install -e '.[test]'
claude mcp add bambu -e BAMBU_STUDIO_BIN=/Applications/BambuStudio.app/Contents/MacOS/BambuStudio \
  -- "$PWD/.venv/bin/bambu-mcp"
```

Env: `BAMBU_STUDIO_BIN` (binary), `BAMBU_MCP_HOME` (cache + sessions, default `~/.bambu-mcp`).

Built on [FastMCP](https://gofastmcp.com) 4.x. Every tool declares MCP annotations (`readOnlyHint`,
`destructiveHint`, `idempotentHint`, `openWorldHint`, `title`), a typed input schema with per-field
descriptions and bounds, and a typed output schema (structured content). `tests/test_server_schema.py` fails
if a tool loses any of these.

## Tools

| Tool | Purpose |
|---|---|
| `studio_info`, `presets_list`, `parameters_catalog` | environment, preset names, tunable knobs |
| `project_open`, `project_inspect` | open .3mf (embedded settings) or mesh + presets; list objects |
| `project_validate` | optional read-only lib3mf check of source mesh validity/manifoldness; separate strict compliance and vendor warnings |
| `settings_set` / `settings_reset` | global or per-object overrides (validated; G-code keys refused) |
| `object_transform`, `orient`, `arrange` | per-object rotation + drop to bed, auto-orient, auto-arrange |
| `slice`, `evaluate_batch`, `compare` | time / grams / per-role time; parallel; cached by variant hash |
| `compare_project` | isolated native whole-assembly candidate comparison, print constraints and Pareto reports |
| `optimize` | staged search: orientation → support → infill → shells → layer height; adhesion opt-in |
| `export`, `open_in_studio` | write sliced 3MF; open in the GUI |

The source file is never modified; each session works on a copy.

For independent geometry inspection, install `uv pip install -e '.[validation]'`
in the server environment and call `project_validate(session_id)`. It inspects the
session source, excluding pending variant changes, in a bounded subprocess.
`geometry_valid` covers lib3mf validity/manifold checks; it does not certify
self-intersection freedom or printability. Valid Bambu projects commonly return
`needs_attention` because vendor metadata fails strict compliance. This advisory
result never rejects a slice or changes optimizer ranking.

Slice metrics and comparison reports include per-plate `toolpath_profiles` from
Bambu comments: declared extrusion heights and feature-block counts, with a
`complete` flag and a 64 MiB scan limit. Heights can include combined infill or
support thickness and are not the nominal layer ladder. Counts are neither mass
nor simulated motion. See [measured tool evaluations](../../docs/bambu-mcp/tool-evaluation.md).

## Test

```bash
.venv/bin/pytest -m "not slow"   # unit
.venv/bin/pytest -m slow         # slices real models with the installed binary
```

## Known limits

- Orientation candidates are the 6 axis-aligned poses + Bambu auto-orient; no face-based candidates yet.
- One object's pose changes per orientation stage (multi-object: run the stage again).
- Z-drop reads every vertex in pure Python; very large meshes are slow.
- Search is greedy coordinate descent, not Bayesian.
- Sessions live in memory; the run cache survives restarts.

## Maintenance ownership

This repository owns the Python package, MCP schemas, optimizer, tests, and docs.
Bambu Studio remains an external executable; installation needs no sibling source checkout.
The package and imported fixtures retain the upstream AGPL license in [LICENSE](LICENSE);
this subtree's license is separate from the repository root's MIT license.

The unit/schema suite runs in `.github/workflows/bambu-mcp.yml`. Before accepting
CLI, preset, 3MF, or optimizer changes, also run the slow suite against the supported
installed binary and report its version. Keep predicted savings separate from physical
print evidence. The current integration fixtures target A1 mini 0.4 mm with PLA Basic.

`tests/fixtures` contains the local 3MF fixture and a source-backed option-name snapshot
with provenance. Neither unit tests nor CI read the BambuStudio source tree.

Settings resolution covers catalog keys only. `settings_errors` means resolution is
incomplete. Functional floors exempt unchanged baseline values; they do not certify
strength. Adhesion is opt-in, brim removal requires `functional=false`, and auto-orient
with locked objects is rejected. Batch rows include their input index and full variant.

## Migration from BambuStudio

Reinstall from this directory into a fresh virtual environment; do not move an old venv
(editable installs and launchers embed absolute paths). Update any MCP registration
pointing at `BambuStudio/tools/bambu-mcp` to this checkout. The command remains
`bambu-mcp`; `BAMBU_MCP_HOME` and existing run caches remain compatible.

## Unified print pipeline

See [the shared specification and workflow](../../docs/bambu-mcp/unified-pipeline.md).
`compare_project` compares native whole-assembly projects with presets, cached
parallel slices, strict plate/G-code checks, and fastest/least-material/conservative
alternatives. Session `optimize(print_spec=...)` uses the same specification and
returns additional alternatives in `report`. The FreeCAD bridge selects this
backend with `compare_print_candidates(backend="bambu", print_spec=...)`.

Shared `print_pipeline_core` modules are MIT-licensed (their LICENSE is bundled),
while `bambu_mcp` and the imported calibration fixture retain the subtree's AGPL
license. Unit CI exercises legacy and cross-process comparison contracts. The
slow suite also runs the five-case native-project benchmark corpus.
