# Run and connect the evidence companion

The companion reads one configured JSONL source into a local SQLite database. The launcher scripts in this directory work when called by absolute path from another working directory. They pass all CLI and MCP behavior to the existing `evidence_engine` package.

## Prepare a local configuration

Use Python 3.11 or newer. The core CLI needs only the standard library and SQLite with FTS5. Copy the sample fixture and write a configuration that names both files by absolute path:

```sh
COMPANION="/absolute/path/to/companion"
PYTHON="/absolute/path/to/python"
WORK="/absolute/path/to/private-evidence-work"
mkdir -p "$WORK"
cp "$COMPANION/fixtures/codex.jsonl" "$WORK/source.jsonl"
```

Create `$WORK/evidence.json` with the following shape, substituting absolute paths for `$WORK`:

```json
{
  "database": "/absolute/path/to/private-evidence-work/evidence.sqlite",
  "source": {
    "source_key": "lab/demo/main",
    "path": "/absolute/path/to/private-evidence-work/source.jsonl",
    "provider": "codex",
    "session_id": "demo-session",
    "lane": "main",
    "adapter_id": "codex_native_rollout",
    "adapter_version": "1"
  }
}
```

Protect the source and database as private evidence. Do not point the sample configuration at a real session until you have reviewed and sanitized its contents.

## Use the CLI from any directory

`evidence_cli.py` accepts the same commands as the package CLI. Pass the configuration explicitly so the current directory does not affect config lookup:

```sh
"$PYTHON" "$COMPANION/evidence_cli.py" --config "$WORK/evidence.json" init
"$PYTHON" "$COMPANION/evidence_cli.py" --config "$WORK/evidence.json" ingest lab/demo/main
"$PYTHON" "$COMPANION/evidence_cli.py" --config "$WORK/evidence.json" sources
"$PYTHON" "$COMPANION/evidence_cli.py" --config "$WORK/evidence.json" search "tests passed"
"$PYTHON" "$COMPANION/evidence_cli.py" --config "$WORK/evidence.json" activity
```

Use a `ref` returned by `search` or `activity` with `event REF` or `context REF`. Results are JSON on stdout; bounded diagnostic messages go to stderr. Read operations use the same `Reader` implementation as the package CLI and MCP tools.

## Connect an MCP host

Install the optional, pinned SDK into an isolated environment. The absolute interpreter path in host configuration must be that environment's Python:

```sh
"$PYTHON" -m venv "$WORK/mcp-venv"
"$WORK/mcp-venv/bin/python" -m pip install -r "$COMPANION/requirements-mcp.txt"
```

Configure the host to start the server with:

- command: `"$WORK/mcp-venv/bin/python"`
- arguments: `["$COMPANION/serve_mcp.py"]`
- working directory: `"$COMPANION"`
- environment: `EVIDENCE_CONFIG` set to `"$WORK/evidence.json"`

For Codex, add a project-scoped entry to the trusted project's `.codex/config.toml` (Codex project configuration is loaded only for trusted projects). Substitute concrete absolute paths; TOML does not expand shell variables:

```toml
[mcp_servers.evidence_engine]
command = "/absolute/path/to/private-evidence-work/mcp-venv/bin/python"
args = ["/absolute/path/to/companion/serve_mcp.py"]
cwd = "/absolute/path/to/companion"
env = { EVIDENCE_CONFIG = "/absolute/path/to/private-evidence-work/evidence.json" }
```

For Claude Code, run this from the project where the server should be available. `--scope project` writes the project configuration; review the generated change before trusting it:

```sh
claude mcp add-json --scope project evidence-engine '{"type":"stdio","command":"/absolute/path/to/private-evidence-work/mcp-venv/bin/python","args":["/absolute/path/to/companion/serve_mcp.py"],"env":{"EVIDENCE_CONFIG":"/absolute/path/to/private-evidence-work/evidence.json"}}'
claude mcp list
```

The server exposes four read-only tools: `evidence_search`, `evidence_event`, `evidence_context`, and `evidence_activity`. It does not expose initialization, ingestion, arbitrary file paths, or SQL.

## Verify the connection

From the companion directory, run the focused suite and the real stdio probe with the MCP environment's Python:

```sh
"$WORK/mcp-venv/bin/python" -m unittest discover -s "$COMPANION/tests" -v
"$WORK/mcp-venv/bin/python" "$COMPANION/verify_mcp.py"
```

The probe creates disposable synthetic files, starts the absolute `serve_mcp.py` path with a temporary foreign working directory, discovers the four tools, exercises reads, confirms missing and out-of-scope references are rejected, checks pending and unavailable freshness, and compares SQLite row/checkpoint counts before and after. A successful probe proves the SDK client/server exchange for this source snapshot. It does not prove the host has loaded or trusted its configuration, nor that an agent actually invoked a tool in a real session. Confirm that separately in the host's MCP status view and with an explicit user invocation.

## Configuration references

- [Codex MCP configuration](https://developers.openai.com/codex/mcp) documents MCP setup and project configuration. Current Codex configuration supports stdio `command`, `args`, `cwd`, and `env`; the project config is applied only after the project is trusted.
- [Claude Code MCP documentation](https://docs.anthropic.com/en/docs/claude-code/mcp) describes project-scoped servers and the `claude mcp` command. The installed `claude mcp add-json --help` lists `--scope` values `local`, `user`, and `project`; the command above uses `project`.
