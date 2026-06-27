"""Length-prefixed bridge framing tests without a real network listener."""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import freecad_bridge as bridge


class FramedSocket:
    """Small socket double that fragments a large framed response."""

    def __init__(self, response):
        data = json.dumps(response).encode("utf-8")
        self.remaining = len(data).to_bytes(4, "big") + data
        self.sent = b""
        self.timeout = None
        self.address = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, address):
        self.address = address

    def sendall(self, data):
        self.sent += data

    def recv(self, size):
        chunk = self.remaining[:min(size, 1024)]
        self.remaining = self.remaining[len(chunk):]
        return chunk


def test_large_framed_response_round_trips():
    command = {"type": "send_command", "params": {"command": "noop"}}
    response = {"status": "success", "echo": command, "blob": "x" * 50000}
    fake_socket = FramedSocket(response)

    with patch.object(bridge.socket, "socket", return_value=fake_socket):
        result = json.loads(bridge._call_blocking(command).decode("utf-8"))

    request_length = int.from_bytes(fake_socket.sent[:4], "big")
    assert request_length == len(fake_socket.sent[4:])
    assert json.loads(fake_socket.sent[4:].decode("utf-8")) == command
    assert fake_socket.timeout == 30
    assert fake_socket.address == (bridge.FREECAD_HOST, bridge.FREECAD_PORT)
    assert result["status"] == "success"
    assert len(result["blob"]) == 50000
    assert result["echo"] == command


def main():
    test_large_framed_response_round_trips()
    print("OK: framed >4096B response round-trips intact")


if __name__ == "__main__":
    main()
