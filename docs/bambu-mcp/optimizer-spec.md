> Migrated to `freecad_mcp` on 2026-10-11. This is the historical BambuStudio
> design document; C++ paths refer to upstream source, not files in this repository.
> The maintained package is `tools/bambu-mcp`. Its README and source describe
> current behavior; proposed features below are not implementation guarantees.

# Bambu Studio MCP Optimizer — Specification & Implementation Plan

Status: draft v1 · Date: 2026-10-10 · Target: BambuStudio master @ `47a13eeb1`

## 1. Goal

An MCP (Model Context Protocol) server that lets an LLM agent (Claude Code, Claude Desktop, any MCP client)
**inspect a Bambu Studio project, change print parameters, orientation and placement, slice, measure,
and iterate** to cut **print time** and **material use** while staying inside quality constraints the user sets.

Non-goals (v1):
- Sending jobs to printers / cloud (network plugin, MQTT). Out of scope; risky, separate concern.
- Quality evaluation of G-code beyond what the slicer reports (no FEA/strength simulation).
- Replacing the GUI. The GUI stays the place where a human reviews and prints.

## 2. Key finding: no Bambu Studio changes are required for v1

Bambu Studio has no general plugin API (the only dynamically loaded module is the closed `bambu_networking`
network agent, `src/slic3r/Utils/NetworkAgent.cpp`, a fixed C ABI for cloud/printer traffic — not usable as an
extension point). But the **same binary has a full headless CLI** that already exposes everything an optimizer
needs. The MCP server therefore lives **outside** Bambu Studio and drives the CLI as a black-box evaluator.

What the CLI already gives us (verified in source):

| Need | Existing CLI capability | Source |
|---|---|---|
| Headless mode | Binary runs CLI when any action flag is given; GUI only if `m_actions.empty()` | `src/BambuStudio.cpp:1642` |
| Load project / meshes | Positional `file.3mf` / `file.stl` inputs | `src/BambuStudio.cpp:8509` |
| Load presets | `--load_settings "machine.json;process.json"`, `--load_filaments "a.json;b.json"` | `src/libslic3r/PrintConfig.cpp:10247-10255` (CLIMiscConfigDef) |
| **Override any print/printer/filament key** | Any `PrintConfig` key on the command line, e.g. `--sparse_infill_density 10% --wall_loops 2`. Parsed by `m_config.read_cli()` into the full config, applied last | `src/BambuStudio.cpp:8446`, `:4136` |
| Override precedence | CLI > `--load_settings/--load_filaments` > values in 3MF | `src/BambuStudio.cpp:8518-8521` (help text) |
| Orientation | `--orient 1` (force auto-orient all), `--rotate`, `--rotate_x`, `--rotate_y` (deg, **applies to all objects**) | `src/BambuStudio.cpp:5036-5146` |
| Placement | `--arrange 1`, `--allow_rotations`, `--avoid_extrusion_cali_region`, `--allow_multicolor_oneplate`, `--ensure_on_bed` | `src/BambuStudio.cpp:5114`, `PrintConfig.cpp` CLIMiscConfigDef |
| Scale / copies | `--scale`, `--repetitions`, `--clone_objects`, `--skip_objects` | same |
| Slice | `--slice 0` (all plates) / `--slice N` | `PrintConfig.cpp:10010` |
| Outputs | `--export_3mf out.3mf` (sliced 3MF with G-code), `--outputdir`, `--export_settings`, `--export_png`, `--min_save` | CLIActionsConfigDef |
| **Machine-readable metrics** | Every run writes `<outputdir>/result.json`: return code, error string, per plate `total_predication` (s), `main_predication`, `filament_change_times`, per-filament `total_used_g`, `feature_type_times` (time per extrusion role: walls, infill, support, prime tower…), objects with bbox, warnings, timings, `peak_rss_mb` | `record_exit_reson()` `src/BambuStudio.cpp:488-640` |
| Guardrails | `--mstpp` max slicing seconds per plate, `--mtcpp` max triangles per plate, `--no_check` | CLIActionsConfigDef |
| Slicing cache | `--export_slicedata dir` / `--load_slicedata dir` (`obj_cached_cnt` in result) | CLIActionsConfigDef |
| Hand result back to GUI | Running `bambu-studio file.3mf` while the GUI is open (single-instance mode) forwards the path to the running instance, which loads it | `OtherInstanceMessageHandler::handle_message` `src/slic3r/GUI/InstanceCheck.cpp:488` → `EVT_LOAD_MODEL_OTHER_INSTANCE` `src/slic3r/GUI/Plater.cpp:8017` |

The one gap: **per-object** orientation/placement. CLI rotations apply to every object. We close the gap by
editing the 3MF package directly (it is a zip of XML) before handing it to the CLI — see §6.3. Still no source change.

A GUI seam (live, in-editor control) is optional and specified in §9 as phase 2 with the smallest change found.

## 3. Architecture

