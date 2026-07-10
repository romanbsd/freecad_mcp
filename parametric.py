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
import hashlib
import os
import re
import tempfile

import FreeCAD as App

_TOKEN = re.compile(r"\$([A-Za-z_]\w*)")
_SUBELEMENTS = {
    "face": "Faces", "edge": "Edges", "vertex": "Vertexes", "solid": "Solids",
}


def _normalize_common_feature(feat):
    """Normalize shared feature fields while preserving legacy spellings."""
    normalized = dict(feat)
    placement = dict(normalized.get("placement") or {})
    if normalized.get("position") is not None and not any(
            key in placement for key in ("translation", "translate")):
        placement["translation"] = normalized["position"]
    if normalized.get("type") == "transform" and normalized.get("translate") \
            and not any(key in placement for key in ("translation", "translate")):
        placement["translation"] = normalized["translate"]
    if normalized.get("frame") is not None and "frame" not in placement:
        placement["frame"] = normalized["frame"]
    if normalized.get("support") is not None and "support" not in placement:
        placement["support"] = normalized["support"]
    for key in ("reversed", "symmetric"):
        if key not in normalized and key in placement:
            normalized[key] = bool(placement[key])
    if placement:
        normalized["placement"] = placement
        if placement.get("frame") is not None:
            normalized.setdefault("frame", placement["frame"])
        if placement.get("support") is not None:
            normalized.setdefault("support", placement["support"])
    return normalized


def _selector_body(selector):
    selector = selector or {}
    return selector.get("where") or selector


def _bounds_center(element):
    bounds = element.BoundBox
    return App.Vector(
        (bounds.XMin + bounds.XMax) / 2,
        (bounds.YMin + bounds.YMax) / 2,
        (bounds.ZMin + bounds.ZMax) / 2,
    )


def _edge_matches(edge, selector, tolerance=1e-6, host=None):
    selector = _selector_body(selector)
    bb = edge.BoundBox
    if selector.get("parallel_to") or selector.get("axis"):
        axis = (selector.get("parallel_to") or selector.get("axis")).lower()
        lengths = {"x": bb.XLength, "y": bb.YLength, "z": bb.ZLength}
        dominant = max(lengths, key=lengths.get)
        if dominant != axis:
            return False
        if any(value > tolerance for key, value in lengths.items()
               if key != axis):
            return False
    if selector.get("length") is not None:
        target = _quantity_value(selector["length"], host)
        edge_tolerance = _quantity_value(selector.get("tolerance", tolerance), host)
        if abs(edge.Length - target) > edge_tolerance:
            return False
    if selector.get("radius") is not None:
        radius = getattr(getattr(edge, "Curve", None), "Radius", None)
        if radius is None:
            return False
        edge_tolerance = _quantity_value(selector.get("tolerance", tolerance), host)
        if abs(radius - _quantity_value(selector["radius"], host)) > edge_tolerance:
            return False
    return True


def _profile_host(obj):
    host_name = getattr(obj, "HostName", "")
    if not host_name:
        return None
    doc = getattr(obj, "Document", None) or App.ActiveDocument
    return doc.getObject(host_name) if doc is not None else None


def _quantity_value(value, host=None):
    if isinstance(value, (int, float)):
        return float(value)
    expr = str(value)
    if host is not None:
        for name in _TOKEN.findall(expr):
            replacement = getattr(host, name)
            if hasattr(replacement, "Value"):
                replacement = "%s mm" % replacement.Value
            expr = expr.replace("$" + name, str(replacement))
    return App.Units.Quantity(expr).Value


def _profile_point(point, host):
    if isinstance(point, dict):
        return [_quantity_value(point.get("x", 0), host),
                _quantity_value(point.get("y", 0), host)]
    return [_quantity_value(point[0], host), _quantity_value(point[1], host)]


def _vector(value):
    if isinstance(value, dict):
        return App.Vector(float(value.get("x", 0)), float(value.get("y", 0)),
                          float(value.get("z", 0)))
    return App.Vector(float(value[0]), float(value[1]), float(value[2]))


def _unit(vector, message="zero-length vector"):
    if vector.Length <= 1e-12:
        raise ValueError(message)
    return vector / vector.Length


def _support_feature_id(support, default=None):
    if isinstance(support, str):
        return support
    if isinstance(support, dict):
        return support.get("feature") or default
    return default if support else None


def _face_normal(face):
    if getattr(face.Surface, "TypeId", "") != "Part::GeomPlane":
        raise ValueError("support face is not planar")
    u0, u1, v0, v1 = face.ParameterRange
    return _unit(face.normalAt((u0 + u1) / 2, (v0 + v1) / 2),
                 "support face has no usable normal")


def _face_bounds_center(face):
    return _bounds_center(face)


def _element_matches(element, kind, selector, host=None):
    selector = _selector_body(selector)
    tolerance = _quantity_value(selector.get("tolerance", 1e-6), host)
    if kind == "edge" and not _edge_matches(element, selector, tolerance, host):
        return False
    if kind == "face":
        if selector.get("planar") is True and \
                getattr(element.Surface, "TypeId", "") != "Part::GeomPlane":
            return False
        requested_normal = selector.get("normal")
        if requested_normal is None and selector.get("axis"):
            sign = -1 if selector.get("side", "max") == "min" else 1
            requested_normal = {
                "x": [sign, 0, 0], "y": [0, sign, 0],
                "z": [0, 0, sign],
            }.get(selector["axis"])
        if requested_normal is not None:
            requested = _unit(_vector(requested_normal))
            try:
                if _face_normal(element).dot(requested) < float(selector.get("min_dot", 0.999)):
                    return False
            except ValueError:
                return False
        if selector.get("area") is not None and \
                abs(element.Area - _quantity_value(selector["area"], host)) > tolerance:
            return False
    if kind == "vertex" and selector.get("point") is not None:
        if element.Point.distanceToPoint(_vector(selector["point"])) > tolerance:
            return False
    if kind == "solid" and selector.get("volume") is not None and \
            abs(element.Volume - _quantity_value(selector["volume"], host)) > tolerance:
        return False
    return True


def _select_subelements(shape, selector, kind=None, host=None, expect=None):
    selector = selector or {}
    kind = (kind or selector.get("kind") or "face").lower().rstrip("s")
    if kind not in _SUBELEMENTS:
        raise ValueError("selector kind must be face, edge, vertex, or solid")
    elements = list(getattr(shape, _SUBELEMENTS[kind]))
    body = _selector_body(selector)
    indices = selector.get("indices", body.get("indices"))
    index = selector.get("index", body.get("index"))
    if index is not None:
        indices = [index]
    if indices is not None:
        selected = []
        for raw in indices:
            number = int(str(raw).replace(kind.title(), ""))
            if number < 1 or number > len(elements):
                raise ValueError("%s index %d is outside 1..%d" %
                                 (kind, number, len(elements)))
            selected.append(elements[number - 1])
    else:
        selected = [element for element in elements
                    if _element_matches(element, kind, body, host)]

    side = body.get("side")
    axis = body.get("axis")
    direction = None
    if axis in ("x", "y", "z"):
        direction = {"x": App.Vector(1, 0, 0), "y": App.Vector(0, 1, 0),
                     "z": App.Vector(0, 0, 1)}[axis]
    elif body.get("normal") is not None:
        direction = _unit(_vector(body["normal"]))
    if selected and side in ("min", "max") and direction is not None:
        choose = min if side == "min" else max
        selected = [choose(selected, key=lambda item: _bounds_center(item).dot(direction))]

    expected = expect if expect is not None else selector.get("expect")
    if expected == "one":
        expected = 1
    if isinstance(expected, int) and len(selected) != expected:
        raise ValueError("%s selector expected %d match(es), got %d" %
                         (kind, expected, len(selected)))
    return selected


def _select_support_face(support_obj, support_spec):
    selector = dict(support_spec.get("selector") or support_spec.get("face") or {})
    if support_spec.get("face_index") is not None:
        selector["index"] = support_spec["face_index"]
    selector.setdefault("kind", "face")
    selector.setdefault("where", {})
    body = selector["where"] if selector.get("where") else selector
    body.setdefault("planar", True)
    if body.get("axis") and body.get("normal") is None:
        axis = body["axis"].lower()
        sign = -1 if body.get("side", "max") == "min" else 1
        body["normal"] = {
            "x": [sign, 0, 0], "y": [0, sign, 0], "z": [0, 0, sign]
        }.get(axis)
    return _select_subelements(support_obj.Shape, selector, "face", expect=1)[0]


