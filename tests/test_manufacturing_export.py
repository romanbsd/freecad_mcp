import xml.etree.ElementTree as ET
import zipfile

import pytest
import manufacturing_export as me


def tetra(name="Body", color="#AA0000", offset=0):
    return dict(name=name, color=color, vertices=[[offset,0,0],[offset+1,0,0],[offset,1,0],[offset,0,1]],
                triangles=[[0,2,1],[0,1,3],[0,3,2],[1,2,3]])


def test_named_material_assembly_independent_xml_roundtrip(tmp_path):
    path = tmp_path / "colored.3mf"
    parts = [tetra(), tetra("Bumper", "#C0C0C0", 10)]
    me.write_3mf(path, parts)
    with zipfile.ZipFile(path) as z:
        rel = ET.fromstring(z.read("_rels/.rels"))
        assert list(rel)[0].get("Target") == "/3D/3dmodel.model"
        model = ET.fromstring(z.read("3D/3dmodel.model"))
    ns = {"m": me.CORE}
    assert model.get("unit") == "millimeter"
    bases = model.findall("m:resources/m:basematerials/m:base", ns)
    assert [b.get("displaycolor") for b in bases] == ["#AA0000FF", "#C0C0C0FF"]
    meshes = model.findall("m:resources/m:object[m:mesh]", ns)
    assert [o.get("name") for o in meshes] == ["Body", "Bumper"]
    for obj, x, color_index in zip(meshes, [0,10], [0,1]):
        vs = obj.findall("m:mesh/m:vertices/m:vertex", ns)
        assert [float(v.get("x")) for v in vs] == [x,x+1,x,x]
        ts = obj.findall("m:mesh/m:triangles/m:triangle", ns)
        assert [[int(t.get(k)) for k in ("v1","v2","v3")] for t in ts] == [[0,2,1],[0,1,3],[0,3,2],[1,2,3]]
        assert all(t.get("pid") == "1" and int(t.get("p1")) == color_index for t in ts)
    assembly = model.find("m:resources/m:object[m:components]", ns)
    assert [c.get("objectid") for c in assembly.find("m:components", ns)] == [o.get("id") for o in meshes]
    assert model.find("m:build/m:item", ns).get("objectid") == assembly.get("id")
    read = me.read_3mf(path)
    assert read[1]["vertices"][1] == [11,0,0]
    assert read[1]["color"] == "#C0C0C0FF"


@pytest.mark.parametrize("mutation,message", [
    (lambda p: p["triangles"].pop(), "closed"),
    (lambda p: p["triangles"][0].reverse(), "oriented"),
    (lambda p: p["triangles"][0].__setitem__(0, 99), "index"),
    (lambda p: p["vertices"][0].__setitem__(0, float('nan')), "finite"),
    (lambda p: p["vertices"][3].__setitem__(2, 0), "degenerate"),
    (lambda p: p.__setitem__("color", "red"), "color"),
    (lambda p: p["vertices"].__setitem__(3, [1,1,0]), "positive volume"),
    (lambda p: [t.reverse() for t in p["triangles"]], "outward"),
])
def test_rejects_invalid_mesh_without_overwriting(tmp_path, mutation, message):
    target = tmp_path / "existing.3mf"
    target.write_bytes(b"keep me")
    part = tetra()
    mutation(part)
    with pytest.raises(ValueError, match=message):
        me.write_3mf(target, [part])
    assert target.read_bytes() == b"keep me"
    assert list(tmp_path.iterdir()) == [target]


def test_duplicate_names_and_failed_roundtrip_are_atomic(tmp_path, monkeypatch):
    target = tmp_path / "existing.3mf"
    target.write_bytes(b"old")
    with pytest.raises(ValueError, match="unique"):
        me.write_3mf(target, [tetra(), tetra()])
    monkeypatch.setattr(me, "read_3mf", lambda p: [])
    with pytest.raises(ValueError, match="roundtrip"):
        me.write_3mf(target, [tetra()])
    assert target.read_bytes() == b"old"
    assert list(tmp_path.iterdir()) == [target]


def test_vertex_pinched_mesh_is_rejected_before_overwrite(tmp_path):
    # Two outward closed tetrahedra share only vertex 0: edge incidence is
    # valid, but the vertex neighborhood is two disconnected fans.
    part = dict(name="pinched", vertices=[[0,0,0],[1,0,0],[0,1,0],[0,0,1],
                                         [-1,0,0],[0,-1,0],[0,0,-1]],
                triangles=[[0,2,1],[0,1,3],[0,3,2],[1,2,3],
                           [0,4,5],[0,6,4],[0,5,6],[4,6,5]])
    target = tmp_path / "pinched.3mf"
    target.write_bytes(b"old export")
    with pytest.raises(ValueError, match="vertex.*manifold"):
        me.write_3mf(target, [part])
    assert target.read_bytes() == b"old export"


@pytest.mark.parametrize("mutation,message", [
    (lambda m,n: m.find('m:build/m:item',n).set('transform','1 0 0 0 1 0 0 0 1 20 0 0'), "transform"),
    (lambda m,n: ET.SubElement(m.find('m:build',n), '{%s}item' % me.CORE, {'objectid':'3'}), "one build item"),
    (lambda m,n: m.find('m:resources/m:object/m:mesh/m:triangles/m:triangle',n).set('p1','-1'), "material index"),
    (lambda m,n: m.find('m:resources/m:object/m:mesh/m:triangles/m:triangle',n).set('p2','1'), "materials"),
    (lambda m,n: m.remove(m.find('m:resources',n)), "structure"),
    (lambda m,n: m.find('m:resources/m:object',n).remove(m.find('m:resources/m:object/m:mesh',n)), "structure"),
])
def test_roundtrip_rejects_geometry_and_material_changes_atomically(tmp_path, monkeypatch, mutation, message):
    target = tmp_path / "verified.3mf"
    target.write_bytes(b"old export")
    original = zipfile.ZipFile.writestr
    def corrupt(archive, name, data, *args, **kwargs):
        if name == '3D/3dmodel.model':
            model = ET.fromstring(data)
            mutation(model, {'m':me.CORE})
            data = ET.tostring(model)
        return original(archive, name, data, *args, **kwargs)
    monkeypatch.setattr(zipfile.ZipFile, 'writestr', corrupt)
    with pytest.raises(ValueError, match=message):
        me.write_3mf(target, [tetra()])
    assert target.read_bytes() == b"old export"
    assert list(tmp_path.iterdir()) == [target]
