from typing import Any, Dict
import asyncio
import base64
import os
import socket
import json
import sys
from pathlib import Path
from fastmcp import FastMCP
from fastmcp.utilities.types import Image
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from fastmcp.server.middleware import Middleware

# Initialize FastMCP server
mcp = FastMCP("freecad-bridge", instructions=(
    "Start with capabilities. Prefer editable declarative components and validation over execute. "
    "Use inspect_printability before measured print comparisons. Read freecad://guide/cookbook "
    "before scripting. Slicer estimates do not certify physical strength or fit."))

# The desktop addon and local print adapters live beside src/, rather than
# inside the bridge package. Resolve this installation, not the caller's cwd.
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

def _ann(*, read_only=False, destructive=False, idempotent=True, open_world=False):
    """Explicit tool behavior hints shared by bridge decorators."""
    return dict(readOnlyHint=read_only, destructiveHint=destructive,
                idempotentHint=idempotent, openWorldHint=open_world)


# Constants
FREECAD_HOST = 'localhost'
FREECAD_PORT = 9876
# execute runs on FreeCAD's single GUI thread, so a heavy script blocks the
# whole UI. The default 30s covers typical ops; raise FREECAD_MCP_TIMEOUT for
# long recomputes (the server keeps running the script even past a client timeout).
FREECAD_TIMEOUT = float(os.environ.get('FREECAD_MCP_TIMEOUT', '30'))


class FreeCADToolError(ToolError, RuntimeError):
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
        raise FreeCADToolError("INVALID_RESPONSE", "invalid response from FreeCAD")
    if response.get("status") == "error":
        error = response.get("error") or {}
        raise FreeCADToolError(
            error.get("code", "UNKNOWN_ERROR"),
            error.get("message", "FreeCAD command failed"),
            error.get("recoverable", False),
        )
    result = response.get("result", response)
    if isinstance(result, dict) and result.get("error"):
        raise FreeCADToolError("COMMAND_ERROR", str(result["error"]))
    return result


class BridgeMiddleware(Middleware):
    """Preserve actionable wire errors and report actual start/completion events."""
    async def on_call_tool(self, context, call_next):
        name = context.message.name
        progress = context.fastmcp_context
        long_call = name in {
            'execute', 'solve_parameter_target', 'build_component', 'patch_component',
            'export', 'export_bundle', 'export_component',
            'suggest_print_orientations', 'compare_print_candidates',
        }
        if long_call and progress:
            await progress.report_progress(0, 1, f'{name} started')
        try:
            result = await call_next(context)
        except FreeCADToolError as exc:
            return ToolResult(structured_content={'error': {
                'code': exc.code, 'message': str(exc), 'recoverable': exc.recoverable,
            }}, is_error=True)
        if long_call and progress and not result.is_error:
            await progress.report_progress(1, 1, f'{name} completed')
        return result


mcp.add_middleware(BridgeMiddleware())

# Namespaced composition keeps existing CAD tool names and exposes the full
# owned print pipeline through the same connection. Runner initialization stays lazy.
from bambu_mcp.server import mcp as bambu_server
mcp.mount(bambu_server, namespace='bambu')


async def _request(command: Dict[str, Any]) -> Any:
    return _unwrap_response(await send_to_freecad(command))


@mcp.tool(annotations=_ann(destructive=True, idempotent=False, open_world=True), tags={"cad"})
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


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def list_objects() -> Any:
    """List every object in the active document (name, label, type). Cheap
    overview — use get_object for the full properties of one object."""
    return await _request({"type": "list_objects"})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def get_object(name: str, compact: bool = False) -> Any:
    """Detail for one object: validity/state and shape bbox + topology counts
    (verts/edges/faces/solids) when present.

    Args:
        name: the object's Name (unique id), as shown by list_objects.
        compact: omit the full property dump (smaller — when you only need
            identity + shape summary).
    """
    return await _request({"type": "get_object",
                           "params": {"name": name, "compact": compact}})


