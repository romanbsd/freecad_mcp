"""Parametric component workflow for FreeCAD MCP.

A thin declarative layer over FreeCAD's *native* expression + dependency engine
(parametric-spec.md). A component is an App::Part container holding a parameter
host (App::FeaturePython with typed dynamic properties) and groups of generated
features whose dimensions/placement are bound to the host via FreeCAD
expressions. FreeCAD itself does the unit math, derived-value evaluation,
incremental rebuild, and dependency tracking — we only translate the spec's
`$param` syntax and orchestrate.

Each public op_* function takes the MCP params and returns a JSON-able dict.
They are dispatched by FreeCADMCPServer in freecad_mcp.py.
"""
import json
import os
import re
import tempfile

import FreeCAD as App

_TOKEN = re.compile(r"\$([A-Za-z_]\w*)")

# spec parameter type -> FreeCAD property type
TYPE_MAP = {
    "length": "App::PropertyLength",
    "angle": "App::PropertyAngle",
    "bool": "App::PropertyBool",
    "boolean": "App::PropertyBool",
    "int": "App::PropertyInteger",
    "integer": "App::PropertyInteger",
    "float": "App::PropertyFloat",
    "number": "App::PropertyFloat",
    "string": "App::PropertyString",
    "text": "App::PropertyString",
    "enum": "App::PropertyEnumeration",
    "enumeration": "App::PropertyEnumeration",
}

_GROUPS = ("Features", "Construction", "Validation", "Variants")
_MAX_RENDER_DIMENSION = 4096
_MAX_RENDER_PIXELS = 16 * 1024 * 1024


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _gui():
    try:
        import FreeCADGui as Gui
        return Gui
    except Exception:
        return None


def _translate(expr, host_name):
    """Spec expression -> FreeCAD expression ($param -> Host.param)."""
    return _TOKEN.sub(lambda m: "%s.%s" % (host_name, m.group(1)), str(expr))


def _kind(p):
    return p.get("kind") or ("derived" if p.get("expression") else "input")


def _prop_type(p):
    return TYPE_MAP.get(str(p.get("type", "length")).lower(), "App::PropertyFloat")


def _set_value(obj, name, ptype, value):
    if ptype in ("App::PropertyLength", "App::PropertyAngle"):
        setattr(obj, name, App.Units.Quantity(str(value)))
    elif ptype == "App::PropertyBool":
        setattr(obj, name, bool(value))
    elif ptype == "App::PropertyInteger":
        setattr(obj, name, int(value))
    elif ptype == "App::PropertyFloat":
        setattr(obj, name, float(value))
    else:  # string / enumeration
        setattr(obj, name, str(value))


def _bind(obj, prop_path, value, host_name):
    """Bind a feature property (or .Placement.Base.x style path). String values
    become FreeCAD expressions (so units + $params work); numbers are set."""
    if isinstance(value, str):
        obj.setExpression(prop_path, _translate(value, host_name))
    elif value is not None:
        setattr(obj, prop_path, value)


def _load(container):
    return json.loads(container.mcp_meta or "{}")


def _save(container, meta):
    container.mcp_meta = json.dumps(meta)


def _resolve(component_id):
    if not str(component_id).startswith("component://"):
        raise ValueError("component_id must look like component://<doc>/<name>")
    docname, _, name = component_id[len("component://"):].partition("/")
    if docname not in App.listDocuments():
        raise ValueError("document %r is not open" % docname)
    doc = App.getDocument(docname)
    for o in doc.Objects:
        if getattr(o, "mcp_component_id", None) == component_id:
            return doc, o, _load(o)
    raise ValueError("component %r not found in %r" % (name, docname))


def _host(doc, meta):
    return doc.getObject(meta["host"])


def _group(doc, meta, which):
    return doc.getObject(meta["groups"][which])


def _begin(doc, label):
    # Programmatically-created documents have undo OFF by default, which makes
    # openTransaction/abortTransaction no-ops. Enable it so a failed rebuild
    # actually rolls back (and the user gets undo history). Idempotent.
    doc.UndoMode = 1
    doc.openTransaction(label)


def _abort(doc):
    try:
        doc.abortTransaction()
    except Exception:
        pass


