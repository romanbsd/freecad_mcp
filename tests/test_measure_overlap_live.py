"""Live check: measure(a, b) reports overlap_volume / clash.

Prereq: FreeCAD open, "FreeCAD MCP" workbench active, server Started (port 9876),
running the CURRENT freecad_mcp.py (restart FreeCAD / reload the workbench after
editing the server, or this asserts against the old handler and fails).
Run: python3 tests/test_measure_overlap_live.py

Builds two overlapping boxes and two separated boxes, asserts overlap_volume>0 /
clash for the first and ==0 / no-clash with a positive distance for the second.
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
doc = App.newDocument("OverlapTest")
a = doc.addObject("Part::Feature", "A"); a.Shape = Part.makeBox(10,10,10)
b = doc.addObject("Part::Feature", "B"); b.Shape = Part.makeBox(10,10,10, App.Vector(5,0,0))
c = doc.addObject("Part::Feature", "C"); c.Shape = Part.makeBox(10,10,10, App.Vector(20,0,0))
doc.recompute()
""")
    clash = call("measure", a="A", b="B")
    assert clash.get("clash") is True, clash
    assert clash["overlap_volume"] > 100, clash          # 5x10x10 overlap = 500
    assert abs(clash["distance"]) < 1e-6, clash

    apart = call("measure", a="A", b="C")
    assert apart.get("clash") is False, apart
    assert apart["overlap_volume"] == 0, apart
    assert apart["distance"] > 9.0, apart                 # 10mm gap

    call("execute", code="import FreeCAD as App; App.closeDocument('OverlapTest')")
    print("PASS: measure overlap_volume/clash")


if __name__ == "__main__":
    main()
