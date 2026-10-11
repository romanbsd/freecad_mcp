"""Staged greedy search. Each stage = one parallel batch; keep the best feasible variant and move on.

ponytail: coordinate descent, no Bayesian/genetic search. Revisit if benchmarks leave >5% on the table.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .catalog import CATALOG
from .runner import Metrics, Runner, Variant
from .threemf import Package

AXIS_POSES = [(90, 0, 0), (-90, 0, 0), (0, 90, 0), (0, -90, 0), (180, 0, 0)]
MIN_GAIN = 0.005
# adhesion is opt-in: a successful slice proves nothing about bed adhesion
DEFAULT_STAGES = ["orientation", "support", "infill", "shells", "layer_height"]


def floor(key: str, functional: bool) -> int:
    """Lowest value the search may try. Functional parts keep the catalog floor; otherwise half of it."""
    f = CATALOG[key]["floor"]
    return f if functional else max(CATALOG[key]["range"][0], f // 2)


@dataclass
class Objective:
    time: float = 0.6
    material: float = 0.4
    max_time_increase_pct: float = 10.0
    max_material_increase_pct: float = 10.0
    functional: bool = True       # floors: 2 walls, 3 top layers, 10% infill, no lightning
    max_layer_height: float = 0.28
    keep_supports: str = "auto"    # auto | required | forbidden
    locked_keys: tuple = ()
    locked_objects: tuple = ()


def score(m: Metrics, base: Metrics, o: Objective) -> float:
    if not m.ok:
        return float("-inf")
    t = base.time_s / m.time_s if m.time_s else 1.0
    g = base.filament_g / m.filament_g if m.filament_g else 1.0
    return o.time * t + o.material * g


def feasible(m: Metrics, base: Metrics, o: Objective) -> bool:
    if not m.ok:
        return False
    if base.time_s and m.time_s > base.time_s * (1 + o.max_time_increase_pct / 100):
        return False
    if base.filament_g and m.filament_g > base.filament_g * (1 + o.max_material_increase_pct / 100):
        return False
    return True


def _pct(v) -> float | None:
    try:
        return float(str(v).rstrip("%"))
    except ValueError:
        return None


def _violations(settings: dict, o: Objective) -> list[str]:
    """Functional-floor / limit breaches in one flat {key: value-string} settings dict."""
    bad = []
    for k in ("wall_loops", "top_shell_layers", "bottom_shell_layers"):
        v = _pct(settings.get(k, ""))
        if v is not None and v < floor(k, o.functional):
            bad.append(f"{k}={settings[k]} below floor {floor(k, o.functional)}")
    d = _pct(settings.get("sparse_infill_density", ""))
    if d is not None and d < floor("sparse_infill_density", o.functional):
        bad.append(f"sparse_infill_density={settings['sparse_infill_density']} below floor")
    if o.functional and settings.get("sparse_infill_pattern") == "lightning":
        bad.append("lightning infill in a functional part")
    h = _pct(settings.get("layer_height", ""))
    if h is not None and h > o.max_layer_height + 1e-9:
        bad.append(f"layer_height={settings['layer_height']} above max {o.max_layer_height}")
    return bad


def violations(v: Variant, base: Variant, source, o: Objective) -> list[str]:
    """Breaches in the *effective* settings of v (global + per-object, incl. values stored in the 3MF).
    Values identical to the baseline's are the user's own choice and are exempt."""
    out = _violations({k: x for k, x in v.overrides.items() if base.overrides.get(k) != x}, o)
    try:
        stored = Package(source).object_settings()
    except (OSError, ValueError, KeyError):
        stored = {}
    for oid in set(v.object_overrides) | set(stored):
        eff = {**stored.get(oid, {}), **v.object_overrides.get(oid, {})}
        ref = {**stored.get(oid, {}), **base.object_overrides.get(oid, {})}
        out += [f"object {oid}: {m}" for m in _violations({k: x for k, x in eff.items() if ref.get(k) != x}, o)]
    return out


def evaluate_batch(runner: Runner, source, variants: list[Variant], workers: int = 2, **kw) -> list[Metrics]:
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        return list(ex.map(lambda v: runner.evaluate(source, v, **kw), variants))


def _with(v: Variant, **overrides) -> Variant:
    n = v.copy()
    n.overrides.update({k: str(x) for k, x in overrides.items()})
    return n


def _changed(c: Variant, ref: Variant) -> set:
    """Knobs where c differs from ref, as hashable ids (a rotation or flag change counts as one knob)."""
    k = {("o", x) for x, v in c.overrides.items() if ref.overrides.get(x) != v}
    k |= {("r", x) for x, v in c.object_rotation.items() if ref.object_rotation.get(x) != v}
    k |= {("f", x) for x, v in c.flags.items() if ref.flags.get(x) != v}
    return k


def _merge_into(dst: Variant, src: Variant, ref: Variant):
    for x, v in src.overrides.items():
        if ref.overrides.get(x) != v:
            dst.overrides[x] = v


def candidates(stage: str, cur: Variant, source, o: Objective) -> list[Variant]:
    lock = set(o.locked_keys)
    out: list[Variant] = []

    def add(**kw):
        if not (set(kw) & lock):
            out.append(_with(cur, **kw))

    if stage == "orientation":
        locked = set(map(str, o.locked_objects))
        if not locked:  # auto-orient touches every object, so it is off the table when any is locked
            a = cur.copy()
            a.flags["orient"] = 1
            out.append(a)
        for oid in Package(source).items():
            if oid in locked:
                continue
            for pose in AXIS_POSES:
                n = cur.copy()
                n.object_rotation[oid] = pose
                n.flags["orient"] = 0   # auto-orient would supersede the explicit pose
                n.flags["arrange"] = 1   # footprint changes; avoid collisions
                out.append(n)
    elif stage == "support" and o.keep_supports != "forbidden":
        for t, s in [("tree(auto)", "tree_hybrid"), ("tree(auto)", "tree_slim"),
                     ("normal(auto)", "snug"), ("tree(auto)", "tree_organic")]:
            add(support_type=t, support_style=s)
        for ang in (40, 50):
            add(support_threshold_angle=ang)
    elif stage == "infill":
        lo = floor("sparse_infill_density", o.functional)
        for d in (15, 10, 7, 5):
            if d >= lo:
                add(sparse_infill_density=f"{d}%")
        pats = ["gyroid", "adaptivecubic"] + ([] if o.functional else ["lightning"])
        for p in pats:
            add(sparse_infill_pattern=p)
    elif stage == "shells":
        for w in range(floor("wall_loops", o.functional), 5):
            add(wall_loops=w)
        add(top_shell_layers=floor("top_shell_layers", o.functional))
    elif stage == "layer_height":
        for h in (0.2, 0.24, 0.28):
            if h <= o.max_layer_height:
                add(layer_height=h)
    elif stage == "adhesion":
        if not o.functional:  # brim removal can cost bed adhesion; slicing cannot tell
            add(brim_type="no_brim")
        add(skirt_loops=0)
        add(flush_into_infill=1)
    return out


@dataclass
class Result:
    baseline: Metrics
    best: Variant
    best_metrics: Metrics
    history: list = field(default_factory=list)   # [{stage, overrides, rotation, flags, time_s, filament_g, ok, error, score, run_id}]


def optimize(runner: Runner, source, base_variant: Variant, o: Objective, stages=None, max_slices: int = 40,
             workers: int = 2, progress=None, **kw) -> Result:
    if o.locked_objects and base_variant.flags.get("orient") == 1:
        raise ValueError("auto-orient (orient=1) rotates every object, which conflicts with locked_objects; "
                         "turn it off first (orient enable=false)")
    if o.keep_supports in ("required", "forbidden"):  # pin it; the baseline already reflects the pin
        base_variant = _with(base_variant, enable_support=int(o.keep_supports == "required"))
    base = runner.evaluate(source, base_variant, **kw)
    if not base.ok:
        raise RuntimeError(f"baseline slice failed: {base.error}")
    cur, cur_m, cur_s = base_variant, base, score(base, base, o)
    res = Result(base, cur, cur_m)
    used = 1
    for stage in (DEFAULT_STAGES if stages is None else stages):
        start = cur  # combine relative to the variant entering this stage, not the baseline
        cands = candidates(stage, cur, source, o)[: max(0, max_slices - used)]
        if not cands:
            continue
        ms = evaluate_batch(runner, source, cands, workers, **kw)
        used += len(cands)
        best_i, best_s = None, cur_s
        cur_s0 = cur_s
        scored = []
        for i, (c, m) in enumerate(zip(cands, ms)):
            bad = violations(c, base_variant, source, o)
            s = score(m, base, o) if feasible(m, base, o) and not bad else float("-inf")
            scored.append(s)
            res.history.append({"stage": stage, "overrides": c.overrides, "rotation": c.object_rotation,
                                "flags": c.flags, "time_s": m.time_s, "filament_g": m.filament_g,
                                "ok": m.ok, "error": m.error or "; ".join(bad), "score": s, "run_id": m.run_id})
            if s > best_s + MIN_GAIN:  # ignore noise-level wins (placement jitter)
                best_i, best_s = i, s
        if best_i is not None:
            cur, cur_m, cur_s = cands[best_i], ms[best_i], best_s
            # combine winners that touch different knobs (e.g. density + pattern) with one extra slice
            if used < max_slices:
                merged, touched = cands[best_i].copy(), _changed(cands[best_i], start)
                n = 1
                for j in sorted((j for j, x in enumerate(scored) if x > cur_s0 + MIN_GAIN and j != best_i),
                                key=lambda j: -scored[j]):
                    k = _changed(cands[j], start)
                    if k & touched or any(t[0] != "o" for t in k):  # settings-only merges
                        continue
                    _merge_into(merged, cands[j], start)
                    touched |= k
                    n += 1
                if n > 1 and not violations(merged, base_variant, source, o):
                    mm = runner.evaluate(source, merged, **kw)
                    used += 1
                    ms2 = score(mm, base, o) if feasible(mm, base, o) else float("-inf")
                    res.history.append({"stage": stage + "+combined", "overrides": merged.overrides,
                                        "rotation": merged.object_rotation, "flags": merged.flags,
                                        "time_s": mm.time_s, "filament_g": mm.filament_g, "ok": mm.ok,
                                        "error": mm.error, "score": ms2, "run_id": mm.run_id})
                    if ms2 > cur_s + MIN_GAIN:
                        cur, cur_m, cur_s = merged, mm, ms2
            res.best, res.best_metrics = cur, cur_m
        if progress:
            progress(used, max_slices, stage)
        if used >= max_slices:
            break
    return res
