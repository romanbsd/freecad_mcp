"""Declarative design-rule-check (DRC) engine for FreeCAD MCP components.

freecad-design-rule-api-spec.md: the MCP owns rule definitions, profiles, and
the result schema; FreeCAD owns geometry/units. This module evaluates a list of
resolved rules over a component *context* (parameter values + named feature
shapes) and returns structured findings. It does not mutate the document.

A rule is `{id, type, severity, <targets>, <thresholds>, message}`. `evaluate`
returns `(findings, passed_count)` where findings exclude rules that passed.
`expand_profile` turns a versioned profile into concrete rules for a context.
"""
import FreeCAD as App

PROFILE_VERSIONS = {
    "geometry_baseline": 1,
    "cnc_plywood": 1,
    "fdm": 1,
}


# --------------------------------------------------------------------------- #
# units helpers
# --------------------------------------------------------------------------- #
def _q(x):
    return App.Units.Quantity(str(x))


def _mm(x):
    """Length value in mm as a float (works for Quantity or '12 mm')."""
    return _q(x).getValueAs("mm").Value


def tol_volume(tolerance):
    """Geometry tolerance is a length; the volume epsilon is its cube."""
    t = _mm(tolerance)
    return t * t * t


def _finding(rule, severity, message, **extra):
    out = {"id": rule.get("id", rule["type"]), "severity": severity,
           "rule": rule["type"], "message": message}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


# --------------------------------------------------------------------------- #
# rule evaluators  (return a finding with severity "pass" on success,
# the rule's declared severity on failure, or None to skip a missing target)
# --------------------------------------------------------------------------- #
def _param(ctx, name):
    return ctx["params"].get(name)


def ev_required(ctx, r):
    name = r["parameter"]
    p = _param(ctx, name)
    if p is not None and p.get("value") not in (None, ""):
        return _finding(r, "pass", "%s present" % name, parameter=name)
    return _finding(r, r.get("severity", "error"), "%s is required" % name, parameter=name)


def _cmp_param(ctx, r, op, kind):
    name = r["parameter"]
    p = _param(ctx, name)
    if p is None:
        return None  # profile rule targeting an absent param: not applicable
    bound = r.get("minimum") if kind == "min" else r.get("maximum")
    if bound is None:
        return None
    val = p["value"]
    try:
        a, b = _mm(val), _mm(bound)
    except Exception:
        a, b = float(str(val).split()[0]), float(str(bound).split()[0])
    ok = a >= b if kind == "min" else a <= b
    if ok:
        return _finding(r, "pass", "%s within bound" % name, parameter=name,
                        actual=str(val), required=("%s %s" % (op, bound)))
    return _finding(r, r.get("severity", "warning"),
                    r.get("message", "%s %s is %s; required %s %s"
                          % (name, "below" if kind == "min" else "above", val, op, bound)),
                    parameter=name, actual=str(val), required=str(bound),
                    suggested_parameter_change={name: str(bound)})


def ev_param_min(ctx, r):
    return _cmp_param(ctx, r, ">=", "min")


def ev_param_max(ctx, r):
    return _cmp_param(ctx, r, "<=", "max")


def ev_enum(ctx, r):
    name = r["parameter"]
    p = _param(ctx, name)
    if p is None:
        return None
    opts = r.get("values") or p.get("enum") or []
    if str(p["value"]) in [str(o) for o in opts]:
        return _finding(r, "pass", "%s in allowed set" % name, parameter=name)
    return _finding(r, r.get("severity", "error"),
                    r.get("message", "%s=%s not in %s" % (name, p["value"], opts)),
                    parameter=name, actual=str(p["value"]), required=str(opts))


def _feat(ctx, fid):
    fe = ctx["features"].get(fid)
    if not fe or fe.get("shape") is None:
        return None
    return fe


def ev_geometry_valid(ctx, r):
    fe = _feat(ctx, r["feature"])
    if fe is None:
        return None
    if fe["shape"].isValid():
        return _finding(r, "pass", "%s geometry valid" % r["feature"], target=r["feature"])
    return _finding(r, r.get("severity", "error"),
                    "%s has invalid geometry" % r["feature"], target=r["feature"])