```
┌──────────────┐  MCP (stdio)   ┌─────────────────────────────────────────────┐
│  MCP client  │◄──────────────►│  bambu-mcp (Python 3.11+, `mcp` SDK)        │
│ (Claude etc.)│                │                                             │
└──────────────┘                │  tools.py      MCP tool/resource handlers   │
                                │  session.py    working copy + overrides     │
                                │  threemf.py    read/patch 3MF zip (XML)     │
                                │  geometry.py   mesh z-drop, candidate axes  │
                                │  runner.py     CLI invocation, pool, cache  │
                                │  optimize.py   search strategies, scoring   │
                                │  catalog.yaml  curated parameter space      │
                                └───────────────┬─────────────────────────────┘
                                                │ subprocess (one per evaluation)
                                                ▼
                                ┌─────────────────────────────────────────────┐
                                │ BambuStudio binary, CLI mode                │
                                │  in.3mf + --load_settings + --key value ... │
                                │  --slice N --outputdir run/ --export_3mf    │
                                │  ──► run/result.json, run/out.3mf           │
                                └─────────────────────────────────────────────┘
                                                │ optional: `bambu-studio best.3mf`
                                                ▼
                                   running Bambu Studio GUI loads the result
```

Design principles:
- **Slicer is the oracle.** Never estimate time/material ourselves; always slice and read `result.json`.
- **Immutable inputs.** The user's file is never modified. Each session works on a copy; each evaluation gets
  its own run directory. Export is an explicit tool call.
- **Variants are data.** A variant = `{base project, global overrides, per-object overrides, per-object transforms,
  CLI transform flags}`. It hashes to a cache key; identical variants are never sliced twice.
- **Stateless CLI, stateful server.** All state (sessions, variants, results) lives in the server's workspace dir
  as JSON, so a crashed server can resume.

### 3.1 Language & dependencies

Python: official MCP SDK has FastMCP; stdlib covers zip/XML/subprocess/concurrency.

| Dependency | Why | Alternative rejected |
|---|---|---|
| `fastmcp` (>=4) | MCP server framework (typed schemas, annotations) | hand-rolled JSON-RPC: pointless |
| `numpy` | transform matrices, vertex min-z after rotation | pure Python: too slow on 1M-triangle meshes |
| `pyyaml` | load `catalog.yaml` | JSON works too; YAML chosen for comments in catalog. Drop if unwanted. |

No `trimesh`/`lib3mf`: the 3MF mesh XML is simple (`<vertex x y z>`), we only need vertices and the build/component
transforms. `zipfile` + `xml.etree.ElementTree` (iterparse for big meshes) suffice.

## 4. Locating the binary & environment

Resolution order:
1. `BAMBU_STUDIO_BIN` env var.
2. Platform defaults:
   - macOS: `/Applications/BambuStudio.app/Contents/MacOS/BambuStudio`
   - Windows: `%ProgramFiles%\Bambu Studio\bambu-studio.exe`
   - Linux: `bambu-studio` on `PATH`, AppImage path from env.
3. Local build: `<repo>/build/src/**/BambuStudio[.exe]` if `BAMBU_STUDIO_REPO` set.

Startup check (`studio_info` tool): run `<bin> --help` with 20 s timeout; fail fast with a clear message if the binary
is missing or the output lacks `--slice` (wrong binary).

Presets: system profiles ship in `resources/profiles/BBL/{machine,process,filament}/*.json`
(next to the binary: `Contents/Resources/profiles` on macOS). User presets live in the data dir
(`~/Library/Application Support/BambuStudio/user/<uid>/...`, `%APPDATA%\BambuStudio\user\...`, `~/.config/BambuStudio/user/...`).
The server indexes both read-only for `presets_list`. System profiles use `inherits` chains; the CLI resolves them,
so we pass file paths and never flatten ourselves.

## 5. MCP surface

All tools return JSON. Errors use MCP tool errors with `{code, message, hint}`; slicer failures include the CLI
`return_code`, `error_string` and the last 40 lines of stderr.

### 5.1 Tools

#### Session & inspection

| Tool | Input | Output |
|---|---|---|
| `studio_info` | — | binary path, version line, profiles dir, data dir, worker count |
| `presets_list` | `kind: machine\|process\|filament`, `filter?` | `[{name, path, inherits, printer_model, nozzle}]` |
| `project_open` | `path` (3mf/stl/step/obj…), `machine_preset?`, `process_preset?`, `filament_presets?[]` | `session_id`, summary (see `project_inspect`) |
| `project_inspect` | `session_id` | plates; objects `{id, name, plate, bbox, triangles, transform, per_object_overrides}`; filaments; effective values for catalog keys; last baseline metrics if any |
| `settings_get` | `session_id`, `keys?[]` or `group?` (`speed`,`strength`,`support`,…), `object_id?` | `{key: {value, source: 3mf\|preset\|override, type, allowed}}` |
| `parameters_catalog` | `group?`, `search?` | catalog entries (§7) — what the agent may tune and how each knob trades time/material/quality |

