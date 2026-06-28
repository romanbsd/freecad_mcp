"""Live checks for the transform / fillet / screenshot tools.

Prereq: FreeCAD open, "FreeCAD MCP" workbench active, server Started (port 9876),
running the CURRENT freecad_mcp.py (restart FreeCAD / reload the workbench after
editing the server, or this asserts against the old handlers and fails).
Run: python3 tests/test_tools_live.py
"""
import json
import socket

HOST, PORT = "localhost", 9876


def _recv(s, n):
    b = bytearray()
    while len(b) < n:
        c = s.recv(min(65536, n - len(b)))
        if not c:
            raise ConnectionError("closed")
        b += c
    return bytes(b)


def call(cmd, **params):
    with socket.socket() as s:
        s.settimeout(60)
        s.connect((HOST, PORT))
        m = json.dumps({"type": cmd, "params": params}).encode()
        s.sendall(len(m).to_bytes(4, "big") + m)
        n = int.from_bytes(_recv(s, 4), "big")
        r = json.loads(_recv(s, n).decode())
    return r.get("result", r)


def main():
    call("execute", code="""
import Part, FreeCAD as App
doc = App.newDocument("ToolsTest")
b = doc.addObject("Part::Feature", "Blk"); b.Shape = Part.makeBox(10, 10, 10)
b2 = doc.addObject("Part::Feature", "Blk2"); b2.Shape = Part.makeBox(10, 10, 10)
doc.recompute()
""")

    # transform: move by [10,0,0]
    t = call("transform", object="Blk", translate=[10, 0, 0])
    assert t["position"] == [10.0, 0.0, 0.0], t
    # rotate 90deg about Z, relative
    t = call("transform", object="Blk", rotate={"axis": [0, 0, 1], "angle": 90})
    assert abs(t["angle_deg"] - 90) < 1e-6, t

    # fillet edge 1 of the box: a box has 6 faces -> 7 after rounding one edge
    f = call("fillet", object="Blk", edges=[1], radius=1.0)
    assert f["operation"] == "fillet" and f["faces"] == 7, f

    # chamfer a fresh box onto a copy, original Blk2 untouched
    f2 = call("fillet", object="Blk2", edges=[1], radius=0.5, chamfer=True,
              copy_to="Blk2_ch")
    assert f2["object"] == "Blk2_ch" and f2["operation"] == "chamfer", f2
    assert f2["faces"] == 7, f2

    # screenshot: max_dim caps the longest side
    shot = call("get_screenshot", width=1024, height=768, max_dim=200, view="iso")
    assert "image_base64" in shot and shot["image_base64"], "no image"
    assert max(shot["width"], shot["height"]) <= 200, shot

    # wireframe render still returns an image
    wf = call("get_screenshot", width=400, height=300, wireframe=True, view="iso")
    assert wf["image_base64"], wf

    # mcp helper prelude (persists in the execute namespace)
    p = call("execute", code="result = [mcp.add('P', mcp.box(5,5,5)).Name, len(mcp.grid(2,2,8))]")
    assert p.get("result") == ["P", 4], p

    # boolean fuse of two overlapping boxes -> one solid
    call("execute", code="""
import Part, FreeCAD as App
doc = App.ActiveDocument
u = doc.addObject('Part::Feature','U'); u.Shape = Part.makeBox(10,10,10)
v = doc.addObject('Part::Feature','V'); v.Shape = Part.makeBox(10,10,10, App.Vector(5,0,0))
doc.recompute()
""")
    bo = call("boolean", op="fuse", objects=["U", "V"], name="UV")
    assert bo["object"] == "UV" and bo["solids"] == 1, bo
    assert abs(bo["volume"] - 1500.0) < 1e-3, bo          # 10x10x10 + 5x10x10 overlap

    # duplicate as a linear array -> 3 separate objects
    call("execute", code="""
import Part, FreeCAD as App
doc = App.ActiveDocument
a = doc.addObject('Part::Feature','Arr'); a.Shape = Part.makeBox(4,4,4)
doc.recompute()
""")
    du = call("duplicate", object="Arr", count=3, translate=[8, 0, 0])
    assert du["count"] == 3 and len(du["created"]) == 3, du

    # set_property on a parametric Part::Box, verified via measure bbox
    call("execute", code="import FreeCAD as App; App.ActiveDocument.addObject('Part::Box','PB'); App.ActiveDocument.recompute()")
    call("set_property", object="PB", properties={"Length": 30})
    m = call("measure", a="PB")
    assert abs(m["bbox_size"][0] - 30.0) < 1e-6, m

    # compact get_object omits the property dump
    go = call("get_object", name="PB", compact=True)
    assert "properties" not in go and go["name"] == "PB", go

    call("execute", code="import FreeCAD as App; App.closeDocument('ToolsTest')")
    print("PASS: transform / fillet / screenshot / boolean / duplicate / "
          "set_property / get_object compact / mcp prelude")


if __name__ == "__main__":
    main()
