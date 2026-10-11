"""MCP server: tools to inspect, tweak, slice and optimize Bambu Studio projects via the CLI."""
import asyncio
import json
import math
import shutil
import logging
import re
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal

from fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from . import optimize as opt
from .catalog import CATALOG, validate
from .runner import Metrics, Runner, Variant
from .schemas import (BatchRow, CompareRow, ExportResult, Flags, FlagsState, HistoryEntry, LaunchResult, ObjectId,
                      ObjectInfo, OptimizeResult, ParamSpec, ProjectOpened, ProjectSummary, SessionId,
                      SettingsState, SettingValue, StudioInfo, TransformState, VariantOut, VariantSpec)
from .threemf import Package

mcp = FastMCP(
    "bambu-studio",
    instructions=(
        "Optimize Bambu Studio print time and filament use. Typical flow: studio_info -> presets_list -> "
        "project_open -> slice (baseline) -> settings_set / object_transform / orient / arrange -> "
        "evaluate_batch or optimize -> export. Slicing is the oracle: always measure, never guess."),
)
_runner: Runner | None = None
_runner_lock = threading.Lock()
log = logging.getLogger("bambu_mcp")

Stage = Literal["orientation", "support", "infill", "shells", "layer_height", "adhesion"]


def _ann(title: str, *, read_only: bool = False, destructive: bool = False, idempotent: bool = True,
         open_world: bool = False) -> ToolAnnotations:
    # destructiveHint/idempotentHint only matter for non-read-only tools (MCP spec)
    return ToolAnnotations(title=title, readOnlyHint=read_only,
                           destructiveHint=None if read_only else destructive,
                           idempotentHint=idempotent, openWorldHint=open_world)


def runner() -> Runner:
    global _runner
    if _runner is None:
        with _runner_lock:  # tools run on a thread pool; one Runner so per-key locks are shared
            if _runner is None:
                _runner = Runner()
    return _runner


@dataclass
class Session:
    source: Path
    machine: str | None = None
    process: str | None = None
    filaments: list = field(default_factory=list)
    variant: Variant = field(default_factory=Variant)
    baseline: Metrics | None = None
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


SESSIONS: dict[str, Session] = {}


def sess(sid: str) -> Session:
    if sid not in SESSIONS:
        raise ValueError(f"unknown session {sid!r}; call project_open first")
    return SESSIONS[sid]


def _kw(s: Session) -> dict:
    return dict(machine=s.machine, process=s.process, filaments=s.filaments)


def _run_dir(run_id: str) -> Path:
    """runs/<run_id>, refusing anything that is not a cache key we emitted (no path traversal)."""
    if not re.fullmatch(r"[0-9a-f]{24}", run_id):
        raise ValueError(f"bad run_id {run_id!r}")
    return runner().home / "runs" / run_id


def _flags(f: Flags) -> dict:
    return f.model_dump(exclude_none=True)


# ---- info ----------------------------------------------------------------------------------------
@mcp.tool(annotations=_ann("Studio info", read_only=True))
def studio_info() -> StudioInfo:
    """Locate the Bambu Studio binary and report its version and preset counts. Call first to confirm the
    environment works."""
    r = runner()
    return StudioInfo(binary=r.binary, version=r.version(), profile_roots=[str(x) for x in r.roots],
                      presets={k: len(v) for k, v in r.index.items()})


@mcp.tool(annotations=_ann("List presets", read_only=True))
def presets_list(
    kind: Annotated[Literal["machine", "process", "filament"], Field(description="preset kind")],
    name_contains: Annotated[str, Field(description="case-insensitive substring filter")] = "",
    limit: Annotated[int, Field(ge=1, le=500, description="max names returned")] = 50,
) -> list[str]:
    """List preset names usable as machine_preset / process_preset / filament_presets in project_open."""
    names = sorted(runner().index[kind])
    return [n for n in names if name_contains.lower() in n.lower()][:limit]


