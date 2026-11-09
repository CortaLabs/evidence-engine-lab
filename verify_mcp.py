"""Real SDK client/server verification against temporary synthetic evidence."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from evidence_engine import Store, Reader

ROOT = Path(__file__).resolve().parent


async def probe():
    with tempfile.TemporaryDirectory(prefix="evidence-mcp-") as scratch:
        root = Path(scratch)
        source_path = root / "source.jsonl"
        source_path.write_bytes((ROOT / "fixtures/codex.jsonl").read_bytes())
        source = dict(source_key="lab/demo/main", path=str(source_path), provider="codex",
                      session_id="demo-session", lane="main", adapter_id="codex_native_rollout",
                      adapter_version="1")
        database = root / "evidence.sqlite"
        store = Store(database, source); store.initialize(); store.ingest(source["source_key"], source_path)
        other_path = root / "other.jsonl"
        other_path.write_bytes(source_path.read_bytes())
        other = dict(source, source_key="lab/other/main", path=str(other_path))
        other_store = Store(database, other); other_store.initialize(); other_store.ingest(other["source_key"], other_path)
        foreign = Reader(database, other["source_key"]).search("tests passed")["results"][0]["ref"]
        config = root / "evidence.json"
        config.write_text(json.dumps(dict(database=str(database), source=source)))
        foreign_cwd = root / "foreign-cwd"
        foreign_cwd.mkdir()
        with sqlite3.connect(database) as db:
            before = list(db.execute("SELECT (SELECT count(*) FROM events),(SELECT sum(offset) FROM sources)"))[0]
        env = dict(os.environ, EVIDENCE_CONFIG=str(config))
        parameters = StdioServerParameters(command=sys.executable,
            args=[str(ROOT / "serve_mcp.py")], cwd=str(foreign_cwd), env=env)
        async with Client(stdio_client(parameters), read_timeout_seconds=20) as client:
            names = sorted(t.name for t in (await client.list_tools()).tools)
            assert names == ["evidence_activity", "evidence_context", "evidence_event", "evidence_search"]

            async def call(name, arguments):
                result = await client.call_tool(name, arguments)
                assert not result.is_error, name
                return result.structured_content

            search = await call("evidence_search", dict(query="tests passed"))
            ref = search["results"][0]["ref"]
            assert (await call("evidence_context", dict(ref=ref)))["anchor"]["ref"] == ref
            assert (await call("evidence_event", dict(ref=ref, text_limit=5)))["event"]["text_is_partial"]
            assert (await call("evidence_event", dict(ref="missing")))["status"] == "not_found"
            assert (await call("evidence_event", dict(ref=foreign)))["status"] == "not_found"
            activity = await call("evidence_activity", dict(limit=1))
            assert activity["next_page"]
            await call("evidence_activity", dict(after=activity["next_page"], limit=1))
            page = await call("evidence_search", dict(query="Process", limit=1))
            assert page["next_page"]
            await call("evidence_search", dict(query="Process", limit=1, after=page["next_page"]))
            bad = await client.call_tool("evidence_search", dict(query="x" * 1025))
            assert bad.is_error
            source_path.write_bytes(source_path.read_bytes() + b"unfinished")
            assert (await call("evidence_search", dict(query="tests passed")))["freshness"]["state"] == "pending"
            source_path.unlink()
            assert (await call("evidence_event", dict(ref=ref)))["freshness"]["state"] == "unavailable"
        with sqlite3.connect(database) as db:
            after = list(db.execute("SELECT (SELECT count(*) FROM events),(SELECT sum(offset) FROM sources)"))[0]
        assert before == after
        return dict(status="passed", tools=names, claim_ref=ref, readonly=True,
                    launch_script=str(ROOT / "serve_mcp.py"), foreign_cwd=str(foreign_cwd),
                    checks=["discovery", "search", "event expansion", "context", "activity",
                            "continuations", "missing", "foreign scope", "oversized query", "stale", "unavailable"])


if __name__ == "__main__":
    print(json.dumps(asyncio.run(probe()), indent=2))
