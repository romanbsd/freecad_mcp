import base64
import contextlib
import io
import json
import os
import socket
import tempfile
import traceback

import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtCore, QtGui


class FreeCADMCPServer:
    MAX_MESSAGE = 64 * 1024 * 1024  # 64 MiB frame guard
    # Actionable errors so an agent that calls a tool before anything exists
    # knows how to recover instead of just seeing "no active document".
    NO_DOC = "no active document — create one first, e.g. App.newDocument() via the execute tool"
    NO_VIEW = "no active 3D view — open or create a document in the FreeCAD GUI first"

    def __init__(self, host="localhost", port=9876):
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.client = None
        self.buffer = b""
        self.timer = None

    def start(self):
        self.running = True
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            self.socket.bind((self.host, self.port))
            self.socket.listen(1)
            self.socket.setblocking(False)
            self.timer = QtCore.QTimer()
            self.timer.timeout.connect(self._process_server)
            self.timer.start(100)  # 100ms interval
            App.Console.PrintMessage(
                f"FreeCAD MCP server started on {self.host}:{self.port}\n"
            )
        except Exception as e:
            App.Console.PrintError(f"Failed to start server: {str(e)}\n")
            self.stop()

    def stop(self):
        self.running = False
        if self.timer:
            self.timer.stop()
            self.timer = None
        if self.socket:
            self.socket.close()
        if self.client:
            self.client.close()
        self.socket = None
        self.client = None
        App.Console.PrintMessage("FreeCAD MCP server stopped\n")

    def _process_server(self):
        if not self.running:
            return

        try:
            if not self.client and self.socket:
                try:
                    self.client, address = self.socket.accept()
                    self.client.setblocking(False)
                    App.Console.PrintMessage(f"Connected to client: {address}\n")
                except BlockingIOError:
                    pass
                except Exception as e:
                    App.Console.PrintError(f"Error accepting connection: {str(e)}\n")

            if self.client:
                try:
                    try:
                        data = self.client.recv(65536)
                        if data:
                            self.buffer += data
                            self._process_frames()
                        else:
                            App.Console.PrintMessage("Client disconnected\n")
                            self._reset_client()
                    except BlockingIOError:
                        pass
                    except Exception as e:
                        App.Console.PrintError(f"Error receiving data: {str(e)}\n")
                        self._reset_client()

                except Exception as e:
                    App.Console.PrintError(f"Error with client: {str(e)}\n")
                    self._reset_client()

        except Exception as e:
            App.Console.PrintError(f"Server error: {str(e)}\n")

    def _reset_client(self):
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
        self.client = None
        self.buffer = b""

    def _send_frame(self, payload):
        # Length-prefixed framing: 4-byte big-endian length + UTF-8 JSON.
        # default=str keeps an unserializable context value from killing the
        # connection (ponytail: best-effort stringify, fine for a debug dump).
        data = json.dumps(payload, default=str).encode("utf-8")
        self.client.sendall(len(data).to_bytes(4, "big") + data)

    def _process_frames(self):
        # Drain every complete frame currently buffered. Each frame is
        # 4-byte length header + that many bytes of JSON. Partial frames stay
        # in the buffer for the next tick; oversized or garbage frames drop the
        # client instead of wedging it forever.
        while len(self.buffer) >= 4:
            length = int.from_bytes(self.buffer[:4], "big")
            if length > self.MAX_MESSAGE:
                App.Console.PrintError(
                    f"Message too large ({length} bytes); dropping client\n"
                )
                self._reset_client()
                return
            if len(self.buffer) < 4 + length:
                return  # wait for the rest of this frame
            frame = self.buffer[4 : 4 + length]
            self.buffer = self.buffer[4 + length :]
            try:
                command = json.loads(frame.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                self._send_frame({"status": "error", "message": f"Invalid JSON: {e}"})
                continue
            self._send_frame(self.execute_command(command))

    def execute_command(self, command):
        try:
            cmd_type = command.get("type")
            params = command.get("params", {})

            handlers = {
                "execute": self.handle_execute,
                "get_screenshot": self.handle_get_screenshot,
                "export": self.handle_export,
                "get_object": self.handle_get_object,
                "list_objects": self.handle_list_objects,
                "list_types": self.handle_list_types,
                "describe_type": self.handle_describe_type,
                "measure": self.handle_measure,
                "get_selection": self.handle_get_selection,
                "set_selection": self.handle_set_selection,
                "get_subelements": self.handle_get_subelements,
            }
            handlers.update(self._component_handlers())

            handler = handlers.get(cmd_type)
            if handler:
                try:
                    App.Console.PrintMessage(f"Executing handler for {cmd_type}\n")
                    result = handler(**params)
                    return {"status": "success", "result": result}
                except Exception as e:
                    App.Console.PrintError(f"Error in handler: {str(e)}\n")
                    traceback.print_exc()
                    return {"status": "error", "message": str(e)}
            else:
                return {
                    "status": "error",
                    "message": f"Unknown command type: {cmd_type}",
                }

        except Exception as e:
            App.Console.PrintError(f"Error executing command: {str(e)}\n")
            traceback.print_exc()
            return {"status": "error", "message": str(e)}

    def handle_execute(self, code, return_context=False, recompute=True):
        """Execute Python in the FreeCAD context and report back.

        Namespace: App, Gui, doc (the active document). The script can return
        data two ways, both captured: assign to `result`, or print(). Runs
        inside an undo transaction so the whole action reverts as one Ctrl-Z;
        recomputes afterwards unless recompute=False. With return_context=True
        the document/object/view summary is appended.
        """
        doc = App.ActiveDocument
        out = io.StringIO()
        # Group every mutation under one undo step (no-op if no document yet).
        if doc:
            doc.openTransaction("MCP execute")
        try:
            ns = {"App": App, "Gui": Gui, "doc": doc}
            with contextlib.redirect_stdout(out):
                exec(code, ns)

            if recompute and App.ActiveDocument:
                App.ActiveDocument.recompute()
            # Best-effort: the script may have closed/replaced `doc`, leaving a
            # dangling handle whose commit would throw — the work is already done.
            if doc:
                try:
                    doc.commitTransaction()
                except Exception:
                    pass

            response = {"command_result": "success", "stdout": out.getvalue()}
            if "result" in ns:
                # _send_frame uses default=str, so a non-serializable result
                # (a FreeCAD object, vector, etc.) is stringified rather than
                # killing the response.
                response["result"] = ns["result"]
            # Surface objects that failed to rebuild so a silent breakage is
            # visible without a second round-trip.
            errors = self._recompute_errors()
            if errors:
                response["recompute_errors"] = errors
            if return_context:
                response["context"] = self.get_document_context()
            return response
        except Exception as e:
            if doc:
                try:
                    doc.abortTransaction()
                except Exception:
                    pass
            return {
                "command_result": "error",
                "error": str(e),
                "stdout": out.getvalue(),
                "traceback": traceback.format_exc(),
            }

    # Standard view orientations a caller can request before capture.
    STANDARD_VIEWS = (
        "iso", "front", "rear", "top", "bottom", "left", "right",
    )

    def handle_get_screenshot(self, width=1024, height=768, view="iso", fit=True,
                              targets=None, transparent=None, temporary=True,
                              camera=None, views=None):
        """Orient the camera, optionally fit, and return the view as a PNG.

        view: one of STANDARD_VIEWS, or "current" to leave the camera as-is.
        fit:  zoom to fit all visible geometry before capturing.
        """
        if not (Gui.ActiveDocument and Gui.ActiveDocument.ActiveView):
            return {"error": self.NO_VIEW}
        v = Gui.ActiveDocument.ActiveView

        orient = {
            "iso": v.viewIsometric, "front": v.viewFront, "rear": v.viewRear,
            "top": v.viewTop, "bottom": v.viewBottom,
            "left": v.viewLeft, "right": v.viewRight,
        }
        targets = set(targets or [])
        transparent = set(transparent or [])
        doc = App.ActiveDocument
        objects = list(doc.Objects) if doc else []
        state = {
            o.Name: (o.ViewObject.Visibility,
                     getattr(o.ViewObject, "Transparency", None))
            for o in objects if hasattr(o, "ViewObject")
        }
        old_camera = v.getCameraOrientation()
        try:
            if targets:
                missing = sorted(targets - {o.Name for o in objects})
                if missing:
                    raise ValueError("unknown screenshot targets: %s" % ", ".join(missing))
                for obj in objects:
                    if hasattr(obj, "ViewObject"):
                        obj.ViewObject.Visibility = obj.Name in targets
            for name in transparent:
                obj = doc.getObject(name) if doc else None
                if obj is None:
                    raise ValueError("unknown transparent object %s" % name)
                obj.ViewObject.Visibility = True
                if hasattr(obj.ViewObject, "Transparency"):
                    obj.ViewObject.Transparency = 70

            def orient_camera(requested_view):
                if camera and camera.get("quaternion"):
                    q = camera["quaternion"]
                    if not isinstance(q, (list, tuple)) or len(q) != 4:
                        raise ValueError(
                            "camera.quaternion must contain four numbers"
                        )
                    v.setCameraOrientation(tuple(float(x) for x in q))
                    return "quaternion"
                if camera and camera.get("direction"):
                    direction = camera["direction"]
                    up = camera.get("up", [0, 0, 1])
                    if len(direction) != 3 or len(up) != 3:
                        raise ValueError(
                            "camera direction and up must contain three numbers"
                        )
                    forward = App.Vector(*[float(x) for x in direction])
                    up_vector = App.Vector(*[float(x) for x in up])
                    if forward.Length <= 1e-12 or up_vector.Length <= 1e-12:
                        raise ValueError("camera direction and up must be non-zero")
                    forward.normalize()
                    up_vector.normalize()
                    right = forward.cross(up_vector)
                    if right.Length <= 1e-12:
                        raise ValueError(
                            "camera direction and up may not be parallel"
                        )
                    right.normalize()
                    corrected_up = right.cross(forward)
                    corrected_up.normalize()
                    rotation = App.Rotation(
                        right, corrected_up,
                        App.Vector(-forward.x, -forward.y, -forward.z),
                        "ZXY",
                    )
                    v.setCameraOrientation(rotation.Q)
                    return "vector"
                if requested_view != "current":
                    setter = orient.get(requested_view)
                    if setter is None:
                        raise ValueError(
                            f"unknown view {requested_view!r}; use one of "
                            f"{', '.join(self.STANDARD_VIEWS)} or 'current'"
                        )
                    setter()
                return requested_view

            requested_views = list(views or [])
            if requested_views:
                tile_paths = []
                for index, requested in enumerate(requested_views):
                    orient_camera(requested)
                    if fit:
                        v.fitAll()
                    tile_path = os.path.join(
                        tempfile.gettempdir(),
                        "freecad_mcp_screenshot_%d.png" % index,
                    )
                    v.saveImage(tile_path, int(width), int(height), "Current")
                    tile_paths.append(tile_path)
                canvas = QtGui.QImage(
                    int(width) * len(tile_paths), int(height),
                    QtGui.QImage.Format_ARGB32,
                )
                canvas.fill(QtGui.QColor("white"))
                painter = QtGui.QPainter(canvas)
                try:
                    for index, tile_path in enumerate(tile_paths):
                        painter.drawImage(
                            int(width) * index, 0, QtGui.QImage(tile_path)
                        )
                finally:
                    painter.end()
                path = os.path.join(
                    tempfile.gettempdir(), "freecad_mcp_screenshot.png"
                )
                canvas.save(path, "PNG")
                resolved_view = "contact_sheet"
                output_width = int(width) * len(tile_paths)
            else:
                resolved_view = orient_camera(view)
                if fit:
                    v.fitAll()
                path = os.path.join(
                    tempfile.gettempdir(), "freecad_mcp_screenshot.png"
                )
                v.saveImage(path, int(width), int(height), "Current")
                output_width = int(width)
            with open(path, "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
            return {
                "image_base64": data,
                "width": output_width,
                "height": int(height),
                "view": resolved_view,
                "views": requested_views or None,
                "visible_objects": [
                    o.Name for o in objects
                    if hasattr(o, "ViewObject") and o.ViewObject.Visibility
                ],
                "axis_convention": {
                    "x": "right", "y": "depth", "z": "up",
                    "named_views": "FreeCAD standard camera orientations",
                },
                "temporary": bool(temporary),
            }
        finally:
            if temporary:
                for obj in objects:
                    previous = state.get(obj.Name)
                    if previous is None:
                        continue
                    obj.ViewObject.Visibility = previous[0]
                    if previous[1] is not None:
                        obj.ViewObject.Transparency = previous[1]
                v.setCameraOrientation(old_camera)

    def _recompute_errors(self):
        """Names of objects currently flagged as in error (failed rebuild)."""
        if not App.ActiveDocument:
            return []
        errs = []
        for o in App.ActiveDocument.Objects:
            try:
                if "Error" in o.State:
                    errs.append(o.Name)
            except Exception:
                pass
        return errs

    def handle_list_objects(self):
        """Cheap overview: name/label/type for every object."""
        doc = App.ActiveDocument
        if not doc:
            return {"objects": []}
        return {"objects": [
            {"name": o.Name, "label": o.Label, "type": o.TypeId}
            for o in doc.Objects
        ]}

    def handle_get_object(self, name):
        """Full property dump for one object: all properties, validity/state,
        and shape bbox/topology when present."""
        doc = App.ActiveDocument
        if not doc:
            return {"error": self.NO_DOC}
        obj = doc.getObject(name)
        if obj is None:
            return {"error": f"unknown object: {name}"}

        props = {}
        for p in obj.PropertiesList:
            try:
                props[p] = getattr(obj, p)  # default=str stringifies non-JSON values
            except Exception as e:
                props[p] = f"<error: {e}>"

        info = {
            "name": obj.Name,
            "label": obj.Label,
            "type": obj.TypeId,
            "state": list(obj.State) if hasattr(obj, "State") else None,
            "valid": obj.isValid() if hasattr(obj, "isValid") else None,
            "properties": props,
        }
        if hasattr(obj, "Shape"):
            try:
                s = obj.Shape
                bb = s.BoundBox
                info["shape"] = {
                    "type": s.ShapeType,
                    "bbox": {"xmin": bb.XMin, "ymin": bb.YMin, "zmin": bb.ZMin,
                             "xmax": bb.XMax, "ymax": bb.YMax, "zmax": bb.ZMax},
                    "volume": float(s.Volume),
                    "area": float(s.Area),
                    "vertexes": len(s.Vertexes),
                    "edges": len(s.Edges),
                    "faces": len(s.Faces),
                    "solids": len(s.Solids),
                }
            except Exception as e:
                info["shape"] = {"error": str(e)}
        return info

    def handle_export(self, names, path):
        """Export objects to a file. Format is chosen by `path` extension:
        step/stp, iges/igs, brep/brp (BREP/STEP/IGES via Part), or stl."""
        doc = App.ActiveDocument
        if not doc:
            return {"error": self.NO_DOC}
        objs = [doc.getObject(n) for n in names]
        missing = [n for n, o in zip(names, objs) if o is None]
        if missing:
            return {"error": f"unknown object(s): {missing}"}

        ext = os.path.splitext(path)[1].lower()
        try:
            if ext == ".stl":
                import Part
                shapes = [o.Shape for o in objs if hasattr(o, "Shape")]
                if not shapes:
                    return {"error": "selected objects have no Shape to export"}
                shape = shapes[0] if len(shapes) == 1 else Part.makeCompound(shapes)
                shape.exportStl(path)
            elif ext in (".step", ".stp", ".iges", ".igs", ".brep", ".brp"):
                import Part
                Part.export(objs, path)
            else:
                return {"error": f"unsupported extension {ext!r}; "
                                 f"use step/iges/brep/stl"}
        except Exception as e:
            return {"error": str(e), "traceback": traceback.format_exc()}

        return {"exported": names, "path": path, "bytes": os.path.getsize(path)}

    def handle_list_types(self, filter=""):
        """All creatable object TypeIds (optionally substring-filtered)."""
        # supportedTypes() only lists types from imported modules, so pull in
        # the common workbenches first to give a complete picture.
        for m in ("Part", "PartDesign", "Sketcher", "Draft", "Mesh"):
            try:
                __import__(m)
            except Exception:
                pass
        doc = App.ActiveDocument
        temp = None
        if doc is None:
            temp = App.newDocument("__types__")
            doc = temp
        try:
            types = sorted(doc.supportedTypes())
        finally:
            if temp is not None:
                App.closeDocument(temp.Name)
        if filter:
            f = filter.lower()
            types = [t for t in types if f in t.lower()]
        return {"count": len(types), "types": types}

    def handle_describe_type(self, type_id):
        """Property schema for a type: per-property type, group, doc, enums.
        Instantiates the type in a throwaway document, introspects, cleans up."""
        prev = App.ActiveDocument
        temp = App.newDocument("__describe__")
        try:
            try:
                obj = temp.addObject(type_id, "probe")
            except Exception as e:
                return {"error": f"cannot create {type_id!r}: {e}"}
            props = []
            for p in obj.PropertiesList:
                entry = {"name": p}
                for key, getter in (
                    ("type", obj.getTypeIdOfProperty),
                    ("group", obj.getGroupOfProperty),
                    ("doc", obj.getDocumentationOfProperty),
                ):
                    try:
                        entry[key] = getter(p)
                    except Exception:
                        pass
                try:
                    enums = obj.getEnumerationsOfProperty(p)
                    if enums:
                        entry["enums"] = enums
                except Exception:
                    pass
                props.append(entry)
            return {"type": type_id, "properties": props}
        finally:
            App.closeDocument(temp.Name)
            if prev is not None:
                App.setActiveDocument(prev.Name)

    def handle_measure(self, a, b=None):
        """Measure one shape (volume/area/center/bbox) or the minimum distance
        between two shapes (by object Name)."""
        doc = App.ActiveDocument
        if not doc:
            return {"error": self.NO_DOC}
        oa = doc.getObject(a)
        if oa is None or not hasattr(oa, "Shape"):
            return {"error": f"{a!r} not found or has no shape"}

        if b is None:
            s = oa.Shape
            bb = s.BoundBox
            # CenterOfMass isn't defined on every shape type (e.g. a Compound);
            # fall back to the bounding-box center.
            try:
                c = s.CenterOfMass
                com = [c.x, c.y, c.z]
            except Exception:
                com = [bb.Center.x, bb.Center.y, bb.Center.z]
            return {
                "object": a,
                "shape_type": s.ShapeType,
                "volume": float(s.Volume),
                "area": float(s.Area),
                "center_of_mass": com,
                "bbox": {"xmin": bb.XMin, "ymin": bb.YMin, "zmin": bb.ZMin,
                         "xmax": bb.XMax, "ymax": bb.YMax, "zmax": bb.ZMax},
                "bbox_size": [bb.XLength, bb.YLength, bb.ZLength],
            }

        ob = doc.getObject(b)
        if ob is None or not hasattr(ob, "Shape"):
            return {"error": f"{b!r} not found or has no shape"}
        # distToShape -> (distance, [(pntA, pntB), ...], [topo info])
        dist, pairs, _ = oa.Shape.distToShape(ob.Shape)
        p = pairs[0] if pairs else None
        return {
            "from_object": a,
            "to_object": b,
            "distance": float(dist),
            "from_point": [p[0].x, p[0].y, p[0].z] if p else None,
            "to_point": [p[1].x, p[1].y, p[1].z] if p else None,
        }

    def handle_get_selection(self):
        """What the user has selected (objects + sub-elements). GUI only."""
        try:
            sel = Gui.Selection.getSelectionEx()
        except Exception as e:
            return {"error": f"selection unavailable (GUI only): {e}"}
        return {"selection": [
            {"document": s.DocumentName, "object": s.ObjectName,
             "sub_elements": list(s.SubElementNames)}
            for s in sel
        ]}

    def handle_set_selection(self, names):
        """Replace the current selection with the given object Names. GUI only."""
        doc = App.ActiveDocument
        if not doc:
            return {"error": self.NO_DOC}
        try:
            Gui.Selection.clearSelection()
            for n in names:
                obj = doc.getObject(n)
                if obj is None:
                    return {"error": f"unknown object: {n}"}
                Gui.Selection.addSelection(obj)
        except Exception as e:
            return {"error": f"selection unavailable (GUI only): {e}"}
        return self.handle_get_selection()

    def handle_get_subelements(self, name, limit=200):
        """Sub-geometry of an object so it can be targeted by index.

        For any shape: its edges (Edge1..N, with curve type, length, center) and
        faces (Face1..N, with area, center) — feed these names to fillet/chamfer
        or set_selection. For a Sketcher sketch: its Geometry and Constraints.
        Edge/face lists are capped at `limit`.
        """
        doc = App.ActiveDocument
        if not doc:
            return {"error": self.NO_DOC}
        obj = doc.getObject(name)
        if obj is None:
            return {"error": f"unknown object: {name}"}

        out = {"name": name, "type": obj.TypeId}

        if obj.TypeId == "Sketcher::SketchObject":
            geom = []
            for i, g in enumerate(obj.Geometry):
                g_info = {"index": i, "type": g.__class__.__name__}
                for attr in ("StartPoint", "EndPoint", "Center", "Radius"):
                    if hasattr(g, attr):
                        v = getattr(g, attr)
                        g_info[attr.lower()] = [v.x, v.y, v.z] if hasattr(v, "x") else float(v)
                geom.append(g_info)
            out["geometry"] = geom
            out["constraints"] = [
                {"index": i, "type": c.Type} for i, c in enumerate(obj.Constraints)
            ]

        if hasattr(obj, "Shape"):
            try:
                s = obj.Shape
                edges = []
                for i, e in enumerate(s.Edges[:limit]):
                    c = e.CenterOfMass
                    try:
                        curve = e.Curve.__class__.__name__
                    except Exception:
                        curve = None
                    info = {"name": f"Edge{i + 1}", "curve": curve,
                            "length": float(e.Length),
                            "center": [c.x, c.y, c.z]}
                    # Endpoints + unit direction let an agent tell a vertical
                    # edge from a horizontal one. Closed/curved edges have a
                    # single vertex, so direction is null there.
                    vs = e.Vertexes
                    if len(vs) >= 2:
                        p0, p1 = vs[0].Point, vs[-1].Point
                        info["start"] = [p0.x, p0.y, p0.z]
                        info["end"] = [p1.x, p1.y, p1.z]
                        d = p1 - p0
                        if d.Length > 1e-9:
                            d = d / d.Length
                            info["direction"] = [d.x, d.y, d.z]
                    edges.append(info)
                faces = []
                for i, f in enumerate(s.Faces[:limit]):
                    c = f.CenterOfMass
                    faces.append({"name": f"Face{i + 1}", "area": float(f.Area),
                                  "center": [c.x, c.y, c.z]})
                out["edges"] = edges
                out["faces"] = faces
                out["truncated"] = len(s.Edges) > limit or len(s.Faces) > limit
            except Exception as e:
                out["shape_error"] = str(e)
        return out

    def _component_handlers(self):
        """Parametric component workflow ops (parametric.py). Module functions
        already match the handler(**params) contract, so map them directly."""
        import parametric
        return {
            "create_component": parametric.op_create_component,
            "define_component": parametric.op_define_component,
            "set_component_parameters": parametric.op_set_component_parameters,
            "get_component": parametric.op_get_component,
            "get_component_graph": parametric.op_get_component_graph,
            "patch_component": parametric.op_patch_component,
            "list_feature_types": parametric.op_list_feature_types,
            "describe_feature_type": parametric.op_describe_feature_type,
            "capabilities": parametric.op_capabilities,
            "list_patterns": parametric.op_list_patterns,
            "expand_pattern": parametric.op_expand_pattern,
            "check_fit": parametric.op_check_fit,
            "export_bundle": parametric.op_export_bundle,
            "build_component": parametric.op_build_component,
            "create_component_variant": parametric.op_create_component_variant,
            "validate_component": parametric.op_validate_component,
            "render_component": parametric.op_render_component,
            "export_component": parametric.op_export_component,
        }

    def get_document_context(self):
        """Get comprehensive information about the current document state"""
        doc = App.ActiveDocument
        if not doc:
            return {"document": None, "objects": [], "view": None}

        # Document info
        doc_info = {
            "name": doc.Name,
            "filename": doc.FileName if hasattr(doc, "FileName") else None,
            "object_count": len(doc.Objects),
        }

        # Objects info. Each object is enriched best-effort: a single object
        # that raises (invalid shape, None ViewObject in console mode, etc.)
        # must not abort the whole dump.
        objects = []
        for obj in doc.Objects:
            obj_info = {
                "name": obj.Name,
                "label": obj.Label,
                "type": obj.TypeId,
            }
            try:
                # ViewObject is None in headless mode; hasattr is True regardless.
                vo = getattr(obj, "ViewObject", None)
                obj_info["visibility"] = vo.Visibility if vo is not None else None

                # Add placement if available
                if hasattr(obj, "Placement"):
                    pos = obj.Placement.Base
                    rot = obj.Placement.Rotation
                    obj_info["placement"] = {
                        "position": [float(pos.x), float(pos.y), float(pos.z)],
                        "rotation": [
                            float(rot.Axis.x),
                            float(rot.Axis.y),
                            float(rot.Axis.z),
                            float(rot.Angle),
                        ],
                    }

                # Add shape properties if available
                if hasattr(obj, "Shape"):
                    shape = obj.Shape
                    obj_info["shape"] = {
                        "type": shape.ShapeType,
                        "solids": len(shape.Solids),
                        "volume": (
                            float(shape.Volume) if hasattr(shape, "Volume") else None
                        ),
                        "area": float(shape.Area) if hasattr(shape, "Area") else None,
                    }
            except Exception as e:
                obj_info["error"] = str(e)

            objects.append(obj_info)

        # View state. Best-effort: camera introspection needs the Coin/pivy
        # SWIG bindings, which aren't always loaded ("No SWIG wrapped library
        # loaded"). Never let view state abort the whole context dump.
        view_info = None
        try:
            if Gui.ActiveDocument and Gui.ActiveDocument.ActiveView:
                cam = Gui.ActiveDocument.ActiveView.getCameraNode()
                view_info = {
                    "camera_type": str(cam.getTypeId().getName()),
                    "camera_position": [float(x) for x in cam.position.getValue()],
                    "camera_orientation": [
                        float(x) for x in cam.orientation.getValue()
                    ],
                }
        except Exception as e:
            view_info = {"error": str(e)}

        return {"document": doc_info, "objects": objects, "view": view_info}


class FreeCADMCPPanel:
    def __init__(self):
        self.form = QtGui.QWidget()
        self.form.setWindowTitle("FreeCAD MCP")

        layout = QtGui.QVBoxLayout(self.form)

        # Server status
        self.status_label = QtGui.QLabel("Server: Stopped")
        layout.addWidget(self.status_label)

        # Start/Stop buttons
        button_layout = QtGui.QHBoxLayout()
        self.start_button = QtGui.QPushButton("Start Server")
        self.stop_button = QtGui.QPushButton("Stop Server")
        self.stop_button.setEnabled(False)

        self.start_button.clicked.connect(self.start_server)
        self.stop_button.clicked.connect(self.stop_server)

        button_layout.addWidget(self.start_button)
        button_layout.addWidget(self.stop_button)
        layout.addLayout(button_layout)

        # Server instance
        self.server = None

    def start_server(self):
        if not self.server:
            self.server = FreeCADMCPServer()
            self.server.start()
            self.status_label.setText("Server: Running")
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)

    def stop_server(self):
        if self.server:
            self.server.stop()
            self.server = None
            self.status_label.setText("Server: Stopped")
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)


# Module-level reference so the panel isn't garbage-collected while open.
_panel = None


def show_panel():
    global _panel
    if _panel is None:
        _panel = FreeCADMCPPanel()
    _panel.form.show()
    _panel.form.raise_()
    _panel.form.activateWindow()
