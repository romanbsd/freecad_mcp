"""Bounded BRep print preflight. Sampling is evidence, not a thickness proof."""
import math


def _finite(value, name, lower=0, upper=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if value <= lower or (upper is not None and value > upper):
        raise ValueError(f"{name} out of range")
    return float(value)


def _integer(value, name, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return value


def _parameters(min_wall, samples_per_face, build_direction, overhang_angle, max_samples):
    wall = _finite(min_wall, "min_wall")
    count = _integer(samples_per_face, "samples_per_face", 10000)
    budget = _integer(max_samples, "max_samples", 10000)
    angle = _finite(overhang_angle, "overhang_angle", upper=90)
    if not isinstance(build_direction, (list, tuple)) or len(build_direction) != 3:
        raise ValueError("build_direction must contain three finite numbers")
    direction = []
    for value in build_direction:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("build_direction must contain three finite numbers")
        direction.append(float(value))
    length = math.hypot(*direction)
    if not math.isfinite(length) or length <= 0:
        raise ValueError("build_direction must be nonzero")
    return wall, count, [x / length for x in direction], angle, budget


def _uv_samples(face, count):
    """Center first, followed by a deterministic grid of interior UV candidates."""
    u0, u1, v0, v1 = face.ParameterRange
    yield (u0 + u1) / 2, (v0 + v1) / 2
    n = math.ceil(math.sqrt(max(1, count - 1)))
    if 1 + n*n - (n % 2) < count:
        n += 1  # An odd grid contains the center already yielded above.
    emitted = 1
    for i in range(n):
        for j in range(n):
            u, v = u0 + (i + .5) * (u1-u0)/n, v0 + (j + .5) * (v1-v0)/n
            if abs(u-(u0+u1)/2) < 1e-14 and abs(v-(v0+v1)/2) < 1e-14:
                continue
            if emitted >= count:
                return
            yield u, v
            emitted += 1


def _wall_at(shape, point, normal, ray_length, epsilon, Part):
    """First material interval from an inset surface point, never across a cavity."""
    plus = shape.isInside(point + normal * epsilon, 0, False)
    minus = shape.isInside(point - normal * epsilon, 0, False)
    if plus == minus:
        return None, None, "normal probes do not identify one material side"
    inward = normal if plus else -normal
    origin = point + inward * epsilon
    common = Part.makeLine(origin, origin + inward * ray_length).common(shape)
    intervals = []
    for edge in common.Edges:
        distances = [(vertex.Point-origin).dot(inward) for vertex in edge.Vertexes]
        if len(distances) >= 2:
            intervals.append((min(distances), max(distances)))
    for start, end in sorted(intervals):
        if start <= epsilon * .1 and end > epsilon * .1:
            if end >= ray_length - epsilon:
                return None, -inward, "ray did not reach a material exit"
            return end + epsilon, -inward, None
    return None, -inward, "no material segment covers inset origin"


def analyze_shape(shape, min_wall=0.8, samples_per_face=5,
                  build_direction=None, overhang_angle=45, max_samples=2000):
    """Inspect native closed solids in world coordinates without mutating them.

    Wall values are local distances along sampled surface normals. This does
    not prove a global minimum, nor predict support/toolpath correctness.
    """
    wall, count, direction, angle, budget = _parameters(
        min_wall, samples_per_face, [0, 0, 1] if build_direction is None else build_direction,
        overhang_angle, max_samples)
    if shape.isNull() or not shape.isValid() or not shape.Solids:
        raise ValueError("printability requires a valid closed solid shape")
    def only_solids(item):
        if item.ShapeType == "Solid":
            return item.isClosed()
        if item.ShapeType in ("Compound", "CompSolid"):
            return all(only_solids(child) for child in item.childShapes())
        return False
    if not only_solids(shape):
        raise ValueError("printability requires only closed solids")
    import FreeCAD as App
    import Part
    build = App.Vector(*direction)
    ray_length = shape.BoundBox.DiagonalLength * 2 + wall * 2
    epsilon = min(1e-5, wall * 1e-4)
    # Vertices alone miss extrema of spheres/cylinders. Rotate a transient copy
    # into the build frame and use the native BRep bounding box instead.
    build_rotation = App.Placement(App.Vector(), App.Rotation(build, App.Vector(0, 0, 1)))
    def build_min(item):
        copy = item.copy()
        copy.Placement = build_rotation.multiply(item.Placement)
        return copy.BoundBox.ZMin
    bed = build_min(shape)
    findings, samples = [], []
    attempted = resolved = unresolved = skipped_trim = overhangs = 0
    observed = None
    thin_by_face, overhang_by_face = {}, {}
    truncated = False
    for face_id, face in enumerate(shape.Faces, 1):
        for u, v in _uv_samples(face, count):
            if attempted >= budget:
                truncated = True
                break
            attempted += 1
            record = {"face": face_id}
            try:
                point = face.valueAt(u, v)
                record["point"] = [point.x, point.y, point.z]
                if Part.Vertex(point).distToShape(face)[0] > epsilon:
                    skipped_trim += 1
                    unresolved += 1
                    record.update(status="unresolved", reason="UV sample is outside trimmed face")
                    samples.append(record)
                    continue
                normal = face.normalAt(u, v)
                normal.normalize()
                thickness, outward, reason = _wall_at(shape, point, normal, ray_length, epsilon, Part)
                if thickness is None:
                    unresolved += 1
                    record.update(status="unresolved", reason=reason)
                else:
                    resolved += 1
                    observed = thickness if observed is None else min(observed, thickness)
                    record.update(status="resolved", wall_mm=thickness)
                    if thickness < wall:
                        candidate = {"kind": "thin_wall", "face": face_id, "point": record["point"],
                                     "actual_mm": thickness, "threshold_mm": wall, "severity": "warning"}
                        if face_id not in thin_by_face or thickness < thin_by_face[face_id]["actual_mm"]:
                            thin_by_face[face_id] = candidate
                if outward is not None and point.dot(build) > bed + epsilon * 10:
                    if outward.dot(build) < -math.sin(math.radians(angle)) - 1e-9:
                        overhangs += 1
                        record["overhang"] = True
                        overhang_by_face.setdefault(face_id, {"kind": "overhang", "face": face_id,
                            "point": record["point"], "threshold_degrees": angle,
                            "severity": "warning"})
            except Exception as exc:
                unresolved += 1
                record.update(status="unresolved", reason=f"geometry evaluation failed: {type(exc).__name__}")
            samples.append(record)
        if truncated:
            break
    findings.extend(thin_by_face.values())
    findings.extend(overhang_by_face.values())
    solids = shape.Solids
    connectivity_complete = len(solids) <= 64
    clusters = []
    if connectivity_complete:
        parents = list(range(len(solids)))
        def root(index):
            while parents[index] != index:
                index = parents[index]
            return index
        for i, first in enumerate(solids):
            for j in range(i):
                if first.distToShape(solids[j])[0] <= epsilon * 10:
                    parents[root(i)] = root(j)
        grouped = {}
        for index in range(len(solids)):
            grouped.setdefault(root(index), []).append(index)
        clusters = list(grouped.values())
        if len(clusters) > 1:
            findings.append({"kind": "disconnected_solids", "count": len(clusters),
                             "solid_count": len(solids), "severity": "warning"})
        for indices in clusters:
            low = min(build_min(solids[index]) for index in indices)
            if low > bed + epsilon * 10:
                findings.append({"kind": "floating_cluster", "solids": [index + 1 for index in indices],
                                 "clearance_mm": low-bed, "severity": "warning"})
    else:
        findings.append({"kind": "incomplete_connectivity", "solid_count": len(solids),
                         "limit": 64, "severity": "warning"})
    if unresolved or truncated:
        findings.append({"kind": "incomplete_sampling", "severity": "warning"})
    return {"status": "WARN" if findings else "PASS", "findings": findings,
            "min_observed_wall_mm": observed, "samples": samples,
            "sampling": {"attempted": attempted, "resolved": resolved, "unresolved": unresolved,
                         "outside_trim": skipped_trim, "truncated": truncated,
                         "overhang_samples": overhangs,
                         "overhang_sample_fraction": overhangs/attempted if attempted else 0},
            "connectivity": {"complete": connectivity_complete, "clusters": len(clusters) if connectivity_complete else None},
            "method": "BRep UV sampling, inward normal ray, first material exit",
            "limitations": ["Bounded samples do not guarantee the global minimum wall thickness.",
                            "UV sampling is not uniform in surface area; overhang fraction is a sample fraction.",
                            "Normal wall distances are not a nearest-surface thickness field.",
                            "Overhang screening does not predict supports, bridges, or slicer toolpaths."]}
