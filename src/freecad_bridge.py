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
# execute runs on FreeCAD's single GUI thread, so a heavy script blocks the
# whole UI. The default 30s covers typical ops; raise FREECAD_MCP_TIMEOUT for
# long recomputes (the server keeps running the script even past a client timeout).
FREECAD_TIMEOUT = float(os.environ.get('FREECAD_MCP_TIMEOUT', '30'))


class FreeCADToolError(RuntimeError):
    def __init__(self, code: str, message: str, recoverable: bool = False):
        super().__init__(message)
        self.code = code
        self.recoverable = bool(recoverable)


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
        sock.settimeout(FREECAD_TIMEOUT)
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
        return {"status": "error", "error": {
            "code": "TRANSPORT_ERROR",
            "message": str(e),
            "recoverable": True,
        }}


def _unwrap_response(response: Any) -> Any:
    """Normalize the wire envelope and surface failures as tool errors."""
    if not isinstance(response, dict):
        raise RuntimeError("invalid response from FreeCAD")
    if response.get("status") == "error":
        error = response.get("error") or {}
        raise FreeCADToolError(
            error.get("code", "UNKNOWN_ERROR"),
            error.get("message", "FreeCAD command failed"),
            error.get("recoverable", False),
        )
    result = response.get("result", response)
    if isinstance(result, dict) and result.get("error"):
        raise RuntimeError(result["error"])
    return result


async def _request(command: Dict[str, Any]) -> Any:
    return _unwrap_response(await send_to_freecad(command))