@mcp.tool(annotations=_ann("Parameter catalog", read_only=True))
def parameters_catalog(
    key_contains: Annotated[str, Field(description="only keys containing this text")] = "",
) -> dict[str, ParamSpec]:
    """The curated settings worth tuning for time/material, with types, ranges, enum values and trade-off notes.
    Keys outside this catalog need force=true in settings_set."""
    return {k: ParamSpec(**v) for k, v in CATALOG.items() if key_contains in k}


# ---- session -------------------------------------------------------------------------------------
def _summary(s: Session) -> dict:
    with s.lock:
        variant = VariantOut.of(s.variant)
    p = Package(s.source)
    per = p.object_settings()
    objs = {}
    for oid in p.items():
        try:
            lo, hi = p.world_bbox(oid)
            size = [round(b - a, 2) for a, b in zip(lo, hi)]
        except (KeyError, ValueError, OSError):
            size = None
        objs[oid] = ObjectInfo(name=per.get(oid, {}).get("name"), size_mm=size, settings=per.get(oid, {}))
    return dict(source=str(s.source), objects=objs, working_variant=variant,
                baseline=s.baseline, **_effective(s, p, variant))


def _effective(s: Session, p: Package, variant: VariantOut) -> dict:
    """Catalog keys resolved the way the slicer sees them: project settings < presets < working overrides."""
    def flat(v):
        return {k: ",".join(map(str, x)) if isinstance(x, list) else str(x) for k, x in v.items() if k in CATALOG}
    from .settings import resolve_settings
    context = resolve_settings(p, runner()._load_preset, s.machine, s.process, s.filaments, strict=False)
    eff, _ = context.effective(variant)
    return dict(effective_settings=flat(eff), settings_errors=context.errors)



@mcp.tool(annotations=_ann("Open project", idempotent=False))
def project_open(
    path: Annotated[str, Field(description="absolute path to .3mf, .stl, .obj or .step")],
    machine_preset: Annotated[str, Field(description="machine preset name or .json path; required for meshes")] = "",
    process_preset: Annotated[str, Field(description="process preset name or .json path; required for meshes")] = "",
    filament_presets: Annotated[list[str] | None, Field(description="filament preset names, one per extruder/slot")] = None,
) -> ProjectOpened:
    """Open a project in a new session. A .3mf uses its embedded settings unless presets are given; a mesh is
    converted to a 3MF with one baseline slice and needs machine+process presets. The file is copied; the
    original is never modified."""
    src = Path(path).expanduser().resolve()
    if not src.is_file():
        raise ValueError(f"{src} not found")
    is_mesh = src.suffix.lower() in (".stl", ".obj", ".step", ".stp")
    if is_mesh and not (machine_preset and process_preset):
        raise ValueError("mesh input needs machine_preset and process_preset")
    r = runner()
    s = Session(src, machine_preset or None, process_preset or None, filament_presets or [])
    baseline = None
    if is_mesh:  # slice first: a failure must not leave a session dir behind
        baseline = r.evaluate(src, Variant(flags={"arrange": 1, "orient": 0}), **_kw(s))
        if not baseline.ok:
            raise RuntimeError(baseline.error)
    sid = uuid.uuid4().hex[:12]
    work = r.home / "sessions" / sid
    work.mkdir(parents=True)
    try:
        shutil.copy(baseline.out_3mf if baseline else src, work / "source.3mf")
    except OSError:
        shutil.rmtree(work, ignore_errors=True)
        raise
    s.source, s.baseline = work / "source.3mf", baseline
    SESSIONS[sid] = s
    return ProjectOpened(session_id=sid, **_summary(s))


@mcp.tool(annotations=_ann("Inspect project", read_only=True))
def project_inspect(session_id: SessionId) -> ProjectSummary:
    """Objects (name, size, per-object settings), the working variant and the baseline metrics, if sliced."""
    return ProjectSummary(**_summary(sess(session_id)))


