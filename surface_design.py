"""Declarative curves and surface patches; FreeCAD is imported only at use time.

Guided patches use a polynomial Coons control net: all four Bezier boundaries
are reproduced exactly. This guarantees positional (G0) boundary agreement;
it does not silently assert tangency to an adjacent face.
"""
import math


def coons_net(bottom, top, left, right, tolerance=1e-7):
    """Boundaries run in increasing u/v; return a u-major Bezier pole net."""
    if len(bottom) != len(top) or len(left) != len(right):
        raise ValueError("opposite guides must have equal pole counts")
    if min(len(bottom), len(left)) < 2:
        raise ValueError("guides need at least two poles")
    for a, b in ((bottom[0], left[0]), (bottom[-1], right[0]),
                 (top[0], left[-1]), (top[-1], right[-1])):
        if math.dist(a, b) > tolerance:
            raise ValueError("guide corners do not meet")
    m, n = len(bottom) - 1, len(left) - 1
    p00, p10, p01, p11 = bottom[0], bottom[-1], top[0], top[-1]
    return [[[((1-j/n)*bottom[i][k] + (j/n)*top[i][k]
               + (1-i/m)*left[j][k] + (i/m)*right[j][k]
               - ((1-i/m)*(1-j/n)*p00[k] + (i/m)*(1-j/n)*p10[k]
                  + (1-i/m)*(j/n)*p01[k] + (i/m)*(j/n)*p11[k]))
              for k in range(3)] for j in range(n+1)] for i in range(m+1)]


def curve(spec, vectors):
    import Part
    kind = str(spec.get("type", "bspline")).lower()
    if len(vectors) < 2:
        raise ValueError("curve requires at least two points")
    if kind == "bezier":
        result = Part.BezierCurve()
        result.setPoles(vectors)
    elif kind == "bspline":
        result = Part.BSplineCurve()
        if spec.get("mode", "interpolate") == "interpolate":
            args = {"Points": vectors, "PeriodicFlag": bool(spec.get("periodic", False))}
            tangents = spec.get("tangents")
            if tangents:
                if len(tangents) != len(vectors):
                    raise ValueError("tangents must match interpolation points")
                import FreeCAD as App
                args["Tangents"] = [App.Vector(*(t or [0, 0, 0])) for t in tangents]
                args["TangentFlags"] = [t is not None for t in tangents]
            if spec.get("parameters") is not None:
                args["Parameters"] = spec["parameters"]
            result.interpolate(**args)
        elif spec["mode"] == "poles":
            degree = int(spec.get("degree", 3))
            if not 1 <= degree < len(vectors):
                raise ValueError("degree must be positive and smaller than pole count")
            knots, mults = spec.get("knots"), spec.get("multiplicities")
            periodic = bool(spec.get("periodic", False))
            if (knots is None) != (mults is None):
                raise ValueError("knots and multiplicities must be supplied together")
            if knots is None:
                if periodic:
                    raise ValueError("periodic pole splines require explicit knots and multiplicities")
                # Open uniform clamped spline.
                count = len(vectors) - degree
                knots = [i / count for i in range(count+1)]
                mults = [degree+1] + [1]*(count-1) + [degree+1]
            if len(knots) < 2 or len(knots) != len(mults):
                raise ValueError("knots and multiplicities must match in length")
            if any(type(k) not in (int, float) or not math.isfinite(k) for k in knots):
                raise ValueError("knots must be finite numbers")
            if any(a >= b for a, b in zip(knots, knots[1:])):
                raise ValueError("knots must be strictly increasing")
            if any(type(m) is not int or m < 1 for m in mults):
                raise ValueError("multiplicities must be positive integers")
            if periodic:
                # The first/last knot represent the same seam. Count its
                # multiplicity only once in the periodic pole relation.
                valid = (mults[0] == mults[-1] and max(mults) <= degree
                         and sum(mults)-mults[0] == len(vectors))
            else:
                valid = (sum(mults) == len(vectors)+degree+1
                         and max(mults[1:-1], default=1) <= degree
                         and max(mults[0], mults[-1]) <= degree+1)
            if not valid:
                raise ValueError("knot multiplicities must match pole count and degree")
            result.buildFromPolesMultsKnots(vectors, mults, knots,
                                           periodic, degree,
                                           spec.get("weights") or [1.0]*len(vectors))
        else:
            raise ValueError("unknown spline mode")
    else:
        raise ValueError("curve type must be bezier or bspline")
    return result


def make_shape(spec, host):
    import FreeCAD as App
    import Part
    from parametric import _quantity_value

    def point(p):
        if len(p) != 3:
            raise ValueError("surface points must have three coordinates")
        values = [_quantity_value(x, host) for x in p]
        if not all(math.isfinite(x) for x in values):
            raise ValueError("surface coordinates must be finite")
        return values

    kind = spec["type"]
    if kind == "curve":
        cs = spec["curve"]
        cs = dict(cs)
        if cs.get("tangents"):
            cs["tangents"] = [point(t) if t is not None else None for t in cs["tangents"]]
        return curve(cs, [App.Vector(*point(p)) for p in cs.get("points", cs.get("poles", []))]).toShape()
    if spec.get("continuity", "G0") != "G0":
        raise ValueError("surface patches guarantee G0 only; use control poles for tangent design")
    if kind == "guided_surface":
        guides = spec["guides"]
        net = coons_net(*[[point(p) for p in guides[name]]
                          for name in ("bottom", "top", "left", "right")])
        mode = "poles"
    else:
        net = [[point(p) for p in row] for row in spec["grid"]]
        mode = spec.get("mode", "interpolate")
    if len(net) < 2 or len(net[0]) < 2 or any(len(row) != len(net[0]) for row in net):
        raise ValueError("surface grid must be rectangular and at least 2 by 2")
    poles = [[App.Vector(*p) for p in row] for row in net]
    surface = Part.BSplineSurface()
    if mode == "interpolate":
        surface.interpolate(poles)
    elif mode == "poles":
        du, dv = len(net)-1, len(net[0])-1
        surface.buildFromPolesMultsKnots(poles, [du+1, du+1], [dv+1, dv+1],
                                        [0, 1], [0, 1], False, False, du, dv)
    else:
        raise ValueError("surface mode must be interpolate or poles")
    shape = surface.toShape()
    if not shape.isValid():
        raise ValueError("surface patch is invalid")
    return shape


class DesignProxy:
    def dumps(self):
        return None

    def loads(self, state):
        pass

    __getstate__ = dumps
    __setstate__ = loads

    def execute(self, obj):
        import json
        from parametric import _profile_host, _apply_common_placement
        host = _profile_host(obj)
        spec = json.loads(obj.DesignSpec)
        obj.Shape = make_shape(spec, host)
        _apply_common_placement(obj, spec, host,
                                dict(zip(json.loads(obj.DesignLinkIds), obj.DesignLinks)))