@mcp.tool()
async def execute(code: str, return_context: bool = False, reset: bool = False) -> Any:
    """Execute Python inside the running FreeCAD instance.

    Namespace already bound: `App` (FreeCAD), `Gui` (FreeCADGui), `doc` (the
    active document, may be None — `doc = App.ActiveDocument or App.newDocument()`).
    Import `Part` / `Sketcher` / `Draft` yourself as needed.

    Variables persist across calls — define parameters/helpers once in an early
    call and reference them later instead of re-declaring them (saves tokens and
    avoids copy-paste drift). `doc` is re-bound each call; `result` is per-call.
    Pass reset=True to clear all persisted state.

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
    return await _request({
        "type": "execute",
        "params": {"code": code, "return_context": return_context, "reset": reset},
    })


@mcp.tool()
async def list_objects() -> Any:
    """List every object in the active document (name, label, type). Cheap
    overview — use get_object for the full properties of one object."""
    return await _request({"type": "list_objects"})


@mcp.tool()
async def get_object(name: str) -> Any:
    """Full detail for one object: all properties, validity/state, and shape
    bounding box + topology counts (verts/edges/faces/solids) when present.

    Args:
        name: the object's Name (unique id), as shown by list_objects.
    """
    return await _request({"type": "get_object", "params": {"name": name}})


@mcp.tool()
async def export(names: list[str], path: str) -> Any:
    """Export objects to a CAD file. The format is chosen by the file
    extension: .step/.stp, .iges/.igs, .brep/.brp, or .stl.

    Args:
        names: object Names to export (from list_objects).
        path: absolute output path; its extension picks the format.
    """
    return await _request({"type": "export", "params": {"names": names, "path": path}})


@mcp.tool()
async def list_types(filter: str = "") -> Any:
    """List creatable FreeCAD object TypeIds (what you can pass to addObject).

    Args:
        filter: case-insensitive substring, e.g. "Part::" or "Sketch".
    """
    return await _request({"type": "list_types", "params": {"filter": filter}})


@mcp.tool()
async def describe_type(type_id: str) -> Any:
    """Property schema for a TypeId: each property's type, group, doc string and
    enum options. Use this to learn what you can set before writing execute code.

    Args:
        type_id: e.g. "Part::Box", "PartDesign::Pad", "Sketcher::SketchObject".
    """
    return await _request({"type": "describe_type", "params": {"type_id": type_id}})


@mcp.tool()
async def measure(a: str, b: str = "") -> Any:
    """Measure geometry. With one object: volume, area, center of mass, bbox.
    With two: the minimum distance between them, the closest points, and the
    overlap volume (overlap_volume>0 / clash=True means they interfere — use
    this to verify a fit instead of building a probe solid in execute).

    Args:
        a: object Name.
        b: optional second object Name; omit to measure `a` alone.
    """
    params = {"a": a, "b": b or None}
    return await _request({"type": "measure", "params": params})


@mcp.tool()
async def transform(
    object: str, translate: list[float] = None, rotate: dict = None,
    center: list[float] = None, relative: bool = True, copy_to: str = "",
) -> Any:
    """Move/rotate a named object via its Placement — no execute script needed.

    Args:
        object: object Name to move.
        translate: [dx, dy, dz] in mm.
        rotate: {"axis": [x,y,z], "angle": degrees}.
        center: rotation pivot [x,y,z] (default origin).
        relative: True composes a delta onto the current placement (a "move
            by"); False sets the placement absolutely.
        copy_to: if set, leave the original and apply to a named copy.
    """
    return await _request({"type": "transform", "params": {
        "object": object, "translate": translate, "rotate": rotate,
        "center": center, "relative": relative, "copy_to": copy_to or None}})


@mcp.tool()
async def fillet(
    object: str, edges: list[int], radius: float,
    chamfer: bool = False, copy_to: str = "",
) -> Any:
    """Fillet (round) or chamfer edges of a named object by edge index.

    Pairs with get_subelements: pass the 1-based Edge indices it reports.

    Args:
        object: object Name.
        edges: 1-based edge indices (or "EdgeN" strings).
        radius: fillet radius / chamfer size in mm.
        chamfer: True for a chamfer instead of a round.
        copy_to: write the result to a new object, keeping the original;
            otherwise the object is modified in place (Part::Feature solids; a
            parametric primitive would revert on recompute).
    """
    return await _request({"type": "fillet", "params": {
        "object": object, "edges": edges, "radius": radius,
        "chamfer": chamfer, "copy_to": copy_to or None}})


@mcp.tool()
async def get_selection() -> Any:
    """What the user has selected in FreeCAD: objects and sub-elements
    (e.g. Edge1, Face2) — use these to target fillets/chamfers. GUI only."""
    return await _request({"type": "get_selection"})


@mcp.tool()
async def set_selection(names: list[str]) -> Any:
    """Replace the current selection with the given object Names. GUI only.

    Args:
        names: object Names to select.
    """
    return await _request({"type": "set_selection", "params": {"names": names}})


@mcp.tool()
async def get_subelements(name: str, limit: int = 200) -> Any:
    """Sub-geometry of an object, so you can target it by index.

    For any shape: its edges (Edge1..N with curve type, length, center point)
    and faces (Face1..N with area, center) — pass those sub-element names to a
    fillet/chamfer or to set_selection. For a Sketcher sketch: its Geometry
    (lines/arcs/circles with coordinates) and Constraints.

    Args:
        name: object Name.
        limit: max edges/faces to return (default 200).
    """
    return await _request({"type": "get_subelements",
                           "params": {"name": name, "limit": limit}})


@mcp.tool()
async def get_screenshot(
    width: int = 1024, height: int = 768, view: str = "iso", fit: bool = True,
    targets: list[str] = None, transparent: list[str] = None,
    temporary: bool = True, camera: dict = None, views: list[str] = None,
    max_dim: int = None, wireframe: bool = False,
) -> Image:
    """Capture the active FreeCAD 3D view as a PNG so you can see the model.

    Args:
        width, height: image size in pixels.
        view: camera orientation before capture — one of iso, front, rear,
            top, bottom, left, right, or "current" to leave the camera as-is.
        fit: zoom to fit all visible geometry before capturing (default True).
        max_dim: cap the longest image side (keeps aspect) for cheaper images
            on quick shape checks.
        wireframe: render edges only — a much smaller PNG; restored afterwards.
    """
    inner = await _request({
        "type": "get_screenshot",
        "params": {"width": width, "height": height, "view": view, "fit": fit,
                   "targets": targets or [], "transparent": transparent or [],
                   "temporary": temporary, "camera": camera,
                   "views": views or [], "max_dim": max_dim,
                   "wireframe": wireframe},
    })
    if not isinstance(inner, dict) or "image_base64" not in inner:
        raise RuntimeError("screenshot failed")
    return Image(data=base64.b64decode(inner["image_base64"]), format="png")


@mcp.tool()
async def create_component(document: str, name: str, label: str = "",
                          parameters: list = None) -> Any:
    """Create a parametric component: an App::Part container with a typed
    parameter registry. Each parameter declares kind "input" (concrete, with
    `default` and optional min/max/enum/unit) or "derived" (read-only, with an
    `expression` over other params using $name). Returns the component_id used
    by the other component tools.
    """
    return await _request({"type": "create_component", "params": {
        "document": document, "name": name, "label": label or None,
        "parameters": parameters or []}})


@mcp.tool()
async def define_component(component_id: str, features: list,
                          rules: list = None, profiles: list = None) -> Any:
    """Define (or replace) the component's build graph — a list of features.
    Feature types: box, cylinder, cone, prism, transform, cut, union,
    intersection, array, group. Sizes/positions are expressions over $params
    (with units), e.g. "$width / 2", "$wall_thickness + 2 mm". Booleans/arrays
    reference other features by id. Each feature may set `role`
    (output/construction/tool/inspection) and `tags`. Read
    freecad://guide/cookbook for shapes.

    Optional `rules` (declarative design-rule definitions, each with id/type/
    severity/targets/message) and default `profiles` are stored with the
    component for validate_component. A build that yields invalid geometry is
    rolled back, keeping the prior model.
    """
    return await _request({"type": "define_component", "params": {
        "component_id": component_id, "features": features,
        "rules": rules, "profiles": profiles}})


@mcp.tool()
async def set_component_parameters(component_id: str, values: dict,
                                   rebuild: bool = True, validate: bool = False,
                                   enforce_bounds: bool = False) -> Any:
    """Update input parameters and rebuild only affected features (native
    incremental recompute). Returns changed params, regenerated feature ids,
    optional validation, and the component bounding box. Derived params are
    read-only and rejected.

    Declared min/max bounds are advisory by default (a value outside them is
    accepted and only flagged when you validate). Set enforce_bounds=True to
    reject out-of-range values up front instead."""
    return await _request({"type": "set_component_parameters", "params": {
        "component_id": component_id, "values": values,
        "rebuild": rebuild, "validate": validate,
        "enforce_bounds": enforce_bounds}})


@mcp.tool()
async def get_component(component_id: str) -> Any:
    """Return the component's parameter registry (with current values),
    dependency graph summary, variants, and latest validation status."""
    return await _request({"type": "get_component", "params": {
        "component_id": component_id}})


@mcp.tool()
async def get_component_graph(component_id: str, detail: str = "summary") -> Any:
    """Return the revisioned feature graph. Use detail='full' for editable
    feature definitions and 'summary' for ids, types, roles, and inputs."""
    return await _request({"type": "get_component_graph", "params": {
        "component_id": component_id, "detail": detail}})


@mcp.tool()
async def patch_component(component_id: str, operations: list,
                          expected_revision: int = None,
                          validate: bool = False,
                          dry_run: bool = False) -> Any:
    """Apply revision-checked structural edits to a component graph."""
    return await _request({"type": "patch_component", "params": {
        "component_id": component_id, "operations": operations,
        "expected_revision": expected_revision, "validate": validate,
        "dry_run": dry_run}})


@mcp.tool()
async def list_feature_types() -> Any:
    """List declarative component feature types supported by this server."""
    return await _request({"type": "list_feature_types"})


@mcp.tool()
async def describe_feature_type(feature_type: str) -> Any:
    """Return the machine-readable schema for one declarative feature type."""
    return await _request({"type": "describe_feature_type", "params": {
        "feature_type": feature_type}})


@mcp.tool()
async def capabilities() -> Any:
    """Return API, FreeCAD, feature, pattern, and validation capabilities."""
    return await _request({"type": "capabilities"})


@mcp.tool()
async def list_patterns() -> Any:
    """List versioned server-owned CAD patterns."""
    return await _request({"type": "list_patterns"})


@mcp.tool()
async def expand_pattern(feature: dict) -> Any:
    """Expand one pattern to its ordinary, editable component features."""
    return await _request({"type": "expand_pattern", "params": {
        "feature": feature}})


@mcp.tool()
async def check_fit(component_id: str, container: str, insert: str,
                    retainers: list[str] = None, probe_steps: int = 16,
                    tolerance: float = 0.01) -> Any:
    """Check containment, clearance, interference, and blocked translations."""
    return await _request({"type": "check_fit", "params": {
        "component_id": component_id, "container": container, "insert": insert,
        "retainers": retainers or [], "probe_steps": probe_steps,
        "tolerance": tolerance}})


@mcp.tool()
async def export_bundle(component_id: str, directory: str,
                        formats: list[str] = None, per_output: bool = True,
                        assembly: bool = True, overwrite: bool = True,
                        basename: str = "") -> Any:
    """Export several formats and component outputs in one request."""
    return await _request({"type": "export_bundle", "params": {
        "component_id": component_id, "directory": directory,
        "formats": formats, "per_output": per_output, "assembly": assembly,
        "overwrite": overwrite, "basename": basename or None}})


@mcp.tool()
async def build_component(document: dict, component: dict,
                          validate: dict = None, exports: dict = None) -> Any:
    """Create, define, validate, and optionally export a component in one call."""
    return await _request({"type": "build_component", "params": {
        "document": document, "component": component,
        "validate": validate, "exports": exports}})


@mcp.tool()
async def create_component_variant(component_id: str, name: str, values: dict) -> Any:
    """Store a named set of parameter overrides that export_component can apply."""
    return await _request({"type": "create_component_variant", "params": {
        "component_id": component_id, "name": name, "values": values}})


@mcp.tool()
async def validate_component(component_id: str, profiles: list = None,
                            rule_ids: list = None, include_measurements: bool = False,
                            tolerance: str = "") -> Any:
    """Run design-rule checks over the recomputed component (read-only). Combines
    built-in profiles (geometry_baseline, cnc_plywood; defaults
    to the component's stored profiles or geometry_baseline) with its custom rules
    (filter via rule_ids). `tolerance` is a length in document units (default
    0.01 mm) and is echoed in the result. Returns build_status, validation_status,
    a summary count, and findings carrying severity, rule, target, actual/required,
    message, and suggested_parameter_change. Set include_measurements for per-feature
    volume/area/bbox."""
    return await _request({"type": "validate_component", "params": {
        "component_id": component_id, "profiles": profiles, "rule_ids": rule_ids,
        "include_measurements": include_measurements,
        "tolerance": tolerance or None}})


@mcp.tool()
async def render_component(component_id: str, view: str = "iso", section: dict = None,
                          hide_features: list = None,
                          width: int = 900, height: int = 700) -> Image:
    """Render the component to a PNG. `view` is iso/front/top/etc. Optional
    `section` = {"plane":"XZ","offset":"90 mm"} produces a cross-section.
    `hide_features` is a list of feature ids to hide for this render."""
    inner = await _request({"type": "render_component", "params": {
        "component_id": component_id, "view": view, "section": section,
        "hide_features": hide_features, "width": width, "height": height}})
    if not isinstance(inner, dict) or "image_base64" not in inner:
        raise RuntimeError("render failed")
    return Image(data=base64.b64decode(inner["image_base64"]), format="png")


@mcp.tool()
async def export_component(component_id: str, path: str, format: str = "FCStd",
                          variant: str = "") -> Any:
    """Export the component (or a named variant) to a file. Formats: FCStd,
    STEP, STL, IGES, BREP. FCStd saves the whole document; the others export
    the generated solids."""
    return await _request({"type": "export_component", "params": {
        "component_id": component_id, "path": path, "format": format,
        "variant": variant or None}})


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
