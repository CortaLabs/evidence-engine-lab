# Evidence Engine minimal-core build contract

**Status:** Public normative build specification, revision 3.2. Supplied alongside the article so an independent builder needs no private design notes. Verification receipts identify their exact revision. Revision 3.2 clarifies result envelopes and executable-command classification after the independent build exposed underspecified fields.

This document defines one local, single-user evidence index for harness records. A builder may choose internal modules and SQL details, but the externally visible behavior and invariants below are fixed. The article is a guide to why and how to build it; this contract is the implementation authority when an example and a rule differ.

## 1. Outcome and prerequisites

Build a small Python package and CLI that can ingest an explicitly approved append-only JSONL stream, retain original records, normalize known events, index event text, and answer four read questions: search, expand one event, retrieve its local context, and reconstruct tool activity. Every answer must preserve provenance, disclose incomplete coverage, and avoid turning an interpretation into an observation.

The builder needs Python 3.11+, SQLite compiled with FTS5, and one synthetic fixture. The core uses the standard library. An MCP adapter is optional and must be a thin wrapper over the same reader API. It must not be required for the core or CLI.

This first slice supports one configured source stream at a time. It does not discover home directories, collect live sessions, authorize retries, manage task state, make model calls, or publish results to a remote service.

## 2. Required files and public Python interface

Use one package boundary named `evidence_engine` (or preserve an existing equivalent package if the implementation environment already supplies one). The required public symbols are:

```python
from pathlib import Path
from typing import Any, Protocol

class Adapter(Protocol):
    adapter_id: str
    adapter_version: str
    def parse_line(self, raw: bytes, *, source_key: str,
                   line_no: int) -> list[dict[str, Any]]: ...

class Store:
    def __init__(self, database: Path, source: dict, adapter=None) -> None: ...
    def initialize(self) -> None: ...
    def ingest(self, source_key: str, path: Path, *,
               max_records: int = 500, max_bytes: int = 1_048_576) -> dict: ...

class Reader:
    def __init__(self, database: Path, source_key: str) -> None: ...
    def search(self, query: str, *, limit: int = 20,
               after: str | None = None) -> dict: ...
    def event(self, ref: str, *, text_offset: int = 0,
              text_limit: int = 4096) -> dict: ...
    def context(self, ref: str, *, before: int = 5, after: int = 5,
                text_limit: int = 512) -> dict: ...
    def activity(self, *, after: str | None = None,
                 limit: int = 100) -> dict: ...
```

The protocol is descriptive: implementations may use dataclasses and typed result models internally, but the serialized result keys and meanings in this contract are stable. The adapter receives exactly one complete raw JSONL line. It must not read files or query the database.

Run from the source directory using `python -m evidence_engine --config evidence.json COMMAND`; no installation is needed. Store.initialize creates the database parent directory and schema, then registers its source. Reader opens an existing database in SQLite read-only mode; it never initializes or migrates it. An adapter argument is for owner-controlled Python composition, never a model-facing parameter.

The JSON configuration is `{"database":"/absolute/path/state/evidence.sqlite","source":SOURCE_OBJECT}`. Reject relative paths, missing identities, unknown adapters and empty source keys. There is no home-directory fallback or recursive scan. The CLI defaults to evidence.json in its working directory. Adapter IDs are `codex_native_rollout` and `claude_code_jsonl`, version `1`. CLI Reader scope is the configured source key.

Store.ingest must require its path argument to equal the configured absolute source path. To approve a relocation, construct Store with a new configuration path for the same stable identity; ingestion must still verify the consumed prefix before adopting it. A caller cannot substitute an unconfigured file. JSON must reject non-finite constants. Adapter timestamps come from the outer record's optional timestamp, not import time or an invented nested field.

