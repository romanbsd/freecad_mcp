"""Dependency-free, atomic 3MF Core serializer for named colored mesh parts."""
import math
import os
import re
import tempfile
import zipfile
import xml.etree.ElementTree as ET

CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def color_hex(value):
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", value):
        raise ValueError("color must be #RRGGBB or #RRGGBBAA")
    return value.upper() + ("FF" if len(value) == 7 else "")


def validate_parts(parts):
    if not parts:
        raise ValueError("3MF needs at least one mesh part")
    result, names = [], set()
    for part in parts:
        name = part["name"]
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("part names must be unique non-empty strings")
        names.add(name)
        color = color_hex(part.get("color", "#CCCCCC"))
        vertices = [list(p) for p in part["vertices"]]
        if len(vertices) < 4 or any(len(p) != 3 or any(
                isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x)
                for x in p) for p in vertices):
            raise ValueError("vertices must contain finite 3D coordinates")
        triangles, edges, vertex_links, volume = [], {}, {}, 0.0
        # Translate before computing volume to avoid cancellation far from origin.
        origin = vertices[0]
        for tri in part["triangles"]:
            if len(tri) != 3 or any(type(i) is not int or i < 0 or i >= len(vertices) for i in tri):
                raise ValueError("invalid triangle index")
            a, b, c = [[vertices[i][k]-origin[k] for k in range(3)] for i in tri]
            ab, ac = [b[k]-a[k] for k in range(3)], [c[k]-a[k] for k in range(3)]
            cross = [ab[1]*ac[2]-ab[2]*ac[1], ab[2]*ac[0]-ab[0]*ac[2], ab[0]*ac[1]-ab[1]*ac[0]]
            if sum(x*x for x in cross) <= 1e-24:
                raise ValueError("degenerate triangle")
            volume += sum(a[k]*(b[(k+1)%3]*c[(k+2)%3]-b[(k+2)%3]*c[(k+1)%3]) for k in range(3))/6
            for i, j in zip(tri, (tri[1], tri[2], tri[0])):
                key = tuple(sorted((i, j)))
                edges.setdefault(key, []).append((i, j))
            for number, vertex in enumerate(tri):
                i, j = tri[(number+1)%3], tri[(number+2)%3]
                link = vertex_links.setdefault(vertex, {})
                link.setdefault(i, set()).add(j)
                link.setdefault(j, set()).add(i)
            triangles.append(list(tri))
        if not edges or any(len(e) != 2 or e[0] != e[1][::-1] for e in edges.values()):
            raise ValueError("mesh must be closed, oriented and two-manifold")
        for link in vertex_links.values():
            # A closed manifold vertex has one circular link. Edge incidence
            # alone cannot distinguish this from two shells pinched together.
            pending, visited = [next(iter(link))], set()
            while pending:
                vertex = pending.pop()
                if vertex not in visited:
                    visited.add(vertex)
                    pending.extend(link[vertex]-visited)
            if len(visited) != len(link) or any(len(adjacent) != 2 for adjacent in link.values()):
                raise ValueError("mesh vertex neighborhood must be manifold")
        if volume <= 1e-12:
            raise ValueError("mesh must enclose positive volume with outward orientation")
        result.append(dict(name=name, color=color, vertices=vertices, triangles=triangles))
    return result


def read_3mf(path):
    """Read this serializer's strict Core assembly contract for roundtrip checks."""
    try:
        return _read_3mf(path)
    except (AttributeError, KeyError, TypeError, IndexError,
            ET.ParseError, zipfile.BadZipFile) as error:
        raise ValueError("invalid 3MF assembly structure: %s" % error) from error


