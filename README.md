# FreeCAD MCP (Model Control Protocol)

## Overview

The FreeCAD MCP (Model Control Protocol) provides a simplified interface for interacting with FreeCAD through a server-client architecture. This allows users to execute commands and retrieve information about the current FreeCAD document and scene.

https://github.com/user-attachments/assets/5acafa17-4b5b-4fef-9f6c-617e85357d44
## Configuration

To configure the MCP server, you can use a JSON format to specify the server settings. Below is an example configuration:

```json
{
    "mcpServers": {
        "freecad": {
            "command": "C:\\ProgramData\\anaconda3\\python.exe",
            "args": [
                "C:\\Users\\USER\\AppData\\Roaming\\FreeCAD\\Mod\\freecad_mcp\\src\\freecad_bridge.py"
            ]
        }
    }
}
```

### Configuration Details

- **command**: The path to the Python executable that will run the FreeCAD MCP server. This can vary based on your operating system:
  - **Windows**: Typically, it might look like `C:\\ProgramData\\anaconda3\\python.exe` or `C:\\Python39\\python.exe`.
  - **Linux**: It could be `/usr/bin/python3` or the path to your Python installation.
  - **macOS**: Usually, it would be `/usr/local/bin/python3` or the path to your Python installation.

- **args**: An array of arguments to pass to the Python command. The first argument should be the path to the `freecad_bridge.py` script, which is responsible for handling the MCP server logic. Make sure to adjust the path according to your installation.

### Example for Different Operating Systems

#### Windows
```json
{
    "mcpServers": {
        "freecad": {
            "command": "C:\\ProgramData\\anaconda3\\python.exe",
            "args": [
                "C:\\Users\\USER\\AppData\\Roaming\\FreeCAD\\Mod\\freecad_mcp\\src\\freecad_bridge.py"
            ]
        }
    }
}
```

#### Linux
```json
{
    "mcpServers": {
        "freecad": {
            "command": "/usr/bin/python3",
            "args": [
                "/home/USER/.FreeCAD/Mod/freecad_mcp/src/freecad_bridge.py"
            ]
        }
    }
}
```

#### macOS
```json
{
    "mcpServers": {
        "freecad": {
            "command": "/usr/local/bin/python3",
            "args": [
                "/Users/USER/Library/Preferences/FreeCAD/Mod/freecad_mcp/src/freecad_bridge.py"
            ]
        }
    }
}
```

## Features

The FreeCAD MCP exposes two tools:

### 1. `execute`

- **Description**: Executes Python inside the running FreeCAD instance. The namespace has `App` (FreeCAD), `Gui` (FreeCADGui) and `doc` (active document).
- **Returning data**: assign to `result` or `print()` — both are captured and returned (`result` and `stdout`).
- **Safety/freshness**: the action runs inside one undo transaction (revert with a single Ctrl-Z; aborted automatically on error) and the document is recomputed afterwards.
- **`return_context`** (default `False`): when `True`, also returns a document summary:
  - Document properties (name, filename, object count)
  - Per-object info (name, label, type, visibility, placement, and shape type/volume/area when present) — best-effort per object; one that fails to introspect reports an `error` field instead of aborting the whole dump
  - View/camera state (best-effort — `error` note if the Coin/pivy bindings aren't loaded)

### 2. `get_screenshot`

- **Description**: Captures the active 3D view as a PNG (`width`/`height`, default 1024×768) and returns it as an image so the model can see the model.

### Example Usage

The server speaks a length-prefixed framing protocol: each message (both
directions) is a 4-byte big-endian length followed by that many bytes of
UTF-8 JSON. A minimal client:

```python
import socket
import json

def _recv(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(65536, n - len(buf)))
        if not chunk:
            raise ConnectionError("closed early")
        buf += chunk
    return bytes(buf)

def call(command):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect(('localhost', 9876))
        payload = json.dumps(command).encode('utf-8')
        s.sendall(len(payload).to_bytes(4, 'big') + payload)
        length = int.from_bytes(_recv(s, 4), 'big')
        return json.loads(_recv(s, length).decode('utf-8'))

# Execute code; return a value via `result` and read the document context back
print(call({
    "type": "execute",
    "params": {
        "code": (
            "doc = App.ActiveDocument or App.newDocument()\n"
            "box = doc.addObject('Part::Box', 'MyBox'); box.Length = 20\n"
            "result = box.Shape.Volume\n"
        ),
        "return_context": True,
    },
}))

# Capture the 3D view (image_base64 in the response)
print(call({"type": "get_screenshot", "params": {"width": 800, "height": 600}}).keys())
```

See `test_e2e.py` for a runnable version of this against a live FreeCAD.

## Installation

1. Clone the repository or download the files.
2. Place the `freecad_mcp` directory in your FreeCAD modules directory:
   - Windows: `%APPDATA%/FreeCAD/Mod/`
   - Linux: `~/.FreeCAD/Mod/`
   - macOS: `~/Library/Preferences/FreeCAD/Mod/`
3. Restart FreeCAD and select the "FreeCAD MCP" workbench from the workbench selector.

## Contributing

Feel free to contribute by submitting issues or pull requests. Your feedback and contributions are welcome!

## License

This project is licensed under the MIT License. See the LICENSE file for details.