Lab bounds: source at most 64 MiB; complete line at most 256 KiB including newline; default batch at most 500 complete lines and 1 MiB. A next line exceeding the remaining batch budget stays pending. If no line fits the supplied byte budget, report an invalid limit rather than succeeding forever without progress. Reject nonpositive ingestion limits. Cap queries at 1,024 characters, event slices at 4,096 characters, search previews at 512 characters and serialized responses at 64 KiB UTF-8. Under the response cap reduce rows/neighbors first, protect the context anchor, disclose shortened text and expose an expansion offset. Return an explicit size error if the minimum envelope cannot fit. These are output/data bounds, not hard CPU deadlines.

## 3. Source configuration and provenance

An operator explicitly registers a source with:

```json
{
  "source_key": "project/session/main",
  "path": "/absolute/approved/path/session.jsonl",
  "provider": "codex",
  "session_id": "demo-session",
  "lane": "main",
  "adapter_id": "codex_native_rollout",
  "adapter_version": "1"
}
```

`source_key` is a stable logical identifier, unique within the database and never inferred from a display name or path. It binds one provider, session and lane. A moved file may retain the key only when its already-consumed prefix matches. A new stream reusing a path needs a new key. Reject source configuration with missing identity fields, a non-absolute path, or conflicting identity for an existing key.

Keep three times distinct where available: source event time (`occurred_at`), collector observation time (`observed_at`), and database insertion/order. Never substitute import time for an absent event time. A raw record's author/role and provider-supplied IDs are observations, not authentication.

## 4. Normalized event schema

Each adapter output is one event object. Required keys and allowed values:

```json
{
  "kind": "message|tool_call|tool_result|session|unknown|diagnostic",
  "role": "system|developer|user|assistant|tool|null",
  "text": "string",
  "tool": "string|null",
  "call_id": "string|null",
  "occurred_at": "RFC3339 string|null",
  "extra": {},
  "classification": "recognized|unsupported|malformed"
}
```

The store adds `ref`, `record_ref`, `source_key`, `line_no`, `part`, `adapter_id`, `adapter_version`, and `observed_at`. `part` is zero-based within a raw record. `extra` is a JSON object for bounded, source-derived fields needed to interpret the event; it must not contain secrets, arbitrary paths discovered by the model, or a duplicate of the full record.

Unknown record types produce an `unknown` event with `classification=unsupported`, a bounded safe summary, and a pointer to retained raw bytes. A malformed complete line produces a `diagnostic` event with `classification=malformed`; ingestion continues with the next complete line. Do not silently discard either category. Incomplete final lines are not malformed and are not consumed. Oversized records fail the batch without advancing its checkpoint.

Both mappings below must have public deterministic fixtures. Synthetic examples are labeled synthetic; fixture tests do not establish live compatibility. All records must be JSON objects. Invalid JSON/UTF-8 and wrong required field types in known shapes become diagnostics. Optional string timestamp supplies occurred_at; otherwise it is null.

- Codex session_meta reads payload.id as native session identity; a conflict with configured session_id rejects the batch. For response_item, inspect payload.type.
- Codex message requires role and a content list. Each input_text/output_text/text block with string text emits one message event. Unsupported blocks emit unknown events while preserving recognized neighboring blocks.
- Codex function_call requires string name, call_id and arguments (JSON encoded as a string); emit tool_call with literal arguments. function_call_output requires string call_id and output; emit tool_result.
- Codex custom_tool_call requires string name, call_id and input; preserve input literally. custom_tool_call_output requires call_id and output as string or list of text blocks. Combine supported block texts with newlines into one tool_result; unknown blocks remain discoverable and make completeness uncertain. Do not execute or interpret JavaScript wrapper input.
- Claude assistant/user records read optional sessionId (reject conflicts) and nested message.role/content. String content emits one message. In lists, text emits a message; tool_use requires string id/name and object input and emits tool_call with compact JSON input; tool_result requires tool_use_id and string or text-block-list content and emits one tool_result. Preserve is_error in extra when supplied. Unsupported blocks emit unknown events. Other record types remain unknown.

