"""End-to-end check against a REAL running FreeCAD server.

Prereq: FreeCAD open, "FreeCAD MCP" workbench active, server Started (port 9876).
Run: python3 test_e2e.py

Talks the framed protocol directly (no MCP client needed): exercises execute
(stdout + result capture, undo transaction, context) and the screenshot tool.
"""
import base64
import json
import socket

HOST, PORT = "localhost", 9876


def call(command: dict) -> dict:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(30)
        s.connect((HOST, PORT))
        payload = json.dumps(command).encode()
        s.sendall(len(payload).to_bytes(4, "big") + payload)
        n = int.from_bytes(_recv(s, 4), "big")
        return json.loads(_recv(s, n).decode())


def _recv(s, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = s.recv(min(65536, n - len(buf)))
        if not chunk:
            raise ConnectionError("closed early")
        buf += chunk
    return bytes(buf)


if __name__ == "__main__":
    # 1. execute: create a box, print a value, and return one via `result`.
    code = (
        "doc = App.ActiveDocument or App.newDocument('mcp_e2e')\n"
        "b = doc.addObject('Part::Box', 'MCPBox')\n"
        "b.Length, b.Width, b.Height = 20, 20, 20\n"
        "doc.recompute()\n"
        "print('volume is', b.Shape.Volume)\n"
        "result = b.Shape.Volume\n"
    )
    resp = call({"type": "execute", "params": {"code": code, "return_context": True}})
    print(json.dumps(resp, indent=2))
    assert resp.get("status") == "success", resp
    r = resp["result"]
    assert r["command_result"] == "success", r
    assert "volume is" in r["stdout"], f"stdout not captured: {r['stdout']!r}"
    assert abs(float(r["result"]) - 8000) < 1e-6, f"result not captured: {r['result']!r}"
    names = [o["name"] for o in r["context"]["objects"]]
    assert "MCPBox" in names, f"box not in context: {names}"
    print("OK: execute captured stdout + result, context has MCPBox")

    # 2. error path: aborted transaction, traceback returned, no crash.
    bad = call({"type": "execute", "params": {"code": "raise ValueError('boom')"}})
    assert bad["result"]["command_result"] == "error", bad
    assert "boom" in bad["result"]["error"], bad
    print("OK: error path returns traceback without dropping the connection")

    # 3. screenshot: front view, fit, PNG bytes come back.
    shot = call({"type": "get_screenshot",
                 "params": {"width": 400, "height": 300, "view": "front", "fit": True}})
    if "image_base64" in shot.get("result", {}):
        png = base64.b64decode(shot["result"]["image_base64"])
        assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
        assert shot["result"]["view"] == "front", shot["result"]
        print(f"OK: front-view screenshot returned {len(png)} bytes of PNG")
    else:
        print(f"NOTE: screenshot skipped ({shot['result']})")

    # bad view name is rejected cleanly, not crashed.
    bad_view = call({"type": "get_screenshot", "params": {"view": "sideways"}})
    assert "unknown view" in bad_view["result"].get("error", ""), bad_view
    print("OK: unknown view name rejected with a helpful error")

    # 4. list_objects / get_object.
    lst = call({"type": "list_objects"})["result"]
    assert any(o["name"] == "MCPBox" for o in lst["objects"]), lst
    obj = call({"type": "get_object", "params": {"name": "MCPBox"}})["result"]
    assert obj["valid"] is True, obj
    assert obj["properties"]["Length"], obj
    assert obj["shape"]["faces"] == 6, obj["shape"]
    assert abs(obj["shape"]["volume"] - 8000) < 1e-6, obj["shape"]
    print("OK: get_object returns properties, validity, bbox, topology")

    miss = call({"type": "get_object", "params": {"name": "NoSuchObj"}})["result"]
    assert "unknown object" in miss.get("error", ""), miss

    # 5. export STEP + STL.
    import os as _os, tempfile as _tmp
    for ext in ("step", "stl"):
        p = _os.path.join(_tmp.gettempdir(), f"mcp_e2e.{ext}")
        if _os.path.exists(p):
            _os.remove(p)
        exp = call({"type": "export", "params": {"names": ["MCPBox"], "path": p}})["result"]
        assert exp.get("bytes", 0) > 0 and _os.path.exists(p), exp
        print(f"OK: exported {ext.upper()} ({exp['bytes']} bytes)")

    bad_exp = call({"type": "export", "params": {"names": ["MCPBox"], "path": "/tmp/x.foo"}})["result"]
    assert "unsupported extension" in bad_exp.get("error", ""), bad_exp
    print("OK: unsupported export extension rejected")

    # 6. introspection: list_types / describe_type.
    types = call({"type": "list_types", "params": {"filter": "Part::"}})["result"]
    assert types["count"] > 0 and any(t == "Part::Box" for t in types["types"]), types
    print(f"OK: list_types returned {types['count']} Part:: types")

    desc = call({"type": "describe_type", "params": {"type_id": "Part::Box"}})["result"]
    length = next((p for p in desc["properties"] if p["name"] == "Length"), None)
    assert length and length.get("type") == "App::PropertyLength", desc
    print("OK: describe_type returns property schema (Length -> App::PropertyLength)")

    bad_type = call({"type": "describe_type", "params": {"type_id": "Nope::Nope"}})["result"]
    assert "cannot create" in bad_type.get("error", ""), bad_type

    # 7. measure: single object and pair distance.
    one = call({"type": "measure", "params": {"a": "MCPBox"}})["result"]
    assert one["bbox_size"] == [20, 20, 20], one
    call({"type": "execute", "params": {"code":
        "b = doc.addObject('Part::Box','Far'); b.Placement.Base = App.Vector(100,0,0)"}})
    pair = call({"type": "measure", "params": {"a": "MCPBox", "b": "Far"}})["result"]
    assert abs(pair["distance"] - 80) < 1e-6, pair  # 100 - 20
    print(f"OK: measure single bbox + pair distance ({pair['distance']})")

    # 7b. sub-elements: a box has 12 edges / 6 faces, named for targeting.
    sub = call({"type": "get_subelements", "params": {"name": "MCPBox"}})["result"]
    assert len(sub["edges"]) == 12 and len(sub["faces"]) == 6, sub
    assert sub["edges"][0]["name"] == "Edge1" and sub["faces"][0]["name"] == "Face1", sub
    # straight box edges carry endpoints + unit direction for orientation filtering
    e0 = sub["edges"][0]
    assert "direction" in e0 and abs(sum(c * c for c in e0["direction"]) - 1.0) < 1e-6, e0
    vertical = [e["name"] for e in sub["edges"] if e.get("direction") == [0.0, 0.0, 1.0]
                or e.get("direction") == [0.0, 0.0, -1.0]]
    assert len(vertical) == 4, f"expected 4 vertical edges, got {vertical}"
    print(f"OK: get_subelements lists {len(sub['edges'])} edges / {len(sub['faces'])} "
          f"faces; {len(vertical)} vertical by direction")

    # 8. selection (GUI only — skip gracefully headless / no GUI view).
    sel = call({"type": "set_selection", "params": {"names": ["MCPBox"]}})["result"]
    if "error" in sel:
        print(f"NOTE: selection skipped ({sel['error']})")
    else:
        objs = [s["object"] for s in sel["selection"]]
        assert "MCPBox" in objs, sel
        print(f"OK: set/get_selection round-trip ({objs})")

    print("\nEnd-to-end works.")
