from typing import Any, Dict
import asyncio
import base64
import os
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

    Namespace already bound: `App` (FreeCAD), `Gui` (FreeCADGui), `doc` (the
    active document, may be None — `doc = App.ActiveDocument or App.newDocument()`).
    Import `Part` / `Sketcher` / `Draft` yourself as needed.

    Returning data: assign to `result` or `print()` — both are captured.
    The document is recomputed for you after the call, and the whole call is a
    single undo step (auto-aborted on error), so do NOT wrap your own
    transactions.

    Choosing an approach: prefer the `Part` workbench (Part::Box, Part::Cut,
    Part::Fillet, …) for straightforward solids; use `PartDesign` (Body +
    Sketch + Pad/Pocket) when you need an editable parametric feature tree.
    For full working idioms (booleans, fillets, sketch→pad, export, inspect,
    error-checking) read the `freecad://guide/cookbook` resource first.

    After building geometry, call the `get_screenshot` tool to see the result
    and verify it before reporting success.

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
async def list_objects() -> str:
    """List every object in the active document (name, label, type). Cheap
    overview — use get_object for the full properties of one object."""
    return json.dumps(await send_to_freecad({"type": "list_objects"}), indent=2)


@mcp.tool()
async def get_object(name: str) -> str:
    """Full detail for one object: all properties, validity/state, and shape
    bounding box + topology counts (verts/edges/faces/solids) when present.

    Args:
        name: the object's Name (unique id), as shown by list_objects.
    """
    return json.dumps(
        await send_to_freecad({"type": "get_object", "params": {"name": name}}),
        indent=2,
    )


@mcp.tool()
async def export(names: list[str], path: str) -> str:
    """Export objects to a CAD file. The format is chosen by the file
    extension: .step/.stp, .iges/.igs, .brep/.brp, or .stl.

    Args:
        names: object Names to export (from list_objects).
        path: absolute output path; its extension picks the format.
    """
    return json.dumps(
        await send_to_freecad({"type": "export", "params": {"names": names, "path": path}}),
        indent=2,
    )


@mcp.tool()
async def get_screenshot(
    width: int = 1024, height: int = 768, view: str = "iso", fit: bool = True
) -> Image:
    """Capture the active FreeCAD 3D view as a PNG so you can see the model.

    Args:
        width, height: image size in pixels.
        view: camera orientation before capture — one of iso, front, rear,
            top, bottom, left, right, or "current" to leave the camera as-is.
        fit: zoom to fit all visible geometry before capturing (default True).
    """
    result = await send_to_freecad({
        "type": "get_screenshot",
        "params": {"width": width, "height": height, "view": view, "fit": fit},
    })
    if "image_base64" not in result:
        raise RuntimeError(result.get("message") or result.get("error") or "screenshot failed")
    return Image(data=base64.b64decode(result["image_base64"]), format="png")


@mcp.resource("freecad://guide/cookbook", mime_type="text/markdown")
def cookbook() -> str:
    """Working FreeCAD scripting idioms for the `execute` tool. Read this before
    writing non-trivial geometry code."""
    path = os.path.join(os.path.dirname(__file__), "cookbook.md")
    with open(path, encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    # Initialize and run the server
    mcp.run(transport='stdio')