Pair only by recorded call ID inside one source key. The new Codex native custom-call capture establishes those shapes only. Claude has synthetic adapter tests until a real installed-harness tool session passes separately. Never infer format support from filename extensions or equate the separate codex exec JSON stream with its native rollout.

Text blocks in the supported Codex shapes have type text, input_text or output_text and a string text field. Completeness is source data: when a Codex result payload or Claude tool_result block supplies a boolean truncated field, preserve it as normalized extra.truncated. If an output list includes unsupported blocks, set extra.truncated=true on its recognized result preview as well as emitting unknown coverage. The activity classifier must inspect that stored flag even when its database row type is not a Python dict. A truncated source result cannot establish reported_pass or reported_failure from its preview. Reader text_is_partial describes a bounded view and is separate from this source completeness flag.

## 5. Record identity and retained bytes

Store each complete raw line once, byte-for-byte, with its source key, start/end byte offsets, line number, SHA-256 digest, and retained-content state. Store events as references to that record. The default local mode retains raw bytes. If a later retention policy removes them, keep the record metadata and mark `text_retained=false`; never present a placeholder as original text.

Define deterministic opaque references as:

```text
record:v1:<sha256(canonical-json([source_key, byte_start, raw_sha256]))>
event:v1:<sha256(canonical-json([record_ref, adapter_id, adapter_version, part]))>
```

Canonical JSON means UTF-8, compact separators, stable array order, and no non-finite numbers. Identical raw bytes at different offsets are distinct occurrences. Reimporting the same source with the same key and bytes recreates the same references. References identify retained data; hashes do not authenticate the author or prove tamper resistance.

## 6. Storage and atomic ingestion

Use SQLite tables for `sources`, `records`, and `events`, plus an FTS5 index over event text. Enforce uniqueness for `(source_key, byte_start)` in records and `ref` in both record/event tables. Add indexes for source order and `(source_key, call_id, kind)`. Foreign keys must be enabled on each connection. All user values are parameterized.

The source row stores path, provider/session/lane, adapter identity/version, consumed byte offset, consumed line number, consumed-prefix digest, last observed file metadata, and the count of unknown/diagnostic events. The FTS index must remain transactionally aligned with events; triggers or explicit statements are acceptable if they commit with event rows.

Ingestion is bounded by both `max_records` and `max_bytes`. Read binary data from the committed byte offset and plan only newline-terminated records. Compute the next byte offset, line count, and prefix digest before opening the write transaction. Then:

1. `BEGIN IMMEDIATE`.
2. Re-read and compare the stored source checkpoint to the plan's starting checkpoint. If another writer advanced it, abort and re-plan.
3. Insert raw records, normalized events, and their FTS entries.
4. Update the source checkpoint and observed metadata in the same transaction.
5. Commit. Any error rolls back the entire batch and preserves the original exception.

An independent reader must observe either the previous complete batch or the new complete batch, never newly advanced progress without searchable events. A crash before commit must leave the batch eligible for re-ingestion without duplicates.

Before appending, verify that all previously consumed bytes still match the stored digest. Reject truncation, same-length edits, or a changed prefix; do not overwrite retained evidence. A relocated file can be adopted only if the consumed prefix matches. The initial implementation may hash the whole consumed prefix for integrity, but must disclose that cost. Metadata-only freshness checks are not integrity checks.

## 7. Search and result contract

Search uses FTS5 over normalized event text. Treat the input as a literal phrase: escape it for FTS syntax, bind it as a SQL parameter, and do not expose SQL or arbitrary FTS expressions. Return newest/oldest ordering consistently (choose ascending source ingestion order for this implementation and preserve it across pages). Clamp `limit` to 1..100.

Every search response has:

```json
{
  "results": [{"ref":"event:v1:…","record_ref":"record:v1:…",
    "source_key":"…","kind":"…","role":null,"tool":null,
    "call_id":null,"occurred_at":null,"text":"…",
    "text_is_partial":false,"classification":"recognized"}],
  "next_page": null,
  "scope": ["project/session/main"],
  "freshness": {"state":"current|pending|unavailable|unknown",
    "check":"metadata_only|prefix_verified|not_checked",
    "pending_bytes":0},
  "coverage": {"records":0,"unknown":0,"diagnostic":0}
}
```

