# Agent workflow

<!-- codebase-memory-mcp:start -->
For structural codebase exploration, use the installed `codebase-memory` skill.
Check index readiness and coverage for the affected paths; fall back to source
reads and `rg` when coverage is missing or stale.
<!-- codebase-memory-mcp:end -->

## Establish context

- Confirm the checkout and `git status` before editing. Personal CAD projects,
  local MCP configuration and unrelated work may be untracked; stage explicit
  task paths rather than the entire working tree.
- Search Hindsight before new implementation, test changes, bug fixes or commits;
  read relevant initiative/convention pages before substantial work. Verify past
  claims against current code and artifacts. Record measured experiments,
  adoption/rejection reasons and corrections when the user requests memory work.
- Start with `capabilities` for live FreeCAD work and `studio_info`,
  `presets_list`, `parameters_catalog` for Bambu work. Discover installed versions
  and actual preset names instead of guessing supported options.

## Pick the shortest useful tool path

- Use declarative component parameters and features for editable CAD. Batch
  creation/validation/export with `build_component`; use `patch_component` with
  its preview/concurrency facilities for changes and `export_bundle` for several
  formats. Inspect the component/build result before export.
- Run `validate_component` and sampled `inspect_printability` before expensive
  slicing. Check connector seating with `inspect_attachment` and relevant
  clearance/intersection rules. `create_connector_features` returns proposed
  editable features; preview/apply them with `patch_component`.
- Preserve accepted dimensions, interfaces, colors and assembly alignment.
  Re-read STEP/STL exports when changing export or geometry behavior; use views,
  cross-sections or overlays when appearance matters. A valid B-rep or manifold
  mesh alone does not establish printability, strength or physical clearance.
- For measured whole-assembly comparison, use FreeCAD
  `compare_print_candidates(backend="bambu", print_spec=..., workers=2)` or the
  standalone `compare_project`. The bridge default remains `backend="legacy"`;
  select the owned backend explicitly for presets, cache and parallel slicing.
- For iterative setting tweaks in a session, use `project_open` →
  `project_inspect` → `slice` baseline → `evaluate_batch` or `optimize` →
  `compare` → `export`. Set `apply=false` on exploratory optimization if the
  working variant should not change. Reuse an accepted `run_id` for export.
  `open_in_studio` supports visual inspection; these tools do not send print jobs.

## Print constraints and slice efficiency

- Supply native Bambu 3MF input with Bambu Application metadata, millimeter units
  and one aligned whole-assembly build item for comparison. Generic 3MF plus a
  settings file is insufficient. Canonicalize benchmark inputs through the real
  slicer with explicit presets; never overwrite a user's source to do this.
- Use the shared versioned specification in
  [docs/bambu-mcp/unified-pipeline.md](docs/bambu-mcp/unified-pipeline.md): select
  machine/process/ordered filament presets, functional floors, nozzle/layer
  limits, support/brim requirements, locks and measured time/material caps.
  Inspection `settings_errors` means preset resolution is incomplete; resolve
  those errors before slicing. Catalog `effective_settings` is not the complete
  nozzle-specific configuration.
- Keep a valid baseline and change a small number of relevant settings first.
  `compare_project`/bridge comparison accepts 1–12 candidates and adds a baseline;
  session `evaluate_batch` accepts 1–32 variants. Batch rows are sorted by time:
  identify them by `input_index` and returned variant, never row position.
- Start with two slice workers and an explicit timeout/budget. `optimize` counts
  the baseline in `max_slices`; select useful `stages` rather than repeating the
  full search. Winners on independent settings within a stage are combined and
  re-sliced; this is staged greedy search, not a global optimum guarantee.
- Reuse cached slices and canonical benchmark corpora. Give comparison/benchmark
  runs a new output directory. Reports retain source hashes, settings, run IDs,
  artifact paths and rejection reasons; preserve that evidence when comparing
  experiments. Baseline-identical numeric choices are exempt from floors, while
  explicit support/brim requirements and locks still apply.
- Any locked object prevents whole-assembly rotation. Session optimization with
  auto-orient enabled and object locks must first call `orient(enable=false)`.
  Explicit orientation candidates disable auto-orient. Adhesion is opt-in;
  brim removal is only attempted with `functional=false`.
- Use optional `suggest_print_orientations(backend="tweaker3-python", ...,
  max_candidates=4)` when axis poses leave plausible orientation opportunities.
  Pass `[provider_python, "/path/Tweaker-3/Tweaker.py"]` as `executable`. Apply
  each returned rigid matrix to the whole native assembly and measure it; the
  heuristic score is not predicted time/material savings. Preserve original
  scale and relative placement. `stl-fit` is also an optional proposal provider.
