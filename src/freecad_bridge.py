from typing import Any, Dict
import asyncio
import base64
import socket
import json
from mcp.server.fastmcp import FastMCP, Image

# Initialize FastMCP server
mcp = FastMCP("freecad-bridge")

# Constants
FREECAD_HOST = 'localhost'
FREECAD_PORT = 9876


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes or raise (the socket has a timeout set)."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            raise ConnectionError("connection closed before full message received")
        buf += chunk
    return bytes(buf)


def _call_blocking(command: Dict[str, Any]) -> bytes:
    """Synchronous framed request/response. Wire format (both directions):
    4-byte big-endian length prefix followed by that many bytes of UTF-8 JSON."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(30)
        sock.connect((FREECAD_HOST, FREECAD_PORT))
        payload = json.dumps(command).encode('utf-8')
        sock.sendall(len(payload).to_bytes(4, 'big') + payload)
        length = int.from_bytes(_recv_exactly(sock, 4), 'big')
        return _recv_exactly(sock, length)


async def send_to_freecad(command: Dict[str, Any]) -> Dict[str, Any]:
    """Send a command to FreeCAD and get the response. Runs the blocking
    socket I/O off the event loop so concurrent tool calls don't stall."""
    try:
        raw = await asyncio.to_thread(_call_blocking, command)
        return json.loads(raw.decode('utf-8'))
    except Exception as e:
        return {"status": "error", "message": str(e)}


@mcp.tool()
async def execute(code: str, return_context: bool = False) -> str:
    """Execute Python inside the running FreeCAD instance.

    The script namespace has `App` (FreeCAD), `Gui` (FreeCADGui) and `doc`
    (the active document). To return data, either assign to `result` or use
    print() — both are captured and sent back. The action runs inside one undo
    transaction and the document is recomputed afterwards.

    Args:
        code: Python source to execute.
        return_context: When True, also return a summary of the document
            (objects, placements, shapes, view state). Off by default because
            it can be large.

    Returns:
        JSON string with command_result, stdout, optional result, and (when
        requested) context.
    """
    result = await send_to_freecad({
        "type": "execute",
        "params": {"code": code, "return_context": return_context},
    })
    return json.dumps(result, indent=2)


@mcp.tool()
async def get_screenshot(width: int = 1024, height: int = 768) -> Image:
    """Capture the active FreeCAD 3D view as a PNG so you can see the model."""
    result = await send_to_freecad({
        "type": "get_screenshot",
        "params": {"width": width, "height": height},
    })
    if "image_base64" not in result:
        raise RuntimeError(result.get("message") or result.get("error") or "screenshot failed")
    return Image(data=base64.b64decode(result["image_base64"]), format="png")


if __name__ == "__main__":
    # Initialize and run the server
    mcp.run(transport='stdio')
