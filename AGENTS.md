# Agent workflow

<!-- codebase-memory-mcp:start -->
Use the installed `codebase-memory` skill for structural exploration. Check index
readiness and affected-path coverage; use source reads and `rg` for missing/stale coverage.
<!-- codebase-memory-mcp:end -->

## Establish context

- Confirm checkout and `git status` before editing. Preserve unrelated/untracked
  CAD projects and local configuration; stage only explicit task paths.
- Search Hindsight before implementation, test changes, bug fixes or commits;
  read relevant initiative/convention pages before substantial work. Verify memory
  against current code/artifacts. When asked to update memory, record measured
  experiments, adoption/rejection reasons and corrections.
- Discover live versions/options: start FreeCAD work with `capabilities`; Bambu
  work with `studio_info`, `presets_list`, `parameters_catalog`. Use actual presets.

## CAD and slicing workflow

- Prefer editable declarative parameters/features. `build_component` batches
  creation/validation/export; `patch_component` previews/applies changes with
  concurrency checks; `export_bundle` exports multiple formats. Inspect build
  results before export.
- Run `validate_component` and sampled `inspect_printability` before expensive
  slicing. Check connector seating with `inspect_attachment` and relevant
  clearance/intersection rules. `create_connector_features` returns proposed
  editable features; preview/apply them with `patch_component`.
- Preserve accepted dimensions, interfaces, colors and assembly alignment.
  Re-read STEP/STL exports when changing export or geometry behavior; use views,
  cross-sections or overlays when appearance matters. A valid B-rep or manifold
  mesh alone does not establish printability, strength or physical clearance.
- Compare whole assemblies with
  `compare_print_candidates(backend="bambu", print_spec=..., workers=2)` or
  standalone `compare_project`. The bridge defaults to `backend="legacy"`;
  explicitly select Bambu for owned presets, cache and parallel slicing.
- Iterate settings via `project_open` → `project_inspect` → baseline `slice` →
  `evaluate_batch`/`optimize` → `compare` → `export`. Use `apply=false` to preserve
  the working variant during exploration; export an accepted `run_id`.
  `open_in_studio` enables visual inspection; these tools send no print jobs.

## Print constraints and measurement

- Compare native Bambu 3MFs with Bambu Application metadata, millimeter units and
  one aligned whole-assembly build item. Generic 3MF plus settings is insufficient.
  Canonicalize benchmarks through the real slicer with explicit presets without
  overwriting user sources.
- Use the shared versioned specification in
  [docs/bambu-mcp/unified-pipeline.md](docs/bambu-mcp/unified-pipeline.md): select
  machine/process/ordered filament presets, functional floors, nozzle/layer
  limits, support/brim requirements, locks and measured time/material caps.
  Inspection `settings_errors` means preset resolution is incomplete; resolve
  those errors before slicing. Catalog `effective_settings` is not the complete
  nozzle-specific configuration.
- Keep a valid baseline; initially vary few relevant settings. `compare_project`/
  bridge comparisons accept 1–12 candidates plus an added baseline; session
  `evaluate_batch` accepts 1–32 variants. Rows sort by time: identify by
  `input_index` and returned variant, never position.
- Start with two slice workers and explicit timeout/budget. `optimize` includes
  the baseline in `max_slices`. Select useful `stages`; independent winners within
  each stage are combined and re-sliced (greedy search, no global-optimum guarantee).
- Reuse cached slices and canonical benchmark corpora. Give comparison/benchmark
  runs a new output directory. Reports retain source hashes, settings, run IDs,
  artifact paths and rejection reasons; preserve that evidence when comparing
  experiments. Baseline-identical numeric choices are exempt from floors, while
  explicit support/brim requirements and locks still apply.
- Any object lock blocks whole-assembly rotation. With locks and session
  auto-orient enabled, call `orient(enable=false)` before optimization. Explicit
  orientation candidates disable auto-orient. Adhesion is opt-in; attempt brim
  removal only with `functional=false`.
- Use optional `suggest_print_orientations(backend="tweaker3-python", ...,
  max_candidates=4)` when axis poses leave plausible orientation opportunities.
  Pass `[provider_python, "/path/Tweaker-3/Tweaker.py"]` as `executable`. Apply
  each returned rigid matrix to the whole native assembly and measure it; the
  heuristic score is not predicted time/material savings. Preserve original
  scale and relative placement. `stl-fit` is also an optional proposal provider.
- Rank only successful, complete artifacts satisfying plate/constraint limits.
  Compare fastest, least-material, conservative and Pareto choices; unavailable
  mass/counts stay null. Conservative ranking is a heuristic: tolerance/protected
  surfaces need separate evidence. Separate slicer estimates from physical
  printing/calibration evidence.

## Environments and optional tools