The page continuation is opaque, binds the query, scope, ordering and high-water event ID, and cannot broaden the configured source scope. It must be stable for an append-only snapshot: later inserts do not enter an already-started page sequence. A cursor is not an access credential. A response limit may shorten text but must set `text_is_partial=true`; disclose truncation and provide an offset-based expansion route.

An empty result means no match in the indexed scope at the reported freshness boundary. It does not mean the source never contained a match. If unconsumed bytes exist, report `pending`; if the source is missing, report `unavailable`; if freshness was not checked, say so.

## 8. Event, context and activity contracts

### `event(ref)`

Return the exact event plus record reference, source identity, source position, adapter version, classification, retained-text state, full text or the requested bounded slice, and whether more text remains. An unknown reference returns a stable `not_found` result without leaking another source's data.

The serialized envelope is `{"status":"ok","event":EVENT,"scope":[SOURCE_KEY],"freshness":FRESHNESS,"coverage":COVERAGE}`; it may contain extra disclosed metadata. EVENT includes ref, record_ref, source_key, line_no, part, adapter_id, adapter_version, observed_at, occurred_at, kind, role, tool, call_id, text, classification, extra, text_offset, text_is_partial and next_text_offset (integer or null). A not-found envelope uses status not_found and no foreign event. Keep record-byte retention distinct from normalized-text retention if implementing deletion policies; this core has no deletion API.

### `context(ref)`

Return the requested event in a dedicated `anchor` field and nearby events separately ordered by source position. The anchor's identity is mandatory and cannot be dropped to meet a response budget. Clamp `before` and `after` to 0..20. Each event has its own text truncation flag. If even the anchor reference and minimum valid envelope cannot fit, return an explicit size error.

The envelope has status, anchor, before (ascending array of earlier events), after (ascending array of later events), scope, freshness and coverage. Count neighbors within the source, not by subtracting global database IDs: interleaved ingestion of another source cannot remove this source's neighbors. A size error has status size_error and preserves anchor_ref. No row may be silently omitted while continuing past it in a later page.

### `activity()`

Group tool calls and results within one source by exact `call_id`. A call may have zero, one, or multiple candidate results; classify these as `pending`, `complete`, or `ambiguous`. Never pair by timestamp, textual similarity, or tool name. Keep observed events distinct from derived interpretations.

For these append-only ordered source adapters, a candidate result must occur after its call in normalized source order (line/part, equivalently ingestion ID within this source). A prior same-ID result is retained but cannot settle a later call. If no following result exists, that call stays pending; a following result ingested later can complete it. Do not silently extend this ordered-source rule to adapters whose native records can arrive out of order.

The envelope has results (an array), next_page, scope, freshness, coverage and snapshot_high_water. Each activity row has call (an EVENT view including call_id/ref), results (candidate EVENT views including ref), pair_state and interpretation. Duplicate call records sharing one ID are ambiguous even if only one result exists. A bounded candidate preview discloses that cap; it cannot turn multiple candidates into a unique result. When shrinking a page to meet the response cap, its continuation resumes after the last returned call, never after a dropped call. Search uses the same rule for dropped rows.

For this minimal core, classify a test result as `reported_pass` only when the call is an explicitly recognized direct pytest command, one uniquely paired result contains a supported terminal exit receipt of zero and a recognized summary with a positive pass count and no failures/errors, and the output is not marked truncated. Use `reported_failure` for a recognized nonzero exit or failures/errors. Everything else is `unknown`, `pending`, or `ambiguous`. This is a classification of recorded output, not independent proof that a test process or source is authentic.

