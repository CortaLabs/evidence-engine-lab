"""Contract tests: independent expected outputs, disposable records and databases."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from evidence_engine import Store, Reader, CodexAdapter, ClaudeAdapter
from evidence_engine.store import SourceChanged, connect
from evidence_engine.__main__ import main

ROOT = Path(__file__).resolve().parents[1]


def line(payload):
    return (json.dumps({"type": "response_item", "payload": payload}, ensure_ascii=False) + "\n").encode()


def message(text):
    return line({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})


def call(cid="a", command="pytest -q"):
    return line({"type": "function_call", "name": "exec_command", "call_id": cid,
                 "arguments": json.dumps({"cmd": command})})


def output(cid="a", text="Process exited with code 0\n3 passed in 0.02s\n", **extra):
    return line({"type": "function_call_output", "call_id": cid, "output": text, **extra})


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / "session.jsonl"
        self.path.write_bytes(b"")
        self.db = self.root / "state" / "evidence.sqlite"
        self.source = dict(source_key="demo/main", path=str(self.path), provider="codex",
                           session_id="demo-session", lane="main", adapter_id="codex_native_rollout", adapter_version="1")
        self.store = Store(self.db, self.source)
        self.store.initialize()
        self.reader = Reader(self.db, "demo/main")

    def ingest(self, data, **bounds):
        self.path.write_bytes(data)
        return self.store.ingest("demo/main", self.path, **bounds)

    def sql(self, query):
        with connect(self.db) as db:
            return [dict(r) for r in db.execute(query).fetchall()]

    def cli(self, *args):
        config = self.root / "evidence.json"
        config.write_text(json.dumps({"database": str(self.db), "source": self.source}))
        return subprocess.run([sys.executable, "-m", "evidence_engine", "--config", str(config), *args],
                              cwd=ROOT, capture_output=True, text=True, timeout=20)

    def test_fixture_end_to_end_expected_failure_pending_unknown(self):
        self.ingest((ROOT / "fixtures/codex.jsonl").read_bytes())
        found = self.reader.search("tests passed")
        self.assertEqual(len(found["results"]), 1)
        ref = found["results"][0]["ref"]
        self.assertEqual(self.reader.context(ref)["anchor"]["ref"], ref)
        self.assertEqual(self.reader.event(ref)["event"]["role"], "assistant")
        self.assertEqual([r["interpretation"] for r in self.reader.activity()["results"]],
                         ["reported_failure", "pending", "unknown"])

    def test_adapters_supported_shapes_and_unknown_neighbors(self):
        parsed = CodexAdapter().parse_line(line({"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "one"}, {"type": "image"}, {"type": "text", "text": "two"}]}), source_key="x", line_no=1)
        self.assertEqual([(r["kind"], r["text"]) for r in parsed],
                         [("message", "one"), ("unknown", "Unsupported message block"), ("message", "two")])
        self.assertEqual(parsed[0]["occurred_at"], None)
        for raw in (b"not json\n", b"[]\n", b"\xff\n", line({"type": "function_call", "name": 3})):
            self.assertEqual(CodexAdapter().parse_line(raw, source_key="x", line_no=1)[0]["classification"], "malformed")
        self.assertEqual(CodexAdapter().parse_line(b'{"type":"new_kind"}\n', source_key="x", line_no=1)[0]["kind"], "unknown")

    def test_claude_fixture_mapping_independent_expected(self):
        source = {**self.source, "provider": "claude", "adapter_id": "claude_code_jsonl"}
        store = Store(self.root / "claude.sqlite", source)
        store.initialize()
        self.path.write_bytes((ROOT / "fixtures/claude.jsonl").read_bytes())
        store.ingest(source["source_key"], self.path)
        result = Reader(store.database, source["source_key"]).activity()["results"]
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["call"]["tool"], result[0]["call"]["call_id"], result[0]["interpretation"]),
                         ("Bash", "claude-test-1", "reported_pass"))
        record = {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "a", "is_error": True, "content": [{"type": "text", "text": "bad"}]}]}}
        events = ClaudeAdapter().parse_line(json.dumps(record).encode(), source_key="x", line_no=1)
        self.assertTrue(events[0]["extra"]["is_error"])
        self.assertEqual(events[0]["text"], "bad")

    def test_fresh_native_codex_preserves_wrapper_and_unknown_semantics(self):
        raw = (ROOT / "fixtures/codex_native.jsonl").read_bytes()
        expected = json.loads(raw.splitlines()[0])["payload"]
        self.ingest(raw)
        activity = self.reader.activity()["results"][0]
        ref = activity["call"]["ref"]
        self.assertEqual(self.reader.event(ref)["event"]["text"], expected["input"])
        self.assertEqual(activity["call"]["call_id"], expected["call_id"])
        self.assertEqual(activity["pair_state"], "complete")
        self.assertEqual(activity["interpretation"], "unknown")
        self.assertIn('"exit_code":7', self.reader.event(activity["results"][0]["ref"])["event"]["text"])

    def test_custom_unknown_output_discloses_incomplete_mapping(self):
        raw = line({"type": "custom_tool_call_output", "call_id": "x", "output": [
            {"type": "input_text", "text": "known"}, {"type": "future"}]})
        mapped = CodexAdapter().parse_line(raw, source_key="x", line_no=1)
        self.assertEqual([r["kind"] for r in mapped], ["tool_result", "unknown"])
        self.assertTrue(mapped[0]["extra"]["truncated"])

    def test_conflicting_session_rejects_entire_batch(self):
        raw = message("first") + b'{"type":"session_meta","payload":{"id":"foreign"}}\n'
        with self.assertRaises(ValueError):
            self.ingest(raw)
        self.assertEqual(self.sql("SELECT offset FROM sources")[0]["offset"], 0)
        self.assertEqual(self.reader.search("first")["results"], [])

    def test_claude_conflicting_identity_rejected(self):
        source = {**self.source, "provider": "claude", "adapter_id": "claude_code_jsonl"}
        store = Store(self.root / "other.sqlite", source)
        store.initialize()
        self.path.write_text('{"type":"user","sessionId":"foreign","message":{"role":"user","content":"hello"}}\n')
        with self.assertRaises(ValueError):
            store.ingest(source["source_key"], self.path)

    def test_complete_malformed_and_unknown_counted_raw_retained(self):
        raw = b'bad\n{"type":"future"}\n' + message("good")
        self.ingest(raw)
        self.assertEqual(self.reader.search("good")["coverage"], {"records": 3, "unknown": 1, "diagnostic": 1})
        with connect(self.db) as db:
            self.assertEqual(b"".join(r["raw"] for r in db.execute("SELECT raw FROM records ORDER BY byte_start")), raw)

    def test_partial_multibyte_tail_not_consumed(self):
        raw = message("café")
        split = raw.index("é".encode()) + 1
        self.ingest(raw[:split])
        self.assertEqual(self.sql("SELECT offset FROM sources")[0]["offset"], 0)
        self.assertEqual(self.reader.search("café")["freshness"]["state"], "pending")
        self.ingest(raw)
        self.assertEqual(self.reader.search("café")["coverage"]["diagnostic"], 0)

    def test_deterministic_refs_duplicate_offsets_and_no_reingest(self):
        raw = message("same")
        self.ingest(raw * 2)
        refs = [r["ref"] for r in self.reader.search("same")["results"]]
        self.assertEqual(len(set(refs)), 2)
        self.assertEqual(self.store.ingest("demo/main", self.path)["records_ingested"], 0)
        other = Store(self.root / "rebuild.sqlite", self.source)
        other.initialize()
        other.ingest("demo/main", self.path)
        self.assertEqual(refs, [r["ref"] for r in Reader(other.database, "demo/main").search("same")["results"]])
        rr = self.sql("SELECT ref,sha256 FROM records ORDER BY byte_start")[0]
        expected = "record:v1:" + hashlib.sha256(json.dumps(["demo/main", 0, hashlib.sha256(raw).hexdigest()], separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(rr["ref"], expected)

    def test_record_and_byte_bounds_no_false_progress(self):
        raw = message("budget")
        self.ingest(raw * 3, max_records=1)
        self.assertEqual(self.sql("SELECT line_no FROM sources")[0]["line_no"], 1)
        self.store.ingest("demo/main", self.path, max_bytes=len(raw))
        self.assertEqual(self.sql("SELECT line_no FROM sources")[0]["line_no"], 2)
        with self.assertRaises(ValueError):
            self.store.ingest("demo/main", self.path, max_bytes=len(raw) - 1)
        with self.assertRaises(ValueError):
            self.store.ingest("demo/main", self.path, max_records=0)

    def test_oversized_line_rolls_back_planned_batch(self):
        with self.assertRaises(ValueError):
            self.ingest(message("first") + b"x" * 262145 + b"\n")
        self.assertEqual(self.sql("SELECT offset FROM sources")[0]["offset"], 0)

    def test_source_size_limit(self):
        with self.path.open("wb") as f:
            f.truncate(64 * 1024 * 1024 + 1)
        with self.assertRaises(ValueError):
            self.store.ingest("demo/main", self.path)

    def test_append_rewrite_truncation_and_relocation(self):
        raw = message("old")
        self.ingest(raw)
        self.ingest(raw + message("new"))
        ref = self.reader.search("old")["results"][0]["ref"]
        moved = self.root / "moved.jsonl"
        self.path.rename(moved)
        source = {**self.source, "path": str(moved)}
        store = Store(self.db, source)
        store.initialize()
        self.assertEqual(store.ingest("demo/main", moved)["records_ingested"], 0)
        self.assertEqual(self.reader.search("old")["results"][0]["ref"], ref)
        moved.write_bytes((raw + message("new")).replace(b"old", b"bad"))
        with self.assertRaises(SourceChanged):
            store.ingest("demo/main", moved)
        moved.write_bytes(b"")
        with self.assertRaises(SourceChanged):
            store.ingest("demo/main", moved)

    def test_identity_and_path_validation(self):
        for changes in ({"path": "relative"}, {"session_id": ""}, {"adapter_version": "2"}, {"provider": "other"}):
            with self.assertRaises(ValueError):
                Store(self.db, {**self.source, **changes})
        with self.assertRaises(ValueError):
            Store(self.db, {**self.source, "session_id": "foreign"}).initialize()
        with self.assertRaises(ValueError):
            self.store.ingest("foreign", self.path)

    def test_literal_phrase_quoting_and_parameterization(self):
        self.ingest(message('alpha OR beta "quoted"') + message("alpha alone"))
        self.assertEqual(len(self.reader.search("alpha OR beta")["results"]), 1)
        self.assertEqual(len(self.reader.search('"quoted"')["results"]), 1)
        self.assertEqual(self.reader.search("' OR 1=1 --")["results"], [])
        self.assertEqual(len(self.reader.search("alpha", limit=0)["results"]), 1)
        with self.assertRaises(ValueError):
            self.reader.search("x" * 1025)

    def test_search_snapshot_stable_under_appends_and_cursor_binding(self):
        raw = message("match") * 3
        self.ingest(raw)
        first = self.reader.search("match", limit=1)
        self.ingest(raw + message("match"))
        second = self.reader.search("match", after=first["next_page"], limit=100)
        self.assertEqual(len(second["results"]), 2)
        self.assertEqual(first["snapshot_high_water"], second["snapshot_high_water"])
        with self.assertRaises(ValueError):
            self.reader.search("different", after=first["next_page"])
        with self.assertRaises(ValueError):
            self.reader.activity(after=first["next_page"])
        with self.assertRaises(ValueError):
            self.reader.search("match", after="!invalid!")

    def test_freshness_pending_unavailable_unknown_and_retained_evidence(self):
        raw = message("retained")
        self.ingest(raw)
        self.assertEqual(self.reader.search("retained")["freshness"]["state"], "current")
        self.path.write_bytes(raw + b"partial")
        self.assertEqual(self.reader.search("retained")["freshness"]["state"], "pending")
        self.path.unlink()
        result = self.reader.search("retained")
        self.assertEqual(result["freshness"]["state"], "unavailable")
        self.assertEqual(self.reader.event(result["results"][0]["ref"])["event"]["text"], "retained")

    def test_context_anchor_expansion_unknown_and_retention_state(self):
        self.ingest(message("before") + message("anchor " + "長" * 5000) + message("after"))
        ref = self.reader.search("anchor")["results"][0]["ref"]
        expanded = self.reader.event(ref, text_offset=4096, text_limit=99999)["event"]
        self.assertEqual(len(expanded["text"]), 911)
        self.assertTrue(expanded["text_is_partial"])
        context = self.reader.context(ref, before=99, after=99, text_limit=1)
        self.assertEqual(context["anchor"]["ref"], ref)
        self.assertEqual(context["anchor"]["text"], "a")
        self.assertTrue(context["anchor"]["text_is_partial"])
        self.assertEqual(context["before"][0]["text"], "b")
        self.assertEqual(self.reader.event("missing")["status"], "not_found")
        with connect(self.db) as db:
            db.execute("UPDATE records SET raw=NULL,text_retained=0")
        self.assertFalse(self.reader.event(ref)["event"]["text_retained"])

    def test_reader_scope_and_cross_source_pairing(self):
        self.ingest(call("a"))
        foreign_path = self.root / "foreign.jsonl"
        foreign_path.write_bytes(output("a") + message("private foreign"))
        foreign = {**self.source, "source_key": "foreign", "path": str(foreign_path), "session_id": "foreign"}
        s = Store(self.db, foreign)
        s.initialize()
        s.ingest("foreign", foreign_path)
        foreign_ref = Reader(self.db, "foreign").search("private")["results"][0]["ref"]
        self.assertEqual(self.reader.search("private")["results"], [])
        self.assertEqual(self.reader.event(foreign_ref)["status"], "not_found")
        self.assertEqual(self.reader.context(foreign_ref)["status"], "not_found")
        self.assertEqual(self.reader.activity()["results"][0]["pair_state"], "pending")

    def test_ambiguous_results_and_duplicate_calls(self):
        self.ingest(call() + output() + output())
        self.assertEqual(self.reader.activity()["results"][0]["pair_state"], "ambiguous")
        self.ingest(call() + output() + output() + call())
        self.assertTrue(all(r["pair_state"] == "ambiguous" for r in self.reader.activity()["results"]))

    def test_activity_snapshot_late_result(self):
        raw = call("a") + call("b")
        self.ingest(raw)
        page = self.reader.activity(limit=1)
        self.ingest(raw + output("b"))
        self.assertEqual(self.reader.activity(after=page["next_page"])["results"][0]["pair_state"], "pending")
        self.assertEqual(self.reader.activity()["results"][1]["pair_state"], "complete")

    def test_verdict_requires_direct_command_terminal_summary_and_untruncated_output(self):
        cases = [
            ("pytest -q", "Process exited with code 0\n3 passed in 0.02s\n", {}, "reported_pass"),
            ("pytest -q", "Process exited with code 1\n1 failed in 0.02s\n", {}, "reported_failure"),
            ("pytest --collect-only", "Process exited with code 0\n3 passed in 0.02s\n", {}, "unknown"),
            ("cat report", "Process exited with code 0\n3 passed in 0.02s\n", {}, "unknown"),
            ("pytest | cat", "Process exited with code 0\n3 passed in 0.02s\n", {}, "unknown"),
            ("pytest", "3 passed in 0.02s\n", {}, "unknown"),
            ("pytest", "Process exited with code 0\n0 passed in 0.02s\n", {}, "unknown"),
            ("pytest", "Process exited with code 0\n3 passed in 0.02s\nActually incomplete\n", {}, "unknown"),
            ("pytest", "Process exited with code 0\n3 passed in 0.02s\n", {"truncated": True}, "unknown"),
        ]
        raw = b"".join(call(str(i), cmd) + output(str(i), text, **extra) for i, (cmd, text, extra, _) in enumerate(cases))
        self.ingest(raw)
        self.assertEqual([r["interpretation"] for r in self.reader.activity()["results"]], [c[3] for c in cases])

    def test_exception_atomicity_and_independent_reader_visibility(self):
        self.path.write_bytes(message("atomic"))
        def interrupt(db):
            independent = self.reader.search("atomic")
            self.assertEqual(independent["results"], [])
            self.assertEqual(independent["coverage"]["records"], 0)
            self.assertEqual(self.sql("SELECT offset FROM sources")[0]["offset"], 0)
            raise RuntimeError("injected")
        with patch.object(self.store, "_before_commit", side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.store.ingest("demo/main", self.path)
        self.assertEqual(self.reader.search("atomic")["results"], [])
        self.assertEqual(self.store.ingest("demo/main", self.path)["records_ingested"], 1)

    def test_process_exit_before_commit_rolls_back(self):
        self.path.write_bytes(message("crash"))
        script = "import os,json; from pathlib import Path; from evidence_engine import Store; s=Store(Path(os.environ['LAB_DB']),json.loads(os.environ['LAB_SOURCE'])); s._before_commit=lambda db: os._exit(19); s.ingest(s.source['source_key'],Path(s.source['path']))"
        env = {**os.environ, "LAB_DB": str(self.db), "LAB_SOURCE": json.dumps(self.source)}
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, timeout=20)
        self.assertEqual(result.returncode, 19)
        self.assertEqual(self.reader.search("crash")["results"], [])
        self.assertEqual(self.sql("SELECT offset FROM sources")[0]["offset"], 0)
        self.assertEqual(self.store.ingest("demo/main", self.path)["records_ingested"], 1)

    def test_reader_does_not_write_and_fts_update_delete_are_atomic(self):
        self.ingest(message("before"))
        original = self.db.read_bytes()
        self.reader.search("before")
        self.reader.activity()
        ref = self.reader.search("before")["results"][0]["ref"]
        self.reader.event(ref)
        self.reader.context(ref)
        self.assertEqual(self.db.read_bytes(), original)
        with connect(self.db, readonly=True) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM events")
        with connect(self.db) as db:
            db.execute("UPDATE events SET text='after'")
        self.assertEqual(self.reader.search("before")["results"], [])
        self.assertEqual(len(self.reader.search("after")["results"]), 1)
        with connect(self.db) as db:
            db.execute("DELETE FROM events")
        self.assertEqual(self.reader.search("after")["results"], [])

    def test_response_budget_preserves_anchor_and_page_progress(self):
        self.ingest(message("big " + "長" * 5000) * 60)
        first = self.reader.search("big", limit=100)
        self.assertLessEqual(len(json.dumps(first).encode()), 65536)
        self.assertTrue(first["next_page"])
        ref = first["results"][20]["ref"] if len(first["results"]) > 20 else first["results"][0]["ref"]
        context = self.reader.context(ref, before=20, after=20, text_limit=4096)
        self.assertEqual(context["anchor"]["ref"], ref)
        self.assertLessEqual(len(json.dumps(context).encode()), 65536)
        self.assertTrue(context["response_shortened"])
        second = self.reader.search("big", limit=100, after=first["next_page"])
        self.assertFalse(set(r["ref"] for r in first["results"]) & set(r["ref"] for r in second["results"]))

    def test_cli_cold_start_json_and_stderr_exit_codes(self):
        # Remove only this test's empty initialized database to exercise actual init.
        self.db.unlink()
        result = self.cli("init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "initialized")
        self.path.write_bytes((ROOT / "fixtures/codex.jsonl").read_bytes())
        for args in (("ingest", "demo/main"), ("sources",), ("search", "tests passed"), ("activity",)):
            result = self.cli(*args)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(result.stderr)
            json.loads(result.stdout)
        ref = json.loads(self.cli("search", "tests passed").stdout)["results"][0]["ref"]
        for operation in ("event", "context"):
            self.assertEqual(self.cli(operation, ref).returncode, 0)
        self.assertEqual(self.cli("ingest", "foreign").returncode, 2)
        self.path.unlink()
        self.assertEqual(self.cli("ingest", "demo/main").returncode, 3)
        self.db.write_bytes(b"corrupt database")
        result = self.cli("search", "secret-untrusted-value")
        self.assertEqual(result.returncode, 4)
        self.assertFalse(result.stdout)
        self.assertNotIn("secret-untrusted-value", result.stderr)

    def test_source_append_during_planning_rejects_batch(self):
        self.path.write_bytes(message("planned"))
        original = self.store.adapter.parse_line
        changed = False

        def changing(raw, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                with self.path.open("ab") as stream:
                    stream.write(message("arrived during planning"))
            return original(raw, **kwargs)

        self.store.adapter.parse_line = changing
        with self.assertRaises(SourceChanged):
            self.store.ingest(self.source["source_key"], self.path)
        self.assertEqual(self.sql("SELECT offset FROM sources")[0]["offset"], 0)
        self.assertEqual(self.reader.search("planned")["results"], [])

    def test_backup_restores_references_and_retained_bytes(self):
        raw = message("backup evidence")
        self.ingest(raw)
        ref = self.reader.search("backup")["results"][0]["ref"]
        backup = self.root / "restored.sqlite"
        with connect(self.db) as source, sqlite3.connect(backup) as destination:
            source.backup(destination)
        self.path.unlink()
        restored = Reader(backup, self.source["source_key"])
        self.assertEqual(restored.event(ref)["event"]["text"], "backup evidence")
        self.assertEqual(restored.search("backup")["freshness"]["state"], "unavailable")
        with connect(backup, readonly=True) as db:
            self.assertEqual(bytes(db.execute("SELECT raw FROM records").fetchone()["raw"]), raw)

    def test_known_codex_outer_record_with_invalid_payload_is_malformed(self):
        for payload in (None, [], "wrong", 3):
            raw = json.dumps({"type": "response_item", "payload": payload}).encode() + b"\n"
            mapped = self.store.adapter.parse_line(raw, source_key=self.source["source_key"], line_no=1)
            self.assertEqual([(e["kind"], e["classification"]) for e in mapped],
                             [("diagnostic", "malformed")])

    def test_result_before_call_cannot_establish_new_test_outcome(self):
        prior = output("reused", "Process exited with code 0\n2 passed in 0.1s\n")
        self.ingest(prior + call("reused"))
        row = self.reader.activity()["results"][0]
        self.assertEqual(row["pair_state"], "pending")
        self.assertNotEqual(row["interpretation"], "reported_pass")
        self.assertEqual(row["results"], [])
        self.assertEqual(len(self.reader.search("2 passed")["results"]), 1)
        # A later actual result can complete the call; the prior orphan cannot.
        with self.path.open("ab") as stream:
            stream.write(output("reused", "Process exited with code 1\n1 failed in 0.2s\n"))
        self.store.ingest(self.source["source_key"], self.path)
        row = self.reader.activity()["results"][0]
        self.assertEqual(row["pair_state"], "complete")
        self.assertEqual(row["interpretation"], "reported_failure")
        self.assertEqual(len(row["results"]), 1)

    def test_internal_cli_errors_sanitized(self):
        config = self.root / "config.json"
        config.write_text(json.dumps({"database": str(self.db), "source": self.source}))
        with patch("evidence_engine.Reader.search", side_effect=RuntimeError("sensitive detail")):
            with patch("sys.stderr") as stream:
                self.assertEqual(main(["--config", str(config), "search", "test"]), 5)
                self.assertNotIn("sensitive detail", str(stream.mock_calls))


if __name__ == "__main__":
    unittest.main()