def _component_objects(doc, meta, extra=()):
    """Live objects owned by one component, optionally including new objects.

    A document may contain unrelated, invalid FreeCAD objects. Component
    operations must not treat those as failures of this component.
    """
    names = {meta.get("host")}
    names.update(meta.get("id_map", {}).values())
    objects = []
    seen = set()
    for name in names:
        obj = doc.getObject(name) if name else None
        if obj is not None and obj.Name not in seen:
            objects.append(obj)
            seen.add(obj.Name)
    for obj in extra:
        if obj is not None and obj.Name not in seen:
            objects.append(obj)
            seen.add(obj.Name)
    return objects


def _errors(objects):
    """Names of component objects left in an error/invalid state."""
    return [o.Name for o in objects
            if any(s in ("Error", "Invalid") for s in o.State)]


def _feature_solids(doc, meta):
    """Top-level generated solids (boolean inputs were removed from Features)."""
    grp = _group(doc, meta, "Features")
    out = []
    for o in grp.Group:
        if hasattr(o, "Shape") and o.Shape and not o.Shape.isNull():
            out.append(o)
    return out


def _merge_bbox(objs):
    bb = None
    for o in objs:
        try:
            b = o.Shape.BoundBox
        except Exception:
            continue
        bb = b if bb is None else bb.united(b)
    if bb is None:
        return None
    return {"xmin": bb.XMin, "ymin": bb.YMin, "zmin": bb.ZMin,
            "xmax": bb.XMax, "ymax": bb.YMax, "zmax": bb.ZMax,
            "size": [bb.XLength, bb.YLength, bb.ZLength]}


def _render_dimensions(width, height):
    """Validate image dimensions before asking the GUI to allocate a bitmap."""
    try:
        width, height = int(width), int(height)
    except (TypeError, ValueError):
        raise ValueError("render width and height must be integers")
    if width < 1 or height < 1:
        raise ValueError("render width and height must be positive")
    if width > _MAX_RENDER_DIMENSION or height > _MAX_RENDER_DIMENSION:
        raise ValueError("render dimensions may not exceed %d pixels" % _MAX_RENDER_DIMENSION)
    if width * height > _MAX_RENDER_PIXELS:
        raise ValueError("render image may not exceed %d pixels" % _MAX_RENDER_PIXELS)
    return width, height


def _axis_rotation(axis):
    a = (axis or "z").lower()
    if a == "x":
        return App.Rotation(App.Vector(0, 1, 0), 90)
    if a == "y":
        return App.Rotation(App.Vector(1, 0, 0), -90)
    return App.Rotation()


def _apply_material(obj, name):
    if not name:
        return
    try:
        import Materials
        mm = Materials.MaterialManager()
        for _u, m in mm.Materials.items():
            if m.Name == name and "ShapeMaterial" in obj.PropertiesList:
                obj.ShapeMaterial = m
                return
    except Exception:
        pass  # material library unavailable or name not found; name kept in graph


# --------------------------------------------------------------------------- #
# parameter registry
# --------------------------------------------------------------------------- #
_ROLE_GROUP = {"output": "Features", "construction": "Construction",
               "inspection": "Construction", "tool": "Construction"}


def _check_cycles(parameters):
    """Reject cyclic derived expressions before any setExpression (PARAMETER_CYCLE)."""
    derived = {p["name"]: set(_TOKEN.findall(p.get("expression", "")))
               for p in parameters if _kind(p) == "derived"}
    names = set(derived)
    color = {n: 0 for n in names}  # 0=unvisited 1=in-stack 2=done

    def visit(n):
        color[n] = 1
        for m in derived[n] & names:
            if color[m] == 1:
                raise ValueError("PARAMETER_CYCLE: derived parameters form a cycle at %r" % m)
            if color[m] == 0:
                visit(m)
        color[n] = 2

    for n in names:
        if color[n] == 0:
            visit(n)


def _add_param(host, p):
    name = p["name"]
    ptype = _prop_type(p)
    kind = _kind(p)
    group = "Derived" if kind == "derived" else "Parameters"
    if name not in host.PropertiesList:
        host.addProperty(ptype, name, group, p.get("description", ""))
    if ptype == "App::PropertyEnumeration" and p.get("enum"):
        setattr(host, name, list(p["enum"]))
    if kind == "derived":
        host.setExpression(name, _translate(p["expression"], host.Name))
        host.setEditorMode(name, 1)  # read-only in the GUI
    elif p.get("default") is not None:
        _set_value(host, name, ptype, p["default"])
    return kind