Recognized command tools are exec_command (JSON arguments key cmd) and Bash (JSON arguments key command). Parse the command conservatively: direct pytest or python/python3 -m pytest only; reject shell separators, pipelines, redirections, command substitutions, newlines and collection-only flags such as --collect-only or --co before interpreting output. Tool receipt framing must establish exactly one terminal `Process exited with code N` line; unfamiliar or contradictory framing stays unknown. A pytest-like phrase inside a file read, wrapper program or arbitrary output is never a new execution. No recognized terminal receipt means no reported pass or failure, even if text mentions failures. Recognized nonzero terminal exit yields reported_failure for this narrow direct-test operation. Support further harness receipt formats only through explicit fixtures and contract changes.

Activity pagination uses an event high-water boundary and an opaque continuation. If a result arrives after the boundary, a later activity query may change the derived status; disclose the snapshot boundary. Do not rewrite older observations to make the report appear final.

## 9. CLI contract

Provide `python -m evidence_engine --config evidence.json init` and subcommands `ingest SOURCE_KEY`, `sources`, `search QUERY`, `event REF`, `context REF`, and `activity` with that same module prefix. An installed alias is optional; no printed command relies on one. Search supports --limit/--after; event supports --text-offset/--text-limit; context supports --before/--after (neighbor counts)/--text-limit; activity supports --limit/--after (opaque continuation); ingest supports --max-records/--max-bytes. Output JSON to stdout and diagnostics to stderr. Source discovery lists configured sources only. No model-facing method accepts a filesystem path, database path, SQL or a source outside its configured scope.

Exit codes: `0` success; `2` invalid arguments/configuration; `3` source unavailable or changed; `4` storage/transaction failure; `5` internal error with a sanitized message. Do not print credentials, raw environment values, or full untrusted record contents to diagnostics.

## 10. Optional MCP adapter

If provided, register exactly the four read operations `evidence_search`, `evidence_event`, `evidence_context`, and `evidence_activity`. They call `Reader` directly and preserve its result fields. Database path, source allowlist, retention policy and resource limits come from host configuration, never tool arguments. There is no MCP ingestion, initialization, source-discovery, or arbitrary SQL tool.

The adapter must fail closed if the SDK cannot preserve the required input/output fields. It must not weaken bounds, scope, freshness, truncation, or not-found behavior to satisfy transport constraints. MCP integration tests are additional to the required core tests and must use an actual client/server round trip.

## 11. Required verification

Use only synthetic temporary files and databases. The required tests must prove:

- adapter behavior for every supported shape, unknown types, malformed complete lines, conflicting session metadata, and incomplete final lines;
- deterministic record/event references, including duplicate content at distinct byte offsets and re-ingestion without duplicate rows;
- literal search quoting, parameterized data handling, scope enforcement, stable pagination, and disclosure of pending/unavailable freshness;
- event expansion, anchor-preserving context, per-event truncation, unknown references, and retention state;
- call/result pairing by source and call ID, duplicate/missing results, and conservative test-result classification;
- bounded ingestion by record and byte limits; append recognition; prefix rewrite/truncation rejection; and relocation with a matching prefix;
- injected failure and process interruption before commit, verified from a separate reader connection to show FTS, event rows and checkpoint remain atomic;
- CLI JSON/stdout and diagnostic/stderr separation, exit codes, and no writes through reader operations.

Run the package's targeted test modules, direct neighbor modules, and import smoke checks for every changed module. Run a separate MCP round trip only if the adapter is implemented. Do not describe tests as passed unless the builder records the command, environment, and result.

## 12. Acceptance and explicit limits

The builder is ready for independent review when the public API and serialized schemas above are implemented, all required checks pass, the synthetic end-to-end path demonstrates `ingest → search → context/event → activity`, and the handoff lists unsupported formats, freshness limits, retention behavior, and exact verification evidence.

The minimal core does not claim live compatibility with Codex, Claude, or other installed harness versions; authenticated authorship; tamper-proof custody; complete child-process reconstruction; hard process-level timeouts; multi-user access control; billing authority; project/task ownership; semantic/vector search; distributed ingestion; or production readiness. These need separate fixtures, contracts, and evidence before they can be claimed.