@mcp.tool(annotations=_ann("Validate source geometry", read_only=True))
def project_validate(session_id: SessionId) -> dict:
    """Read-only lib3mf inspection of the session source meshes, excluding working variant changes.
    Requires the optional validation extra. Bambu metadata warnings are advisory; this does not certify
    printability or change optimization rankings. Runs in a bounded subprocess."""
    from print_pipeline_core.validation import validate_file
    return validate_file(sess(session_id).source)


# ---- editing -------------------------------------------------------------------------------------
@mcp.tool(annotations=_ann("Set settings"))
def settings_set(
    session_id: SessionId,
    overrides: Annotated[dict[str, SettingValue], Field(
        description="{setting key: value}, e.g. {'sparse_infill_density': '10%', 'wall_loops': 2}")],
    object_id: Annotated[str, Field(description="apply to this object only; empty = global", pattern=r"^(\d+)?$")] = "",
    force: Annotated[bool, Field(description="allow keys outside the catalog (syntax-checked only)")] = False,
) -> SettingsState:
    """Set print settings on the session's working variant (nothing is sliced). Values are validated against
    the catalog; G-code/script keys are always refused."""
    s = sess(session_id)
    clean = {k: validate(k, v, force) for k, v in overrides.items()}
    with s.lock:
        if object_id:
            if object_id not in Package(s.source).items():
                raise ValueError(f"object {object_id} not in project")
            if clean:  # an empty dict would force a patched 3MF + cache miss
                s.variant.object_overrides.setdefault(object_id, {}).update(clean)
        else:
            s.variant.overrides.update(clean)
        return SettingsState(overrides=dict(s.variant.overrides),
                             object_overrides={k: dict(v) for k, v in s.variant.object_overrides.items()})


@mcp.tool(annotations=_ann("Reset settings"))
def settings_reset(
    session_id: SessionId,
    keys: Annotated[list[str] | None, Field(description="keys to drop; omit to drop all")] = None,
    object_id: Annotated[str, Field(description="reset this object's overrides; empty = global", pattern=r"^(\d+)?$")] = "",
) -> SettingsState:
    """Drop overrides from the working variant, falling back to the project/preset values."""
    s = sess(session_id)
    with s.lock:
        tgt = s.variant.object_overrides.get(object_id, {}) if object_id else s.variant.overrides
        for k in (keys if keys is not None else list(tgt)):
            tgt.pop(k, None)
        if object_id and not tgt:
            s.variant.object_overrides.pop(object_id, None)  # empty dict would force a patched 3MF + cache miss
        return SettingsState(overrides=dict(s.variant.overrides),
                             object_overrides={k: dict(v) for k, v in s.variant.object_overrides.items()})


@mcp.tool(annotations=_ann("Rotate object"))
def object_transform(
    session_id: SessionId,
    object_id: ObjectId,
    rotate_x_deg: Annotated[float, Field(ge=-360, le=360, description="degrees about the X axis")] = 0,
    rotate_y_deg: Annotated[float, Field(ge=-360, le=360, description="degrees about the Y axis")] = 0,
    rotate_z_deg: Annotated[float, Field(ge=-360, le=360, description="degrees about the Z axis")] = 0,
) -> TransformState:
    """Rotate one object about its centre (X then Y then Z) and drop it onto the bed at the next slice. Replaces
    any earlier rotation of that object and turns arrange on, since the footprint changes."""
    s = sess(session_id)
    if object_id not in Package(s.source).items():
        raise ValueError(f"object {object_id} not in project")
    with s.lock:
        s.variant.object_rotation[object_id] = (rotate_x_deg, rotate_y_deg, rotate_z_deg)
        s.variant.flags["arrange"] = 1
        return TransformState(rotations={k: list(v) for k, v in s.variant.object_rotation.items()},
                              flags=dict(s.variant.flags))