def ev_requires_solid(ctx, r):
    fe = _feat(ctx, r["feature"])
    if fe is None:
        return None
    n = len(fe["shape"].Solids)
    if n >= 1:
        return _finding(r, "pass", "%s is a solid" % r["feature"], target=r["feature"])
    return _finding(r, r.get("severity", "error"),
                    "%s produced no solid" % r["feature"], target=r["feature"],
                    actual="%d solids" % n, required=">= 1 solid")


def _pair(ctx, r):
    fs = r["features"]
    a, b = _feat(ctx, fs[0]), _feat(ctx, fs[1])
    if a is None or b is None:
        return None, None
    return a, b


def _common_volume(a, b):
    try:
        return a["shape"].common(b["shape"]).Volume
    except Exception:
        return 0.0


def ev_must_intersect(ctx, r):
    a, b = _pair(ctx, r)
    if a is None:
        return None
    vol = _common_volume(a, b)
    if vol > tol_volume(ctx["tolerance"]):
        return _finding(r, "pass", "%s intersect" % (r["features"],), features=r["features"])
    return _finding(r, r.get("severity", "error"),
                    r.get("message", "%s do not intersect" % (r["features"],)),
                    features=r["features"], actual="overlap %.3g mm^3" % vol,
                    required="> tolerance")


def ev_must_not_intersect(ctx, r):
    a, b = _pair(ctx, r)
    if a is None:
        return None
    vol = _common_volume(a, b)
    if vol <= tol_volume(ctx["tolerance"]):
        return _finding(r, "pass", "%s clear" % (r["features"],), features=r["features"])
    return _finding(r, r.get("severity", "error"),
                    r.get("message", "%s overlap (collision)" % (r["features"],)),
                    features=r["features"], actual="overlap %.3g mm^3" % vol,
                    required="<= tolerance")


def ev_minimum_overlap(ctx, r):
    a, b = _pair(ctx, r)
    if a is None:
        return None
    common = a["shape"].common(b["shape"])
    if common.Volume <= tol_volume(ctx["tolerance"]):
        depth = 0.0
    else:
        bb = common.BoundBox
        depth = min(bb.XLength, bb.YLength, bb.ZLength)
    req = _mm(r["minimum_overlap"])
    if depth >= req:
        return _finding(r, "pass", "overlap ok", features=r["features"])
    return _finding(r, r.get("severity", "error"),
                    r.get("message", "%s overlap %.2f mm < %s"
                          % (r["features"], depth, r["minimum_overlap"])),
                    features=r["features"], actual="%.2f mm" % depth,
                    required=str(r["minimum_overlap"]))


def ev_touching_allowed(ctx, r):
    a, b = _pair(ctx, r)
    if a is None:
        return None
    vol = _common_volume(a, b)
    if vol <= tol_volume(ctx["tolerance"]):
        return _finding(r, "pass", "touching/clear ok", features=r["features"])
    return _finding(r, r.get("severity", "error"),
                    r.get("message", "%s overlap beyond touching" % (r["features"],)),
                    features=r["features"], actual="overlap %.3g mm^3" % vol,
                    required="touching only")


def _distance(a, b):
    return a["shape"].distToShape(b["shape"])[0]


def ev_minimum_clearance(ctx, r):
    a, b = _pair(ctx, r)
    if a is None:
        return None
    d = _distance(a, b)
    req = _mm(r["minimum_clearance"])
    if d >= req:
        return _finding(r, "pass", "clearance ok", features=r["features"])
    return _finding(r, r.get("severity", "warning"),
                    r.get("message", "%s clearance %.2f mm < %s"
                          % (r["features"], d, r["minimum_clearance"])),
                    features=r["features"], actual="%.2f mm" % d,
                    required=str(r["minimum_clearance"]))


def ev_maximum_gap(ctx, r):
    a, b = _pair(ctx, r)
    if a is None:
        return None
    d = _distance(a, b)
    req = _mm(r["maximum_gap"])
    if d <= req:
        return _finding(r, "pass", "gap ok", features=r["features"])
    return _finding(r, r.get("severity", "warning"),
                    r.get("message", "%s gap %.2f mm > %s"
                          % (r["features"], d, r["maximum_gap"])),
                    features=r["features"], actual="%.2f mm" % d,
                    required=str(r["maximum_gap"]))


def _bbox_of(ctx, r):
    if r.get("feature"):
        fe = _feat(ctx, r["feature"])
        return fe["shape"].BoundBox if fe else None
    bb = None
    for fe in ctx["features"].values():
        if fe.get("role", "output") != "output" or fe.get("shape") is None:
            continue
        b = fe["shape"].BoundBox
        bb = b if bb is None else bb.united(b)
    return bb