- Rank only successful complete artifacts within the plate and constraints.
  Compare fastest, least-material, conservative and Pareto alternatives.
  Unavailable mass/counts remain null. Conservative ranking is a review heuristic;
  dimensional tolerance/protected surfaces need separate evidence. Report slicer
  estimates separately from physical print or calibration results.

## Environments and optional tools

- Use one root environment for the FastMCP 4 bridge and owned Bambu server:

  ```bash
  uv sync --locked --extra validation
  uv run --locked --extra validation python src/freecad_bridge.py
  ```

  `validation` is optional. The uv workspace installs `tools/bambu-mcp` into
  `.venv`; nested tool venvs are unnecessary for this checkout. The bridge mounts
  Bambu tools as `bambu_...`; standalone `.venv/bin/bambu-mcp` is also available.
  Comparison workers default to the current interpreter; `backend_python` is
  only needed for an explicitly different installation. Configure
  `BAMBU_STUDIO_BIN` and `BAMBU_MCP_HOME` for executable/cache locations.
- `project_validate(session_id)` checks the source copy in a bounded lib3mf
  subprocess, excluding working variant edits. Inspect `geometry_valid`, strict
  compliance and vendor warnings separately. Bambu vendor warnings are advisory;
  this tool does not certify self-intersection freedom or strength.
- Provider dependencies can share the root environment when compatible; use
  optional extras or a disposable probe environment for experiments. Do not add
  NumPy/SciPy/Trimesh/Optuna to base dependencies or the shared core merely to
  run an experiment. Shared core is stdlib-only and MIT; the standalone
  `bambu_mcp` subtree retains AGPL licensing.
- Consult [tool-evaluation.md](docs/bambu-mcp/tool-evaluation.md) before proposing
  another optimizer/repair/parser integration. Require representative failures or
  improvement at equal actual slice budgets. Reuse `--projects` in
  `tools/benchmark_print_tools.py`; `tools/benchmark_print_validation.py` reuses
  existing native projects/artifacts without slicing again. Write probe results
  to files so library stdout/stderr cannot corrupt JSON. Comment toolpath profiles
  describe blocks/extrusion heights, not material mass or nominal layer ladders.

## Keep shared behavior centralized

- Constraint checks live in `print_pipeline_core.spec.setting_breaches`; legacy
  messages/defaults are adapters, not a second validation algorithm.
- Resolve settings through `bambu_mcp.settings.resolve_settings`. Inspection can
  request partial results; slicing is strict. `snapshot_presets` supplies the
  same flattened inputs to optimizer/comparison checks, cache keys and slicing.
- Encode CLI/project setting strings through `print_pipeline_core.settings`; preserve
  public numeric values with `normalize_settings`. Catalog validation stays separate.
- Finalize comparison budgets/rankings and write reports through
  `print_pipeline_core.report`. Reuse the runner's validated artifact result;
  retain total plate counts so single-plate comparisons cannot accept a selected
  plate from a multi-plate output. Every fresh cache lookup revalidates the files.
- Use `print_pipeline_core.process.run_bounded` for provider and slicer processes.
  Its returned output is a diagnostic preview; parse successful provider JSON
  from the complete bounded log file.

## Validation and review

Run focused tests first, then the relevant boundary checks. From the repository
root, with the environments installed:

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

- Optional real-provider tests use JSON argv environment variables:
  `FREECAD_TWEAKER_ARGV`, `FREECAD_TWEAKER_PYTHON_ARGV`,
  `FREECAD_STL_FIT_ARGV`. Without configured providers they skip; report skips
  explicitly. `PRINT_VALIDATION_PYTHON` selects a lib3mf-enabled interpreter.
- Use `uv build --out-dir /tmp/<new-release-dir>` from `tools/bambu-mcp` for
  packaging changes; check shared modules and both licenses in the wheel/sdist.
- For OpenCodeReview, preview exact committed paths/range before sending source
  under the user's authorization. Prefer a tracked-files-only isolated checkout,
  short relevant background and built-in file tools; unrelated global MCP
  connections add noise. Keep reports outside tracked source. Verify findings
  against current code, fix concrete defects, then rerun affected review coverage.
- A review is complete only with `status=complete`,
  `manifest.terminal_state=complete`, all selected files completed/reused and zero
  failed tools/command/schema errors. Zero comments in a failed run is not clean.
  Distinguish deferred suggestions, rejected findings, local tests, unrun remote
  CI, GUI evidence and physical printing in the handoff.
