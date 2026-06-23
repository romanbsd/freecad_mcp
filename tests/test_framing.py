"""Runnable check for the length-prefixed framing fix (C1/C2).

Drives the real bridge `send_to_freecad` against a loopback server that speaks
the same 4-byte-length protocol and replies with a payload far larger than the
old 4096-byte single-recv cap. Run: python3 test_framing.py
"""
import asyncio
import json
import socket
import sys
import threading

sys.path.insert(0, "src")
from freecad_bridge import send_to_freecad, _recv_exactly, FREECAD_PORT


def _serve_once(port, ready):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("localhost", port))
    srv.listen(1)
    ready.set()
    conn, _ = srv.accept()
    with conn:
        # Read the framed request (mirrors the FreeCAD-side parser).
        length = int.from_bytes(_recv_exactly(conn, 4), "big")
        req = json.loads(_recv_exactly(conn, length).decode())
        # Reply with a deliberately big payload (>> 4096 bytes).
        big = {"status": "success", "echo": req, "blob": "x" * 50000}
        data = json.dumps(big).encode()
        conn.sendall(len(data).to_bytes(4, "big") + data)
    srv.close()


def main():
    ready = threading.Event()
    t = threading.Thread(target=_serve_once, args=(FREECAD_PORT, ready), daemon=True)
    t.start()
    ready.wait(timeout=5)

    resp = asyncio.run(send_to_freecad({"type": "send_command", "params": {"command": "noop"}}))
    t.join(timeout=5)

    assert resp.get("status") == "success", resp
    assert len(resp["blob"]) == 50000, "large payload truncated -> C1 not fixed"
    assert resp["echo"]["params"]["command"] == "noop", "request not round-tripped"
    print("OK: framed >4096B response round-trips intact")


if __name__ == "__main__":
    main()
