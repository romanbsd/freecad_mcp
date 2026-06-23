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
            if doc:
                doc.commitTransaction()

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
                doc.abortTransaction()
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

    def handle_get_screenshot(self, width=1024, height=768, view="iso", fit=True):
        """Orient the camera, optionally fit, and return the view as a PNG.

        view: one of STANDARD_VIEWS, or "current" to leave the camera as-is.
        fit:  zoom to fit all visible geometry before capturing.
        """
        if not (Gui.ActiveDocument and Gui.ActiveDocument.ActiveView):
            return {"error": "no active 3D view to capture"}
        v = Gui.ActiveDocument.ActiveView

        orient = {
            "iso": v.viewIsometric, "front": v.viewFront, "rear": v.viewRear,
            "top": v.viewTop, "bottom": v.viewBottom,
            "left": v.viewLeft, "right": v.viewRight,
        }
        if view != "current":
            setter = orient.get(view)
            if setter is None:
                return {"error": f"unknown view {view!r}; use one of "
                                 f"{', '.join(self.STANDARD_VIEWS)} or 'current'"}
            setter()
        if fit:
            v.fitAll()

        path = os.path.join(tempfile.gettempdir(), "freecad_mcp_screenshot.png")
        v.saveImage(path, int(width), int(height), "Current")
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
        return {"image_base64": data, "width": int(width), "height": int(height),
                "view": view}

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
            return {"error": "no active document"}
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
            return {"error": "no active document"}
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
            return {"error": "no active document"}
        oa = doc.getObject(a)
        if oa is None or not hasattr(oa, "Shape"):
            return {"error": f"{a!r} not found or has no shape"}

        if b is None:
            s = oa.Shape
            bb = s.BoundBox
            c = s.CenterOfMass
            return {
                "object": a,
                "volume": float(s.Volume),
                "area": float(s.Area),
                "center_of_mass": [c.x, c.y, c.z],
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
            return {"error": "no active document"}
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
            return {"error": "no active document"}
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
                    edges.append({"name": f"Edge{i + 1}", "curve": curve,
                                  "length": float(e.Length),
                                  "center": [c.x, c.y, c.z]})
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