# --------------------------------------------------------------------------- #
# feature build graph
# --------------------------------------------------------------------------- #
def _build_feature(doc, host, feat, id_map):
    """Create one feature object and bind its expressions. Returns the object."""
    ftype = feat["type"]
    hn = host.Name
    pos = feat.get("position") or {}

    def place(obj):
        for ax in ("x", "y", "z"):
            if ax in pos:
                _bind(obj, ".Placement.Base.%s" % ax, pos[ax], hn)

    if ftype == "box":
        obj = doc.addObject("Part::Box", feat["id"])
        size = feat.get("size") or {}
        _bind(obj, "Length", size.get("x"), hn)
        _bind(obj, "Width", size.get("y"), hn)
        _bind(obj, "Height", size.get("z"), hn)
        place(obj)
    elif ftype == "cylinder":
        obj = doc.addObject("Part::Cylinder", feat["id"])
        _bind(obj, "Radius", feat.get("radius"), hn)
        _bind(obj, "Height", feat.get("height"), hn)
        obj.Placement.Rotation = _axis_rotation(feat.get("axis"))
        place(obj)
    elif ftype == "cone":
        obj = doc.addObject("Part::Cone", feat["id"])
        _bind(obj, "Radius1", feat.get("radius1"), hn)
        _bind(obj, "Radius2", feat.get("radius2"), hn)
        _bind(obj, "Height", feat.get("height"), hn)
        obj.Placement.Rotation = _axis_rotation(feat.get("axis"))
        place(obj)
    elif ftype == "prism":
        obj = doc.addObject("Part::Prism", feat["id"])
        if feat.get("sides") is not None:
            obj.Polygon = int(feat["sides"])
        _bind(obj, "Circumradius", feat.get("radius") or feat.get("circumradius"), hn)
        _bind(obj, "Height", feat.get("height"), hn)
        place(obj)
    elif ftype == "transform":
        obj = id_map[feat["base"]]  # mutate the referenced feature's placement
        obj.Placement.Rotation = _axis_rotation(feat.get("axis"))
        for ax in ("x", "y", "z"):
            if (feat.get("translate") or {}).get(ax) is not None:
                _bind(obj, ".Placement.Base.%s" % ax, feat["translate"][ax], hn)
        return obj  # not a new object
    elif ftype in ("cut", "union", "intersection"):
        obj = _build_boolean(doc, feat, id_map)
    elif ftype == "array":
        obj = _build_array(doc, host, feat, id_map)
    elif ftype == "group":
        obj = doc.addObject("App::DocumentObjectGroup", feat["id"])
    else:
        raise ValueError("unknown feature type %r" % ftype)

    if feat.get("label"):
        obj.Label = feat["label"]
    _apply_material(obj, feat.get("material"))
    return obj


def _build_boolean(doc, feat, id_map):
    ftype = feat["type"]
    if ftype == "cut":
        obj = doc.addObject("Part::Cut", feat["id"])
        obj.Base = id_map[feat["base"]]
        obj.Tool = id_map[feat["tool"]]
        consumed = [feat["base"], feat["tool"]]
    else:
        cls = "Part::MultiFuse" if ftype == "union" else "Part::MultiCommon"
        obj = doc.addObject(cls, feat["id"])
        shapes = feat.get("shapes") or [feat.get("base"), feat.get("tool")]
        obj.Shapes = [id_map[s] for s in shapes]
        consumed = list(shapes)
    feat["_consumed"] = consumed
    return obj


def _build_array(doc, host, feat, id_map):
    import Draft
    base = id_map[feat["base"]]
    arr = Draft.make_array(base, App.Vector(1, 0, 0), App.Vector(0, 1, 0), 1, 1)
    arr.Label = feat.get("id", arr.Label)
    hn = host.Name
    _bind(arr, "NumberX", feat.get("count"), hn)
    if feat.get("spacing") is not None:
        axis = (feat.get("axis") or "x").lower()
        arr.setExpression("IntervalX.%s" % axis, _translate(feat["spacing"], hn))
    feat["_consumed"] = [feat["base"]]
    return arr


