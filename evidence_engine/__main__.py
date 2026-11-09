"""Operator CLI; JSON stdout, bounded fixed diagnostics on stderr."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from . import Reader, Store
from .store import SourceChanged, connect


def load_config(path):
    value = json.loads(Path(path).read_text())
    if not isinstance(value, dict) or not isinstance(value.get("database"), str) or not Path(value["database"]).is_absolute():
        raise ValueError("Invalid host configuration")
    store = Store(value["database"], value.get("source", {}))
    return value, store, Reader(value["database"], store.source["source_key"])


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's usual message can echo untrusted arguments.
        self.exit(2, "Invalid command arguments; use --help\n")


def main(argv=None):
    parser = Parser(prog="evidence")
    parser.add_argument("--config", default="evidence.json")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("sources")
    ingest = commands.add_parser("ingest")
    ingest.add_argument("source_key")
    ingest.add_argument("--max-records", type=int, default=500)
    ingest.add_argument("--max-bytes", type=int, default=1_048_576)
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--after")
    expand = commands.add_parser("event")
    expand.add_argument("ref")
    expand.add_argument("--text-offset", type=int, default=0)
    expand.add_argument("--text-limit", type=int, default=4096)
    context = commands.add_parser("context")
    context.add_argument("ref")
    context.add_argument("--before", type=int, default=5)
    context.add_argument("--after", type=int, default=5)
    context.add_argument("--text-limit", type=int, default=512)
    activity = commands.add_parser("activity")
    activity.add_argument("--limit", type=int, default=100)
    activity.add_argument("--after")
    args = parser.parse_args(argv)
    try:
        config, store, reader = load_config(args.config)
        command = args.command
        if command == "init":
            store.initialize()
            result = {"status": "initialized", "scope": [store.source["source_key"]]}
        elif command == "ingest":
            result = store.ingest(args.source_key, Path(store.source["path"]),
                                  max_records=args.max_records, max_bytes=args.max_bytes)
        elif command == "sources":
            with connect(config["database"], readonly=True) as db:
                rows = db.execute("SELECT * FROM sources WHERE source_key=?", (reader.source_key,)).fetchall()
                result = {"sources": [dict(row) for row in rows]}
        elif command == "search":
            result = reader.search(args.query, limit=args.limit, after=args.after)
        elif command == "event":
            result = reader.event(args.ref, text_offset=args.text_offset, text_limit=args.text_limit)
        elif command == "context":
            result = reader.context(args.ref, before=args.before, after=args.after, text_limit=args.text_limit)
        else:
            result = reader.activity(limit=args.limit, after=args.after)
        print(json.dumps(result, ensure_ascii=True, allow_nan=False))
        return 0
    except SourceChanged:
        print("Source unavailable or changed", file=sys.stderr)
        return 3
    except (ValueError, TypeError, KeyError, OSError):
        print("Invalid configuration or arguments", file=sys.stderr)
        return 2
    except sqlite3.Error:
        print("Storage operation failed", file=sys.stderr)
        return 4
    except Exception:
        print("Internal operation failed", file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
