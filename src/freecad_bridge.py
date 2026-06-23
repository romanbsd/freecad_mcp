from typing import Any, Dict
import socket
import json
from mcp.server.fastmcp import FastMCP

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


async def send_to_freecad(command: Dict[str, Any]) -> Dict[str, Any]:
    """Send a command to FreeCAD and get the response.

    Wire format (both directions): 4-byte big-endian length prefix followed by
    that many bytes of UTF-8 JSON. This lets responses exceed any single recv()
    and keeps message boundaries unambiguous.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(30)
            sock.connect((FREECAD_HOST, FREECAD_PORT))
            payload = json.dumps(command).encode('utf-8')
            sock.sendall(len(payload).to_bytes(4, 'big') + payload)
            length = int.from_bytes(_recv_exactly(sock, 4), 'big')
            response = _recv_exactly(sock, length)
        return json.loads(response.decode('utf-8'))
    except Exception as e:
        return {"status": "error", "message": str(e)}

@mcp.tool()
async def send_command(command: str) -> str:
    """Send a command to FreeCAD and get document context information.
    
    Args:
        command: Command to execute in FreeCAD
    
    Returns:
        JSON string containing:
        - Command execution result
        - Current document information
        - Active objects and their properties
        - View state
    """
    command_data = {
        "type": "send_command",
        "params": {
            "command": command,
            "get_context": True
        }
    }
    result = await send_to_freecad(command_data)
    return json.dumps(result, indent=2)

@mcp.tool()
async def run_script(script: str) -> str:
    """Run an arbitrary Python script in FreeCAD context.
    
    Args:
        script: Python script to execute in FreeCAD
    
    Returns:
        JSON string containing the execution result
    """
    command = {
        "type": "run_script",
        "params": {
            "script": script
        }
    }
    result = await send_to_freecad(command)
    return json.dumps(result, indent=2)

if __name__ == "__main__":
    # Initialize and run the server
    mcp.run(transport='stdio')