# --------------------------------------------------------------------------- #
# operations
# --------------------------------------------------------------------------- #
def op_create_component(document, name, label=None, parameters=None):
    doc = (App.getDocument(document) if document in App.listDocuments()
           else App.newDocument(document))
    _begin(doc, "create_component")
    try:
        container = doc.addObject("App::Part", name)
        container.Label = label or name
        host = doc.addObject("App::FeaturePython", "%s_Params" % name)
        host.Label = "%s Parameters" % name

        groups = {}
        for g in _GROUPS:
            grp = doc.addObject("App::DocumentObjectGroup", "%s_%s" % (name, g))
            grp.Label = g
            groups[g] = grp.Name
            container.addObject(grp)
        container.addObject(host)

        for prop in ("mcp_component_id", "mcp_meta"):
            container.addProperty("App::PropertyString", prop, "MCP", "")
            container.setEditorMode(prop, 2)  # hidden
        cid = "component://%s/%s" % (document, name)
        container.mcp_component_id = cid

        _check_cycles(parameters or [])
        created = 0
        registry = []
        for p in (parameters or []):
            p = dict(p)
            p["kind"] = _add_param(host, p)
            registry.append(p)
            created += 1

        _save(container, {"host": host.Name, "groups": groups,
                          "schema": registry, "build_graph": [],
                          "id_map": {}, "variants": {}, "validation": None})
        doc.recompute()
        doc.commitTransaction()
    except Exception:
        _abort(doc)
        raise
    return {"component_id": cid, "object_name": container.Name,
            "parameters_created": created}


def op_define_component(component_id, features, rules=None, profiles=None):
    doc, container, meta = _resolve(component_id)
    host = _host(doc, meta)
    _begin(doc, "define_component")
    try:
        # remove any previously generated features
        for nm in meta.get("id_map", {}).values():
            old = doc.getObject(nm)
            if old is not None:
                doc.removeObject(old.Name)

        id_map = {}
        graph = []
        for feat in features:
            feat = dict(feat)
            obj = _build_feature(doc, host, feat, id_map)
            id_map[feat["id"]] = obj
            grp = _group(doc, meta, _ROLE_GROUP.get(feat.get("role", "output"), "Features"))
            if obj.Name not in [o.Name for o in grp.Group]:
                grp.addObject(obj)
            graph.append(feat)

        # Boolean and array inputs are claimed by their parent — drop them from
        # visible groups. A transform mutates its base in place, so consuming
        # that base would incorrectly remove the transformed output.
        for feat in graph:
            for cons in _consumed(feat):
                o = id_map[cons]
                for g in ("Features", "Construction"):
                    grp = _group(doc, meta, g)
                    if o in grp.Group:
                        grp.removeObject(o)

        name_map = {fid: o.Name for fid, o in id_map.items()}
        doc.recompute()
        errs = _errors(_component_objects(doc, meta, id_map.values()))
        if errs:
            # Transactional: roll back so the prior valid graph survives.
            raise ValueError("build failed; invalid features: %s" % ", ".join(errs))
        meta["build_graph"] = graph
        meta["id_map"] = name_map
        meta["dependency_index"] = _dependency_index(meta["schema"], graph)
        if rules is not None:
            meta["rules"] = rules
        if profiles is not None:
            meta["profiles"] = profiles
        _save(container, meta)
        doc.commitTransaction()
    except Exception:
        _abort(doc)
        raise

    return {"features_created": list(name_map.keys()), "object_names": name_map}


def _feature_inputs(feat):
    """Feature ids referenced by a graph node."""
    ids = []
    for k in ("base", "tool"):
        if isinstance(feat.get(k), str):
            ids.append(feat[k])
    ids += list(feat.get("shapes") or [])
    return ids


def _consumed(feat):
    """Inputs hidden because a boolean or array owns their visible output."""
    if feat.get("type") not in ("cut", "union", "intersection", "array"):
        return []
    return _feature_inputs(feat)


def _dependency_index(schema, graph):
    """Precompute reporting dependencies when the build graph is defined."""
    parameter_features = {p["name"]: [] for p in schema}
    downstream = {f["id"]: [] for f in graph}
    derived = {p["name"]: list(_TOKEN.findall(p.get("expression", "")))
               for p in schema if p.get("kind") == "derived"}
    for feat in graph:
        fid = feat["id"]
        for name in set(_TOKEN.findall(json.dumps(feat))):
            parameter_features.setdefault(name, []).append(fid)
        for source in _feature_inputs(feat):
            downstream.setdefault(source, []).append(fid)
    return {"derived": derived, "parameter_features": parameter_features,
            "downstream": downstream}


