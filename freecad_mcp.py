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

    def handle_get_screenshot(self, width=1024, height=768):
        """Save the active 3D view to a PNG and return it base64-encoded."""
        if not (Gui.ActiveDocument and Gui.ActiveDocument.ActiveView):
            return {"error": "no active 3D view to capture"}
        path = os.path.join(tempfile.gettempdir(), "freecad_mcp_screenshot.png")
        Gui.ActiveDocument.ActiveView.saveImage(path, int(width), int(height), "Current")
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
        return {"image_base64": data, "width": int(width), "height": int(height)}

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