@mcp.tool(annotations=_ann("Auto-orient"))
def orient(
    session_id: SessionId,
    enable: Annotated[bool, Field(description="true = Bambu auto-orient all objects; false = leave poses alone")] = True,
) -> FlagsState:
    """Toggle Bambu's built-in auto-orient (minimises support area) for the next slices."""
    s = sess(session_id)
    with s.lock:
        s.variant.flags["orient"] = 1 if enable else 0
        return FlagsState(flags=dict(s.variant.flags))


@mcp.tool(annotations=_ann("Auto-arrange"))
def arrange(
    session_id: SessionId,
    allow_rotations: Annotated[bool, Field(description="let arrange rotate objects")] = False,
    avoid_cali_region: Annotated[bool, Field(description="keep clear of the calibration strip")] = True,
    multicolor_one_plate: Annotated[bool, Field(description="allow mixed colours on a plate")] = True,
) -> FlagsState:
    """Auto-arrange objects across plates at slice time."""
    s = sess(session_id)
    with s.lock:
        s.variant.flags.update(arrange=1, allow_rotations=allow_rotations,
                               avoid_extrusion_cali_region=avoid_cali_region,
                               allow_multicolor_oneplate=multicolor_one_plate)
        return FlagsState(flags=dict(s.variant.flags))


# ---- evaluation ----------------------------------------------------------------------------------
@mcp.tool(name="slice", annotations=_ann("Slice"))
def slice_plate(
    session_id: SessionId,
    plate: Annotated[int, Field(ge=0, description="plate number; 0 = all plates")] = 0,
    timeout_s: Annotated[int, Field(ge=10, le=7200, description="give up after this many seconds")] = 600,
) -> Metrics:
    """Slice the working variant and return predicted time, grams and per-role time. Results are cached by variant,
    so repeating a slice is free. The first successful slice becomes the session baseline."""
    s = sess(session_id)
    with s.lock:
        v = s.variant.copy()
    v.plate = plate
    m = runner().evaluate(s.source, v, timeout_s=timeout_s, **_kw(s))
    with s.lock:
        if m.ok and s.baseline is None:
            s.baseline = m
    return m


@mcp.tool(annotations=_ann("Slice variants in parallel"))
def evaluate_batch(
    session_id: SessionId,
    variants: Annotated[list[VariantSpec], Field(min_length=1, max_length=32,
                                                 description="change sets applied on top of the working variant")],
    plate: Annotated[int, Field(ge=0, description="plate number; 0 = all")] = 0,
    workers: Annotated[int, Field(ge=1, le=8, description="parallel slicer processes")] = 2,
    force: Annotated[bool, Field(description="allow keys outside the catalog (syntax-checked only)")] = False,
) -> list[BatchRow]:
    """Slice several variants in parallel; rows sorted fastest first (failures last), each carrying its
    input_index and the full variant that was sliced. The session's working variant is not changed."""
    s = sess(session_id)
    vs = []
    for spec in variants:
        with s.lock:
            v = s.variant.copy()
        v.plate = plate
        v.overrides.update({k: validate(k, x, force) for k, x in spec.overrides.items()})
        v.object_rotation.update({k: tuple(x) for k, x in spec.object_rotation.items()})
        for oid, kv in spec.object_overrides.items():
            v.object_overrides.setdefault(oid, {}).update({k: validate(k, x, force) for k, x in kv.items()})
        v.flags.update(_flags(spec.flags))
        vs.append(v)
    out = opt.evaluate_batch(runner(), s.source, vs, workers, **_kw(s))
    rows = [BatchRow(input_index=i, variant=VariantOut.of(v), metrics=m) for i, (v, m) in enumerate(zip(vs, out))]
    return sorted(rows, key=lambda r: (not r.metrics.ok, r.metrics.time_s))