def _support_frame(support_obj, support_spec, host=None):
    face = _select_support_face(support_obj, support_spec)
    normal = _face_normal(face)
    requested_u = support_spec.get("x_direction")
    candidate = _vector(requested_u) if requested_u is not None else App.Vector(1, 0, 0)
    candidate = candidate - normal * candidate.dot(normal)
    if candidate.Length <= 1e-9 and requested_u is None:
        candidate = App.Vector(0, 1, 0) - normal * normal.y
    u_axis = _unit(candidate, "support x_direction is parallel to its face normal")
    v_axis = _unit(normal.cross(u_axis))
    origin = _face_bounds_center(face)
    origin += u_axis * _quantity_value(support_spec.get("u_offset", 0), host)
    origin += v_axis * _quantity_value(support_spec.get("v_offset", 0), host)
    origin += normal * _quantity_value(support_spec.get("offset", 0), host)
    return {"origin": origin, "u": u_axis, "v": v_axis, "normal": normal}


def _static_frame(frame_spec, host=None):
    if isinstance(frame_spec, str):
        plane = frame_spec.upper()
        u_axis = _plane_vector(plane, 1, 0)
        v_axis = _plane_vector(plane, 0, 1)
        normal = _unit(u_axis.cross(v_axis))
        return {"origin": App.Vector(0, 0, 0), "u": u_axis,
                "v": v_axis, "normal": normal}
    spec = frame_spec or {}
    if spec.get("plane"):
        result = _static_frame(spec["plane"], host)
    else:
        normal = _unit(_vector(spec.get("normal", [0, 0, 1])))
        candidate = _vector(spec.get("x_direction", [1, 0, 0]))
        u_axis = _unit(candidate - normal * candidate.dot(normal),
                       "frame x_direction is parallel to its normal")
        result = {"u": u_axis, "v": _unit(normal.cross(u_axis)),
                  "normal": normal}
    origin = spec.get("origin", [0, 0, 0])
    result["origin"] = App.Vector(*[_quantity_value(value, host) for value in origin])
    return result


def _frame_rotation(frame):
    matrix = App.Matrix()
    matrix.A11, matrix.A21, matrix.A31 = frame["u"].x, frame["u"].y, frame["u"].z
    matrix.A12, matrix.A22, matrix.A32 = frame["v"].x, frame["v"].y, frame["v"].z
    matrix.A13, matrix.A23, matrix.A33 = \
        frame["normal"].x, frame["normal"].y, frame["normal"].z
    return App.Rotation(matrix)


def _combined_expression(first, second):
    values = [value for value in (first, second) if value is not None]
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    if all(isinstance(value, (int, float)) for value in values):
        return sum(values)
    rendered = ["%s mm" % value if isinstance(value, (int, float)) else value
                for value in values]
    return "(%s) + (%s)" % (rendered[0], rendered[1])


def _feature_axial_length(feat, host):
    value = feat.get("length") or feat.get("height")
    if value is None and feat.get("type") == "box":
        value = (feat.get("size") or {}).get("z")
    return _quantity_value(value, host) if value is not None else None


def _directional_frame(frame, feat, host):
    result = dict(frame)
    if feat.get("reversed"):
        result["v"] = -result["v"]
        result["normal"] = -result["normal"]
    if feat.get("symmetric"):
        length = _feature_axial_length(feat, host)
        if length is not None:
            result["origin"] = result["origin"] - result["normal"] * length / 2
    return result


def _apply_common_placement(obj, feat, host, id_map=None):
    placement = feat.get("placement") or {}
    translation = placement.get("translation") or placement.get("translate") or {}
    support = placement.get("support") or feat.get("support")
    frame_spec = placement.get("frame") or feat.get("frame")
    frame_origin = {}
    support_id = _support_feature_id(support)
    if support_id:
        if not id_map or support_id not in id_map:
            raise ValueError("unknown placement support feature %r" % support_id)
        support_obj = id_map[support_id]
        if hasattr(support_obj, "Shape") and support_obj.Shape.isNull():
            support_obj.Document.recompute()
        support_spec = dict(support) if isinstance(support, dict) else {
            "feature": support_id,
        }
        frame = _directional_frame(
            _support_frame(support_obj, support_spec, host), feat, host
        )
        obj.Placement.Rotation = _frame_rotation(frame)
        frame_origin = {
            "x": frame["origin"].x, "y": frame["origin"].y,
            "z": frame["origin"].z,
        }
    elif frame_spec:
        frame = _directional_frame(_static_frame(frame_spec, host), feat, host)
        obj.Placement.Rotation = _frame_rotation(frame)
        raw_origin = frame_spec.get("origin", [0, 0, 0]) \
            if isinstance(frame_spec, dict) else [0, 0, 0]
        frame_origin = dict(zip(("x", "y", "z"), raw_origin))
    rotation = placement.get("rotation") or placement.get("rotate")
    if rotation:
        axis = _vector(rotation.get("axis", [0, 0, 1]))
        angle = _quantity_value(rotation.get("angle", 0), host)
        obj.Placement.Rotation = App.Rotation(axis, angle)
    for axis in ("x", "y", "z"):
        value = _combined_expression(frame_origin.get(axis), translation.get(axis))
        if value is not None:
            _bind(obj, ".Placement.Base.%s" % axis, value, host.Name)


def _plane_vector(plane, u, v, frame=None):
    if frame is not None:
        return frame["origin"] + frame["u"] * u + frame["v"] * v
    plane = (plane or "XY").upper()
    if plane in ("XY", "YX"):
        return App.Vector(u, v, 0)
    if plane in ("XZ", "ZX"):
        return App.Vector(u, 0, v)
    if plane in ("YZ", "ZY"):
        return App.Vector(0, u, v)
    raise ValueError("unknown profile plane %r" % plane)


def _profile_axis(axis, length):
    axis = (axis or "z").lower()
    if axis == "x":
        return App.Vector(length, 0, 0)
    if axis == "y":
        return App.Vector(0, length, 0)
    if axis == "z":
        return App.Vector(0, 0, length)
    raise ValueError("unknown extrusion axis %r" % axis)


def _normal_axis_for_plane(plane):
    plane = (plane or "XY").upper()
    if plane in ("XY", "YX"):
        return "z"
    if plane in ("XZ", "ZX"):
        return "y"
    if plane in ("YZ", "ZY"):
        return "x"
    raise ValueError("unknown profile plane %r" % plane)


def _rectangle_face(spec, plane, host, frame=None):
    import Part
    if spec.get("corner") is not None:
        x0, y0 = _profile_point(spec["corner"], host)
    else:
        cx, cy = _profile_point(spec.get("center", [0, 0]), host)
        sx, sy = _profile_point(spec.get("size", [0, 0]), host)
        x0, y0 = cx - sx / 2, cy - sy / 2
    if spec.get("size") is not None:
        sx, sy = _profile_point(spec["size"], host)
        x1, y1 = x0 + sx, y0 + sy
    else:
        x1, y1 = _profile_point(spec["opposite"], host)
    pts = [
        _plane_vector(plane, x0, y0, frame), _plane_vector(plane, x1, y0, frame),
        _plane_vector(plane, x1, y1, frame), _plane_vector(plane, x0, y1, frame),
        _plane_vector(plane, x0, y0, frame),
    ]
    return Part.Face(Part.makePolygon(pts))


def _circle_face(spec, plane, host, frame=None):
    import Part
    center = _profile_point(spec.get("center", [0, 0]), host)
    radius = _quantity_value(spec.get("radius"), host)
    normal = frame["normal"] if frame is not None else \
        _profile_axis(_normal_axis_for_plane(plane), 1)
    return Part.Face(Part.Wire(Part.makeCircle(
        radius, _plane_vector(plane, center[0], center[1], frame), normal
    )))


def _polygon_face(spec, plane, host, frame=None):
    import Part
    points = spec.get("points") or []
    if len(points) < 3:
        raise ValueError("polygon profile needs at least 3 points; got %d" % len(points))
    vectors = [_plane_vector(plane, *_profile_point(point, host), frame=frame)
               for point in points]
    if vectors[0].distanceToPoint(vectors[-1]) > 1e-9:
        vectors.append(vectors[0])
    return Part.Face(Part.makePolygon(vectors))


def _slot_face(spec, plane, host, frame=None):
    import Part
    start = _profile_point(spec["start"], host)
    end = _profile_point(spec["end"], host)
    radius = _quantity_value(spec.get("radius"), host)
    if abs(start[0] - end[0]) >= abs(start[1] - end[1]):
        rect = {
            "corner": [min(start[0], end[0]), start[1] - radius],
            "size": [abs(end[0] - start[0]), radius * 2],
        }
    else:
        rect = {
            "corner": [start[0] - radius, min(start[1], end[1])],
            "size": [radius * 2, abs(end[1] - start[1])],
        }
    return _circle_face({"center": start, "radius": radius}, plane, host, frame).fuse(
        _rectangle_face(rect, plane, host, frame)
    ).fuse(_circle_face({"center": end, "radius": radius}, plane, host, frame)).removeSplitter()


