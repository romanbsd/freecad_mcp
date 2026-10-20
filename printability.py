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


def _only_closed_solids(item):
    if item.ShapeType == "Solid":
        return item.isClosed()
    if item.ShapeType in ("Compound", "CompSolid"):
        return all(_only_closed_solids(child) for child in item.childShapes())
    return False


def analyze_shape(shape, min_wall=0.8, samples_per_face=5,
                  build_direction=None, overhang_angle=45, max_samples=2000, layer_sampling=None):
    """Inspect native closed solids in world coordinates without mutating them.

    Wall values are local distances along sampled surface normals. This does
    not prove a global minimum, nor predict support/toolpath correctness.
    """
    wall, count, direction, angle, budget = _parameters(
        min_wall, samples_per_face, [0, 0, 1] if build_direction is None else build_direction,
        overhang_angle, max_samples)
    if shape.isNull() or not shape.isValid() or not shape.Solids:
        raise ValueError("printability requires a valid closed solid shape")
    if not _only_closed_solids(shape):
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
    result = {"status": "WARN" if findings else "PASS", "findings": findings,
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
    if layer_sampling is not None:
        if not isinstance(layer_sampling, dict):
            raise ValueError("layer_sampling must be a dictionary")
        options = dict(layer_sampling)
        if "build_direction" in options or "overhang_angle" in options:
            raise ValueError("layer_sampling uses the top-level build_direction and overhang_angle")
        result["layers"] = analyze_layers(shape, build_direction=direction,
                                          overhang_angle=angle, **options)
        if result["layers"]["status"] != "PASS":
            result["status"] = "WARN"
    return result



def _cell_components(cells):
    """Four-connected raster islands; diagonal contact alone is not a bond."""
    remaining = set(cells)
    groups = []
    while remaining:
        seed = remaining.pop()
        group, pending = {seed}, [seed]
        while pending:
            x, y = pending.pop()
            for neighbor in ((x-1,y), (x+1,y), (x,y-1), (x,y+1)):
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    group.add(neighbor)
                    pending.append(neighbor)
        groups.append(group)
    return groups


def _layer_events(current, previous, reach, consume=lambda amount: None):
    """Classify raster starts separately from anchored bridges/expansions."""
    events = []
    prior_groups = _cell_components(previous)
    for group in _cell_components(current):
        consume(len(group) * ((2*reach+1)**2 + len(prior_groups)))
        anchors = [index for index, prior in enumerate(prior_groups) if group & prior]
        nearby = {cell for cell in group if any(
            (cell[0]+dx, cell[1]+dy) in previous
            for dx in range(-reach, reach+1) for dy in range(-reach, reach+1)
            if dx*dx+dy*dy <= reach*reach)}
        if not nearby:
            events.append(("floating_layer_island", group))
        elif len(nearby) < len(group):
            events.append(("potential_bridge" if len(anchors) >= 2 else
                           "unsupported_layer_expansion", group - nearby))
    return events


def _raster_section(polygons, pitch, origin, consume):
    """Even/odd scan conversion of closed native section wires, including holes."""
    if not polygons:
        return set()
    edges = [(a, b) for poly in polygons for a, b in zip(poly, poly[1:] + poly[:1])]
    low = math.floor((min(p[1] for poly in polygons for p in poly)-origin[1])/pitch)
    high = math.ceil((max(p[1] for poly in polygons for p in poly)-origin[1])/pitch)
    cells = set()
    for row in range(low, high):
        consume(len(edges))
        y = origin[1] + (row+.5)*pitch
        crossings = sorted(a[0] + (y-a[1])*(b[0]-a[0])/(b[1]-a[1])
                           for a,b in edges if (a[1] <= y < b[1]) or (b[1] <= y < a[1]))
        if len(crossings) % 2:
            raise ValueError("section scan has an odd crossing count")
        for left, right in zip(crossings[::2], crossings[1::2]):
            start = math.ceil((left-origin[0])/pitch-.5)
            stop = math.ceil((right-origin[0])/pitch-.5)
            consume(max(0, stop-start))
            cells.update((col,row) for col in range(start,stop))
    return cells


def analyze_layers(shape, build_direction=None, layer_height=.2, xy_resolution=.8,
                   overhang_angle=45, max_layers=2000, max_work=2000000,
                   time_budget=15, max_findings=200):
    """Opt-in cross-section raster screening, not a support/toolpath certification.

    Time is checked between native calls; one OCC slice cannot be interrupted.
    Work counts polygon scan intersections and occupied cells, not elapsed time.
    """
    import time
    import FreeCAD as App
    _wall, _count, direction, angle, _budget = _parameters(
        .8, 1, [0,0,1] if build_direction is None else build_direction, overhang_angle, 1)
    height = _finite(layer_height, "layer_height", upper=10)
    pitch = _finite(xy_resolution, "xy_resolution", upper=10)
    limit = _integer(max_layers, "max_layers", 10000)
    work_limit = _integer(max_work, "max_work", 10000000)
    seconds = _finite(time_budget, "time_budget", upper=120)
    finding_limit = _integer(max_findings, "max_findings", 1000)
    if shape.isNull() or not shape.isValid() or not shape.Solids or not _only_closed_solids(shape):
        raise ValueError("layer screening requires valid closed solid geometry")
    rotation = App.Placement(App.Vector(), App.Rotation(App.Vector(*direction), App.Vector(0,0,1)))
    oriented = shape.copy()
    oriented.Placement = rotation.multiply(shape.Placement)
    inverse = rotation.inverse()
    box = oriented.BoundBox
    expected = math.ceil((box.ZMax-box.ZMin)/height)
    origin = (box.XMin, box.YMin)
    # Raster allowance is rounded up: uncertainty is explicitly one XY cell.
    reach = math.ceil(height*math.tan(math.radians(angle))/pitch - 1e-9)
    reach = min(reach, 20)
    started = time.monotonic()
    work = 0
    records, counts, previous = [], {}, set()
    examined = 0
    stop_reason = None
    unresolved_layers = 0
    class BudgetExceeded(Exception):
        pass
    def consume(amount):
        nonlocal work
        work += amount
        if work > work_limit:
            raise BudgetExceeded("max_work")
        if time.monotonic()-started >= seconds:
            raise BudgetExceeded("time_budget")
    try:
        for index in range(min(expected, limit)):
            consume(1)
            z = box.ZMin + min((index+.5)*height, box.ZMax-box.ZMin-1e-7)
            wires = oriented.slice(App.Vector(0,0,1), z)
            polygons = []
            for wire in wires:
                consume(1)
                if not wire.isClosed():
                    raise ValueError("open native section wire")
                points = wire.discretize(Deflection=pitch/8)
                consume(len(points))
                polygons.append([(p.x,p.y) for p in points])
            current = _raster_section(polygons, pitch, origin, consume)
            consume(len(current) + len(previous))
            if polygons and not current:
                unresolved_layers += 1
            events = [] if index == 0 else _layer_events(current, previous, reach, consume)
            for kind, cells in events:
                counts[kind] = counts.get(kind,0)+1
                if len(records) < finding_limit:
                    col,row = min(cells)
                    point = inverse.multVec(App.Vector(origin[0]+(col+.5)*pitch,
                                                       origin[1]+(row+.5)*pitch,z))
                    records.append({"kind":kind, "severity":"warning", "layer":index+1,
                                    "build_height_mm":z-box.ZMin,
                                    "point":[point.x,point.y,point.z],
                                    "sampled_area_mm2":len(cells)*pitch*pitch})
            previous = current
            examined += 1
        if examined < expected:
            stop_reason = "max_layers"
    except BudgetExceeded as exc:
        stop_reason = str(exc)
    except Exception as exc:
        stop_reason = "section_evaluation_failed: " + type(exc).__name__ + ": " + str(exc)
    complete = stop_reason is None
    return {"status":"WARN" if counts or not complete or unresolved_layers else "PASS",
            "complete":complete, "stop_reason":stop_reason,
            "expected_layers":expected, "examined_layers":examined,
            "unresolved_empty_raster_layers":unresolved_layers,
            "layer_height_mm":height, "xy_resolution_mm":pitch,
            "work_used":work, "work_limit":work_limit,
            "elapsed_seconds":time.monotonic()-started, "time_budget_seconds":seconds,
            "findings":records, "finding_counts":counts,
            "findings_truncated":sum(counts.values()) > len(records),
            "build_direction":direction,
            "method":"Native planar sections, even/odd raster, adjacent-layer connectivity",
            "limitations":["Raster screening can miss features smaller than the XY resolution or layer interval.",
                           "One-cell raster allowance can hide small unsupported expansions.",
                           "Potential bridges are geometric candidates, not approved bridge lengths or toolpaths.",
                           "Overlapping compound solids should be fused before even/odd section screening.",
                           "Time budget is cooperative between native calls; a single OCC operation is not interruptible.",
                           "Does not certify adhesion, strength, support removal, or successful printing."]}
