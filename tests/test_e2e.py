"""End-to-end check against a REAL running FreeCAD server.

Prereq: FreeCAD open, "FreeCAD MCP" workbench active, server Started (port 9876).
Run: python3 test_e2e.py

Talks the framed protocol directly (no MCP client needed): creates a box via
send_command and prints the document context that comes back.
"""
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
    script = (
        "doc = App.ActiveDocument or App.newDocument('mcp_e2e')\n"
        "b = doc.addObject('Part::Box', 'MCPBox')\n"
        "b.Length, b.Width, b.Height = 20, 20, 20\n"
        "doc.recompute()\n"
    )
    resp = call({"type": "send_command", "params": {"command": script, "get_context": True}})
    print(json.dumps(resp, indent=2))
    assert resp.get("status") == "success", resp
    assert resp["result"]["command_result"] == "success", resp
    names = [o["name"] for o in resp["result"]["context"]["objects"]]
    assert "MCPBox" in names, f"box not found in context: {names}"
    print("\nOK: created MCPBox, context returned it. End-to-end works.")