@mcp.tool(annotations=_ann(destructive=True), tags={"cad"})
async def export(names: list[str], path: str, coordinates: str = "document") -> Any:
    """Export objects to a CAD file. The format is chosen by the file
    extension: .step/.stp, .iges/.igs, .brep/.brp, or .stl.

    Args:
        names: object Names to export (from list_objects).
        path: absolute output path; its extension picks the format.
    """
    return await _request({"type": "export", "params": {
        "names": names, "path": path, "coordinates": coordinates,
    }})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def list_types(filter: str = "") -> Any:
    """List creatable FreeCAD object TypeIds (what you can pass to addObject).

    Args:
        filter: case-insensitive substring, e.g. "Part::" or "Sketch".
    """
    return await _request({"type": "list_types", "params": {"filter": filter}})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def describe_type(type_id: str) -> Any:
    """Property schema for a TypeId: each property's type, group, doc string and
    enum options. Use this to learn what you can set before writing execute code.

    Args:
        type_id: e.g. "Part::Box", "PartDesign::Pad", "Sketcher::SketchObject".
    """
    return await _request({"type": "describe_type", "params": {"type_id": type_id}})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def measure(a: str, b: str = "", about: dict | None = None) -> Any:
    """Measure geometry. With one object: volume, area, center of mass, bbox.
    With two: the minimum distance between them, the closest points, and the
    overlap volume (overlap_volume>0 / clash=True means they interfere — use
    this to verify a fit instead of building a probe solid in execute).

    Args:
        a: object Name.
        b: optional second object Name; omit to measure `a` alone.
    """
    params = {"a": a, "b": b or None, "about": about}
    return await _request({"type": "measure", "params": params})


@mcp.tool(annotations=_ann(destructive=True, idempotent=False), tags={"cad"})
async def solve_parameter_target(
    component_id: str, parameter: str, bounds: list, target: dict,
    tolerance: float = 0.01, max_iterations: int = 40, commit: bool = False,
) -> Any:
    """Solve one bounded monotonic component parameter for a numeric target."""
    return await _request({"type": "solve_parameter_target", "params": {
        "component_id": component_id, "parameter": parameter,
        "bounds": bounds, "target": target, "tolerance": tolerance,
        "max_iterations": max_iterations, "commit": commit,
    }})