def _read_3mf(path):
    with zipfile.ZipFile(path) as archive:
        model = ET.fromstring(archive.read("3D/3dmodel.model"))
    ns = {"m": CORE}
    if model.get("unit") != "millimeter":
        raise ValueError("unexpected 3MF units")
    resources = model.find("m:resources", ns)
    bases = resources.find("m:basematerials", ns)
    colors = [b.attrib["displaycolor"] for b in bases]
    objects = {o.attrib["id"]: o for o in resources.findall("m:object", ns)}
    items = model.findall("m:build/m:item", ns)
    if len(items) != 1:
        raise ValueError("3MF assembly must have exactly one build item")
    item = items[0]
    if "transform" in item.attrib:
        raise ValueError("unexpected build transform")
    assembly = objects[item.attrib["objectid"]]
    parts = []
    for component in assembly.findall("m:components/m:component", ns):
        if "transform" in component.attrib:
            raise ValueError("unexpected component transform")
        obj = objects[component.attrib["objectid"]]
        mesh = obj.find("m:mesh", ns)
        verts = [[float(v.attrib[k]) for k in ("x", "y", "z")]
                 for v in mesh.find("m:vertices", ns)]
        triangles, color = [], None
        for t in mesh.find("m:triangles", ns):
            if t.get("pid") != bases.attrib["id"]:
                raise ValueError("unknown triangle material")
            index = int(t.attrib["p1"])
            if not 0 <= index < len(colors):
                raise ValueError("invalid triangle material index")
            if any(int(t.get(key, index)) != index for key in ("p2", "p3")):
                raise ValueError("part has inconsistent vertex materials")
            current = colors[index]
            if color is not None and current != color:
                raise ValueError("part has inconsistent materials")
            color = current
            triangles.append([int(t.attrib[k]) for k in ("v1", "v2", "v3")])
        parts.append(dict(name=obj.attrib["name"], color=color,
                          vertices=verts, triangles=triangles))
    return validate_parts(parts)


def write_3mf(path, parts):
    parts = validate_parts(parts)
    ET.register_namespace("", CORE)
    def element(parent, tag, **attrs):
        return ET.SubElement(parent, "{%s}%s" % (CORE, tag), {k: str(v) for k, v in attrs.items()})
    model = ET.Element("{%s}model" % CORE, {"unit": "millimeter", "xml:lang": "en-US"})
    resources = element(model, "resources")
    colors = list(dict.fromkeys(p["color"] for p in parts))
    bases = element(resources, "basematerials", id=1)
    for i, color in enumerate(colors):
        element(bases, "base", name="Color_%d" % i, displaycolor=color)
    for oid, part in enumerate(parts, 2):
        obj = element(resources, "object", id=oid, type="model", name=part["name"])
        mesh = element(obj, "mesh")
        vertices, triangles = element(mesh, "vertices"), element(mesh, "triangles")
        for p in part["vertices"]:
            element(vertices, "vertex", **dict(zip(("x", "y", "z"), (repr(float(x)) for x in p))))
        for tri in part["triangles"]:
            element(triangles, "triangle", v1=tri[0], v2=tri[1], v3=tri[2], pid=1, p1=colors.index(part["color"]))
    assembly_id = len(parts)+2
    assembly = element(resources, "object", id=assembly_id, type="model", name="Assembly")
    components = element(assembly, "components")
    for oid in range(2, assembly_id):
        element(components, "component", objectid=oid)
    element(element(model, "build"), "item", objectid=assembly_id)
    content_types = b'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>'
    relationships = b'<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel" Target="/3D/3dmodel.model"/></Relationships>'
    path = os.fspath(path)
    fd, temporary = tempfile.mkstemp(prefix=".freecad_3mf_", suffix=".3mf", dir=os.path.dirname(os.path.abspath(path)))
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", content_types)
            archive.writestr("_rels/.rels", relationships)
            archive.writestr("3D/3dmodel.model", ET.tostring(model, encoding="utf-8", xml_declaration=True))
        if read_3mf(temporary) != parts:
            raise ValueError("3MF roundtrip geometry or material mismatch")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"path": path, "format": "3MF", "bytes": os.path.getsize(path),
            "parts": [{"name": p["name"], "color": p["color"],
                       "vertices": len(p["vertices"]), "triangles": len(p["triangles"])} for p in parts]}
