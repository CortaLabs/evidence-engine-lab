"""Foreign-directory launcher checks using disposable synthetic files."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "evidence_cli.py"
FIXTURE = ROOT / "fixtures" / "codex.jsonl"


class LauncherTests(unittest.TestCase):
    def test_cli_uses_companion_core_from_foreign_directory(self):
        with tempfile.TemporaryDirectory(prefix="evidence-cli-launcher-") as scratch:
            work = Path(scratch)
            source = work / "source.jsonl"
            source.write_bytes(FIXTURE.read_bytes())
            database = work / "evidence.sqlite"
            config = work / "evidence.json"
            config.write_text(json.dumps({
                "database": str(database),
                "source": {
                    "source_key": "lab/launcher/main",
                    "path": str(source),
                    "provider": "codex",
                    "session_id": "demo-session",
                    "lane": "main",
                    "adapter_id": "codex_native_rollout",
                    "adapter_version": "1",
                },
            }))

            def run(*args):
                return subprocess.run(
                    [sys.executable, str(CLI), "--config", str(config), *args],
                    cwd=work,
                    env=dict(os.environ),
                    capture_output=True,
                    text=True,
                    check=True,
                )

            self.assertEqual(json.loads(run("init").stdout)["status"], "initialized")
            self.assertEqual(json.loads(run("ingest", "lab/launcher/main").stdout)["integrity_check"], "prefix_verified")
            self.assertEqual(json.loads(run("sources").stdout)["sources"][0]["source_key"], "lab/launcher/main")
            search = json.loads(run("search", "tests passed").stdout)
            self.assertTrue(search["results"])
            ref = search["results"][0]["ref"]
            self.assertEqual(json.loads(run("event", ref).stdout)["event"]["ref"], ref)
            self.assertEqual(json.loads(run("context", ref).stdout)["anchor"]["ref"], ref)
            self.assertIn("results", json.loads(run("activity").stdout))

    def test_cli_keeps_core_invalid_config_diagnostic(self):
        with tempfile.TemporaryDirectory(prefix="evidence-cli-config-") as scratch:
            work = Path(scratch)
            config = work / "invalid.json"
            config.write_text('{"database":"relative.sqlite","source":{}}')
            result = subprocess.run(
                [sys.executable, str(CLI), "--config", str(config), "sources"],
                cwd=work,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr.strip(), "Invalid configuration or arguments")


if __name__ == "__main__":
    unittest.main()