def _dependents(meta, changed):
    """Feature ids affected by changed params: those whose expressions reference
    a changed param (expanded through derived params), then propagated downstream
    through feature->feature consumption edges (a cut regenerates when its tool
    does)."""
    changed = set(changed)
    graph = meta["build_graph"]
    index = meta.get("dependency_index") or _dependency_index(meta["schema"], graph)
    derived = index["derived"]
    grew = True
    while grew:  # expand changed set through derived params
        grew = False
        for dname, refs in derived.items():
            if set(refs) & changed and dname not in changed:
                changed.add(dname)
                grew = True

    affected = set()
    for name in changed:
        affected.update(index["parameter_features"].get(name, ()))
    pending = list(affected)
    while pending:  # propagate downstream through feature topology
        source = pending.pop()
        for dependent in index["downstream"].get(source, ()):
            if dependent not in affected:
                affected.add(dependent)
                pending.append(dependent)
    return [f["id"] for f in graph if f["id"] in affected]


def op_set_component_parameters(component_id, values, rebuild=True, validate=False):
    doc, container, meta = _resolve(component_id)
    host = _host(doc, meta)
    kinds = {p["name"]: p.get("kind", "input") for p in meta["schema"]}
    types = {p["name"]: _prop_type(p) for p in meta["schema"]}

    _begin(doc, "set_component_parameters")
    try:
        changed = []
        for k, v in (values or {}).items():
            if k not in kinds:
                raise ValueError("unknown parameter %r" % k)
            if kinds[k] == "derived":
                raise ValueError("parameter %r is derived (read-only)" % k)
            _set_value(host, k, types[k], v)
            changed.append(k)
        if rebuild:
            doc.recompute()
            errs = _errors(_component_objects(doc, meta))
            if errs:
                # Transactional: keep the last valid geometry on a bad rebuild.
                raise ValueError("rebuild failed; invalid features: %s" % ", ".join(errs))
        doc.commitTransaction()
    except Exception:
        _abort(doc)
        raise

    result = {"changed": changed,
              "regenerated": _dependents(meta, changed),
              "bbox": _merge_bbox(_feature_solids(doc, meta))}
    if validate:
        result["validation"] = op_validate_component(component_id)
    return result


def op_get_component(component_id):
    doc, container, meta = _resolve(component_id)
    host = _host(doc, meta)
    params = []
    for p in meta["schema"]:
        try:
            cur = getattr(host, p["name"])
            cur = str(cur)
        except Exception:
            cur = None
        params.append({"name": p["name"], "kind": p.get("kind"),
                       "type": p.get("type"), "value": cur,
                       "min": p.get("min"), "max": p.get("max"),
                       "expression": p.get("expression")})
    graph = [{"id": f["id"], "type": f["type"],
              "depends_on": sorted(set(_TOKEN.findall(json.dumps(f))))}
             for f in meta["build_graph"]]
    return {"component_id": component_id, "object_name": container.Name,
            "parameters": params, "build_graph": graph,
            "variants": list(meta.get("variants", {}).keys()),
            "validation": meta.get("validation")}


def op_create_component_variant(component_id, name, values):
    doc, container, meta = _resolve(component_id)
    if not isinstance(name, str) or not name.strip():
        raise ValueError("variant name must be a non-empty string")
    if not isinstance(values, dict):
        raise ValueError("variant values must be an object")
    kinds = {p["name"]: p.get("kind", "input") for p in meta["schema"]}
    unknown = sorted(set(values) - set(kinds))
    derived = sorted(k for k in values if k in kinds and kinds[k] == "derived")
    if unknown:
        raise ValueError("variant contains unknown parameter(s): %s" % ", ".join(unknown))
    if derived:
        raise ValueError("variant overrides derived parameter(s): %s" % ", ".join(derived))
    meta.setdefault("variants", {})[name] = dict(values)
    _save(container, meta)
    return {"variant": name, "values": dict(values),
            "variants": list(meta["variants"].keys())}


