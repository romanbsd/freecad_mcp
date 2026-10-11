"""Read/patch a Bambu 3MF package without rewriting meshes.

Matrices use the 3MF row-vector layout: 12 numbers m00 m01 m02 m10 m11 m12 m20 m21 m22 tx ty tz,
p' = p·A + t.
"""
import json
import math
import re
import xml.etree.ElementTree as ET
from xml.sax.saxutils import quoteattr
import zipfile
from pathlib import Path

MODEL = "3D/3dmodel.model"
SETTINGS = "Metadata/model_settings.config"
NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
P_PATH = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}path"
IDENT = [1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]


# ---- matrix helpers (12-tuples) ------------------------------------------------------------
def mul(a, b):
    """Apply a then b (row-vector convention)."""
    out = []
    for i in range(3):
        for j in range(3):
            out.append(sum(a[i * 3 + k] * b[k * 3 + j] for k in range(3)))
    for j in range(3):
        out.append(sum(a[9 + k] * b[k * 3 + j] for k in range(3)) + b[9 + j])
    return out


def apply(m, p):
    return tuple(sum(p[k] * m[k * 3 + j] for k in range(3)) + m[9 + j] for j in range(3))


def rot_matrix(rx=0.0, ry=0.0, rz=0.0):
    """Rotate about X, then Y, then Z (degrees), row-vector layout."""
    def r(axis, deg):
        c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
        m = {"x": [1, 0, 0, 0, c, s, 0, -s, c], "y": [c, 0, -s, 0, 1, 0, s, 0, c],
             "z": [c, s, 0, -s, c, 0, 0, 0, 1]}[axis]
        return m + [0, 0, 0]
    return mul(mul(r("x", rx), r("y", ry)), r("z", rz))


def parse_m(s):
    v = [float(x) for x in s.split()]
    if len(v) != 12:
        raise ValueError(f"bad transform {s!r}")
    return v


def fmt_m(m):
    return " ".join(f"{x:.9g}" for x in m)