def _profile_spec(feat):
    if feat.get("profile"):
        return None
    spec = dict(feat.get("profile_spec") or {})
    if feat.get("points") is not None:
        spec["points"] = feat.get("points")
    if feat.get("sections") is not None:
        spec["sections"] = feat.get("sections")
    if feat.get("plane") is not None:
        spec["plane"] = feat.get("plane")
    if feat.get("frame") is not None:
        spec["frame"] = feat.get("frame")
    return spec


def _profile_definition_map(profile_definitions):
    definitions = {}
    for definition in profile_definitions or []:
        profile_id = definition.get("id")
        if not profile_id:
            raise ValueError("profile definition requires a non-empty id")
        if profile_id in definitions:
            raise ValueError("duplicate profile definition %r" % profile_id)
        spec = dict(definition)
        spec.pop("id", None)
        resolved = _profile_spec(spec) or {}
        if resolved.get("points") is None and resolved.get("sections") is None:
            raise ValueError("profile definition %r has no geometry" % profile_id)
        definitions[profile_id] = resolved
    return definitions


def _resolve_profile_spec(feat, profile_definitions=None):
    profile_id = feat.get("profile_id")
    base = {}
    if profile_id:
        definitions = profile_definitions or {}
        if profile_id not in definitions:
            raise ValueError("unknown reusable profile %r" % profile_id)
        base = dict(definitions[profile_id])
    inline = _profile_spec(feat) or {}
    base.update(inline)
    return base


def _resolve_loft_entries(entries, profile_definitions, id_map):
    resolved = []
    links = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            entry = {"profile_id": entry}
        if entry.get("profile"):
            profile_name = entry["profile"]
            if profile_name not in id_map:
                raise ValueError("unknown loft profile feature %r" % profile_name)
            links.append(id_map[profile_name])
            resolved.append({"link": len(links) - 1})
            continue
        profile_id = entry.get("profile_id")
        if not profile_id or profile_id not in profile_definitions:
            raise ValueError("loft entry requires a known profile_id or profile")
        spec = dict(profile_definitions[profile_id])
        for key, value in entry.items():
            if key != "profile_id":
                spec[key] = value
        resolved.append({"spec": spec})
    if len(resolved) < 2:
        raise ValueError("loft requires at least two profiles")
    return resolved, links


def _make_profile_shape(spec, host=None, support_obj=None, support_spec=None):
    import Part
    plane = spec.get("plane", "XY")
    frame = _support_frame(support_obj, support_spec or {}, host) \
        if support_obj is not None else \
        (_static_frame(spec.get("frame"), host) if spec.get("frame") else None)
    if spec.get("points") is not None:
        return _polygon_face(spec, plane, host, frame)
    faces = []
    for section in spec.get("sections") or []:
        kind = section.get("type") or section.get("kind")
        if kind == "circle":
            faces.append(_circle_face(section, plane, host, frame))
        elif kind == "rectangle":
            faces.append(_rectangle_face(section, plane, host, frame))
        elif kind == "polygon":
            faces.append(_polygon_face(section, plane, host, frame))
        elif kind == "slot":
            faces.append(_slot_face(section, plane, host, frame))
        else:
            raise ValueError("unknown profile section type %r" % kind)
    if not faces:
        raise ValueError("profile needs points or at least one section")
    shape = faces[0]
    for face in faces[1:]:
        shape = shape.fuse(face)
    return shape.removeSplitter()


def _shape_wire(shape):
    import Part
    if getattr(shape, "ShapeType", "") == "Wire":
        return shape
    if getattr(shape, "ShapeType", "") == "Face":
        return shape.OuterWire
    if getattr(shape, "Wires", None):
        return shape.Wires[0]
    if getattr(shape, "Edges", None):
        return Part.Wire(shape.Edges)
    raise ValueError("shape has no usable wire")


def _path_wire(obj, host):
    import Part
    if getattr(obj, "Path", None):
        return _shape_wire(obj.Path.Shape)
    spec = json.loads(getattr(obj, "PathSpec", "{}") or "{}")
    points = spec.get("points") or []
    if len(points) < 2:
        raise ValueError("sweep requires path or at least two path_points")
    vectors = []
    for point in points:
        values = [point.get(axis, 0) for axis in ("x", "y", "z")] \
            if isinstance(point, dict) else point
        vectors.append(App.Vector(*[_quantity_value(value, host) for value in values]))
    if spec.get("closed") and vectors[0].distanceToPoint(vectors[-1]) > 1e-9:
        vectors.append(vectors[0])
    return Part.Wire(Part.makePolygon(vectors).Edges)


def _operation_frame(obj, spec, host):
    support = getattr(obj, "Support", None)
    if support is not None:
        support_spec = json.loads(getattr(obj, "SupportSpec", "{}") or "{}")
        return _support_frame(support, support_spec, host)
    if spec.get("frame"):
        return _static_frame(spec["frame"], host)
    plane = spec.get("plane", "XY")
    return _static_frame(plane, host)


def _revolve_axis(obj, spec, host):
    frame = _operation_frame(obj, spec, host)
    axis = str(getattr(obj, "Axis", "z") or "z").lower()
    directions = {
        "x": App.Vector(1, 0, 0), "y": App.Vector(0, 1, 0),
        "z": App.Vector(0, 0, 1), "u": frame["u"],
        "v": frame["v"], "normal": frame["normal"],
    }
    if axis not in directions:
        raise ValueError("revolve axis must be x, y, z, u, v, or normal")
    raw_origin = json.loads(getattr(obj, "AxisOrigin", "[]") or "[]")
    if raw_origin:
        origin = App.Vector(*[_quantity_value(value, host) for value in raw_origin])
    else:
        origin = frame["origin"]
    return origin, directions[axis]


def _loft_wires(obj, host):
    entries = json.loads(getattr(obj, "LoftSpec", "[]") or "[]")
    links = list(getattr(obj, "LoftProfiles", []) or [])
    wires = []
    for entry in entries:
        if "link" in entry:
            wires.append(_shape_wire(links[int(entry["link"])].Shape))
        else:
            face = _make_profile_shape(entry["spec"], host)
            if len(face.Wires) != 1:
                raise ValueError("loft profiles with holes are not supported")
            wires.append(face.OuterWire)
    return wires


def _add_profile_properties(obj, feat, host, operation="pad", id_map=None,
                            default_support=None, profile_definitions=None):
    id_map = id_map or {}
    profile_definitions = profile_definitions or {}
    spec = _resolve_profile_spec(feat, profile_definitions)
    obj.addProperty("App::PropertyString", "ProfileSpec", "Geometry")
    obj.addProperty("App::PropertyString", "HostName", "Geometry")
    obj.addProperty("App::PropertyLink", "Profile", "Geometry")
    obj.addProperty("App::PropertyLink", "Support", "Geometry")
    obj.addProperty("App::PropertyString", "SupportSpec", "Geometry")
    obj.addProperty("App::PropertyLength", "Length", "Geometry")
    obj.addProperty("App::PropertyEnumeration", "Axis", "Geometry")
    obj.addProperty("App::PropertyEnumeration", "Operation", "Geometry")
    obj.addProperty("App::PropertyBool", "Reversed", "Geometry")
    obj.addProperty("App::PropertyBool", "Symmetric", "Geometry")
    obj.addProperty("App::PropertyAngle", "Angle", "Geometry")
    obj.addProperty("App::PropertyString", "AxisOrigin", "Geometry")
    obj.addProperty("App::PropertyLink", "Path", "Geometry")
    obj.addProperty("App::PropertyString", "PathSpec", "Geometry")
    obj.addProperty("App::PropertyLinkList", "LoftProfiles", "Geometry")
    obj.addProperty("App::PropertyString", "LoftSpec", "Geometry")
    obj.addProperty("App::PropertyBool", "Frenet", "Geometry")
    obj.addProperty("App::PropertyBool", "Ruled", "Geometry")
    obj.addProperty("App::PropertyBool", "Closed", "Geometry")
    obj.ProfileSpec = json.dumps(spec)
    obj.HostName = host.Name
    profile_id = feat.get("profile")
    if profile_id:
        obj.Profile = id_map[profile_id]
    support = feat.get("support")
    support_id = _support_feature_id(support, default_support)
    if support_id:
        obj.Support = id_map[support_id]
        support_spec = dict(support) if isinstance(support, dict) else {"feature": support_id}
        support_spec.setdefault("feature", support_id)
        obj.SupportSpec = json.dumps(support_spec)
    elif profile_id and hasattr(obj.Profile, "Support") and obj.Profile.Support:
        obj.Support = obj.Profile.Support
        obj.SupportSpec = obj.Profile.SupportSpec
    else:
        obj.SupportSpec = "{}"
    obj.Axis = ["x", "y", "z", "u", "v", "normal"]
    obj.Operation = ["face", "pad", "revolve", "sweep", "loft"]
    plane = spec.get("plane", feat.get("plane", "XY"))
    default_axis = "u" if operation == "revolve" and support_id else \
        ("x" if operation == "revolve" else _normal_axis_for_plane(plane))
    obj.Axis = (feat.get("axis") or default_axis).lower()
    obj.Operation = operation
    obj.Reversed = bool(feat.get("reversed", False))
    obj.Symmetric = bool(feat.get("symmetric", False))
    _bind(obj, "Length", feat.get("length") or feat.get("depth") or "1 mm", host.Name)
    _bind(obj, "Angle", feat.get("angle") or "360 deg", host.Name)
    obj.AxisOrigin = json.dumps(feat.get("axis_origin") or [])
    path_id = feat.get("path")
    if path_id:
        obj.Path = id_map[path_id]
    obj.PathSpec = json.dumps({
        "points": feat.get("path_points") or [],
        "closed": bool(feat.get("path_closed", False)),
    })
    loft_entries, loft_links = ([], [])
    if operation == "loft":
        loft_entries, loft_links = _resolve_loft_entries(
            feat.get("loft_profiles"), profile_definitions, id_map
        )
    obj.LoftSpec = json.dumps(loft_entries)
    obj.LoftProfiles = loft_links
    obj.Frenet = bool(feat.get("frenet", False))
    obj.Ruled = bool(feat.get("ruled", False))
    obj.Closed = bool(feat.get("closed", False))
    obj.Proxy = _ProfileExtrusionProxy()


