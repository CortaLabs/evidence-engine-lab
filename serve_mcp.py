"""Run the companion MCP server from any working directory."""
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


if __name__ == "__main__":
    runpy.run_module("evidence_engine.mcp_server", run_name="__main__")
