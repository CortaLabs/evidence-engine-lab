"""Optional SDK 2.3.0 stdio adapter. Configuration is host-owned."""
import os
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .__main__ import load_config


def safe_read(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except (ValueError, TypeError):
        raise ToolError("Invalid reader arguments") from None
    except Exception:
        raise ToolError("Evidence read unavailable") from None


def build_server(reader):
    server = MCPServer("evidence-engine")

    @server.tool(structured_output=True)
    def evidence_search(query: str, limit: int = 20, after: str | None = None) -> dict[str, object]:
        return safe_read(reader.search, query, limit=limit, after=after)

    @server.tool(structured_output=True)
    def evidence_event(ref: str, text_offset: int = 0, text_limit: int = 4096) -> dict[str, object]:
        return safe_read(reader.event, ref, text_offset=text_offset, text_limit=text_limit)

    @server.tool(structured_output=True)
    def evidence_context(ref: str, before: int = 5, after: int = 5, text_limit: int = 512) -> dict[str, object]:
        return safe_read(reader.context, ref, before=before, after=after, text_limit=text_limit)

    @server.tool(structured_output=True)
    def evidence_activity(after: str | None = None, limit: int = 100) -> dict[str, object]:
        return safe_read(reader.activity, after=after, limit=limit)

    return server


if __name__ == "__main__":
    _, _, configured_reader = load_config(os.environ["EVIDENCE_CONFIG"])
    build_server(configured_reader).run(transport="stdio")