class _ProfileExtrusionProxy:
    # FreeCAD serializes obj.Proxy on save and restores it on load; without
    # these the feature reopens with a dead Proxy and stops recomputing.
    # (FreeCAD 0.21+ calls dumps/loads, older calls __getstate__/__setstate__.)
    def dumps(self):
        return None

    def loads(self, state):
        return None

    __getstate__ = dumps
    __setstate__ = loads

    def execute(self, obj):
        host = _profile_host(obj)
        support = getattr(obj, "Support", None)
        support_spec = json.loads(getattr(obj, "SupportSpec", "{}") or "{}")
        operation = getattr(obj, "Operation", "pad")
        spec = json.loads(getattr(obj, "ProfileSpec", "{}") or "{}")
        if operation == "loft":
            import Part
            wires = _loft_wires(obj, host)
            if getattr(obj, "Reversed", False):
                wires.reverse()
            obj.Shape = Part.makeLoft(wires, True, obj.Ruled, obj.Closed)
            return
        if getattr(obj, "Profile", None):
            face = obj.Profile.Shape.copy()
        elif hasattr(obj, "ProfileSpec"):
            face = _make_profile_shape(spec, host, support, support_spec)
        else:
            face = _make_profile_shape({"points": json.loads(obj.ProfilePoints)}, None)
        if operation == "face":
            obj.Shape = face
            return
        if operation == "revolve":
            origin, direction = _revolve_axis(obj, spec, host)
            angle = obj.Angle.Value * (-1 if obj.Reversed else 1)
            if obj.Symmetric:
                face.rotate(origin, direction, -angle / 2)
            obj.Shape = face.revolve(origin, direction, angle)
            return
        if operation == "sweep":
            path = _path_wire(obj, host)
            if obj.Reversed:
                path.reverse()
            solids = [path.makePipeShell([wire], True, obj.Frenet)
                      for wire in face.Wires]
            shape = solids[0]
            for hole in solids[1:]:
                shape = shape.cut(hole)
            obj.Shape = shape
            return
        if support is not None:
            direction = _support_frame(support, support_spec, host)["normal"] * obj.Length.Value
        else:
            direction = _profile_axis(obj.Axis, obj.Length.Value)
        if getattr(obj, "Reversed", False):
            direction = -direction
        if getattr(obj, "Symmetric", False):
            face.translate(-direction * 0.5)
        obj.Shape = face.extrude(direction)


class _EdgeTreatmentProxy:
    def __init__(self, mode="fillet"):
        self.mode = mode

    # mode ("fillet"/"chamfer") lives only on the proxy, so it must be
    # serialized or a reopened document loses which operation to apply.
    def dumps(self):
        return {"mode": self.mode}

    def loads(self, state):
        self.mode = (state or {}).get("mode", "fillet") \
            if isinstance(state, dict) else "fillet"

    __getstate__ = dumps
    __setstate__ = loads

    def execute(self, obj):
        raw = getattr(obj, "SelectorSpec", "") or getattr(obj, "EdgeSelector", "{}")
        selector = json.loads(raw or "{}")
        edges = _select_subelements(
            obj.Base.Shape, selector, "edge", _profile_host(obj)
        )
        if not edges:
            raise ValueError("%s selector matched no edges" % self.mode)
        if self.mode == "fillet":
            obj.Shape = obj.Base.Shape.makeFillet(obj.Size.Value, edges)
        else:
            obj.Shape = obj.Base.Shape.makeChamfer(obj.Size.Value, edges)

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
        if prop_path.startswith(".Placement.Base."):
            axis = prop_path.rsplit(".", 1)[-1]
            base = obj.Placement.Base
            setattr(base, axis, float(value))
            obj.Placement.Base = base
        else:
            setattr(obj, prop_path, value)


def _load(container):
    return json.loads(container.mcp_meta or "{}")


def _save(container, meta):
    container.mcp_meta = json.dumps(meta)


def _resolve(component_id):
    if not str(component_id).startswith("component://"):
        raise ValueError("component_id must look like component://<doc>/<name>")
    docname, _, name = component_id[len("component://"):].partition("/")
    documents = App.listDocuments()
    candidates = []
    if docname in documents:
        candidates.append(App.getDocument(docname))
    candidates.extend(
        doc for key, doc in documents.items()
        if key != docname
    )
    for doc in candidates:
        for o in doc.Objects:
            if getattr(o, "mcp_component_id", None) == component_id:
                return doc, o, _load(o)
    if not candidates:
        raise ValueError("document %r is not open" % docname)
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
def _profile_points(feat):
    """A polygon profile needs at least 3 points; reject anything less with a
    clear error instead of an IndexError deep inside the proxy's execute."""
    points = feat.get("points") or []
    if len(points) < 3:
        raise ValueError(
            "profile_extrude %r needs at least 3 points; got %d"
            % (feat.get("id"), len(points))
        )
    return points


def _apply_profile_placement(obj, feat, host):
    placement_feat = dict(feat)
    placement = dict(placement_feat.get("placement") or {})
    placement.pop("frame", None)
    placement.pop("support", None)
    placement_feat["placement"] = placement
    placement_feat.pop("frame", None)
    placement_feat.pop("support", None)
    _apply_common_placement(obj, placement_feat, host)


def _build_profile_operation(doc, host, feat, id_map, profile_definitions):
    ftype = feat["type"]
    operation = {
        "profile_2d": "face", "profile_extrude": "pad",
        "pad": "pad", "pocket": "pad", "extrude_cut": "pad",
        "revolve": "revolve", "sweep": "sweep", "loft": "loft",
    }[ftype]
    base_id = feat.get("base")
    if ftype in ("pocket", "extrude_cut") and not base_id:
        raise ValueError("%s requires a base feature" % ftype)
    boolean_mode = feat.get("boolean")
    if ftype == "pad" and base_id:
        boolean_mode = boolean_mode or "fuse"
    elif ftype in ("pocket", "extrude_cut"):
        boolean_mode = "cut"
    if boolean_mode and not base_id:
        raise ValueError("profile operation boolean mode requires a base feature")
    if boolean_mode not in (None, "fuse", "cut", "common"):
        raise ValueError("profile operation boolean must be fuse, cut, or common")

    tool_name = feat["id"] if not boolean_mode else "%s_Tool" % feat["id"]
    tool = doc.addObject("Part::FeaturePython", tool_name)
    _add_profile_properties(
        tool, feat, host, operation, id_map, default_support=base_id,
        profile_definitions=profile_definitions,
    )
    _apply_profile_placement(tool, feat, host)
    if not boolean_mode:
        return tool

    classes = {"fuse": "Part::Fuse", "cut": "Part::Cut", "common": "Part::Common"}
    obj = doc.addObject(classes[boolean_mode], feat["id"])
    obj.Base = id_map[base_id]
    obj.Tool = tool
    feat["_consumed"] = [base_id] + \
        ([feat["profile"]] if feat.get("profile") else [])
    return obj