@mcp.tool(annotations=_ann(idempotent=False), tags={"cad"})
async def transform(
    object: str, translate: list[float] | None = None, rotate: dict | None = None,
    center: list[float] | None = None, relative: bool = True, copy_to: str = "",
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


@mcp.tool(annotations=_ann(destructive=True, idempotent=False), tags={"cad"})
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


@mcp.tool(annotations=_ann(destructive=True, idempotent=False), tags={"cad"})
async def boolean(
    op: str, objects: list[str], name: str = "", keep: bool = False,
) -> Any:
    """Boolean cut/fuse/common on named objects -> one new object.

    Args:
        op: "cut" (first minus the rest), "fuse" (union), or "common"
            (intersection).
        objects: >=2 object Names.
        name: result object name (default <first>_<op>).
        keep: keep the input objects (default removes them).
    """
    return await _request({"type": "boolean", "params": {
        "op": op, "objects": objects, "name": name or None, "keep": keep}})


@mcp.tool(annotations=_ann(idempotent=False), tags={"cad"})
async def duplicate(
    object: str, count: int = 1, translate: list[float] | None = None,
    rotate: dict | None = None, center: list[float] | None = None, name: str = "",
    combine: str = "",
) -> Any:
    """Copy a named object, optionally as a linear/polar array.

    Args:
        object: object Name to copy.
        count: number of copies.
        translate: per-step offset [dx,dy,dz] (linear array).
        rotate: per-step {"axis":[x,y,z],"angle":deg} about center (polar array).
        center: rotation pivot [x,y,z].
        name: base name for the copies.
        combine: "" -> separate objects; "compound" or "fuse" -> one object.
    """
    return await _request({"type": "duplicate", "params": {
        "object": object, "count": count, "translate": translate,
        "rotate": rotate, "center": center, "name": name or None,
        "combine": combine or None}})


@mcp.tool(annotations=_ann(destructive=True), tags={"cad"})
async def set_property(object: str, properties: dict) -> Any:
    """Set data properties on a named object, then recompute. Good for tweaking
    parametric primitives (e.g. {"Length": 30} on a Part::Box). Scalars,
    strings, bools and enums work directly; complex types need execute.

    Args:
        object: object Name.
        properties: {property_name: value, ...}.
    """
    return await _request({"type": "set_property", "params": {
        "object": object, "properties": properties}})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def get_selection() -> Any:
    """What the user has selected in FreeCAD: objects and sub-elements
    (e.g. Edge1, Face2) — use these to target fillets/chamfers. GUI only."""
    return await _request({"type": "get_selection"})


@mcp.tool(annotations=_ann(), tags={"cad"})
async def set_selection(names: list[str]) -> Any:
    """Replace the current selection with the given object Names. GUI only.

    Args:
        names: object Names to select.
    """
    return await _request({"type": "set_selection", "params": {"names": names}})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
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


@mcp.tool(annotations=_ann(), tags={"inspection"})
async def get_screenshot(
    width: int = 1024, height: int = 768, view: str = "iso", fit: bool = True,
    targets: list[str] | None = None, transparent: list[str] | None = None,
    temporary: bool = True, camera: dict | None = None, views: list[str] | None = None,
    max_dim: int | None = None, wireframe: bool = False,
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


@mcp.tool(annotations=_ann(idempotent=False), tags={"cad"})
async def create_component(document: str, name: str, label: str = "",
                          parameters: list | None = None) -> Any:
    """Create a parametric component: an App::Part container with a typed
    parameter registry. Each parameter declares kind "input" (concrete, with
    `default` and optional min/max/enum/unit) or "derived" (read-only, with an
    `expression` over other params using $name). Returns the component_id used
    by the other component tools.
    """
    return await _request({"type": "create_component", "params": {
        "document": document, "name": name, "label": label or None,
        "parameters": parameters or []}})


@mcp.tool(annotations=_ann(destructive=True, idempotent=False), tags={"cad"})
async def define_component(component_id: str, features: list,
                          rules: list | None = None, profiles: list | None = None,
                          profile_definitions: list | None = None,
                          geometry_references: list | None = None) -> Any:
    """Define (or replace) the component's build graph — a list of features.
    Feature types include primitives, booleans, arrays, reusable 2D profiles,
    native constrained sketches, reusable datum frames, generic hole/shell/
    draft/rib/face-offset/replace-face manufacturing operations,
    unified linear/grid/polar/mirror/path feature patterns,
    component instances and deterministic rigid/coincident/offset/angle mates,
    pad/pocket/revolve/sweep/loft operations, edge treatments, and groups.
    Sizes/positions are expressions over $params
    (with units), e.g. "$width / 2", "$wall_thickness + 2 mm". Booleans/arrays
    reference other features by id. Shared `placement`, `frame`, `frame_id`,
    `support`, `reversed`, `symmetric`, and selector fields use one schema
    across feature types. Each feature may set `role`
    (output/construction/tool/inspection) and `tags`. Read
    freecad://guide/cookbook for examples and use describe_feature_type for
    machine-readable fields.

    Optional `profile_definitions` register reusable geometry-only profiles;
    operations reference them with `profile_id` without creating document
    objects. Optional `geometry_references` name semantic face/edge/vertex/solid
    selectors for reuse by supports, operations, and sketches. Optional `rules`
    (declarative design-rule definitions, each with id/type/
    severity/targets/message) and default `profiles` are stored with the
    component for validate_component. A build that yields invalid geometry is
    rolled back, keeping the prior model.
    """
    return await _request({"type": "define_component", "params": {
        "component_id": component_id, "features": features,
        "rules": rules, "profiles": profiles,
        "profile_definitions": profile_definitions,
        "geometry_references": geometry_references}})


@mcp.tool(annotations=_ann(destructive=True), tags={"cad"})
async def set_component_parameters(component_id: str, values: dict,
                                   rebuild: bool = True, validate: bool = False,
                                   enforce_bounds: bool = False) -> Any:
    """Update input parameters and rebuild only affected features (native
    incremental recompute). Returns changed params, regenerated feature ids,
    remapped sketch external geometry, optional validation, and the component
    bounding box. Derived params are
    read-only and rejected.

    Declared min/max bounds are advisory by default (a value outside them is
    accepted and only flagged when you validate). Set enforce_bounds=True to
    reject out-of-range values up front instead."""
    return await _request({"type": "set_component_parameters", "params": {
        "component_id": component_id, "values": values,
        "rebuild": rebuild, "validate": validate,
        "enforce_bounds": enforce_bounds}})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def preview_parameter_update(component_id: str, values: dict,
                                   rebuild: bool = True, validate: bool = True,
                                   enforce_bounds: bool = False) -> Any:
    """Run the real parameter rebuild transaction and abort it after analysis."""
    return await _request({"type": "preview_parameter_update", "params": {
        "component_id": component_id, "values": values,
        "rebuild": rebuild, "validate": validate,
        "enforce_bounds": enforce_bounds}})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def get_component(component_id: str) -> Any:
    """Return the component's parameter registry (with current values),
    dependency graph summary, variants, and latest validation status."""
    return await _request({"type": "get_component", "params": {
        "component_id": component_id}})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def get_component_graph(component_id: str, detail: str = "summary") -> Any:
    """Return the revisioned feature graph. Use detail='full' for editable
    feature definitions and 'summary' for ids, types, roles, and inputs."""
    return await _request({"type": "get_component_graph", "params": {
        "component_id": component_id, "detail": detail}})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def list_geometry_references(component_id: str,
                                   resolve: bool = False) -> Any:
    """List named references and optionally report topology tracking status."""
    return await _request({"type": "list_geometry_references", "params": {
        "component_id": component_id, "resolve": resolve}})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def resolve_geometry_reference(component_id: str,
                                     reference_id: str) -> Any:
    """Resolve one reference with stable/remapped/ambiguous/missing diagnostics."""
    return await _request({"type": "resolve_geometry_reference", "params": {
        "component_id": component_id, "reference_id": reference_id}})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def get_sketch_status(component_id: str, feature_id: str) -> Any:
    """Return solver status, DoF, constraint errors, and named sketch maps."""
    return await _request({"type": "get_sketch_status", "params": {
        "component_id": component_id, "feature_id": feature_id}})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def get_assembly_status(component_id: str) -> Any:
    """Inspect assembly DOF, constraint rank, residuals, and solver state."""
    return await _request({"type": "get_assembly_status", "params": {
        "component_id": component_id}})


@mcp.tool(annotations=_ann(), tags={"cad"})
async def refresh_assembly(component_id: str) -> Any:
    """Refresh instance snapshots from their source components, reapply mates
    in dependency order, and return the resulting assembly status and bounds."""
    return await _request({"type": "refresh_assembly", "params": {
        "component_id": component_id}})


@mcp.tool(annotations=_ann(destructive=True, idempotent=False), tags={"cad"})
async def patch_component(component_id: str, operations: list,
                          expected_revision: int | None = None,
                          validate: bool = False,
                          dry_run: bool = False) -> Any:
    """Apply revision-checked structural edits to a component graph."""
    return await _request({"type": "patch_component", "params": {
        "component_id": component_id, "operations": operations,
        "expected_revision": expected_revision, "validate": validate,
        "dry_run": dry_run}})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def validate_patch(component_id: str, operations: list,
                         expected_revision: int | None = None,
                         validate: bool = True) -> Any:
    """Build a structural patch transactionally and return diagnostics only."""
    return await _request({"type": "validate_patch", "params": {
        "component_id": component_id, "operations": operations,
        "expected_revision": expected_revision, "validate": validate}})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def explain_rebuild(component_id: str, parameters: Any = None,
                          feature_ids: list | None = None,
                          operations: list | None = None) -> Any:
    """Explain parameter and structural dependency propagation without rebuilding."""
    return await _request({"type": "explain_rebuild", "params": {
        "component_id": component_id, "parameters": parameters,
        "feature_ids": feature_ids, "operations": operations}})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def get_feature_diagnostics(component_id: str,
                                  feature_id: str | None = None) -> Any:
    """Inspect feature, shape, expression, reference, sketch, and mate health."""
    return await _request({"type": "get_feature_diagnostics", "params": {
        "component_id": component_id, "feature_id": feature_id}})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def list_feature_types() -> Any:
    """List declarative component feature types supported by this server."""
    return await _request({"type": "list_feature_types"})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def describe_feature_type(feature_type: str) -> Any:
    """Return the machine-readable schema for one declarative feature type."""
    return await _request({"type": "describe_feature_type", "params": {
        "feature_type": feature_type}})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def capabilities() -> Any:
    """Return API, FreeCAD, feature, pattern, and validation capabilities."""
    result = await _request({"type": "capabilities"})
    result["local_print_pipeline"] = {
        "slicer": "BambuStudio CLI", "execution": "bridge_host",
        "orientation_providers": ["tweaker3", "stl-fit"],
        "optional_external_dependencies": True, "automatic_install": False,
        "printing_supported": False,
    }
    return result


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def list_patterns() -> Any:
    """List versioned server-owned CAD patterns."""
    return await _request({"type": "list_patterns"})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def expand_pattern(feature: dict) -> Any:
    """Expand one pattern to its ordinary, editable component features."""
    return await _request({"type": "expand_pattern", "params": {
        "feature": feature}})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def check_fit(component_id: str, container: str, insert: str,
                    retainers: list[str] | None = None, probe_steps: int = 16,
                    tolerance: float = 0.01) -> Any:
    """Check containment, clearance, interference, and blocked translations."""
    return await _request({"type": "check_fit", "params": {
        "component_id": component_id, "container": container, "insert": insert,
        "retainers": retainers or [], "probe_steps": probe_steps,
        "tolerance": tolerance}})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def inspect_surface(component_id: str, feature_id: str | None = None, samples: int = 15,
                          stripe_axis: list[float] | None = None, stripe_count: int = 12) -> Any:
    """Sample trimmed faces' principal curvature (1/mm), normals and reflection stripes."""
    return await _request({"type": "inspect_surface", "params": {
        "component_id": component_id, "feature_id": feature_id, "samples": samples,
        "stripe_axis": stripe_axis, "stripe_count": stripe_count}})


@mcp.tool(annotations=_ann(read_only=True), tags={"print"})
async def inspect_printability(component_id: str, feature_id: str | None = None,
                               min_wall: float = .8, samples_per_face: int = 5,
                               build_direction: list[float] | None = None,
                               overhang_angle: float = 45, max_samples: int = 2000,
                               print_spec: dict | None = None) -> Any:
    """Read-only sampled BRep thickness/overhang/island preflight in millimeters.

    Reports unresolved regions; a sampled minimum is not a proven global minimum.
    A shared print_spec.min_wall_mm replaces the sampling threshold when supplied.
    """
    if print_spec is not None:
        from print_pipeline_core.spec import number
        if not isinstance(print_spec, dict):
            raise ValueError('print_spec must be a dictionary')
        if print_spec.get('min_wall_mm') is not None:
            min_wall = number(print_spec['min_wall_mm'])
            if min_wall <= 0:
                raise ValueError('min_wall_mm must be positive')
    return await _request({"type": "inspect_printability", "params": {
        "component_id": component_id, "feature_id": feature_id, "min_wall": min_wall,
        "samples_per_face": samples_per_face, "build_direction": build_direction,
        "overhang_angle": overhang_angle, "max_samples": max_samples}})


@mcp.tool(annotations=_ann(read_only=True), tags={"print"})
async def inspect_attachment(component_id: str, feature_id: str, target_id: str,
                              anchor: list[float], radius: float = 5,
                              tolerance: float = .05) -> Any:
    """Check trim contact within an intended local seat, not anywhere on the body."""
    return await _request({"type": "inspect_attachment", "params": {
        "component_id": component_id, "feature_id": feature_id, "target_id": target_id,
        "anchor": anchor, "radius": radius, "tolerance": tolerance}})


@mcp.tool(annotations=_ann(read_only=True), tags={"print"})
async def create_connector_features(component_id: str, prefix: str, male_base: str,
                                     female_base: str, position: list[float], axis: str = "z",
                                     kind: str = "pin_socket", diameter: float = 3,
                                     length: float = 4, clearance: float = .2, embed: float = 1,
                                     bottom_clearance: float = .2, lead_in: float = .3,
                                     width: float = 6, thickness: float = 1.2) -> Any:
    """Return editable pin/socket or glue-ledge features; does not modify the model.

    Position is the socket entry plane; positive axis points into the receiver.
    Clearance is per side in mm. Preview/apply returned features with patch_component.
    Physical clearance calibration and geometric seating validation remain necessary.
    """
    return await _request({"type": "create_connector_features", "params": {
        "component_id": component_id, "prefix": prefix, "male_base": male_base,
        "female_base": female_base, "position": position, "axis": axis, "kind": kind,
        "diameter": diameter, "length": length, "clearance": clearance, "embed": embed,
        "bottom_clearance": bottom_clearance, "lead_in": lead_in,
        "width": width, "thickness": thickness}})


@mcp.tool(annotations=_ann(idempotent=False), tags={"print"})
async def suggest_print_orientations(stl_path: str, backend: str, executable: list[str] | None = None,
                                      build_volume: list[float] | None = None, timeout: float = 120,
                                      seed: int = 42, samples: int = 5000,
                                      max_candidates: int = 4) -> Any:
    """Run an optional local Tweaker-3/stl-fit adapter on a copied envelope STL.

    backend="tweaker3-python" exposes multiple ranked poses via the upstream API;
    executable=[python, /checkout/Tweaker.py]. The existing tweaker3 CLI is unchanged.

    Returns original-size rigid matrices, not print-time savings. Dependencies
    must be installed separately. Apply one matrix to the whole native assembly.
    """
    from orientation_candidates import suggest_print_orientations as suggest
    return await asyncio.to_thread(suggest, stl_path, backend, executable,
                                   build_volume, timeout, seed, samples, max_candidates)


@mcp.tool(annotations=_ann(idempotent=False), tags={"print"})
async def compare_print_candidates(project_path: str, output_dir: str, candidates: list[dict],
                                    slicer_path: str | None = None, timeout: float = 300,
                                    backend: str = "legacy", print_spec: dict | None = None,
                                    workers: int = 2, backend_python: str | None = None) -> Any:
    """Measure local BambuStudio native-3MF candidates against an automatic baseline.

    Input requires one whole assembly build item in millimeters.
    Preserves source and colors. Candidates have name, print settings, and optional
    rotation degrees or rotation_matrix from suggest_print_orientations. Isolates
    artifacts, bounds execution time, rejects failed/outside-plate results, and
    reports unavailable cost buckets as null. backend="bambu" adds presets, cache and
    parallel evaluation; print_spec is the versioned shared print specification.
    Returns fastest, least-material and conservative alternatives. Does not send a print job.
    """
    from slicer_pipeline import compare_print_candidates as compare
    return await asyncio.to_thread(compare, project_path, output_dir, candidates,
                                   slicer_path, timeout, backend, print_spec, workers, backend_python)


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def cross_sections(component_id: str, plane: str, offsets: list,
                         feature_id: str | None = None, samples: int = 80) -> Any:
    """Return section curves at XY(z), XZ(y), or YZ(x) planes in document coordinates."""
    return await _request({"type": "cross_sections", "params": {
        "component_id": component_id, "plane": plane, "offsets": offsets,
        "feature_id": feature_id, "samples": samples}})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def reference_overlay(component_id: str, image_path: str, plane: str,
                            model_points: list, pixel_points: list,
                            feature_id: str | None = None, samples: int = 80,
                            opacity: float = 0.55, reflection_stripes: bool = False) -> Any:
    """Return self-contained SVG: calibrated PNG plus orthographic model edges.

    Two projected model anchors (mm) and matching image anchors (pixels) set
    uniform scale/rotation. XY/XZ/YZ specify projection axes; model y is up,
    pixel y down. Optional reflection stripes are sampled face markers.
    """
    return await _request({"type": "reference_overlay", "params": {
        "component_id": component_id, "image_path": image_path, "plane": plane,
        "model_points": model_points, "pixel_points": pixel_points,
        "feature_id": feature_id, "samples": samples, "opacity": opacity,
        "reflection_stripes": reflection_stripes}})


@mcp.tool(annotations=_ann(destructive=True), tags={"cad"})
async def export_bundle(component_id: str, directory: str,
                        formats: list[str] | None = None, per_output: bool = True,
                        assembly: bool = True, overwrite: bool = True,
                        basename: str = "") -> Any:
    """Export several formats and component outputs in one request."""
    return await _request({"type": "export_bundle", "params": {
        "component_id": component_id, "directory": directory,
        "formats": formats, "per_output": per_output, "assembly": assembly,
        "overwrite": overwrite, "basename": basename or None}})


@mcp.tool(annotations=_ann(destructive=True, idempotent=False), tags={"cad"})
async def build_component(document: dict, component: dict,
                          validate: dict | None = None, exports: dict | None = None) -> Any:
    """Create, define, validate, and optionally export a component in one call."""
    return await _request({"type": "build_component", "params": {
        "document": document, "component": component,
        "validate": validate, "exports": exports}})


@mcp.tool(annotations=_ann(idempotent=False), tags={"cad"})
async def create_component_variant(component_id: str, name: str, values: dict) -> Any:
    """Store a named set of parameter overrides that export_component can apply."""
    return await _request({"type": "create_component_variant", "params": {
        "component_id": component_id, "name": name, "values": values}})


@mcp.tool(annotations=_ann(read_only=True), tags={"inspection"})
async def validate_component(component_id: str, profiles: list | None = None,
                            rule_ids: list | None = None, include_measurements: bool = False,
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


@mcp.tool(annotations=_ann(), tags={"inspection"})
async def render_component(component_id: str, view: str = "iso", section: dict | None = None,
                          hide_features: list | None = None,
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


@mcp.tool(annotations=_ann(destructive=True), tags={"cad"})
async def export_component(component_id: str, path: str, format: str = "FCStd",
                          variant: str = "") -> Any:
    """Export the component (or a named variant) to a file. Formats: FCStd,
    STEP, STL, IGES, BREP, 3MF. FCStd saves the whole document; the others export
    the generated solids. 3MF preserves named output meshes and their print
    colors in document coordinates, with per-part mesh checks and roundtrip
    verification before atomic replacement."""
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


def main():
    """Run the bridge over stdio for desktop MCP clients."""
    mcp.run(transport='stdio')


if __name__ == "__main__":
    main()
