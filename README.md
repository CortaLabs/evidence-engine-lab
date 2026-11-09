# Evidence Engine Lab

**Unmaintained educational reference / tested snapshot.** This is a small teaching implementation informed by Austin / NoSuchAgency's work on AI Trace at CortaLabs. INVICTUS is the article's publication affiliation. This package is independent of private AI Trace, Council and Scribe. Users own their forks; no support or future compatibility is promised.

## Run

Use Python 3.11+ with SQLite FTS5. The core uses only the standard library. From this directory:

```sh
python -m unittest discover -s tests -v
python run_negative_controls.py
```

The optional MCP test skips when its SDK is absent; that is not MCP verification. Create a configuration for the included synthetic fixture from this source directory:

```sh
python - <<'PY'
import json
from pathlib import Path
root = Path.cwd()
config = {
    "database": str(root / "state" / "evidence.sqlite"),
    "source": {
        "source_key": "lab/demo/main",
        "path": str(root / "fixtures" / "codex.jsonl"),
        "provider": "codex",
        "session_id": "demo-session",
        "lane": "main",
        "adapter_id": "codex_native_rollout",
        "adapter_version": "1"
    }
}
(root / "evidence.json").write_text(json.dumps(config, indent=2) + "\n")
PY
```

Configuration names one approved absolute source path; no filesystem scan occurs. Check FTS5 before initialization with `python -c "import sqlite3; sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE probe USING fts5(text)'); print('FTS5 available')"`.

```sh
python -m evidence_engine --config evidence.json init
python -m evidence_engine --config evidence.json ingest lab/demo/main
python -m evidence_engine --config evidence.json sources
python -m evidence_engine --config evidence.json search "tests passed"
python -m evidence_engine --config evidence.json activity
```

Use a returned event reference with `event REF` and `context REF`. JSON goes to stdout; fixed diagnostics go to stderr. `python -m evidence_engine --help` lists the operator commands. The release archive and repository include `BUILD_CONTRACT.md` and `GUIDE.md` beside this README. The guide is generated from the canonical article; the contract defines required behavior. A separate guide-and-fixtures release ZIP excludes finished implementation code for agent-led construction.

## Adapt a source

Approve one file, inspect that installed harness's actual records and create sanitized fixtures before changing a mapping. Codex native rollout uses response_item payloads; its separate exec JSON stream is not supported. The adapter recognizes text messages, function call/output and custom call/output shapes. Custom JavaScript wrapper input remains literal and has unknown process semantics.

Claude's `claude_code_jsonl` adapter maps assistant/user message blocks, tool_use and tool_result IDs. Use provider `claude` and the fixture's native session identity. Claude support was tested with deterministic synthetic fixtures; the fresh installed-Claude capture was blocked by account quota before tool execution. Do not advertise verified live compatibility until you capture and test a real command/result from your installed version.

Both adapters emit the same event contract. Unknown and malformed complete records remain discoverable. A partial trailing line remains pending until its newline arrives. Configure a new source key for a new stream, even if it reuses a path. To adopt a moved source, change only its configured absolute path and ingest; the consumed-prefix digest must still match. Do not reuse identity fields across unrelated streams.

## Optional MCP

The separately pinned optional dependency is the official Python `mcp==2.3.0` SDK. It has its own transitive dependencies; install it in an isolated environment and preserve the resolver's license notices if redistributing dependencies. This source bundle does not vendor that SDK.

```sh
python -m venv .venv-mcp
.venv-mcp/bin/python -m pip install -r requirements-mcp.txt
.venv-mcp/bin/python verify_mcp.py
```

To register the server in your harness, use its documented stdio configuration: command is your absolute MCP-environment Python interpreter, arguments are the absolute path to `serve_mcp.py`, and host environment variable `EVIDENCE_CONFIG` is the absolute path to your approved configuration. The four tools are evidence_search, evidence_event, evidence_context and evidence_activity. They cannot initialize, ingest, discover files or accept paths/SQL. The absolute launcher works from another working directory. See [CONNECTIONS.md](CONNECTIONS.md) for runnable CLI examples, Codex project TOML, Claude project registration and connection checks. SDK transport verification and registration in your actual coding harness are distinct checks.

## Tested scope and limitations

Prepared on Linux with Python 3.11.13, SQLite 3.50.2 and optional MCP SDK 2.3.0. A newly generated Codex CLI 0.167.0 custom-call excerpt is included as `fixtures/codex_native.jsonl`; user/account IDs, paths and unrelated context were excluded. Other fixtures are synthetic. Claude Code 2.1.319 was present, but its live tool probe did not execute because of quota. No Windows or macOS run is claimed.

The initial Draft 2 archive could not be recovered through the available attachment reader. This is a replacement educational implementation, not an inspected extract of that archive or production AI Trace. Review the source and tests before adopting it.

Sources are trusted local append-only files, capped at 64 MiB. Prefix integrity verification rehashes historical consumed bytes, so it is linear in that prefix. Complete records are capped at 256 KiB. One ingestion call processes a bounded batch; check pending_bytes and repeat when needed. Metadata-only freshness is a clue, not a full integrity check. No adversarial snapshot guarantee, cross-file polling reconstruction, universal test classifier or hard CPU deadline is provided. Missing/ambiguous/unrecognized evidence remains unresolved.

Raw bytes and normalized text are retained in SQLite. Deleting the original source does not delete retained evidence; the reader discloses unavailable freshness. Hash references identify source bytes and parser interpretation, not authenticated authorship. There is no automatic secret scrubber, multi-user security boundary, retention scheduler, network service or automatic harness discovery. Use sanitized inputs, protect the database and test backups before retaining sensitive history. Read-only queries do not sandbox an agent with broader shell access.

## License and source

MIT licensed with Austin's approval. See LICENSE and NOTICE.md for scope and dependency provenance. Source repository: https://github.com/CortaLabs/evidence-engine-lab. This software export excludes the accompanying article, private review notes and INVICTUS image assets. The article is still under editorial review; software publication does not approve a forum post.