Effective settings come from `--export_settings <file>` run once per session (cheap: no `--slice`), plus
`Metadata/project_settings.config` and `Metadata/model_settings.config` parsed from the 3MF.

#### Editing (all operate on the session's *working variant*; nothing is sliced yet)

| Tool | Input | Notes |
|---|---|---|
| `settings_set` | `session_id`, `overrides: {key: value}`, `object_id?` | Validates against catalog type/range and enum values. Global → CLI flags. Per-object → written into `model_settings.config` (§6.3). |
| `settings_reset` | `session_id`, `keys?[]`, `object_id?` | Drop overrides. |
| `object_transform` | `session_id`, `object_id`, `rotate_deg?: {x,y,z}` (applied in that order, about object center), `scale?`, `position_xy?`, `lay_on_face?: face_index` | Patches 3MF build-item matrix; re-drops object to z=0 (§6.3). |
| `orient` | `session_id`, `object_ids?[]`, `mode: auto\|candidates`, `max_candidates=8` | `auto`: sets `--orient 1` (Bambu's support-area minimizer, `src/libslic3r/Orient.cpp`). `candidates`: returns candidate rotations (§8.2) without applying. |
| `arrange` | `session_id`, `allow_rotations=false`, `avoid_cali_region=true`, `multicolor_one_plate=true` | Sets `--arrange 1` + flags. |
| `variant_save` / `variant_load` / `variant_list` | `session_id`, `name` | Snapshot working variant; compare later. |

#### Evaluation

| Tool | Input | Output |
|---|---|---|
| `slice` | `session_id`, `plate=0`, `variant?` (name; default working) , `timeout_s=600` | `Metrics` (§5.3) + `run_id`. Cached by variant hash. |
| `evaluate_batch` | `session_id`, `variants: [{overrides?, object_overrides?, object_rotation?, flags?}]`, `plate` | Validates nonempty change sets, runs up to 32 in parallel, and returns rows sorted by print time with `input_index`, full variant, and metrics. |
| `compare` | `session_id`, `run_ids[]` | Table: Δtime, Δgrams, Δcost, Δsupport g, role-time breakdown, changed settings. |
| `optimize` | see §8 | Pareto set of variants with metrics + explanation of each change. Long-running; streams progress; honours budget. |

#### Output

| Tool | Input | Output |
|---|---|---|
| `export` | `session_id`, `run_id` or `variant`, `path`, `sliced=true` | Writes 3MF (sliced → G-code inside, ready for printer; unsliced → project). Refuses to overwrite the original input unless `overwrite=true`. |
| `open_in_studio` | `session_id`, `run_id` | Spawns `<bin> <exported.3mf>` detached; with the GUI running and single-instance enabled, the GUI loads it. Otherwise opens a new GUI window. |

### 5.2 Resources & prompts

Resources (read-only, cheap context for the agent):
- `bambu://catalog` — full parameter catalog.
- `bambu://session/{id}/summary` — `project_inspect` output.
- `bambu://session/{id}/runs` — run history table.
- `bambu://session/{id}/run/{run_id}/result.json` — raw CLI result.

Prompts:
- `optimize-for-speed` — "minimize time, keep strength defaults, material may rise ≤10%".
- `optimize-for-material` — "minimize grams (support, infill, prime tower), time may rise ≤15%".
- `draft-print` — "fast prototype: relaxed quality bounds".

### 5.3 Metrics object

```json
{
  "run_id": "r_7f3a…",
  "variant_hash": "sha256:…",
  "ok": true,
  "plate": 1,
  "time_s": 8123,                 // result.json sliced_plates[].total_predication
  "time_main_s": 7900,            // main_predication
  "filament_g": 41.7,             // Σ filaments[].total_used_g
  "filaments": [{"id":1,"filament_id":"GFA00","g":41.7}],
  "cost": 1.04,                   // g × filament_cost / 1000, from effective settings
  "filament_changes": 0,
  "role_time_s": {"Outer wall": 1500, "Sparse infill": 2200, "Support": 900, "Prime tower": 0, "...": 0},
  "support_share": 0.11,          // support role time / total (proxy for support material)
  "warnings": ["..."],
  "slice_wall_s": 34.2,
  "score": 0.81                   // only when an objective is active
}
```

Material per role is not in `result.json`; the support-material proxy is support role time. If exact grams per role
is needed, parse the sliced G-code header (`; filament used [g]`) and the `;TYPE:` blocks from `out.3mf/Metadata/plate_N.gcode`
— implemented as an opt-in (`detail=true`) because it costs a G-code scan.

## 6. Core implementation

### 6.1 Session & workspace layout

```
$BAMBU_MCP_HOME (default ~/.bambu-mcp)/
  sessions/<sid>/
    source.3mf               # copy of input; STL/STEP inputs are converted once via `--export_3mf` without --slice
    session.json             # presets, working variant, saved variants
    runs/<variant_hash>/
      in.3mf                 # patched package for this variant (only if per-object edits)
      cmd.json               # exact argv, for reproducibility
      result.json            # copied from CLI outputdir
      out.3mf                # sliced output (deleted by LRU when cache > 5 GB)
      stderr.log
```

### 6.2 Runner (`runner.py`)

```
argv = [bin,
        "--debug", "1",
        "--outputdir", run_dir,
        "--slice", str(plate),
        "--export_3mf", "out.3mf",
        "--min_save",                      # no thumbnails beyond needed → faster write
        "--mstpp", str(timeout_s),
        *(["--load_settings", ";".join(machine, process)] if presets),
        *(["--load_filaments", ";".join(filaments)] if filaments),
        *cli_transform_flags,              # --orient 1 / --arrange 1 / --allow_rotations ...
        *flatten(global_overrides),        # --key value
        in_3mf]
```

- `subprocess.run(argv, cwd=run_dir, timeout=timeout_s+30, capture_output=True)`; kill process tree on timeout.
- Success = exit 0 **and** `result.json.return_code == 0`. Otherwise map `return_code` to message via `error_string`.
- Override encoding: value types from catalog. Percent values keep `%`; vectors (per-extruder / per-filament keys
  like `nozzle_temperature`) are comma-joined; booleans `0/1`; enums by key string.
- **Concurrency:** `ThreadPoolExecutor(max_workers = max(1, min(os.cpu_count()//4, free_ram_gb//3)))`.
  Slicing is itself multithreaded (TBB), so more workers than ~cores/4 just contend. `peak_rss_mb` from past runs
  refines the RAM estimate.
- **Cache:** key = sha256(source.3mf bytes hash, preset file hashes, sorted overrides, transforms, flags, plate, binary version).
  Hit → return stored metrics, no process.
- Each worker gets its own `--outputdir`; no shared mutable files. Do **not** pass `--datadir` pointing at the user's
  live GUI data dir from parallel workers; use a per-server scratch datadir if the CLI turns out to write there (spike S2).

### 6.3 3MF patching (`threemf.py`) — the per-object seam without code changes

Bambu 3MF layout (constants in `src/libslic3r/Format/bbs_3mf.cpp:151-187`):

| Entry | Used for |
|---|---|
| `3D/3dmodel.model` | `<build><item objectid transform="m00 m01 m02 m10 … m32"/>`; objects reference `3D/Objects/object_N.model` via `<components>` (production extension) |
| `3D/Objects/*.model` | meshes (`<vertices><vertex x y z/>`) |
| `Metadata/model_settings.config` | per-object / per-part settings: `<object id><metadata key="wall_loops" value="3"/>…`, `<plate>` assignments with `<model_instance>` |
| `Metadata/project_settings.config` | flattened project config (JSON) |
| `Metadata/slice_info.config` | after slicing: per plate `prediction`, `weight` (`bbs_3mf.cpp:356-357`) — redundant with `result.json`, used as cross-check |

Operations:
1. **Per-object settings:** add/replace `<metadata key=… value=…/>` under the matching `<object id>` in
   `model_settings.config`. The CLI loads these as `ModelObject::config`, exactly like GUI per-object settings.
2. **Per-object rotation:** compose `T' = Translate(c) · R · Translate(-c) · T` where `c` is the object's bbox center in
   world space; write `T'` into the build item `transform`. Then **drop to bed**: compute min z of all vertices under
   `T'` (numpy, vectorized, iterparse to stream big meshes; cache vertices per object) and add `-min_z` to the z translation.
   `--ensure_on_bed` only lifts objects that sink below the bed, it does not lower floating ones, so the drop is ours.
3. **Lay on face:** pick a face normal `n` (from candidate list, §8.2), rotation that maps `n → (0,0,-1)` (Rodrigues),
   then step 2.
4. **Position:** set x/y translation; after any rotation, arrange is re-run unless the agent pinned positions.
5. Write a new zip, copying untouched entries byte-for-byte (`zipfile` with `ZIP_DEFLATED`). Never rewrite meshes.

Risks & mitigations:
- Assembly/multi-part objects: transform applies to the build item, parts move together — correct.
- Instances (`<item>` per instance in Bambu files): patch all items of the object, keep their relative xy.
- Plate assignment is by instance in `model_settings.config`; rotation can push an object off its plate → always
  `--arrange 1` after transforms unless the user locks placement.
- Format drift between Bambu versions: unit tests run patch → CLI load → `--export_settings`/`--info` round trip
  against fixtures produced by the current binary; fail loudly on unknown structure instead of guessing.

### 6.4 STL/STEP inputs

`project_open` on a mesh runs once: `<bin> --load_settings … --load_filaments … --arrange 1 --orient 0 --export_3mf source.3mf mesh.stl`
(no `--slice`) to get a canonical Bambu 3MF. From then on everything is 3MF-based.

## 7. Parameter catalog (`catalog.yaml`)

Rather than dumping all ~700 config keys, the server ships a curated list of knobs that move time/material, with
types, safe ranges and quality notes. The agent can still set any other key via `settings_set` with `force=true`
(validated only for syntax). All keys below verified to exist in `src/libslic3r/PrintConfig.cpp`.

```yaml
# group: geometry
layer_height:            {type: float, unit: mm, range: [0.25*nozzle, 0.75*nozzle], time: "--", material: "0", quality: "visible layer lines ↑"}
initial_layer_print_height: {type: float, range: [0.5*nozzle, 0.75*nozzle], note: "adhesion; don't lower"}
# group: strength (material + time)
wall_loops:              {type: int, range: [2, 6], default_floor: 2, time: "+", material: "+", quality: "strength ↑"}
top_shell_layers:        {type: int, range: [3, 8], floor: 3}
bottom_shell_layers:     {type: int, range: [2, 6], floor: 2}
sparse_infill_density:   {type: percent, range: [5, 40], time: "++", material: "++"}
sparse_infill_pattern:   {type: enum, values: [grid, gyroid, cubic, adaptivecubic, lightning, crosshatch, zig-zag, line, triangles, honeycomb], note: "lightning = minimum material, non-structural only"}
infill_combination:      {type: bool, time: "-", note: "infill every N layers; needs layer_height ≤ 0.5*nozzle"}
# group: support (often the largest lever)
enable_support:          {type: bool}
support_type:            {type: enum, values: ["normal(auto)", "tree(auto)", "normal(manual)", "tree(manual)"]}
support_style:           {type: enum, values: [default, grid, snug, tree_slim, tree_strong, tree_hybrid, tree_organic]}
support_threshold_angle: {type: int, unit: deg, range: [15, 60], note: "higher = fewer supports, more overhang risk"}
# group: adhesion
brim_type:               {type: enum, values: [auto_brim, brim_ears, outer_only, inner_only, outer_and_inner, no_brim]}
skirt_loops:             {type: int, range: [0, 3]}
# group: multi-material waste
enable_prime_tower:      {type: bool, note: "required for multi-color on most machines"}
prime_tower_width:       {type: float, range: [20, 60]}
flush_into_infill:       {type: bool, material: "-"}
flush_into_objects:      {type: bool, material: "-", quality: "color bleed into object"}
# group: speed (bounded by filament_max_volumetric_speed; raising beyond cap is a no-op)
outer_wall_speed:        {type: float_per_extruder, unit: mm/s, quality: "surface finish"}
inner_wall_speed:        {type: float_per_extruder}
sparse_infill_speed:     {type: float_per_extruder}
default_acceleration:    {type: float_per_extruder}
# group: misc
ironing_type:            {type: enum, values: ["no ironing", top, topmost, solid], time: "++ when on"}
print_sequence:          {type: enum, values: [by layer, by object]}
seam_position:           {type: enum, values: [nearest, aligned, back, random]}
```

Each entry also carries a `rationale` string surfaced to the agent so it can explain trade-offs to the user.
Enum values above were checked against `PrintConfig.cpp` (some enums, e.g. `brim_type`, live in `s_keys_map_*` maps, not `enum_values`); the catalog generator still pulls them during
implementation (§10 step 2) so they cannot drift.

Machine-derived bounds (`nozzle`, max volumetric speed, bed size) come from the effective settings of the session.

## 8. Optimization

### 8.1 Objective & constraints

```json
{
  "objective": {"time": 0.6, "material": 0.4, "cost": 0.0},     // weights; normalized against baseline
  "constraints": {
    "max_time_increase_pct": 10, "max_material_increase_pct": 10,
    "min_wall_loops": 2, "min_top_shell_layers": 3, "max_layer_height": 0.28,
    "keep_supports": "auto|required|forbidden",
    "locked_keys": ["sparse_infill_pattern"],
    "locked_objects": [3],                 // no transform changes
    "functional": true                      // forbids lightning infill, density < 15%
  },
  "budget": {"max_slices": 40, "max_minutes": 20},
  "search": ["orientation", "support", "infill", "shells", "layer_height", "adhesion", "prime_tower"],
  "plate": 1
}
```

Score of a run: `score = Σ w_k · (baseline_k / run_k)` over enabled objectives (higher is better; 1.0 = baseline).
Runs violating constraints get `score = -inf` but are kept for reporting.

### 8.2 Strategy (staged greedy with batched evaluation)

Slicing costs seconds to minutes, so the search must be small and parallel. Stage order follows typical leverage:

1. **Baseline** — slice as-is.
2. **Orientation (per object)** — candidates:
   - Bambu auto-orient result (`--orient 1`, read back resulting transform from `out.3mf`),
   - the 6 axis-aligned poses,
   - up to 4 largest flat faces of the convex hull (cluster coplanar triangles by normal; area-weighted; numpy),
   - current pose.
   Dedupe by resulting up-vector (< 5°). Evaluate all in one `evaluate_batch`. Multiple objects: optimize each object
   independently against the same baseline, then combine the winners (objects interact only through placement/plate).
   Prefer poses with lower `support_share`; ties broken by time.
3. **Support** — skip when supports are disabled; otherwise try `support_type ∈ {tree(auto), normal(auto)}` × `support_style ∈
   {default, tree_hybrid, tree_organic, snug}` (only valid combos) and `support_threshold_angle ∈ {current, +10°}`.
4. **Infill** — density ladder (current → 15 → 10 → 7, floor from constraints) × pattern ∈ {current, gyroid, adaptivecubic,
   lightning (if non-functional)}. Coordinate descent: fix pattern winner, then density.
5. **Shells** — `bottom/top_shell_layers` and `wall_loops` down to the functional and print-spec floors; skip unchanged settings.
6. **Layer height** — ladder within nozzle bounds. (`adaptive_layer_height` is not a live option in Bambu Studio; it is commented out in `PrintConfig.cpp` and ignored.)
7. **Adhesion / waste** — `brim_type` (auto vs no_brim if bed contact area large), `skirt_loops 0`; multi-material:
   `flush_into_infill`, prime tower width.
8. **Placement** — `--arrange 1` (+ `--allow_rotations`) on the final variant; mainly matters for multi-plate projects
   (fewer plates → less per-plate overhead) and for `print_sequence: by object`.
9. **Pareto report** — return non-dominated variants (time vs grams), top 3, each with a diff vs baseline and a plain
   explanation of each change and its quality cost from catalog `rationale`.

Each stage keeps the best feasible variant and passes it on; stages are skipped if the budget is exhausted or
`search` excludes them. Each stage is one `evaluate_batch` (≤ 8–12 variants), so a full run is ~40–60 slices.

Why not Bayesian optimization / genetic search: with a 40-slice budget and mostly monotone knobs (less infill = less
time and material) staged greedy gets near the optimum; BO adds a dependency and tuning for little gain. Revisit if
benchmarks (§11) show staged greedy leaving > 5% on the table.

### 8.3 Speeding up evaluations

- `--load_slicedata/--export_slicedata`: cache per-object slicing so variants that only change G-code-level options
  (speeds, accelerations, flush, prime tower) skip geometry stages. Correct cache invalidation is the CLI's job; we
  only reuse a cache dir across variants with identical geometry-affecting keys (`layer_height`, walls, infill,
  supports, transforms). Gate behind spike S4.
- Order batches so cheap variants run first; abort a stage early if the first results already violate constraints
  monotonically (e.g. layer height going down only raises time).
- Agent-driven mode: the agent may skip `optimize` and drive `evaluate_batch` itself — the tools are the primitives,
  `optimize` is a convenience built on them.

## 9. Phase 2 (optional): minimal GUI seam for live control

Only needed if the user wants the agent to act **on the project open in the GUI** (see changes live, no file round trip).
Phase 1 already covers "optimize then open in GUI" via `open_in_studio`.

### 9.1 Options considered

| Option | Change size | Verdict |
|---|---|---|
| Single-instance IPC (exists) | 0 | Can only *open files* in the GUI (`InstanceCheck.cpp:488` → `load_files`). Used in phase 1. One-way, no reads. |
| Reuse `HttpServer` (`src/slic3r/GUI/HttpServer.hpp`, port 13618) | small, but | Built for the login callback, started on demand (`GUI_App::start_http_server`, `GUI_App.cpp:6693`), fixed port shared with login flow. Mixing automation into it couples two concerns. No. |
| Post-processing script hook (`post_process` key) | 0 | Runs after G-code export only; sees G-code, not model. Useless for orientation/settings. No. |
| `bambu_networking` plugin ABI | n/a | Closed fixed C ABI for cloud; not an extension point. No. |
| **New `AutomationServer` (localhost JSON-RPC)** | ~1 new .cpp/.hpp (~400 LOC) + ~10 lines in `GUI_App` + 1 config key | **Chosen.** Self-contained, opt-in, off by default. |

### 9.2 `AutomationServer` design

Files: `src/slic3r/GUI/AutomationServer.{hpp,cpp}`, registered in `src/slic3r/CMakeLists.txt`.

- Boost.Beast HTTP on `127.0.0.1` only, port from app config `automation_port` (default 13619); enabled only when
  app config `automation_server = true` (Preferences checkbox, default **off**).
- Auth: random 32-byte token generated at start, written to `<data_dir>/automation.token` (0600). Requests need
  `Authorization: Bearer <token>`. The MCP server reads the file. Prevents other local processes / browsers
  (DNS rebinding) from driving the slicer; also reject requests whose `Host` is not `127.0.0.1:<port>`.
- Threading: requests arrive on the Beast thread; each handler marshals to the GUI thread with
  `wxGetApp().CallAfter([..]{ …; promise.set_value(json); })` and waits on the future (timeout 30 s). All model/config
  mutation happens on the GUI thread, as the GUI itself does.
- Endpoints (JSON-RPC 2.0 over `POST /rpc`), each a thin wrapper over existing Plater/Tab API:

| Method | Implementation (existing API) |
|---|---|
| `project.info` | iterate `Plater::model()` objects/instances; `Plater::get_partplate_list()` (`Plater.hpp:812`) |
| `project.export` `{path}` | `Plater::export_3mf(path)` (`Plater.hpp:528`) |
| `project.load` `{path}` | `Plater::load_project` / `load_files` (`Plater.hpp:369,436`) |
| `settings.get` `{keys}` | `wxGetApp().preset_bundle->full_config()` |
| `settings.set` `{scope:"print", values}` | build `DynamicPrintConfig`, `Tab::load_config(cfg)` (`Tab.cpp:1619`) on the print tab → marks preset dirty, triggers reslice as a user edit would |
| `object.settings.set` `{obj, values}` | `ModelObject::config.set_deserialize(...)`, `Plater::changed_object(obj)` (`Plater.hpp:550`) |
| `object.transform` `{obj, rotation, offset}` | `ModelInstance::set_rotation/set_offset`, `ensure_on_bed`, `changed_object` |
| `plate.orient` / `plate.arrange` | `Plater::orient()` / `Plater::arrange()` (`Plater.hpp:665-666`) — runs existing jobs |
| `plate.slice` `{plate}` | `Plater::select_plate(i, true)` (`Plater.hpp:815`) + `reslice()`; reply when `EVT_PROCESS_COMPLETED` fires (`Plater.cpp:241`, handler `on_process_completed` `:7225`) |
| `plate.stats` `{plate}` | `PartPlate::get_slice_result()` (`PartPlate.hpp:543`) → `PrintEstimatedStatistics` (time per mode, `used_filaments_per_role`, volumes per extruder — `GCodeProcessor.hpp:69-123`) |

  Every mutating call goes through the same code paths as user clicks, so undo/redo snapshots and dirty-state work.
  Wrap mutations in `Plater::TakeSnapshot` so the user can undo agent changes in one step.

- `GUI_App` change: in `on_init_inner` after main frame creation, `if (app_config->get_bool("automation_server")) m_automation.start();`
  and stop in `OnExit`. Add the Preferences checkbox next to existing network options.

The MCP server gets a second backend implementing the same internal interface as the CLI runner
(`open/inspect/set/transform/slice/stats/export`), selected by `project_open(path, backend="gui")`. Optimization
loops still prefer the CLI backend (parallel, isolated); the GUI backend is for "apply the winner live" and
interactive tweaking.

## 10. Implementation plan

Phase 0 — spikes (½–1 day). Each answers one risk before code is written:

| ID | Question | How |
|---|---|---|
| S1 | Do arbitrary `--<print_key> value` overrides apply when input is a Bambu 3MF? (source says yes: `BambuStudio.cpp:4136`) | slice cube.3mf with `--sparse_infill_density 5%`, compare `result.json` grams vs default |
| S2 | Does the CLI write into the user data dir / conflict with a running GUI? Can N CLIs run in parallel? | run 4 parallel slices with GUI open; watch data dir; try `--datadir <tmp>` |
| S3 | Does a patched build-item transform (rotation + z drop) load and slice correctly, incl. multi-instance objects? | patch fixture, slice, export, open in GUI, inspect |
| S4 | Does `--load_slicedata` give speedups when only speeds change? | time two runs |
| S5 | Does single-instance forwarding load a 3MF into the running GUI on all 3 OSes? | `bambu-studio out.3mf` with GUI open |

Phase 1 — headless MCP server (≈ 6–8 days):

1. Package skeleton `tools/bambu-mcp/` (own `pyproject.toml`; not part of the C++ build). `studio_info`, binary resolution.
2. `catalog.yaml` + generator script that scrapes key names, types and `enum_values` from `PrintConfig.cpp`
   (`def = this->add("key", coType)` blocks) so the catalog cannot drift silently; hand-written ranges/rationales on top.
3. `runner.py`: argv building, timeout/kill, `result.json` → `Metrics`, cache.
4. `threemf.py`: read objects/instances/transforms/meshes/per-object config; patch settings & transforms; z-drop.
5. Session tools: `project_open`, `project_inspect`, `settings_get/set/reset`, `object_transform`, `orient`, `arrange`, variants.
6. Evaluation tools: `slice`, `evaluate_batch` (pool + progress notifications), `compare`.
7. `optimize.py`: stages §8.2, scoring, Pareto, explanations.
8. `export`, `open_in_studio`. Resources and prompts.
9. Docs: `tools/bambu-mcp/README.md` — install (`uv tool install`), Claude Code config:
   ```json
   {"mcpServers": {"bambu": {"command": "uvx", "args": ["bambu-mcp"],
     "env": {"BAMBU_STUDIO_BIN": "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"}}}}
   ```

Phase 2 — GUI seam (≈ 4–5 days, only if wanted): §9.

## 11. Testing

- **Unit (no binary):** 3MF patch round-trip on fixtures (transform composition, z-drop math against a known cube,
  metadata insert/replace), argv encoding of each catalog type, scoring/Pareto, cache key stability. One `pytest` file
  per module, fixtures under `tools/bambu-mcp/tests/fixtures/` (cube, overhang bracket, 2-object multi-color plate).
- **Integration (binary required, marked `@slow`):** slice each fixture; assert `ok`, `time_s > 0`, overrides change
  metrics in the expected direction (density 5% < 20% grams), orientation candidates of the bracket reduce support time.
- **Benchmark:** 10 representative models; record baseline vs optimized time/grams and slices used. Regression gate:
  optimized must not be worse than baseline; report median savings. Used to decide on fancier search (§8.2).
- **MCP contract:** run the server under the MCP Inspector; snapshot tool schemas.

## 12. Security & safety

- Paths: tools accept only absolute paths; `export` refuses to write outside allowed roots (input file's dir,
  `$BAMBU_MCP_HOME`, or roots granted by the MCP client) and never overwrites the source without `overwrite=true`.
- Subprocess: argv list only (no shell), override values validated against catalog types; keys must exist in the
  catalog or match `^[a-z0-9_]+$` with `force=true`; this blocks injecting CLI actions like `--export_stls /etc` via a key name.
  Explicitly deny keys `post_process`, `filename_format`, `machine_start_gcode`, `machine_end_gcode`, `*_gcode`
  (G-code / script injection into printer files), unless `allow_gcode_keys` is set in server config.
- Resource limits: worker cap, per-run timeout + `--mstpp`, cache size LRU.
- Phase 2 server: localhost bind, bearer token file 0600, Host header check, off by default.
- No printer/cloud actions at all in this server.

## 13. Open questions (defaults chosen; change if wrong)

1. **Scope of control:** default = headless CLI optimizer + "open result in GUI". Is live control of the open GUI
   project (phase 2, requires the ~400-line C++ seam) actually required?
2. **Quality guardrails:** defaults are "functional part" floors (2 walls, 3 top / 2 bottom layers, infill ≥ 10%, no
   lightning). Should the default be "visual/prototype" instead?
3. **Language:** Python chosen (MCP SDK maturity, zip/XML in stdlib). TypeScript is equally viable if the team prefers it.
4. **Location:** `tools/bambu-mcp/` inside this repo vs separate repo. A separate repo avoids coupling to the C++
   release cycle; inside the repo lets CI run integration tests against the freshly built binary. Default: inside.
5. **Multi-plate projects:** optimize per plate (default) or allow moving objects between plates to reduce plate count?
6. **Cost:** include filament `filament_cost` and electricity/machine-hour cost in the objective?

## Implementation notes (verified against BambuStudio 02.08.02.61, 2026-10-10)

Phase 1 is implemented in `tools/bambu-mcp/`. Building it against the real binary corrected these assumptions above:

| Spec assumption | What the binary does | Handling |
|---|---|---|
| Overrides as `--key value` (§2, §6.2) | Rejected: `Invalid option --wall-loops 2`. Works only as `--key=value`, with dashes in the key (`--wall-loops=2`). Spike S1 passes in that form: 5% vs 50% infill changed time 2865 s vs 8395 s on a cube | `Runner.argv` |
| CLI resolves `inherits` in `--load-settings/--load-filaments` (§4) | It does not. System filament presets lose density/diameter, so grams read 0.0 | `presets.flatten` merges the chain and writes a flat JSON per run |
| `--min-save` shrinks output (§6.2) | Strips meshes: the output 3MF holds G-code only and cannot be re-opened or re-sliced | Not used |
| `result.json` has `peak_rss_mb` (§5.3) | Absent in the installed 02.08.02.61 build (present on master) | Treated as optional |
| Flag names use underscores | `--help` and parser use dashes (`--export-3mf`, `--load-settings`) | `Runner.argv` |
| Bool flags may take a value | `--min-save 1` is rejected; bare flag only. Int flags use `=`: `--orient=1`, `--arrange=1` | `Runner.argv` |

Server framework is standalone `fastmcp` 4.x (the `mcp` SDK 2.x renamed FastMCP to MCPServer; v1 is deprecated).
Each tool carries annotations and input/output JSON schemas (`bambu_mcp/schemas.py`, enforced by `tests/test_server_schema.py`).

Not implemented yet (deferred, per spec §9/§13): GUI `AutomationServer` (phase 2), `variant_save/load`, MCP prompts,
`--load-slicedata` reuse (spike S4), face-based orientation candidates, cost objective.
Spikes S2 (parallel CLI vs open GUI) and S5 (single-instance forwarding) were not run.
