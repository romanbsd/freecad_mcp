"""Read-only geometry inspection and calibrated orthographic PNG overlays."""
import base64
import math
import struct
import xml.etree.ElementTree as ET


def calibration(model_points, pixel_points):
    """Two anchors define uniform scale/rotation; projected model y points up."""
    message = "calibration needs two finite 2D model and pixel anchors"
    try:
        model_points = [[float(x) for x in point] for point in model_points]
        pixel_points = [[float(x) for x in point] for point in pixel_points]
        if len(model_points) != 2 or len(pixel_points) != 2 or any(
                len(point) != 2 or not all(math.isfinite(x) for x in point)
                for point in model_points+pixel_points):
            raise ValueError(message)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(message) from error
    a, b = model_points
    p, q = pixel_points
    dx, dy = b[0]-a[0], -(b[1]-a[1])
    px, py = q[0]-p[0], q[1]-p[1]
    denom = dx*dx+dy*dy
    if denom <= 1e-20 or px*px+py*py <= 1e-20:
        raise ValueError("calibration anchors must be distinct")
    c, s = (px*dx+py*dy)/denom, (py*dx-px*dy)/denom
    def project(point):
        x, y = point[0]-a[0], -(point[1]-a[1])
        return [p[0]+c*x-s*y, p[1]+s*x+c*y]
    return project, math.hypot(c, s)


def _count(value, name, maximum=100):
    if type(value) is not int or not 2 <= value <= maximum:
        raise ValueError("%s must be an integer from 2 to %d" % (name, maximum))
    return value


def inspect_shape(shape, samples=15, stripe_axis=None, stripe_count=12):
    """Sample principal curvatures and reflection stripes on trimmed faces.

    The reflected orthographic viewing ray is projected onto stripe_axis.
    This is a directional reflection diagnostic, not a full environment render.
    """
    import FreeCAD as App
    _count(samples, "samples")
    _count(stripe_count, "stripe_count", 200)
    axis = App.Vector(*(stripe_axis or [1, 0, 0]))
    if axis.Length < 1e-12:
        raise ValueError("stripe_axis must be nonzero")
    axis.normalize()
    view_ray = App.Vector(0, 0, 1)
    faces = []
    for index, face in enumerate(shape.Faces, 1):
        u0, u1, v0, v1 = face.ParameterRange
        points = []
        for i in range(samples):
            for j in range(samples):
                u, v = u0+(u1-u0)*(i+0.5)/samples, v0+(v1-v0)*(j+0.5)/samples
                point = face.valueAt(u, v)
                if not face.isInside(point, 1e-7, True):
                    continue
                normal = face.normalAt(u, v)
                kmin, kmax = face.curvatureAt(u, v)
                reflected = view_ray-normal*(2*view_ray.dot(normal))
                band = int(math.floor((reflected.dot(axis)+1)*stripe_count/2)) % 2
                points.append({"uv": [u, v], "point": list(point), "normal": list(normal),
                               "principal_curvatures": [kmin, kmax], "stripe": band})
        faces.append({"face": index, "samples": points})
    return {"faces": faces, "curvature_units": "1/mm", "view_direction": [0, 0, 1],
            "stripe_axis": list(axis), "stripe_count": stripe_count}


def sections(shape, plane, offsets, samples=80):
    import FreeCAD as App
    import Part
    _count(samples, "samples", 1000)
    if plane not in ("XY", "XZ", "YZ"):
        raise ValueError("plane must be XY, XZ or YZ")
    if not offsets or len(offsets) > 100:
        raise ValueError("provide 1 to 100 section offsets")
    bounds = shape.BoundBox
    extent = max(bounds.DiagonalLength*2, 1)
    axis = {"XY": App.Vector(0, 0, 1), "XZ": App.Vector(0, 1, 0), "YZ": App.Vector(1, 0, 0)}[plane]
    center = bounds.Center
    output = []
    for offset in offsets:
        offset = float(offset)
        if not math.isfinite(offset):
            raise ValueError("section offset must be finite")
        origin = center-axis*center.dot(axis)+axis*offset
        cutter = Part.makePlane(extent, extent)
        rotation = App.Rotation(App.Vector(0, 0, 1), axis)
        cutter.Placement = App.Placement(origin+rotation.multVec(App.Vector(-extent/2, -extent/2, 0)), rotation)
        cut = shape.section(cutter)
        output.append({"offset": offset, "curves": [[list(p) for p in e.discretize(Number=samples)] for e in cut.Edges]})
    return {"plane": plane, "sections": output}


def reference_svg(shape, image_path, plane, model_points, pixel_points, samples=80,
                  opacity=0.55, inspection=None):
    _count(samples, "samples", 1000)
    indices = {"XY": (0, 1), "XZ": (0, 2), "YZ": (1, 2)}.get(plane)
    if indices is None:
        raise ValueError("plane must be XY, XZ or YZ")
    if not 0 <= opacity <= 1:
        raise ValueError("opacity must be from 0 to 1")
    project, scale = calibration(model_points, pixel_points)
    with open(image_path, "rb") as source:
        data = source.read()
    if len(data) < 24 or data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
        raise ValueError("reference image must be a PNG")
    width, height = struct.unpack(">II", data[16:24])
    if not 1 <= width <= 20000 or not 1 <= height <= 20000:
        raise ValueError("reference PNG dimensions out of range")
    ns = "http://www.w3.org/2000/svg"
    ET.register_namespace("", ns)
    root = ET.Element("{%s}svg" % ns, {"width": str(width), "height": str(height),
                     "viewBox": "0 0 %d %d" % (width, height)})
    ET.SubElement(root, "{%s}image" % ns, {"width": str(width), "height": str(height),
                  "opacity": str(opacity), "href": "data:image/png;base64,"+base64.b64encode(data).decode()})
    def pixel(p):
        return project([p[indices[0]], p[indices[1]]])
    for edge in shape.Edges:
        coords = [pixel(list(p)) for p in edge.discretize(Number=samples)]
        ET.SubElement(root, "{%s}polyline" % ns, {"points": " ".join("%.6f,%.6f" % tuple(p) for p in coords),
                      "fill": "none", "stroke": "#00A5E0", "stroke-width": "1"})
    if inspection:
        for face in inspection["faces"]:
            for sample in face["samples"]:
                x, y = pixel(sample["point"])
                ET.SubElement(root, "{%s}circle" % ns, {"cx": str(x), "cy": str(y), "r": "1.5",
                    "fill": "#FFFFFF" if sample["stripe"] else "#111111"})
    return {"svg": ET.tostring(root, encoding="unicode"), "width": width, "height": height,
            "pixels_per_mm": scale, "plane": plane, "projection": "orthographic_edges"}
