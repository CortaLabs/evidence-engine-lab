"""Binary checkpoints, retained records and atomic FTS publication."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

from .adapters import CodexAdapter, ClaudeAdapter


def canonical(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def reference(prefix, value):
    return prefix + digest(canonical(value).encode())


class SourceChanged(Exception):
    """The approved source is unavailable or no longer append-only."""


def prefix_hash(handle, count):
    handle.seek(0)
    h = hashlib.sha256()
    remaining = count
    while remaining:
        chunk = handle.read(min(65536, remaining))
        if not chunk:
            raise SourceChanged("Source truncated")
        h.update(chunk)
        remaining -= len(chunk)
    return h


def connect(path, *, readonly=False):
    path = Path(path).resolve()
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) if readonly else sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    if readonly:
        db.execute("PRAGMA query_only=ON")
    return db


SCHEMA = """
CREATE TABLE IF NOT EXISTS sources(
 source_key TEXT PRIMARY KEY, path TEXT NOT NULL, provider TEXT NOT NULL,
 session_id TEXT NOT NULL, lane TEXT NOT NULL, adapter_id TEXT NOT NULL,
 adapter_version TEXT NOT NULL, offset INTEGER NOT NULL DEFAULT 0,
 line_no INTEGER NOT NULL DEFAULT 0, prefix TEXT NOT NULL,
 observed_size INTEGER, observed_mtime INTEGER, unknown INTEGER NOT NULL DEFAULT 0,
 diagnostic INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS records(
 ref TEXT PRIMARY KEY, source_key TEXT NOT NULL REFERENCES sources(source_key),
 byte_start INTEGER NOT NULL, byte_end INTEGER NOT NULL, line_no INTEGER NOT NULL,
 sha256 TEXT NOT NULL, raw BLOB, text_retained INTEGER NOT NULL DEFAULT 1,
 UNIQUE(source_key,byte_start));
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT NOT NULL UNIQUE,
 record_ref TEXT NOT NULL REFERENCES records(ref),
 source_key TEXT NOT NULL REFERENCES sources(source_key),
 line_no INTEGER NOT NULL, part INTEGER NOT NULL, adapter_id TEXT NOT NULL,
 adapter_version TEXT NOT NULL, observed_at TEXT NOT NULL, occurred_at TEXT,
 kind TEXT NOT NULL, role TEXT, tool TEXT, call_id TEXT, text TEXT NOT NULL,
 extra_json TEXT NOT NULL, classification TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS source_order ON events(source_key,line_no,part);
CREATE INDEX IF NOT EXISTS pair_identity ON events(source_key,call_id,kind);
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(text,content='events',content_rowid='id');
CREATE TRIGGER IF NOT EXISTS event_insert AFTER INSERT ON events BEGIN
 INSERT INTO events_fts(rowid,text) VALUES(new.id,new.text); END;
CREATE TRIGGER IF NOT EXISTS event_delete AFTER DELETE ON events BEGIN
 INSERT INTO events_fts(events_fts,rowid,text) VALUES('delete',old.id,old.text); END;
CREATE TRIGGER IF NOT EXISTS event_update AFTER UPDATE OF text ON events BEGIN
 INSERT INTO events_fts(events_fts,rowid,text) VALUES('delete',old.id,old.text);
 INSERT INTO events_fts(rowid,text) VALUES(new.id,new.text); END;
"""


class Store:
    def __init__(self, database, source, adapter=None):
        self.database = Path(database).resolve()
        self.source = dict(source)
        adapters = {"codex_native_rollout": CodexAdapter, "claude_code_jsonl": ClaudeAdapter}
        for key in ("source_key", "path", "provider", "session_id", "lane", "adapter_id", "adapter_version"):
            if not isinstance(self.source.get(key), str) or not self.source[key] or len(self.source[key]) > (4096 if key == "path" else 256):
                raise ValueError("Missing source identity")
        if not Path(self.source["path"]).is_absolute():
            raise ValueError("Source path must be absolute")
        if self.source["adapter_id"] not in adapters:
            raise ValueError("Unsupported adapter")
        self.adapter = adapter or adapters[self.source["adapter_id"]]()
        if (self.source["adapter_id"], self.source["adapter_version"]) != (
                self.adapter.adapter_id, self.adapter.adapter_version):
            raise ValueError("Unsupported adapter/version")
        expected_provider = "codex" if self.adapter.adapter_id == "codex_native_rollout" else "claude"
        if self.source["provider"] != expected_provider:
            raise ValueError("Unsupported provider")

    def initialize(self):
        self.database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.database, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        with connect(self.database) as db:
            db.executescript(SCHEMA)
            old = db.execute("SELECT * FROM sources WHERE source_key=?", (self.source["source_key"],)).fetchone()
            identity = ("provider", "session_id", "lane", "adapter_id", "adapter_version")
            if old and any(old[k] != self.source[k] for k in identity):
                raise ValueError("Conflicting source identity")
            if not old:
                db.execute("INSERT INTO sources(source_key,path,provider,session_id,lane,adapter_id,adapter_version,prefix) VALUES(?,?,?,?,?,?,?,?)",
                           tuple(self.source[k] for k in ("source_key", "path", *identity)) + (digest(b""),))

    def ingest(self, source_key, path, *, max_records=500, max_bytes=1_048_576):
        if source_key != self.source["source_key"]:
            raise ValueError("Source outside configured scope")
        path = Path(path)
        if not path.is_absolute() or str(path) != self.source["path"]:
            raise ValueError("Path outside host configuration")
        if not 1 <= max_records <= 10000 or not 1 <= max_bytes <= 16_777_216:
            raise ValueError("Invalid ingestion bounds")
        with connect(self.database, readonly=True) as db:
            row = db.execute("SELECT * FROM sources WHERE source_key=?", (source_key,)).fetchone()
            if row is None:
                raise ValueError("Source not initialized")
            start = dict(row)
        planned, offset, line_no = [], start["offset"], start["line_no"]
        try:
            with path.open("rb") as handle:
                before = os.fstat(handle.fileno())
                if before.st_size > 64 * 1024 * 1024:
                    raise ValueError("Source exceeds lab size limit")
                h = prefix_hash(handle, offset)
                if h.hexdigest() != start["prefix"]:
                    raise SourceChanged("Source prefix changed")
                used = 0
                while len(planned) < max_records:
                    raw = handle.readline(262145)
                    if len(raw) > 262144:
                        raise ValueError("Oversized record")
                    if not raw.endswith(b"\n"):
                        break
                    if used + len(raw) > max_bytes:
                        if not planned:
                            raise ValueError("Byte budget cannot fit next complete record")
                        break
                    events = self.adapter.parse_line(raw, source_key=source_key, line_no=line_no + 1)
                    if any(len(e.get(k) or "") > 256 for e in events for k in ("tool", "call_id", "occurred_at")):
                        raise ValueError("Event metadata exceeds lab bounds")
                    if any(e["kind"] == "session" and e["extra"].get("session_id") != start["session_id"] for e in events):
                        raise ValueError("Conflicting native session metadata")
                    line_no += 1
                    planned.append((offset, line_no, raw, events))
                    offset += len(raw)
                    used += len(raw)
                    h.update(raw)
                stat = os.fstat(handle.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ino, before.st_dev) != (
                        stat.st_size, stat.st_mtime_ns, stat.st_ino, stat.st_dev):
                    raise SourceChanged("Source changed during batch planning")
        except OSError as exc:
            raise SourceChanged("Source unavailable") from exc
        now = datetime.now(timezone.utc).isoformat()
        db = connect(self.database)
        try:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT offset,prefix,line_no FROM sources WHERE source_key=?", (source_key,)).fetchone()
            if tuple(current) != (start["offset"], start["prefix"], start["line_no"]):
                raise SourceChanged("Checkpoint advanced; re-plan required")
            counts = {"unknown": 0, "diagnostic": 0}
            for pos, number, raw, events in planned:
                raw_hash = digest(raw)
                rr = reference("record:v1:", [source_key, pos, raw_hash])
                db.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?,1)",
                           (rr, source_key, pos, pos + len(raw), number, raw_hash, raw))
                for part, e in enumerate(events):
                    er = reference("event:v1:", [rr, self.adapter.adapter_id, self.adapter.adapter_version, part])
                    db.execute("INSERT INTO events(ref,record_ref,source_key,line_no,part,adapter_id,adapter_version,observed_at,occurred_at,kind,role,tool,call_id,text,extra_json,classification) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (er, rr, source_key, number, part, self.adapter.adapter_id,
                                self.adapter.adapter_version, now, e["occurred_at"], e["kind"],
                                e["role"], e["tool"], e["call_id"], e["text"], canonical(e["extra"]), e["classification"]))
                    if e["kind"] in counts:
                        counts[e["kind"]] += 1
            db.execute("UPDATE sources SET path=?,offset=?,line_no=?,prefix=?,observed_size=?,observed_mtime=?,unknown=unknown+?,diagnostic=diagnostic+? WHERE source_key=?",
                       (str(path), offset, line_no, h.hexdigest(), stat.st_size, stat.st_mtime_ns,
                        counts["unknown"], counts["diagnostic"], source_key))
            self._before_commit(db)
            db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()
        return {"records_ingested": len(planned), "offset": offset, "line_no": line_no,
                "pending_bytes": max(0, stat.st_size - offset), "integrity_check": "prefix_verified"}

    def _before_commit(self, db):
        """No-op seam for interruption tests; never configured by clients."""