- Use one Python 3.11+ root environment for the FastMCP 4 bridge and owned Bambu server:

  ```bash
  uv sync --locked --extra validation
  uv run --locked --extra validation python src/freecad_bridge.py
  ```

  `validation` is optional. The uv workspace installs `tools/bambu-mcp` in `.venv`;
  no nested venv is needed. Bridge tools use `bambu_...`; standalone entrypoint:
  `.venv/bin/bambu-mcp`. Workers use the current interpreter; set `backend_python`
  only for another installation. Configure `BAMBU_STUDIO_BIN`/`BAMBU_MCP_HOME`
  for executable/cache locations.
- `project_validate(session_id)` checks the source copy in a bounded lib3mf
  subprocess, excluding working variant edits. Inspect `geometry_valid`, strict
  compliance and vendor warnings separately. Bambu vendor warnings are advisory;
  this tool does not certify self-intersection freedom or strength.
- Compatible providers may share the root environment; use optional extras or
  disposable probe environments for experiments. Do not add NumPy/SciPy/Trimesh/Optuna
  to base dependencies or shared core for experiments. Shared core stays
  stdlib-only/MIT; standalone `bambu_mcp` retains AGPL licensing.
- Consult [tool-evaluation.md](docs/bambu-mcp/tool-evaluation.md) before proposing
  another optimizer/repair/parser integration. Require representative failures or
  improvement at equal actual slice budgets. Reuse `--projects` in
  `tools/benchmark_print_tools.py`; `tools/benchmark_print_validation.py` reuses
  existing native projects/artifacts without slicing again. Write probe results
  to files so library stdout/stderr cannot corrupt JSON. Comment toolpath profiles
  describe blocks/extrusion heights, not material mass or nominal layer ladders.

## Shared implementation contracts

- Constraints: `print_pipeline_core.spec.setting_breaches`; legacy defaults/messages
  are adapters, never a second validation algorithm.
- Settings: `bambu_mcp.settings.resolve_settings` (partial inspection, strict
  slicing). Use `snapshot_presets` for identical flattened inputs across optimizer/
  comparison checks, cache keys and slicing.
- Encode CLI/project setting strings through `print_pipeline_core.settings`; preserve
  public numeric values with `normalize_settings`. Catalog validation stays separate.
- Budgets/rankings/reports: `print_pipeline_core.report`. Reuse validated runner
  artifacts; retain total plate counts to reject multi-plate output in single-plate
  comparisons. Revalidate files on every fresh cache lookup.
- Provider/slicer processes: `print_pipeline_core.process.run_bounded`. Returned
  output is a diagnostic preview; parse successful provider JSON from the complete
  bounded log file.

## Validation and review

Run focused tests, then relevant boundary checks from the repository root:

```bash
# Standalone unit/schema checks; no installed slicer needed.
.venv/bin/python -m pytest tools/bambu-mcp/tests -m 'not slow'

# Root/shared contracts and the actual FastMCP protocol.
.venv/bin/python -m pytest tests/test_slicer_pipeline.py \
  tests/test_unified_print_pipeline.py tests/test_orientation_candidates.py \
  tests/test_print_workflow_bridge.py tests/test_bridge.py tests/test_bridge_mcp.py tests/test_framing.py \
  tests/test_parametric_logic.py tests/test_printability.py \
  tests/test_manufacturing_export.py

# Real Bambu integration for CLI/preset/3MF/optimizer changes.
PRINT_VALIDATION_PYTHON="$PWD/.venv/bin/python" \
  .venv/bin/python -m pytest tools/bambu-mcp/tests -m slow
```

- Optional provider tests accept JSON argv via `FREECAD_TWEAKER_ARGV`,
  `FREECAD_TWEAKER_PYTHON_ARGV`, `FREECAD_STL_FIT_ARGV`; report unconfigured skips.
  `PRINT_VALIDATION_PYTHON` selects a lib3mf-enabled interpreter.
- Use `uv build --out-dir /tmp/<new-release-dir>` from `tools/bambu-mcp` for
  packaging changes; check shared modules and both licenses in the wheel/sdist.
- Before authorized OpenCodeReview source submission, preview exact committed
  paths/range. Prefer a tracked-files-only isolated checkout, brief relevant
  background and built-in file tools; avoid unrelated global MCP connections.
  Keep reports outside tracked source. Verify findings against current code, fix
  concrete defects and rerun affected coverage.
- Review completion requires `status=complete`, `manifest.terminal_state=complete`,
  every selected file completed/reused and zero failed tools/command/schema errors.
  Zero comments in a failed review is not clean. Handoffs distinguish deferred
  suggestions, rejected findings, local tests, unrun remote CI, GUI and physical-print evidence.

- After each CAD design, reflect on process/tool improvements and present proposed
  MCP changes for user approval.
