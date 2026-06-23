"""End-to-end check of the parametric component workflow against a live FreeCAD.

Prereq: FreeCAD open, "FreeCAD MCP" workbench active, server Started (port 9876),
running the current freecad_mcp.py + parametric.py (restart FreeCAD after edits).
Run: python3 tests/test_parametric.py

Builds the budgerigar house from parametric-spec.md and exercises every tool
except render_component (that needs a human-visible GUI view; covered by the
live bridge drive). Maps to the spec's acceptance criteria.
"""
import json
import os
import socket
import tempfile

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


DOC = "BudgieTest"
PARAMS = [
    {"name": "width", "type": "length", "default": "180 mm", "min": "120 mm"},
    {"name": "depth", "type": "length", "default": "180 mm"},
    {"name": "body_height", "type": "length", "default": "250 mm"},
    {"name": "wall_thickness", "type": "length", "default": "12 mm", "min": "6 mm"},
    {"name": "entrance_diameter", "type": "length", "default": "40 mm",
     "min": "35 mm", "max": "45 mm"},
    {"name": "entrance_height", "type": "length", "default": "195 mm"},
    {"name": "inner_width", "kind": "derived", "type": "length",
     "expression": "$width - 2 * $wall_thickness"},
]
FEATURES = [
    {"id": "base", "type": "box", "label": "Floor",
     "size": {"x": "$width", "y": "$depth", "z": "$wall_thickness"}},
    {"id": "front_wall", "type": "box",
     "size": {"x": "$inner_width", "y": "$wall_thickness", "z": "$body_height"},
     "position": {"x": "$wall_thickness", "y": "0 mm", "z": "$wall_thickness"}},
    {"id": "entrance_hole", "type": "cylinder",
     "radius": "$entrance_diameter / 2", "height": "$wall_thickness + 2 mm",
     "axis": "y", "position": {"x": "$width / 2", "y": "-1 mm", "z": "$entrance_height"}},
    {"id": "front_with_entrance", "type": "cut",
     "base": "front_wall", "tool": "entrance_hole"},
]


def main():
    # clean slate if a prior run left the doc open
    call("execute", code="d=App.listDocuments();\n"
         "App.closeDocument('%s') if '%s' in d else None" % (DOC, DOC))

    # 1. create + 2. define  (<= 2 calls to build: acceptance criterion)
    c = call("create_component", document=DOC, name="BudgerigarHouse",
             label="Parametric Budgerigar House", parameters=PARAMS)
    cid = c["component_id"]
    assert c["parameters_created"] == 7, c
    d = call("define_component", component_id=cid, features=FEATURES)
    assert set(d["features_created"]) == {"base", "front_wall", "entrance_hole",
                                          "front_with_entrance"}, d
    print("OK: created + defined budgerigar house in 2 calls")

    # hierarchy is a real App::Part tree
    g = call("get_component", component_id=cid)
    assert next(p["value"] for p in g["parameters"] if p["name"] == "inner_width") \
        .startswith("156"), g
    print("OK: derived inner_width = 156 mm (180 - 2*12)")

    # 3. entrance_diameter rebuilds only entrance-related front-wall geometry
    s = call("set_component_parameters", component_id=cid,
             values={"entrance_diameter": "42 mm"}, validate=True)
    assert set(s["regenerated"]) == {"entrance_hole", "front_with_entrance"}, s
    assert s["validation"]["status"] in ("ok", "warning"), s
    print("OK: entrance_diameter regenerated only %s" % s["regenerated"])

    # 4. width updates all dependent walls + derived dimensions
    s2 = call("set_component_parameters", component_id=cid, values={"width": "200 mm"})
    assert "base" in s2["regenerated"] and "front_wall" in s2["regenerated"], s2
    iw = next(p["value"] for p in call("get_component", component_id=cid)["parameters"]
              if p["name"] == "inner_width")
    assert iw.startswith("176"), iw
    print("OK: width=200 -> regenerated %s, inner_width=%s" % (s2["regenerated"], iw))

    # 5. validation: thin wall flagged, then restored
    call("set_component_parameters", component_id=cid, values={"wall_thickness": "5 mm"})
    v = call("validate_component", component_id=cid)
    assert v["status"] in ("warning", "error"), v
    assert any(f["rule"] == "minimum_wall_thickness" for f in v["findings"]), v
    call("set_component_parameters", component_id=cid, values={"wall_thickness": "12 mm"})
    print("OK: validation flagged thin wall (%s)" % v["status"])

    # 6. transactional: a bad build leaves the prior valid model untouched
    bad = call("define_component", component_id=cid,
               features=[{"id": "oops", "type": "box",
                          "size": {"x": "$nonexistent", "y": "1 mm", "z": "1 mm"}}])
    assert "error" in bad or "build failed" in json.dumps(bad), bad
    still = call("get_component", component_id=cid)["build_graph"]
    assert any(f["id"] == "front_with_entrance" for f in still), still
    print("OK: bad define rejected; prior model intact")

    # 7. variant + export FCStd + STEP
    call("create_component_variant", component_id=cid, name="Large",
         values={"width": "220 mm", "depth": "220 mm"})
    for fmt, ext in [("FCStd", "FCStd"), ("STEP", "step")]:
        p = os.path.join(tempfile.gettempdir(), "budgie_test." + ext)
        if os.path.exists(p):
            os.remove(p)
        e = call("export_component", component_id=cid, path=p, format=fmt, variant="Large")
        assert e.get("bytes", 0) > 0 and os.path.exists(p), e
        print("OK: exported %s (%d bytes)" % (fmt, e["bytes"]))

    print("\nParametric workflow end-to-end works.")


if __name__ == "__main__":
    main()