# --------------------------------------------------------------------------- #
# validation
# --------------------------------------------------------------------------- #
def _build_context(doc, meta, tolerance):
    """Resolve the component into the context the rule engine consumes:
    parameter values, named feature shapes (with role/tags), and tolerance."""
    host = _host(doc, meta)
    params = {}
    for p in meta["schema"]:
        try:
            val = getattr(host, p["name"])
        except Exception:
            val = None
        params[p["name"]] = {"value": val, "kind": p.get("kind"),
                             "type": p.get("type"), "min": p.get("min"),
                             "max": p.get("max"), "enum": p.get("enum")}
    features = {}
    idm = meta.get("id_map", {})
    for feat in meta.get("build_graph", []):
        obj = doc.getObject(idm.get(feat["id"], ""))
        shape = getattr(obj, "Shape", None) if obj is not None else None
        if shape is not None and shape.isNull():
            shape = None
        features[feat["id"]] = {"obj": obj, "shape": shape,
                                "role": feat.get("role", "output"),
                                "tags": feat.get("tags", [])}
    return {"params": params, "features": features,
            "graph": meta.get("build_graph", []), "tolerance": tolerance}


def _measurements(doc, meta):
    out = {}
    idm = meta.get("id_map", {})
    for feat in meta.get("build_graph", []):
        obj = doc.getObject(idm.get(feat["id"], ""))
        s = getattr(obj, "Shape", None) if obj is not None else None
        if s is not None and not s.isNull():
            bb = s.BoundBox
            out[feat["id"]] = {"volume": s.Volume, "area": s.Area,
                               "bbox_size": [bb.XLength, bb.YLength, bb.ZLength]}
    return out


def op_validate_component(component_id, profiles=None, rule_ids=None,
                          include_measurements=False, tolerance=None):
    """Run baseline/profile rules + custom component rules over the recomputed
    component (design_rules.py). Read-only; returns the rich result schema."""
    import design_rules as DR
    doc, container, meta = _resolve(component_id)
    tol = tolerance or meta.get("tolerance") or "0.01 mm"
    ctx = _build_context(doc, meta, tol)

    selected = profiles if profiles is not None \
        else (meta.get("profiles") or ["geometry_baseline"])
    thresholds = meta.get("profile_thresholds", {})
    rules, prof_info = [], []
    for pid in selected:
        ver, prules = DR.expand_profile(pid, ctx, thresholds.get(pid))
        if ver is not None:
            prof_info.append({"id": pid, "version": ver})
            rules += prules

    custom = meta.get("rules", [])
    if rule_ids is not None:
        wanted = set(rule_ids)
        custom = [r for r in custom if r.get("id") in wanted]
    rules += custom

    findings, passed = DR.evaluate(ctx, rules)
    errors = sum(1 for f in findings if f["severity"] == "error")
    warnings = sum(1 for f in findings if f["severity"] == "warning")
    status = "error" if errors else ("warning" if warnings else "ok")

    result = {"component_id": component_id,
              "build_status": "failed" if _errors(_component_objects(doc, meta)) else "success",
              "validation_status": status, "tolerance": str(tol),
              "profiles": prof_info,
              "summary": {"errors": errors, "warnings": warnings, "passed": passed},
              "findings": findings}
    if include_measurements:
        result["measurements"] = _measurements(doc, meta)
    meta["validation"] = result
    _save(container, meta)
    return result


# --------------------------------------------------------------------------- #
# render / export
# --------------------------------------------------------------------------- #
def op_render_component(component_id, view="iso", section=None, hide_features=None,
                        width=900, height=700):
    gui = _gui()
    if gui is None or not gui.ActiveDocument or not gui.ActiveDocument.ActiveView:
        return {"error": "no active 3D view — render requires the FreeCAD GUI"}
    import base64
    doc, container, meta = _resolve(component_id)
    width, height = _render_dimensions(width, height)
    idm = meta["id_map"]
    v = gui.ActiveDocument.ActiveView

    hidden = []          # (obj, prior visibility) to restore
    temp = []            # temp section objects to delete
    path = None
    try:
        for fid in (hide_features or []):
            o = doc.getObject(idm.get(fid, ""))
            if o is not None:
                hidden.append((o, o.ViewObject.Visibility))
                o.ViewObject.Visibility = False

        if section:
            temp, more_hidden = _make_section(doc, meta, section)
            hidden += more_hidden

        orient = {"iso": v.viewIsometric, "front": v.viewFront, "rear": v.viewRear,
                  "top": v.viewTop, "bottom": v.viewBottom,
                  "left": v.viewLeft, "right": v.viewRight}
        if view != "current":
            setter = orient.get(view)
            if setter is None:
                raise ValueError("unknown view %r" % view)
            setter()
        v.fitAll()
        fd, path = tempfile.mkstemp(prefix="freecad_mcp_component_", suffix=".png")
        os.close(fd)
        v.saveImage(path, width, height, "Current")
        with open(path, "rb") as image:
            data = base64.b64encode(image.read()).decode("ascii")
        return {"image_base64": data, "view": view,
                "sectioned": bool(section), "width": width, "height": height}
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
        for o in temp:
            try:
                doc.removeObject(o.Name)
            except Exception:
                pass
        for o, vis in hidden:
            try:
                o.ViewObject.Visibility = vis
            except Exception:
                pass
        doc.recompute()