@mcp.tool(annotations=_ann("Compare runs", read_only=True))
def compare(
    run_ids: Annotated[list[Annotated[str, Field(pattern=r"^[0-9a-f]{24}$")]],
                       Field(min_length=1, max_length=32, description="run_id values from slice results")],
) -> list[CompareRow]:
    """Metrics of earlier runs side by side, with time/grams deltas relative to the first."""
    rows: list[CompareRow] = []
    for rid in run_ids:
        m = runner()._cached(_run_dir(rid), rid, 0, failures=True, invalidate_corrupt=False)
        if m is None:
            raise ValueError(f"unknown run {rid}")
        rows.append(CompareRow(**m.model_dump()))
    for r in rows[1:]:
        r.delta_time_s = round(r.time_s - rows[0].time_s, 1)
        r.delta_g = round(r.filament_g - rows[0].filament_g, 3)
    return rows


@mcp.tool(annotations=_ann("Optimize"))
async def optimize(
    session_id: SessionId,
    ctx: Context,
    time_weight: Annotated[float, Field(ge=0, le=1, description="weight of print time in the score")] = 0.6,
    material_weight: Annotated[float, Field(ge=0, le=1, description="weight of filament use in the score")] = 0.4,
    functional: Annotated[bool, Field(description="true = keep strength floors (2 walls, 3 top layers, 10% infill, "
                                      "no lightning); false = prototype quality")] = True,
    max_time_increase_pct: Annotated[float, Field(ge=0, le=100, description="reject variants slower than this")] = 10,
    max_material_increase_pct: Annotated[float, Field(ge=0, le=100, description="reject heavier variants")] = 10,
    max_layer_height: Annotated[float | None, Field(gt=0, le=1, description="mm; default .28 without print_spec, nozzle bounds with one")] = None,
    keep_supports: Annotated[Literal["auto", "required", "forbidden"], Field(
        description="auto = slicer decides; forbidden = supports off")] = "auto",
    locked_keys: Annotated[list[str] | None, Field(description="settings the search must not change")] = None,
    locked_objects: Annotated[list[int] | None, Field(description="object ids not to rotate")] = None,
    stages: Annotated[list[Stage] | None, Field(description="subset/order of stages; default = all but adhesion (opt-in)")] = None,
    max_slices: Annotated[int, Field(ge=1, le=200, description="slice budget incl. the baseline")] = 40,
    plate: Annotated[int, Field(ge=0, description="plate number; 0 = all")] = 0,
    apply: Annotated[bool, Field(description="make the best variant the session's working variant")] = True,
    workers: Annotated[int, Field(ge=1, le=8, description="parallel slicer processes")] = 2,
    print_spec: Annotated[dict | None, Field(description="shared print specification; selected presets must match project_open")] = None,
) -> OptimizeResult:
    """Staged greedy search over orientation, supports, infill, shells and layer height (adhesion is opt-in via
    `stages`; brim removal is only tried when functional=false). Winners on different knobs within a stage are
    combined. Every result is checked against the functional floors. Reports progress per stage; roughly
    20-40 slices."""
    s = sess(session_id)
    o = opt.Objective(time_weight, material_weight, max_time_increase_pct, max_material_increase_pct, functional,
                      max_layer_height, keep_supports, tuple(locked_keys or ()), tuple(locked_objects or ()))
    with s.lock:
        base = s.variant.copy()
    base.plate = plate
    loop = asyncio.get_running_loop()

    def prog(used, total, stage):
        fut = asyncio.run_coroutine_threadsafe(ctx.report_progress(used, total, f"stage {stage} done"), loop)
        fut.add_done_callback(lambda f: f.exception() and log.warning("progress report failed: %s", f.exception()))

    res = await asyncio.to_thread(opt.optimize, runner(), s.source, base, o, stages, max_slices, workers, prog,
                                  print_spec=print_spec, **_kw(s))
    b, m = res.baseline, res.best_metrics
    with s.lock:
        if apply:
            s.variant = res.best
        if s.baseline is None:
            s.baseline = b
    return OptimizeResult(
        baseline=b, best=m,
        time_saved_pct=round(100 * (1 - m.time_s / b.time_s), 1) if b.time_s else 0.0,
        material_saved_pct=round(100 * (1 - m.filament_g / b.filament_g), 1) if b.filament_g else 0.0,
        best_variant=VariantOut.of(res.best), applied=apply, slices_used=len(res.history) + 1,
        report=res.report, history=[HistoryEntry(stage=h["stage"], overrides=h["overrides"],
                              rotation={k: list(v) for k, v in h["rotation"].items()}, flags=h["flags"],
                              ok=h["ok"], error=h["error"], time_s=h["time_s"], filament_g=h["filament_g"],
                              score=None if not math.isfinite(h["score"]) else round(h["score"], 4),
                              run_id=h["run_id"]) for h in res.history])