# ---- package I/O -----------------------------------------------------------------------------
class Package:
    def __init__(self, path):
        self.path = Path(path)
        with zipfile.ZipFile(self.path) as z:
            self.entries = {n: z.read(n) for n in z.namelist()}
        if MODEL not in self.entries:
            raise ValueError(f"{path}: not a Bambu 3MF (no {MODEL})")

    def save(self, out):
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for n, b in self.entries.items():
                z.writestr(n, b)

    # -- model --
    @property
    def model_xml(self) -> str:
        return self.entries[MODEL].decode("utf-8")

    def _item_tags(self, oid: str):
        for m in re.finditer(r"<item\b[^>]*>", self.model_xml):
            if re.search(rf'objectid="{oid}"', m.group(0)):
                yield m

    def instances(self, oid: str) -> list[list[float]]:
        """Build-item transforms of every instance of an object, in file order."""
        out = []
        for m in self._item_tags(oid):
            tr = re.search(r'transform="([^"]*)"', m.group(0))
            out.append(parse_m(tr.group(1)) if tr else list(IDENT))
        if not out:
            raise KeyError(f"object {oid} not in build")
        return out

    def items(self) -> dict[str, list[float]]:
        """{object id: first instance's build transform}."""
        out = {}
        for m in re.finditer(r"<item\b[^>]*>", self.model_xml):
            oid = re.search(r'objectid="(\d+)"', m.group(0))
            if oid and oid.group(1) not in out:
                out[oid.group(1)] = self.instances(oid.group(1))[0]
        return out

    def set_item_transform(self, oid: str, m, instance: int = 0):
        """Replace the transform of one instance (default: the first) of an object."""
        seen = -1

        def sub(mm):
            nonlocal seen
            tag = mm.group(0)
            if not re.search(rf'objectid="{oid}"', tag):
                return tag
            seen += 1
            if seen != instance:
                return tag
            if 'transform="' in tag:
                return re.sub(r'transform="[^"]*"', f'transform="{fmt_m(m)}"', tag)
            return tag.replace("<item", f'<item transform="{fmt_m(m)}"', 1)
        new = re.sub(r"<item\b[^>]*>", sub, self.model_xml)
        if seen < instance:
            raise KeyError(f"object {oid} instance {instance} not in build")
        self.entries[MODEL] = new.encode("utf-8")

    def vertices(self, oid: str):
        """All vertices of an object in object space (components resolved)."""
        root = ET.fromstring(self.entries[MODEL])
        objs = {o.get("id"): o for o in root.iter(f"{NS}object")}
        if oid not in objs:
            raise KeyError(f"object {oid} not found")

        def walk(obj, xf):
            mesh = obj.find(f"{NS}mesh")
            if mesh is not None:
                for v in mesh.iter(f"{NS}vertex"):
                    yield apply(xf, (float(v.get("x")), float(v.get("y")), float(v.get("z"))))
            comps = obj.find(f"{NS}components")
            if comps is None:
                return
            for c in comps:
                cx = mul(parse_m(c.get("transform")) if c.get("transform") else IDENT, xf)
                path = c.get(P_PATH)
                if path:
                    sub = ET.fromstring(self.entries[path.lstrip("/")])
                    sobjs = {o.get("id"): o for o in sub.iter(f"{NS}object")}
                    yield from walk_in(sobjs[c.get("objectid")], cx, sobjs)
                else:
                    yield from walk(objs[c.get("objectid")], cx)

        def walk_in(obj, xf, local):
            mesh = obj.find(f"{NS}mesh")
            if mesh is not None:
                for v in mesh.iter(f"{NS}vertex"):
                    yield apply(xf, (float(v.get("x")), float(v.get("y")), float(v.get("z"))))
            comps = obj.find(f"{NS}components")
            if comps is not None:
                for c in comps:
                    cx = mul(parse_m(c.get("transform")) if c.get("transform") else IDENT, xf)
                    yield from walk_in(local[c.get("objectid")], cx, local)

        return list(walk(objs[oid], list(IDENT)))

    def world_bbox(self, oid: str, instance: int = 0):
        m = self.instances(oid)[instance]
        pts = [apply(m, v) for v in self.vertices(oid)]
        lo = tuple(min(p[i] for p in pts) for i in range(3))
        hi = tuple(max(p[i] for p in pts) for i in range(3))
        return lo, hi

    def rotate_object(self, oid: str, rx=0.0, ry=0.0, rz=0.0):
        """Rotate every instance about its own bbox centre, then drop each onto z=0."""
        verts = self.vertices(oid)
        for i, old in enumerate(self.instances(oid)):
            pts = [apply(old, v) for v in verts]
            lo = [min(p[k] for p in pts) for k in range(3)]
            hi = [max(p[k] for p in pts) for k in range(3)]
            c = [(a + b) / 2 for a, b in zip(lo, hi)]
            # world-space rotation about c: translate(-c) · R · translate(+c)
            about = mul(mul([*IDENT[:9], -c[0], -c[1], -c[2]], rot_matrix(rx, ry, rz)), [*IDENT[:9], *c])
            new = mul(old, about)
            new[11] -= min(apply(new, v)[2] for v in verts)
            self.set_item_transform(oid, new, i)

    def project_settings(self) -> dict:
        """Embedded project-level print settings (Metadata/project_settings.config, JSON); {} if absent."""
        raw = self.entries.get("Metadata/project_settings.config")
        try:
            d = json.loads(raw) if raw else {}
        except ValueError:
            return {}
        return d if isinstance(d, dict) else {}

    # -- per-object settings --
    def object_settings(self) -> dict[str, dict[str, str]]:
        if SETTINGS not in self.entries:
            return {}
        root = ET.fromstring(self.entries[SETTINGS])
        return {o.get("id"): {m.get("key"): m.get("value") for m in o.findall("metadata") if m.get("key")}
                for o in root.findall("object")}

    def set_object_settings(self, oid: str, values: dict[str, str]):
        if SETTINGS not in self.entries:
            raise ValueError("3MF has no model_settings.config")
        xml = self.entries[SETTINGS].decode("utf-8")
        m = re.search(rf'<object id="{oid}">(.*?)</object>', xml, re.S)
        if not m:
            raise KeyError(f"object {oid} not in model_settings.config")
        body = m.group(1)
        for k, v in values.items():
            tag = f"<metadata key={quoteattr(str(k))} value={quoteattr(str(v))}/>"
            # only the object-level region (before the first <part>) holds this object's own settings
            i = body.find("<part")
            head, tail = (body[:i], body[i:]) if i >= 0 else (body, "")
            pat = rf"""<metadata\s+key=["']{re.escape(str(k))}["']\s+value=(?:"[^"]*"|'[^']*')\s*/>"""
            if re.search(pat, head):
                head = re.sub(pat, lambda _m: tag, head, count=1)
            else:
                head = head.rstrip(" ") + f"{tag}\n    " if tail else head + f"  {tag}\n"
            body = head + tail
        self.entries[SETTINGS] = (xml[:m.start(1)] + body + xml[m.end(1):]).encode("utf-8")