def _build_feature(doc, host, feat, id_map, profile_definitions=None):
    """Create one feature object and bind its expressions. Returns the object."""
    normalized = _normalize_common_feature(feat)
    feat.clear()
    feat.update(normalized)
    ftype = feat["type"]
    hn = host.Name

    def place(obj):
        _apply_common_placement(obj, feat, host, id_map)

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
    elif ftype == "tube":
        outer = doc.addObject("Part::Cylinder", "%s_Outer" % feat["id"])
        inner = doc.addObject("Part::Cylinder", "%s_Inner" % feat["id"])
        _bind(outer, "Radius", feat.get("outer_radius"), hn)
        _bind(inner, "Radius", feat.get("inner_radius"), hn)
        _bind(outer, "Height", feat.get("height"), hn)
        _bind(inner, "Height", feat.get("height"), hn)
        rotation = _axis_rotation(feat.get("axis"))
        outer.Placement.Rotation = rotation
        inner.Placement.Rotation = rotation
        place(outer)
        place(inner)
        obj = doc.addObject("Part::Cut", feat["id"])
        obj.Base, obj.Tool = outer, inner
    elif ftype in ("profile_2d", "profile_extrude", "pad", "pocket",
                   "extrude_cut", "revolve", "sweep", "loft"):
        obj = _build_profile_operation(
            doc, host, feat, id_map, profile_definitions or {}
        )
    elif ftype in ("fillet", "chamfer"):
        obj = doc.addObject("Part::FeaturePython", feat["id"])
        obj.addProperty("App::PropertyLink", "Base", "Geometry")
        obj.addProperty("App::PropertyLength", "Size", "Geometry")
        obj.addProperty("App::PropertyString", "SelectorSpec", "Geometry")
        obj.addProperty("App::PropertyString", "HostName", "Geometry")
        obj.Base = id_map[feat["base"]]
        selector = feat.get("selector") or feat.get("edges") or {}
        if "kind" not in selector:
            selector = dict(selector, kind="edge")
        obj.SelectorSpec = json.dumps(selector)
        obj.HostName = host.Name
        _bind(obj, "Size", feat.get("radius") or feat.get("size"), hn)
        obj.Proxy = _EdgeTreatmentProxy(ftype)
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
        place(obj)
        return obj  # not a new object
    elif ftype in ("cut", "union", "intersection"):
        obj = _build_boolean(doc, feat, id_map)
    elif ftype in ("array", "grid_array"):
        obj = _build_array(doc, host, feat, id_map)
    elif ftype == "group":
        obj = doc.addObject("App::DocumentObjectGroup", feat["id"])
    else:
        raise ValueError("unknown feature type %r" % ftype)

    if feat.get("label"):
        obj.Label = feat["label"]
    if ftype in ("cut", "union", "intersection", "array", "grid_array",
                 "fillet", "chamfer"):
        place(obj)
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


def _require_array_count(feat):
    """A missing count silently produced an array of one (Draft's default).
    Require an explicit count so that footgun fails loudly instead."""
    if (feat.get("count") is None and feat.get("count_x") is None
            and feat.get("count_y") is None):
        raise ValueError(
            "array %r requires 'count' (or 'count_x'/'count_y')" % feat.get("id")
        )


def _build_array(doc, host, feat, id_map):
    import Draft
    _require_array_count(feat)
    base = id_map[feat["base"]]
    arr = Draft.make_array(base, App.Vector(1, 0, 0), App.Vector(0, 1, 0), 1, 1)
    arr.Label = feat.get("id", arr.Label)
    hn = host.Name
    if feat.get("count_x") is not None or feat.get("count_y") is not None:
        _bind(arr, "NumberX", feat.get("count_x") or 1, hn)
        _bind(arr, "NumberY", feat.get("count_y") or 1, hn)
        if feat.get("spacing_x") is not None:
            arr.setExpression("IntervalX.x", _translate(feat["spacing_x"], hn))
        if feat.get("spacing_y") is not None:
            arr.setExpression("IntervalY.y", _translate(feat["spacing_y"], hn))
    else:
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
    existing_doc = document in App.listDocuments()
    doc = App.getDocument(document) if existing_doc else App.newDocument(document)
    cid = "component://%s/%s" % (document, name)
    # addObject auto-renames on a name clash, but the component_id is built from
    # the requested name — so two same-named components would share one id and
    # _resolve would target the wrong one. Reject the duplicate up front.
    if existing_doc:
        for o in doc.Objects:
            if getattr(o, "mcp_component_id", None) == cid:
                raise ValueError(
                    "component %r already exists in document %r" % (name, document)
                )
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
                          "id_map": {}, "variants": {}, "validation": None,
                          "revision": 0, "outputs": [],
                          "profile_definitions": []})
        doc.recompute()
        doc.commitTransaction()
    except Exception:
        _abort(doc)
        raise
    return {"component_id": cid, "object_name": container.Name,
            "parameters_created": created}


def op_define_component(component_id, features, rules=None, profiles=None,
                        profile_definitions=None):
    doc, container, meta = _resolve(component_id)
    host = _host(doc, meta)
    definition_list = profile_definitions if profile_definitions is not None else \
        meta.get("profile_definitions", [])
    definition_map = _profile_definition_map(definition_list)
    _begin(doc, "define_component")
    try:
        # remove any previously generated features
        old_names = meta.get("id_map", {})
        old_graph = meta.get("build_graph", [])
        if old_graph:
            for feature in reversed(old_graph):
                _remove_generated_feature(
                    doc, feature, doc.getObject(old_names.get(feature["id"], ""))
                )
        else:
            for name in old_names.values():
                old = doc.getObject(name)
                if old is not None:
                    doc.removeObject(old.Name)

        from patterns import expand_patterns
        features = expand_patterns(features)
        id_map = {}
        graph = []
        for feat in features:
            feat = dict(feat)
            obj = _build_feature(doc, host, feat, id_map, definition_map)
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
        meta["profile_definitions"] = list(profile_definitions or definition_list)
        meta["dependency_index"] = _dependency_index(
            meta["schema"], graph, definition_map
        )
        meta["revision"] = int(meta.get("revision", 0)) + 1
        meta.setdefault("outputs", [
            f["id"] for f in graph if f.get("role", "output") == "output"
        ])
        if rules is not None:
            meta["rules"] = rules
        if profiles is not None:
            meta["profiles"] = profiles
        _save(container, meta)
        doc.commitTransaction()
    except Exception:
        _abort(doc)
        raise

    return {"features_created": list(name_map.keys()), "object_names": name_map,
            "profiles_registered": list(definition_map.keys())}


def _apply_graph_operations(graph, operations, outputs=None):
    """Apply structural graph edits without touching FreeCAD."""
    updated = [dict(f) for f in graph]
    output_ids = list(outputs or [])
    added, changed, removed = [], [], []
    for operation in operations or []:
        op = operation.get("op")
        if op == "upsert":
            feature = dict(operation.get("feature") or {})
            if not feature.get("id") or not feature.get("type"):
                raise ValueError("upsert requires feature.id and feature.type")
            matches = [i for i, f in enumerate(updated) if f["id"] == feature["id"]]
            if matches:
                updated[matches[0]] = feature
                changed.append(feature["id"])
            else:
                updated.append(feature)
                added.append(feature["id"])
        elif op == "remove":
            fid = operation.get("id")
            if not fid or not any(f["id"] == fid for f in updated):
                raise ValueError("cannot remove unknown feature %r" % fid)
            updated = [f for f in updated if f["id"] != fid]
            output_ids = [x for x in output_ids if x != fid]
            removed.append(fid)
        elif op == "set_output":
            output_ids = list(operation.get("ids") or [])
        else:
            raise ValueError("unknown patch operation %r" % op)

    ids = [f["id"] for f in updated]
    if len(ids) != len(set(ids)):
        raise ValueError("feature ids must be unique")
    known = set(ids)
    for feature in updated:
        missing = [x for x in _feature_inputs(feature) if x not in known]
        if missing:
            raise ValueError("feature %s references missing inputs: %s" %
                             (feature["id"], ", ".join(missing)))
    missing_outputs = [x for x in output_ids if x not in known]
    if missing_outputs:
        raise ValueError("unknown output features: %s" % ", ".join(missing_outputs))
    return {
        "graph": updated, "outputs": output_ids,
        "added": added, "changed": changed, "removed": removed,
    }


def op_get_component_graph(component_id, detail="summary"):
    doc, container, meta = _resolve(component_id)
    graph = meta.get("build_graph", [])
    if detail == "summary":
        features = [
            {"id": f["id"], "type": f["type"],
             "inputs": _feature_inputs(f), "role": f.get("role", "output")}
            for f in graph
        ]
    elif detail == "full":
        features = graph
    else:
        raise ValueError("detail must be 'summary' or 'full'")
    return {
        "component_id": component_id,
        "revision": int(meta.get("revision", 0)),
        "features": features,
        "profile_definitions": list(meta.get("profile_definitions", [])),
        "outputs": list(meta.get("outputs", [])),
    }