def _make_section(doc, meta, section):
    """Cut the visible solids with a half-space box at plane/offset, show only
    the section result. Returns (temp_objects, hidden_pairs)."""
    import Part
    if not isinstance(section, dict):
        raise ValueError("section must be an object with plane and offset")
    solids = _feature_solids(doc, meta)
    if not solids:
        return [], []
    bb = solids[0].Shape.BoundBox
    for o in solids[1:]:
        bb = bb.united(o.Shape.BoundBox)
    pad = max(bb.XLength, bb.YLength, bb.ZLength) + 10
    plane = (section.get("plane") or "XZ").upper()
    if plane not in ("XY", "YX", "XZ", "ZX", "YZ", "ZY"):
        raise ValueError("unknown section plane %r" % plane)
    off = float(App.Units.Quantity(str(section.get("offset", "0 mm"))).getValueAs("mm"))

    # half-space box removing the near side of the cut plane
    if plane in ("XZ", "ZX"):       # cut along Y
        box = Part.makeBox(bb.XLength + 2 * pad, pad, bb.ZLength + 2 * pad,
                           App.Vector(bb.XMin - pad, off, bb.ZMin - pad))
    elif plane in ("YZ", "ZY"):     # cut along X
        box = Part.makeBox(pad, bb.YLength + 2 * pad, bb.ZLength + 2 * pad,
                           App.Vector(off, bb.YMin - pad, bb.ZMin - pad))
    else:                            # XY, cut along Z
        box = Part.makeBox(bb.XLength + 2 * pad, bb.YLength + 2 * pad, pad,
                           App.Vector(bb.XMin - pad, bb.YMin - pad, off))

    comp = Part.makeCompound([o.Shape for o in solids])
    sect = doc.addObject("Part::Feature", "MCP_Section")
    sect.Shape = comp.cut(box)
    hidden = [(o, o.ViewObject.Visibility) for o in solids]
    for o in solids:
        o.ViewObject.Visibility = False
    return [sect], hidden


def op_export_component(component_id, path, format="FCStd", variant=None):
    doc, container, meta = _resolve(component_id)
    host = _host(doc, meta)
    fmt = (format or os.path.splitext(path)[1].lstrip(".")).upper()

    variant_transaction = False
    try:
        if variant:
            values = meta.get("variants", {}).get(variant)
            if values is None:
                return {"error": "unknown variant %r" % variant}
            types = {p["name"]: _prop_type(p) for p in meta["schema"]}
            kinds = {p["name"]: p.get("kind", "input") for p in meta["schema"]}
            unknown = sorted(set(values) - set(types))
            derived = sorted(k for k in values if k in kinds and kinds[k] == "derived")
            if unknown:
                return {"error": "variant contains unknown parameter(s): %s" % ", ".join(unknown)}
            if derived:
                return {"error": "variant overrides derived parameter(s): %s" % ", ".join(derived)}
            _begin(doc, "export_component_variant")
            variant_transaction = True
            for k, v in values.items():
                _set_value(host, k, types[k], v)
            doc.recompute()
            errs = _errors(_component_objects(doc, meta))
            if errs:
                raise ValueError("variant rebuild failed; invalid features: %s" % ", ".join(errs))

        if fmt == "FCSTD":
            doc.saveCopy(path)
        else:
            objs = _feature_solids(doc, meta)
            if not objs:
                return {"error": "component has no exportable output solids"}
            if fmt == "STL":
                import Part
                shape = (objs[0].Shape if len(objs) == 1
                         else Part.makeCompound([o.Shape for o in objs]))
                shape.exportStl(path)
            elif fmt in ("STEP", "STP", "IGES", "IGS", "BREP", "BRP"):
                import Part
                Part.export(objs, path)
            else:
                return {"error": "unsupported format %r" % fmt}
    finally:
        if variant_transaction:
            _abort(doc)

    return {"path": path, "format": fmt, "bytes": os.path.getsize(path),
            "variant": variant}
