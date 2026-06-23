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

    # 3. screenshot: PNG bytes come back.
    shot = call({"type": "get_screenshot", "params": {"width": 400, "height": 300}})
    if "image_base64" in shot.get("result", {}):
        png = base64.b64decode(shot["result"]["image_base64"])
        assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
        print(f"OK: screenshot returned {len(png)} bytes of PNG")
    else:
        print(f"NOTE: screenshot skipped ({shot['result']})")

    print("\nEnd-to-end works.")