def op_patch_component(component_id, operations, expected_revision=None,
                       validate=False, dry_run=False):
    doc, container, meta = _resolve(component_id)
    revision = int(meta.get("revision", 0))
    if expected_revision is not None and int(expected_revision) != revision:
        raise ValueError("STALE_COMPONENT_REVISION: expected %s, current %s" %
                         (expected_revision, revision))
    patch = _apply_graph_operations(
        meta.get("build_graph", []), operations, meta.get("outputs", [])
    )
    if dry_run:
        return {
            "dry_run": True, "revision": revision,
            "added": patch["added"], "changed": patch["changed"],
            "removed": patch["removed"], "outputs": patch["outputs"],
        }

    impacted = _structural_dependents(
        meta.get("build_graph", []), patch["graph"],
        patch["added"] + patch["changed"] + patch["removed"],
    )
    new_meta = _rebuild_graph_patch(
        doc, container, meta, patch["graph"], impacted
    )
    if new_meta is None:
        op_define_component(component_id, patch["graph"])
        doc, container, new_meta = _resolve(component_id)
    new_meta["outputs"] = patch["outputs"]
    _save(container, new_meta)
    result = {
        "revision": int(new_meta.get("revision", revision + 1)),
        "added": patch["added"], "changed": patch["changed"],
        "removed": patch["removed"],
        "regenerated": [
            f["id"] for f in patch["graph"] if f["id"] in impacted
        ],
        "outputs": patch["outputs"],
    }
    if validate:
        result["validation"] = op_validate_component(component_id)
    return result


def op_list_feature_types():
    return {
        "feature_types": [
            "box", "cylinder", "tube", "cone", "prism", "transform",
            "cut", "union", "intersection", "array", "grid_array",
            "profile_2d", "profile_extrude", "pad", "pocket", "extrude_cut",
            "revolve", "sweep", "loft",
            "fillet", "chamfer", "group", "pattern",
        ]
    }


SELECTOR_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"enum": ["face", "edge", "vertex", "solid"]},
        "index": {"type": "integer", "minimum": 1},
        "indices": {"type": "array", "items": {"type": "integer", "minimum": 1}},
        "where": {"properties": {
            "axis": {"enum": ["x", "y", "z"]},
            "side": {"enum": ["min", "max"]},
            "normal": {"type": "vector3"},
            "parallel_to": {"enum": ["x", "y", "z"]},
            "length": {}, "radius": {}, "area": {}, "volume": {},
            "point": {"type": "vector3"}, "tolerance": {},
            "planar": {"type": "boolean"}, "min_dot": {"type": "number"},
        }},
        "expect": {"one_of": ["one", "integer"]},
    },
}

COMMON_FEATURE_PROPERTIES = {
    "role": {"enum": ["output", "construction", "tool", "inspection"]},
    "label": {"type": "string"},
    "tags": {"type": "array"},
    "placement": {"description": "translation, rotation, frame, and support"},
    "frame": {"description": "principal plane or origin/x_direction/normal frame"},
    "support": {"description": "feature plus a shared face selector"},
    "reversed": {"type": "boolean", "default": False},
    "symmetric": {"type": "boolean", "default": False},
    "selector": SELECTOR_SCHEMA,
}

PROFILE_INPUTS = ["profile", "profile_id", "points", "sections", "profile_spec"]
PROFILE_OPERATION_PROPERTIES = {
    "profile": {"description": "graph feature id containing a profile face"},
    "profile_id": {"description": "id from component profile_definitions"},
    "boolean": {"enum": ["fuse", "cut", "common"]},
}

FEATURE_SCHEMAS = {
    "box": {"required": ["id", "size"], "properties": {
        "size": {"required": ["x", "y", "z"]}, "position": {"optional": True}}},
    "cylinder": {"required": ["id", "radius", "height"],
                 "properties": {"axis": {"enum": ["x", "y", "z"]}}},
    "tube": {"required": ["id", "outer_radius", "inner_radius", "height"],
             "properties": {"axis": {"enum": ["x", "y", "z"]}}},
    "grid_array": {"required": ["id", "base", "count_x", "count_y"],
                   "properties": {"spacing_x": {}, "spacing_y": {}}},
    "profile_extrude": {
        "alias_of": "pad", "deprecated": True,
        "required": ["id", "length"],
        "one_of": PROFILE_INPUTS,
        "properties": {
            "plane": {"enum": ["XY", "XZ", "YZ"], "default": "XY"},
            "axis": {"enum": ["x", "y", "z"], "default": "normal to plane"},
            "support": {"description": "optional planar-face support selector"},
            "reversed": {"type": "boolean", "default": False},
            "symmetric": {"type": "boolean", "default": False},
            "sections": {"items": {"type": "circle|rectangle|polygon|slot"}},
        },
    },
    "profile_2d": {
        "required": ["id"],
        "one_of": ["profile_id", "points", "sections", "profile_spec"],
        "properties": {
            "plane": {"enum": ["XY", "XZ", "YZ"], "default": "XY"},
            "support": {"description": "optional planar-face support selector"},
            "sections": {"items": {"type": "circle|rectangle|polygon|slot"}},
        },
    },
    "pad": {
        "required": ["id", "length"],
        "one_of": PROFILE_INPUTS,
        "properties": {
            **PROFILE_OPERATION_PROPERTIES,
            "base": {"optional": True, "description": "feature to fuse with the pad"},
            "support": {"description": "optional planar-face support selector"},
            "reversed": {"type": "boolean", "default": False},
            "symmetric": {"type": "boolean", "default": False},
        },
    },
    "pocket": {
        "required": ["id", "base", "length"],
        "one_of": PROFILE_INPUTS,
        "properties": {
            **PROFILE_OPERATION_PROPERTIES,
            "support": {"description": "optional planar-face support selector; feature defaults to base"},
            "reversed": {"type": "boolean", "default": False},
            "symmetric": {"type": "boolean", "default": False},
        },
    },
    "extrude_cut": {
        "alias_of": "pocket", "deprecated": True,
        "required": ["id", "base", "length"],
        "one_of": PROFILE_INPUTS,
        "properties": {
            "profile": {"description": "feature id of a profile_2d face"},
            "plane": {"enum": ["XY", "XZ", "YZ"], "default": "XY"},
            "axis": {"enum": ["x", "y", "z"], "default": "normal to plane"},
            "support": {"description": "optional planar-face support selector; feature defaults to base"},
        },
    },
    "revolve": {
        "required": ["id"], "one_of": PROFILE_INPUTS,
        "properties": {**PROFILE_OPERATION_PROPERTIES,
            "angle": {"default": "360 deg"},
            "axis": {"enum": ["x", "y", "z", "u", "v", "normal"]},
            "axis_origin": {"type": "vector3"}, "base": {"optional": True}},
    },
    "sweep": {
        "required": ["id"], "one_of": PROFILE_INPUTS,
        "properties": {**PROFILE_OPERATION_PROPERTIES,
            "path": {"description": "graph feature id containing a wire"},
            "path_points": {"type": "array", "items": {"type": "vector3"}},
            "frenet": {"type": "boolean", "default": False},
            "base": {"optional": True}},
    },
    "loft": {
        "required": ["id", "loft_profiles"],
        "properties": {**PROFILE_OPERATION_PROPERTIES,
            "loft_profiles": {"description": "ordered profile/profile_id uses"},
            "ruled": {"type": "boolean", "default": False},
            "closed": {"type": "boolean", "default": False},
            "base": {"optional": True}},
    },
    "fillet": {"required": ["id", "base", "radius"],
                "one_of": ["selector", "edges"]},
    "chamfer": {"required": ["id", "base", "size"],
                 "one_of": ["selector", "edges"]},
    "cut": {"required": ["id", "base", "tool"]},
    "union": {"required": ["id", "base", "tool"]},
    "intersection": {"required": ["id", "base", "tool"]},
    "pattern": {"required": ["id", "pattern", "parameters"],
                "properties": {"version": {"default": "1"}}},
}


def op_describe_feature_type(feature_type):
    if feature_type not in op_list_feature_types()["feature_types"]:
        raise ValueError("unknown feature type %r" % feature_type)
    return {
        "feature_type": feature_type,
        "schema": FEATURE_SCHEMAS.get(feature_type, {
            "required": ["id"], "description": "See cookbook for fields."
        }),
        "common_properties": COMMON_FEATURE_PROPERTIES,
    }


def op_capabilities():
    from patterns import list_patterns
    import FreeCAD
    return {
        "api_version": "2.1",
        "freecad_version": ".".join(FreeCAD.Version()[:3]),
        "feature_types": op_list_feature_types()["feature_types"],
        "patterns": list_patterns(),
        "validation_profiles": ["geometry_baseline", "cnc_plywood", "fdm"],
        "response_format": "native",
        "structural_patch_mode": "dependency_scoped",
        "selector_kinds": list(_SUBELEMENTS.keys()),
        "profile_definitions": True,
        "profile_operations": ["pad", "pocket", "revolve", "sweep", "loft"],
    }