# ---- output --------------------------------------------------------------------------------------
@mcp.tool(annotations=_ann("Export sliced 3MF", destructive=True))
def export(
    session_id: SessionId,
    path: Annotated[str, Field(description="destination .3mf path")],
    run_id: Annotated[str, Field(pattern=r"^([0-9a-f]{24})?$",
                                 description="export this earlier run; empty = slice the working variant now")] = "",
    overwrite: Annotated[bool, Field(description="replace an existing file")] = False,
) -> ExportResult:
    """Write a sliced, reopenable 3MF (G-code included) to `path`. Refuses to overwrite an existing file unless
    overwrite=true. The session's own source copy is never overwritten."""
    s = sess(session_id)
    dst = Path(path).expanduser().resolve()
    if dst == s.source:
        raise ValueError(f"{dst} is the session's source copy and is never overwritten")
    if dst.exists() and not overwrite:
        raise ValueError(f"{dst} exists; pass overwrite=true to replace it")
    if run_id:
        src = _run_dir(run_id) / "out.3mf"
    else:
        with s.lock:
            v = s.variant.copy()
        m = runner().evaluate(s.source, v, **_kw(s))
        if not m.ok:
            raise RuntimeError(m.error)
        src = Path(m.out_3mf)
    if not src.is_file():
        raise ValueError(f"no output for run {run_id!r}")
    shutil.copy(src, dst)
    return ExportResult(path=str(dst))


@mcp.tool(annotations=_ann("Open in Bambu Studio", idempotent=False, open_world=True))
def open_in_studio(
    path: Annotated[str, Field(description="3MF file to open, e.g. from export")],
) -> LaunchResult:
    """Launch Bambu Studio on a file. With the GUI already running in single-instance mode the file loads
    there. Does not wait for the app."""
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise ValueError(f"{p} not found")
    subprocess.Popen([runner().binary, str(p)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return LaunchResult(launched=True)


@mcp.tool(annotations=_ann("Compare native assembly project", idempotent=False))
def compare_project(
    project_path: Annotated[str, Field(description="native Bambu 3MF with one whole-assembly build item")],
    output_dir: Annotated[str, Field(description="new, nonexistent directory for artifacts and comparison.json")],
    candidates: Annotated[list[dict], Field(min_length=1, max_length=12, description="named settings and whole-assembly rotation candidates")],
    print_spec: Annotated[dict | None, Field(description="shared version-1 printer, material and quality constraints")] = None,
    workers: Annotated[int, Field(ge=1, le=8, description="parallel slicer processes")] = 2,
    timeout_s: Annotated[int, Field(ge=10, le=3600, description="per-slice time limit in seconds")] = 300,
) -> dict:
    """Compare baseline and native-assembly candidates using shared constraints, strict artifact/plate checks,
    cached parallel slicing, and fastest/least-material/conservative alternatives. Preserves assembly colors
    and relative placement; returns estimate-only evidence and does not print."""
    from .pipeline import compare_project as compare_native
    return compare_native(project_path, output_dir, candidates, runner().binary, timeout_s,
                          print_spec, workers, runner().home)


@mcp.resource("bambu://catalog", mime_type="application/json")
def catalog_resource() -> str:
    """The parameter catalog as JSON."""
    return json.dumps(CATALOG, indent=2)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
