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

### 1. `send_command`

- **Description**: Executes a Python command string in the FreeCAD context, recomputes the active document, and (unless `get_context=False`) returns the current document context:
  - Document properties (name, filename, object count)
  - Per-object info (name, label, type, visibility, placement, and shape type/volume/area when present)
  - View/camera state (best-effort — omitted with an `error` note if the Coin/pivy bindings aren't loaded)

  Object enrichment is best-effort per object: one object that fails to introspect reports an `error` field instead of aborting the whole response.

### 2. `run_script`

- **Description**: Executes an arbitrary Python script in the FreeCAD context (namespace includes `App`, `Gui`, `doc`). Returns only success/error — no document context.

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

# Run a command and read the document context back
print(call({
    "type": "send_command",
    "params": {
        "command": "box = (App.ActiveDocument or App.newDocument()).addObject('Part::Box', 'MyBox'); box.Length = 20",
        "get_context": True,
    },
}))

# Run a script (no context returned)
print(call({"type": "run_script", "params": {"script": "doc.recompute()"}}))
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