def op_list_patterns():
    from patterns import list_patterns
    return {"patterns": list_patterns()}


def op_expand_pattern(feature):
    from patterns import expand_patterns
    if feature.get("type") != "pattern":
        feature = dict(feature)
        feature["type"] = "pattern"
    return {"features": expand_patterns([feature])}


def _feature_inputs(feat):
    """Feature ids referenced by a graph node."""
    ids = []
    for k in ("base", "tool", "profile", "path"):
        if isinstance(feat.get(k), str):
            ids.append(feat[k])
    for entry in feat.get("loft_profiles") or []:
        if isinstance(entry, dict) and isinstance(entry.get("profile"), str):
            ids.append(entry["profile"])
    support_id = _support_feature_id(feat.get("support"))
    if support_id:
        ids.append(support_id)
    ids += list(feat.get("shapes") or [])
    return list(dict.fromkeys(ids))


def _consumed(feat):
    """Inputs hidden because a boolean or array owns their visible output."""
    if feat.get("_consumed") is not None:
        return list(feat["_consumed"])
    if feat.get("type") in (
            "pad", "pocket", "revolve", "sweep", "loft",
            "profile_extrude", "extrude_cut"):
        combines = feat.get("type") in ("pad", "pocket", "extrude_cut") or \
            bool(feat.get("boolean"))
        if not feat.get("base") or not combines:
            return []
        return [feat["base"]] + ([feat["profile"]] if feat.get("profile") else [])
    if feat.get("type") not in (
        "cut", "union", "intersection", "array", "grid_array",
        "fillet", "chamfer", "pad", "pocket", "revolve", "sweep", "loft",
        "profile_extrude", "extrude_cut"
    ):
        return []
    return _feature_inputs(feat)


def _structural_dependents(old_graph, new_graph, seeds):
    """Changed feature ids plus their old/new downstream closure."""
    impacted = set(seeds)
    combined = list(old_graph) + list(new_graph)
    grew = True
    while grew:
        grew = False
        for feature in combined:
            if feature["id"] in impacted:
                continue
            if impacted.intersection(_feature_inputs(feature)):
                impacted.add(feature["id"])
                grew = True
    return impacted


def _remove_generated_feature(doc, feature, obj):
    """Remove one graph object and any private children owned by its type."""
    if obj is None:
        return
    private_children = []
    if feature.get("type") == "tube":
        private_children = [
            getattr(obj, "Base", None), getattr(obj, "Tool", None)
        ]
    elif feature.get("type") in (
            "pad", "pocket", "revolve", "sweep", "loft",
            "profile_extrude", "extrude_cut") \
            and feature.get("base"):
        private_children = [getattr(obj, "Tool", None)]
    doc.removeObject(obj.Name)
    for child in private_children:
        if child is not None and doc.getObject(child.Name) is not None:
            doc.removeObject(child.Name)


def _rebuild_graph_patch(doc, container, meta, new_graph, impacted):
    """Transactionally rebuild only structurally affected graph nodes."""
    old_graph = meta.get("build_graph", [])
    old_by_id = {feature["id"]: feature for feature in old_graph}
    new_by_id = {feature["id"]: feature for feature in new_graph}
    old_names = dict(meta.get("id_map", {}))

    # A transform aliases its base object instead of owning an independent
    # object. Use the full builder when an affected path contains one.
    if any((old_by_id.get(fid) or new_by_id.get(fid) or {}).get("type") ==
           "transform" for fid in impacted):
        return None

    _begin(doc, "patch_component")
    try:
        for feature in reversed(old_graph):
            fid = feature["id"]
            if fid in impacted:
                _remove_generated_feature(
                    doc, feature, doc.getObject(old_names.get(fid, ""))
                )

        id_map = {}
        for fid, name in old_names.items():
            if fid not in impacted:
                obj = doc.getObject(name)
                if obj is not None:
                    id_map[fid] = obj

        host = _host(doc, meta)
        for feature in new_graph:
            fid = feature["id"]
            if fid not in impacted:
                continue
            obj = _build_feature(
                doc, host, feature, id_map,
                _profile_definition_map(meta.get("profile_definitions", [])),
            )
            id_map[fid] = obj
            group_name = _ROLE_GROUP.get(
                feature.get("role", "output"), "Features"
            )
            group = _group(doc, meta, group_name)
            if obj.Name not in [member.Name for member in group.Group]:
                group.addObject(obj)

        for feature in new_graph:
            for consumed in _consumed(feature):
                obj = id_map.get(consumed)
                if obj is None:
                    continue
                for group_name in ("Features", "Construction"):
                    group = _group(doc, meta, group_name)
                    if obj in group.Group:
                        group.removeObject(obj)

        doc.recompute()
        errors = _errors(_component_objects(doc, meta, id_map.values()))
        if errors:
            raise ValueError(
                "patch rebuild failed; invalid features: %s" %
                ", ".join(errors)
            )
        meta["build_graph"] = new_graph
        meta["id_map"] = {fid: obj.Name for fid, obj in id_map.items()}
        meta["dependency_index"] = _dependency_index(
            meta["schema"], new_graph,
            _profile_definition_map(meta.get("profile_definitions", [])),
        )
        meta["revision"] = int(meta.get("revision", 0)) + 1
        _save(container, meta)
        doc.commitTransaction()
        return meta
    except Exception:
        _abort(doc)
        raise


def _profile_ids_used(feat):
    ids = []
    if feat.get("profile_id"):
        ids.append(feat["profile_id"])
    for entry in feat.get("loft_profiles") or []:
        if isinstance(entry, str):
            ids.append(entry)
        elif isinstance(entry, dict) and entry.get("profile_id"):
            ids.append(entry["profile_id"])
    return ids


def _dependency_index(schema, graph, profile_definitions=None):
    """Precompute reporting dependencies when the build graph is defined."""
    parameter_features = {p["name"]: [] for p in schema}
    downstream = {f["id"]: [] for f in graph}
    derived = {p["name"]: list(_TOKEN.findall(p.get("expression", "")))
               for p in schema if p.get("kind") == "derived"}
    for feat in graph:
        fid = feat["id"]
        payload = [feat]
        for profile_id in _profile_ids_used(feat):
            if profile_id in (profile_definitions or {}):
                payload.append(profile_definitions[profile_id])
        for name in set(_TOKEN.findall(json.dumps(payload))):
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
    index = meta.get("dependency_index") or _dependency_index(
        meta["schema"], graph,
        _profile_definition_map(meta.get("profile_definitions", [])),
    )
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


def _refresh_common_placements(doc, meta, feature_ids, host):
    profile_types = {
        "profile_2d", "profile_extrude", "pad", "pocket", "extrude_cut",
        "revolve", "sweep", "loft",
    }
    id_map = {
        feature_id: doc.getObject(name)
        for feature_id, name in meta.get("id_map", {}).items()
    }
    for feature in meta.get("build_graph", []):
        if feature["id"] not in feature_ids or feature["type"] in profile_types:
            continue
        placement = feature.get("placement") or {}
        if not (placement.get("frame") or placement.get("support") or
                feature.get("frame") or feature.get("support")):
            continue
        obj = id_map.get(feature["id"])
        if obj is None:
            continue
        targets = [obj]
        if feature["type"] == "tube":
            targets = [target for target in (
                getattr(obj, "Base", None), getattr(obj, "Tool", None)
            ) if target is not None]
        for target in targets:
            _apply_common_placement(target, feature, host, id_map)


