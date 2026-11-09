"""Break three isolated copies; require the intended regression tests to fail."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
CONTROLS = [
    ("premature_commit", "store.py", "            self._before_commit(db)",
     "            db.commit()\n            self._before_commit(db)",
     "test_process_exit_before_commit_rolls_back"),
    ("consume_partial_tail", "store.py", 'if not raw.endswith(b"\\n"):',
     'if not raw:', "test_partial_multibyte_tail_not_consumed"),
    ("old_report_as_new_pass", "reader.py", '    if call["tool"] not in',
     '    if "cat " in call["text"]:\n        return "reported_pass"\n    if call["tool"] not in',
     "test_fixture_end_to_end_expected_failure_pending_unknown"),
]


def main():
    receipts = []
    for name, filename, old, new, test in CONTROLS:
        with tempfile.TemporaryDirectory(prefix="evidence-mutant-") as scratch:
            mutant = Path(scratch) / "lab"
            shutil.copytree(ROOT, mutant, ignore=shutil.ignore_patterns(
                "__pycache__", ".venv*", "state", "*.sqlite*", "evidence.json"))
            target = mutant / "evidence_engine" / filename
            source = target.read_text()
            if source.count(old) != 1:
                raise RuntimeError("Mutation anchor changed; review control " + name)
            target.write_text(source.replace(old, new, 1))
            command = [sys.executable, "-m", "unittest", "tests.test_engine.EngineTests." + test]
            result = subprocess.run(command, cwd=mutant, capture_output=True, text=True, timeout=30)
            detected = result.returncode != 0 and "FAIL:" in result.stderr and "AssertionError" in result.stderr
            receipts.append(dict(control=name, test=test, exit_code=result.returncode,
                                 detected=detected, output=result.stdout + result.stderr))
            if not detected:
                print(json.dumps(receipts, indent=2)); return 1
    print(json.dumps(receipts, indent=2)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
