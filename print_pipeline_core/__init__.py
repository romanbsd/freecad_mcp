"""Repository facade for the shared, dependency-free print pipeline contract."""
from pathlib import Path

# The canonical package is also bundled with the standalone Bambu MCP wheel.
__path__ = [str(Path(__file__).resolve().parents[1] / 'tools' / 'bambu-mcp' / 'print_pipeline_core')]