def _as_number(v):
    """Best-effort numeric value of a number / length / '5 mm' for bound checks."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).split()[0])
    except (ValueError, IndexError):
        return None


def _bound_violations(schema, values):
    """Names whose new value falls outside a declared min/max. Best-effort
    numeric compare; units are assumed consistent with the bound (matching how
    the geometry_baseline profile checks the same ranges)."""
    by_name = {p["name"]: p for p in schema}
    bad = []
    for name, raw in (values or {}).items():
        p = by_name.get(name)
        if not p:
            continue
        val = _as_number(raw)
        if val is None:
            continue
        lo, hi = _as_number(p.get("min")), _as_number(p.get("max"))
        if (lo is not None and val < lo) or (hi is not None and val > hi):
            bad.append("%s=%s outside [%s, %s]"
                       % (name, raw, p.get("min"), p.get("max")))
    return bad


def op_set_component_parameters(component_id, values, rebuild=True, validate=False,
                                enforce_bounds=False):
    doc, container, meta = _resolve(component_id)
    host = _host(doc, meta)
    kinds = {p["name"]: p.get("kind", "input") for p in meta["schema"]}
    types = {p["name"]: _prop_type(p) for p in meta["schema"]}

    if enforce_bounds:
        violations = _bound_violations(meta["schema"], values)
        if violations:
            raise ValueError("parameter(s) out of bounds: %s" % "; ".join(violations))

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
            # Whole-document recompute: FreeCAD's touch-tracking makes it
            # incremental internally. The "regenerated" list below is computed
            # only for the response — it does not scope this rebuild.
            doc.recompute()
            affected = _dependents(meta, changed)
            _refresh_common_placements(doc, meta, affected, host)
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
            "profile_definitions": list(meta.get("profile_definitions", [])),
            "revision": int(meta.get("revision", 0)),
            "outputs": list(meta.get("outputs", [])),
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
    rules, prof_info, unknown_profiles = [], [], []
    for pid in selected:
        ver, prules = DR.expand_profile(pid, ctx, thresholds.get(pid))
        if ver is None:
            # A misspelled profile used to be skipped silently, validating
            # nothing while reporting "ok". Surface it as an error finding.
            unknown_profiles.append(pid)
            continue
        prof_info.append({"id": pid, "version": ver})
        rules += prules

    custom = meta.get("rules", [])
    if rule_ids is not None:
        wanted = set(rule_ids)
        custom = [r for r in custom if r.get("id") in wanted]
    rules += custom

    findings, passed = DR.evaluate(ctx, rules)
    for pid in unknown_profiles:
        findings.append({"id": "unknown_profile:%s" % pid, "severity": "error",
                         "rule": "profile",
                         "message": "unknown validation profile %r" % pid})
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


def _bbox_clearances(container_bbox, insert_bbox):
    """Axis-aligned clearances: negative values indicate non-containment."""
    cx0, cy0, cz0, cx1, cy1, cz1 = container_bbox
    ix0, iy0, iz0, ix1, iy1, iz1 = insert_bbox
    values = {
        "-x": ix0 - cx0, "+x": cx1 - ix1,
        "-y": iy0 - cy0, "+y": cy1 - iy1,
        "-z": iz0 - cz0, "+z": cz1 - iz1,
    }
    return {
        "directions": values,
        "minimum": min(values.values()),
        "contained": all(value >= 0 for value in values.values()),
    }


def _bbox_tuple(shape):
    bb = shape.BoundBox
    return (bb.XMin, bb.YMin, bb.ZMin, bb.XMax, bb.YMax, bb.ZMax)


def op_check_fit(component_id, container, insert, retainers=None,
                 probe_steps=16, tolerance=0.01):
    """Check envelope containment, interference, and translational retention."""
    doc, component, meta = _resolve(component_id)
    id_map = meta.get("id_map", {})

    def feature(fid):
        name = id_map.get(fid)
        obj = doc.getObject(name) if name else None
        if obj is None or not hasattr(obj, "Shape"):
            raise ValueError("unknown shape feature %r" % fid)
        return obj

    cavity = feature(container)
    item = feature(insert)
    retainer_objects = [feature(fid) for fid in (retainers or [])]
    clearance = _bbox_clearances(_bbox_tuple(cavity.Shape), _bbox_tuple(item.Shape))
    common_volume = cavity.Shape.common(item.Shape).Volume
    insert_volume = item.Shape.Volume
    contained = insert_volume > 0 and common_volume >= insert_volume - tolerance ** 3

    interference = {}
    for fid, obj in zip(retainers or [], retainer_objects):
        volume = item.Shape.common(obj.Shape).Volume
        if volume > tolerance ** 3:
            interference[fid] = volume

    bb = cavity.Shape.BoundBox
    travel = max(bb.XLength, bb.YLength, bb.ZLength) + max(
        item.Shape.BoundBox.XLength, item.Shape.BoundBox.YLength,
        item.Shape.BoundBox.ZLength,
    )
    axes = {
        "+x": App.Vector(1, 0, 0), "-x": App.Vector(-1, 0, 0),
        "+y": App.Vector(0, 1, 0), "-y": App.Vector(0, -1, 0),
        "+z": App.Vector(0, 0, 1), "-z": App.Vector(0, 0, -1),
    }
    # Cache retainer bounding boxes once; a cheap bbox-overlap test prunes the
    # expensive boolean common() for the many probe positions that can't touch
    # a retainer at all (non-overlapping bboxes can't share volume — exact).
    retainer_bbs = [(obj, obj.Shape.BoundBox) for obj in retainer_objects]
    vol_eps = tolerance ** 3
    blocked = {}
    for label, axis in axes.items():
        hit = False
        for step in range(1, int(probe_steps) + 1):
            moved = item.Shape.copy()
            distance = travel * step / float(probe_steps)
            moved.translate(App.Vector(axis.x * distance, axis.y * distance,
                                       axis.z * distance))
            moved_bb = moved.BoundBox
            for obj, obj_bb in retainer_bbs:
                if not moved_bb.intersect(obj_bb):
                    continue
                if moved.common(obj.Shape).Volume > vol_eps:
                    hit = True
                    break
            if hit:
                break
        blocked[label] = hit

    return {
        "container": container, "insert": insert,
        "contained": contained and clearance["contained"],
        "clearance_mm": clearance,
        "interference_mm3": interference,
        "blocked_translation": blocked,
        "unrestrained_translation": [
            direction for direction, is_blocked in blocked.items()
            if not is_blocked
        ],
        "method": "exact overlap with sampled translational paths",
    }


def _safe_filename(value):
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return cleaned or "component"


def _file_result(path, fmt, output=None):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "path": path, "format": fmt, "output": output,
        "bytes": os.path.getsize(path), "sha256": digest.hexdigest(),
    }


def op_export_bundle(component_id, directory, formats=None, per_output=True,
                     assembly=True, overwrite=True, basename=None):
    doc, container, meta = _resolve(component_id)
    os.makedirs(directory, exist_ok=True)
    formats = [str(fmt).upper() for fmt in (formats or ["FCSTD", "STEP", "STL"])]
    base = _safe_filename(basename or container.Label or container.Name)
    output_ids = list(meta.get("outputs") or [
        f["id"] for f in meta.get("build_graph", [])
        if f.get("role", "output") == "output"
    ])
    ext = {"FCSTD": "FCStd", "STEP": "step", "STP": "step",
           "STL": "stl", "IGES": "iges", "IGS": "iges",
           "BREP": "brep", "BRP": "brep"}
    artifacts = []

    def ensure(path):
        if os.path.exists(path) and not overwrite:
            raise ValueError("export exists and overwrite=false: %s" % path)

    if assembly:
        for fmt in formats:
            suffix = ext.get(fmt)
            if suffix is None:
                raise ValueError("unsupported format %r" % fmt)
            path = os.path.join(directory, "%s.%s" % (base, suffix))
            ensure(path)
            op_export_component(component_id, path, fmt)
            artifacts.append(_file_result(path, fmt, "assembly"))

    if per_output:
        import Part
        for fid in output_ids:
            obj = doc.getObject(meta.get("id_map", {}).get(fid, ""))
            if obj is None or not getattr(obj, "Shape", None):
                continue
            for fmt in formats:
                if fmt == "FCSTD":
                    continue
                suffix = ext.get(fmt)
                path = os.path.join(
                    directory, "%s_%s.%s" % (base, _safe_filename(fid), suffix)
                )
                ensure(path)
                if fmt == "STL":
                    obj.Shape.exportStl(path)
                else:
                    Part.export([obj], path)
                artifacts.append(_file_result(path, fmt, fid))
    return {"component_id": component_id, "artifacts": artifacts}


def op_build_component(document, component, validate=None, exports=None):
    """Create, define, optionally validate, and export a component atomically by stage."""
    document_spec = document if isinstance(document, dict) else {"name": document}
    name = document_spec["name"]
    if document_spec.get("replace") and name in App.listDocuments():
        App.closeDocument(name)
    created = op_create_component(
        name, component["name"], component.get("label"),
        component.get("parameters") or [],
    )
    component_id = created["component_id"]
    defined = op_define_component(
        component_id, component.get("features") or [],
        component.get("rules"), component.get("profiles"),
        component.get("profile_definitions"),
    )
    result = {
        "component_id": component_id,
        "created": created, "defined": defined,
    }
    if component.get("outputs"):
        doc, container, meta = _resolve(component_id)
        meta["outputs"] = list(component["outputs"])
        _save(container, meta)
    if validate is not None:
        spec = validate if isinstance(validate, dict) else {}
        result["validation"] = op_validate_component(
            component_id, profiles=spec.get("profiles"),
            rule_ids=spec.get("rule_ids"),
            include_measurements=spec.get("include_measurements", False),
            tolerance=spec.get("tolerance"),
        )
    if exports:
        result["exports"] = op_export_bundle(component_id, **exports)
    return result