def ev_maximum_dimensions(ctx, r):
    bb = _bbox_of(ctx, r)
    if bb is None:
        return None
    dims = {"x": bb.XLength, "y": bb.YLength, "z": bb.ZLength}
    lim = r["maximum"]
    over = [k for k, v in lim.items() if dims[k] > _mm(v) + _mm(ctx["tolerance"])]
    if not over:
        return _finding(r, "pass", "within envelope", target=r.get("feature", "component"))
    return _finding(r, r.get("severity", "error"),
                    r.get("message", "exceeds maximum on %s" % over),
                    target=r.get("feature", "component"),
                    actual={k: round(dims[k], 2) for k in over},
                    required={k: lim[k] for k in over})


def ev_minimum_dimensions(ctx, r):
    bb = _bbox_of(ctx, r)
    if bb is None:
        return None
    dims = {"x": bb.XLength, "y": bb.YLength, "z": bb.ZLength}
    lim = r["minimum"]
    under = [k for k, v in lim.items() if dims[k] < _mm(v) - _mm(ctx["tolerance"])]
    if not under:
        return _finding(r, "pass", "meets minimum dimensions",
                        target=r.get("feature", "component"))
    return _finding(r, r.get("severity", "warning"),
                    r.get("message", "below minimum on %s" % under),
                    target=r.get("feature", "component"),
                    actual={k: round(dims[k], 2) for k in under},
                    required={k: lim[k] for k in under})


def ev_requires_tagged_feature(ctx, r):
    wanted = set(r.get("any_of", []))
    for fe in ctx["features"].values():
        if wanted & set(fe.get("tags", [])):
            return _finding(r, "pass", "tagged feature present", required=str(sorted(wanted)))
    return _finding(r, r.get("severity", "warning"),
                    r.get("message", "no feature tagged %s" % sorted(wanted)),
                    required=str(sorted(wanted)), actual="none")


def ev_no_cyclic_derived(ctx, r):
    # Cycles are rejected at define time; here it is structurally always satisfied.
    return _finding(r, "pass", "no derived expression cycles")


def ev_info_note(ctx, r):
    return _finding(r, "info", r.get("message", ""), target=r.get("target"))


_EVAL = {
    "required": ev_required,
    "parameter_minimum": ev_param_min,
    "parameter_maximum": ev_param_max,
    "enum": ev_enum,
    "geometry_valid": ev_geometry_valid,
    "requires_solid": ev_requires_solid,
    "must_intersect": ev_must_intersect,
    "must_not_intersect": ev_must_not_intersect,
    "minimum_overlap": ev_minimum_overlap,
    "touching_allowed": ev_touching_allowed,
    "minimum_clearance": ev_minimum_clearance,
    "maximum_gap": ev_maximum_gap,
    "maximum_dimensions": ev_maximum_dimensions,
    "minimum_dimensions": ev_minimum_dimensions,
    "requires_tagged_feature": ev_requires_tagged_feature,
    "no_cyclic_derived": ev_no_cyclic_derived,
    "info_note": ev_info_note,
}


def evaluate(ctx, rules):
    """Run rules over the context. Returns (findings, passed_count)."""
    results = []
    for r in rules:
        fn = _EVAL.get(r.get("type"))
        if fn is None:
            # A typo'd rule type used to be dropped silently, reporting a clean
            # pass for a check that never ran. Make it visible instead.
            results.append(_finding(r, "error", "unknown rule type %r" % r.get("type")))
            continue
        try:
            res = fn(ctx, r)
        except Exception as e:  # a rule never crashes the whole run
            res = _finding(r, "error", "rule errored: %s" % e)
        if res is not None:
            results.append(res)
    findings = [f for f in results if f["severity"] != "pass"]
    passed = sum(1 for f in results if f["severity"] == "pass")
    return findings, passed


# --------------------------------------------------------------------------- #
# profiles  (expand to concrete rules for a given context)
# --------------------------------------------------------------------------- #
def _outputs(ctx):
    return [fid for fid, fe in ctx["features"].items()
            if fe.get("role", "output") == "output"]


def _cuts(ctx):
    return [f for f in ctx.get("graph", []) if f.get("type") == "cut"]


