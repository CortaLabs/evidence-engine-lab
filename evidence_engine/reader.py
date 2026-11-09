"""Read-only scoped queries with snapshot-bound pagination."""
import base64
from contextlib import contextmanager
import json
from pathlib import Path
import re
import shlex

from .store import canonical, connect


def response_size(value):
    return len(json.dumps(value, ensure_ascii=True).encode("utf-8"))


def clamp(value, low, high):
    if not isinstance(value, int):
        raise ValueError("Expected integer bound")
    return min(high, max(low, value))


def sliced(row, offset=0, limit=4096):
    result = dict(row)
    result.pop("id", None)
    result["extra"] = json.loads(result.pop("extra_json"))
    text = result["text"]
    result["text"] = text[offset:offset + limit]
    result["text_offset"] = offset
    result["text_is_partial"] = offset > 0 or offset + limit < len(text)
    result["more_text"] = offset + limit < len(text)
    result["next_text_offset"] = offset + len(result["text"]) if result["more_text"] else None
    result["next_text_offset"] = offset + len(result["text"]) if result["more_text"] else None
    result["text_retained"] = bool(result.get("text_retained", True))
    return result


def cursor(value):
    return base64.urlsafe_b64encode(canonical(value).encode()).decode()


def verdict(call, output):
    """Interpret only direct pytest shell calls and one narrow terminal receipt."""
    if call["tool"] not in {"exec_command", "Bash"}:
        return "unknown"
    try:
        args = json.loads(call["text"])
        command = args.get("cmd") if call["tool"] == "exec_command" else args.get("command")
        if not isinstance(command, str) or any(c in command for c in "\n;&|`$<>"):
            return "unknown"
        argv = shlex.split(command)
    except (ValueError, AttributeError):
        return "unknown"
    if not (argv[:1] == ["pytest"] or argv[:3] in (["python", "-m", "pytest"], ["python3", "-m", "pytest"])):
        return "unknown"
    if any(arg in {"--collect-only", "--co", "--help", "-h", "--version"} or arg.startswith("--collect-only=") for arg in argv):
        return "unknown"
    text = output["text"]
    if json.loads(output["extra_json"]).get("truncated") or re.search(r"truncat(?:ed|ion)", text, re.I):
        return "unknown"
    exits = re.findall(r"^Process exited with code (-?\d+)\s*$", text, re.M)
    if len(exits) != 1:
        return "unknown"
    if int(exits[0]) != 0 or re.search(r"\b[1-9]\d* (?:failed|errors?)\b", text):
        return "reported_failure"
    # The supported summary must be the terminal output line, not quoted history.
    terminal = text.strip().splitlines()[-1] if text.strip() else ""
    summary = re.fullmatch(r"(?:=+\s*)?(\d+) passed(?:, \d+ (?:skipped|deselected|warnings?))* in [0-9.]+s(?:\s*=+)?", terminal)
    return "reported_pass" if summary and int(summary[1]) > 0 else "unknown"