def _expand_geometry_baseline(ctx, th):
    rules = [{"id": "no_cyclic_derived", "type": "no_cyclic_derived", "severity": "error"}]
    # enforce each input parameter's declared bounds/enum (schema-level ranges)
    for name, p in ctx["params"].items():
        if p.get("kind") == "derived":
            continue
        if p.get("min") is not None:
            rules.append({"id": "range_min:%s" % name, "type": "parameter_minimum",
                          "severity": "error", "parameter": name, "minimum": p["min"]})
        if p.get("max") is not None:
            rules.append({"id": "range_max:%s" % name, "type": "parameter_maximum",
                          "severity": "error", "parameter": name, "maximum": p["max"]})
        if p.get("enum"):
            rules.append({"id": "range_enum:%s" % name, "type": "enum",
                          "severity": "error", "parameter": name})
    for fid in _outputs(ctx):
        rules.append({"id": "geometry_valid:%s" % fid, "type": "geometry_valid",
                      "severity": "error", "feature": fid})
        rules.append({"id": "requires_solid:%s" % fid, "type": "requires_solid",
                      "severity": "error", "feature": fid})
    for cut in _cuts(ctx):
        rules.append({"id": "tool_intersects:%s" % cut["id"], "type": "must_intersect",
                      "severity": "error", "features": [cut["base"], cut["tool"]],
                      "message": "cut tool %r must intersect %r" % (cut["tool"], cut["base"])})
    return rules


def _thickness_params(ctx):
    return [n for n in ctx["params"] if "thickness" in n.lower()]


def _expand_cnc_plywood(ctx, th):
    rules = []
    minwall = th.get("min_wall_thickness", "6 mm")
    for n in _thickness_params(ctx):
        rules.append({"id": "min_wall:%s" % n, "type": "parameter_minimum",
                      "severity": "warning", "parameter": n, "minimum": minwall})
    stock = th.get("stock_thicknesses")
    if stock:
        for n in _thickness_params(ctx):
            rules.append({"id": "stock:%s" % n, "type": "enum", "severity": "warning",
                          "parameter": n, "values": stock,
                          "message": "%s must be an allowed stock thickness" % n})
    # minimum hole diameter: cut-tool cylinders
    minhole = th.get("min_hole_diameter", "3 mm")
    idmeta = {f["id"]: f for f in ctx.get("graph", [])}
    for cut in _cuts(ctx):
        tool = idmeta.get(cut["tool"], {})
        if tool.get("type") == "cylinder" and tool.get("radius"):
            rules.append({"id": "min_hole:%s" % cut["tool"], "type": "info_note",
                          "severity": "info", "target": cut["tool"],
                          "message": "min hole diameter %s checked structurally; "
                                     "verify %s diameter >= %s" % (minhole, cut["tool"], minhole)})
    rules.append({"id": "inside_corner_radius", "type": "info_note", "severity": "info",
                  "message": "minimum inside-corner radius (cutter radius) not evaluated: "
                             "requires topological corner detection (out of scope v1)"})
    return rules


def _expand_fdm(ctx, th):
    rules = []
    minwall = th.get("min_wall_thickness", "0.8 mm")
    for name in _thickness_params(ctx):
        rules.append({
            "id": "fdm_min_wall:%s" % name,
            "type": "parameter_minimum", "severity": "warning",
            "parameter": name, "minimum": minwall,
        })
    rules.extend([
        {
            "id": "fdm_nozzle", "type": "info_note", "severity": "info",
            "message": "FDM profile assumes nozzle diameter %s" %
                       th.get("nozzle_diameter", "0.4 mm"),
        },
        {
            "id": "fdm_clearance", "type": "info_note", "severity": "info",
            "message": "verify mating-part clearance >= %s" %
                       th.get("minimum_clearance", "0.2 mm"),
        },
    ])
    return rules


_EXPANDERS = {
    "geometry_baseline": _expand_geometry_baseline,
    "cnc_plywood": _expand_cnc_plywood,
    "fdm": _expand_fdm,
}


def expand_profile(profile_id, ctx, thresholds=None):
    """Return (version, rules) for a profile id, or (None, []) if unknown."""
    fn = _EXPANDERS.get(profile_id)
    if fn is None:
        return None, []
    return PROFILE_VERSIONS[profile_id], fn(ctx, thresholds or {})