class Reader:
    def __init__(self, database, source_key):
        self.database = Path(database).resolve()
        if not isinstance(source_key, str) or not source_key or len(source_key) > 256:
            raise ValueError("Missing configured scope")
        self.source_key = source_key

    @contextmanager
    def _snapshot(self):
        db = connect(self.database, readonly=True)
        try:
            db.execute("BEGIN")
            if not db.execute("SELECT 1 FROM sources WHERE source_key=?", (self.source_key,)).fetchone():
                raise ValueError("Unregistered configured scope")
            yield db
        finally:
            db.close()

    def _boundary(self, db, after, operation, query=""):
        high = db.execute("SELECT coalesce(max(id),0) AS high FROM events WHERE source_key=?", (self.source_key,)).fetchone()["high"]
        if after is None:
            return 0, high
        try:
            if not isinstance(after, str) or len(after) > 4096:
                raise ValueError()
            value = json.loads(base64.urlsafe_b64decode(after.encode()))
            if value[:4] != ["cursor-v1", operation, self.source_key, query] or len(value) != 6:
                raise ValueError()
            last, boundary = value[4:]
            if type(last) is not int or type(boundary) is not int or not 0 <= last <= boundary <= high:
                raise ValueError()
            return last, boundary
        except (ValueError, TypeError, UnicodeError, IndexError):
            raise ValueError("Invalid continuation for this query/scope") from None

    def _next(self, operation, query, last, high):
        return cursor(["cursor-v1", operation, self.source_key, query, last, high])

    def _envelope(self, db, high=None):
        source = db.execute("SELECT * FROM sources WHERE source_key=?", (self.source_key,)).fetchone()
        try:
            stat = Path(source["path"]).stat()
            pending = max(0, stat.st_size - source["offset"])
            state = "pending" if pending else (
                "current" if stat.st_size == source["offset"] and stat.st_mtime_ns == source["observed_mtime"] else "unknown")
        except OSError:
            state, pending = "unavailable", 0
        return {"scope": [self.source_key], "freshness": {"state": state, "check": "metadata_only", "pending_bytes": pending},
                "coverage": {"records": source["line_no"], "unknown": source["unknown"], "diagnostic": source["diagnostic"]},
                "snapshot_high_water": high,
                "coverage_basis": "indexed source at read time; page membership uses snapshot_high_water"}

    def search(self, query, *, limit=20, after=None):
        if not isinstance(query, str) or not query.strip() or len(query) > 1024:
            raise ValueError("Expected nonempty query up to 1024 characters")
        limit = clamp(limit, 1, 100)
        with self._snapshot() as db:
            last, high = self._boundary(db, after, "search", query)
            phrase = '"' + query.replace('"', '""') + '"'
            rows = db.execute("SELECT e.* FROM events_fts JOIN events e ON e.id=events_fts.rowid WHERE events_fts MATCH ? AND e.source_key=? AND e.id>? AND e.id<=? ORDER BY e.id LIMIT ?",
                              (phrase, self.source_key, last, high, limit + 1)).fetchall()
            count = min(limit, len(rows))
            while True:
                result = {**self._envelope(db, high), "results": [sliced(r, limit=512) for r in rows[:count]],
                          "response_shortened": count < min(limit, len(rows)),
                          "next_page": self._next("search", query, rows[count - 1]["id"], high) if len(rows) > count else None}
                if response_size(result) <= 65536:
                    return result
                if count <= 1:
                    return {"status": "size_error", "scope": [self.source_key]}
                count -= 1

    def event(self, ref, *, text_offset=0, text_limit=4096):
        if not isinstance(ref, str) or len(ref) > 256:
            raise ValueError("Invalid reference")
        if not isinstance(text_offset, int) or text_offset < 0:
            raise ValueError("Invalid text offset")
        text_limit = clamp(text_limit, 1, 4096)
        with self._snapshot() as db:
            row = db.execute("SELECT e.*,r.text_retained,r.byte_start,r.byte_end,r.sha256 FROM events e JOIN records r ON r.ref=e.record_ref WHERE e.ref=? AND e.source_key=?", (ref, self.source_key)).fetchone()
            if row is None:
                return {**self._envelope(db), "status": "not_found", "ref": ref}
            source = db.execute("SELECT provider,session_id,lane FROM sources WHERE source_key=?", (self.source_key,)).fetchone()
            return {**self._envelope(db), "status": "ok", "event": sliced(row, text_offset, text_limit),
                    "source_identity": dict(source)}

    def context(self, ref, *, before=5, after=5, text_limit=512):
        if not isinstance(ref, str) or len(ref) > 256:
            raise ValueError("Invalid reference")
        before, after, text_limit = clamp(before, 0, 20), clamp(after, 0, 20), clamp(text_limit, 1, 4096)
        with self._snapshot() as db:
            anchor = db.execute("SELECT * FROM events WHERE ref=? AND source_key=?", (ref, self.source_key)).fetchone()
            if anchor is None:
                return {**self._envelope(db), "status": "not_found", "ref": ref, "anchor": None}
            previous = db.execute("SELECT * FROM events WHERE source_key=? AND id<? ORDER BY id DESC LIMIT ?", (self.source_key, anchor["id"], before)).fetchall()
            following = db.execute("SELECT * FROM events WHERE source_key=? AND id>? ORDER BY id LIMIT ?", (self.source_key, anchor["id"], after)).fetchall()
            result = {**self._envelope(db), "status": "ok", "anchor": sliced(anchor, limit=text_limit),
                    "before": [sliced(r, limit=text_limit) for r in reversed(previous)],
                    "after": [sliced(r, limit=text_limit) for r in following], "response_shortened": False}
            while response_size(result) > 65536:
                result["response_shortened"] = True
                if result["after"]:
                    result["after"].pop()
                elif result["before"]:
                    result["before"].pop(0)
                else:
                    return {"status": "size_error", "anchor_ref": ref, "scope": [self.source_key]}
            return result

    def activity(self, *, after=None, limit=100):
        limit = clamp(limit, 1, 100)
        with self._snapshot() as db:
            last, high = self._boundary(db, after, "activity")
            calls = db.execute("SELECT * FROM events WHERE source_key=? AND kind='tool_call' AND id>? AND id<=? ORDER BY id LIMIT ?", (self.source_key, last, high, limit + 1)).fetchall()
            results = []
            for call in calls[:limit]:
                outputs = db.execute("SELECT * FROM events WHERE source_key=? AND kind='tool_result' AND call_id=? AND id>? AND id<=? ORDER BY id LIMIT 2", (self.source_key, call["call_id"], call["id"], high)).fetchall()
                duplicates = db.execute("SELECT count(*) AS n FROM events WHERE source_key=? AND kind='tool_call' AND call_id=? AND id<=?", (self.source_key, call["call_id"], high)).fetchone()["n"]
                state = "ambiguous" if duplicates != 1 or len(outputs) > 1 else "complete" if outputs else "pending"
                results.append({"call": sliced(call, limit=512), "results": [sliced(r, limit=512) for r in outputs],
                                "pair_state": state, "interpretation": verdict(call, outputs[0]) if state == "complete" else state,
                                "result_candidates_capped_at": 2})
            count = len(results)
            while True:
                result = {**self._envelope(db, high), "results": results[:count], "response_shortened": count < len(results),
                          "next_page": self._next("activity", "", calls[count - 1]["id"], high) if len(calls) > count else None}
                if response_size(result) <= 65536:
                    return result
                if count <= 1:
                    return {"status": "size_error", "scope": [self.source_key]}
                count -= 1
