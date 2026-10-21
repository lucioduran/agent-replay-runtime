"""Command-line interface for the bundled ledger adapter."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import cast

from .ledger import LedgerTool
from .model import JSON, ReplayError, UncertainEffect
from .runtime import Runtime
from .storage import BusyError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    submit = commands.add_parser("submit")
    submit.add_argument("run")
    submit.add_argument("plan", type=Path)
    for command in ("resume", "inspect", "trace"):
        commands.add_parser(command).add_argument("run")
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("run")
    reconcile.add_argument("--result", required=True, help="Externally verified result as JSON")
    reconcile.add_argument("--evidence", required=True)
    args = parser.parse_args(argv)
    try:
        if args.db.resolve() == args.ledger.resolve():
            raise ReplayError("Runtime and ledger must be separate databases")
        runtime = Runtime(
            args.db,
            {
                "ledger.v1": LedgerTool(args.ledger),
                "unsafe-ledger.v1": LedgerTool(args.ledger, idempotent=False),
            },
        )
        output: JSON
        if args.command == "submit":
            output = runtime.submit(args.run, json.loads(args.plan.read_text())).as_json()
        elif args.command == "resume":
            output = runtime.resume(args.run).as_json()
        elif args.command == "reconcile":
            output = runtime.reconcile(args.run, json.loads(args.result), args.evidence).as_json()
        elif args.command == "trace":
            output = cast(JSON, runtime.store.events(args.run))
        else:
            output = runtime.inspect(args.run).as_json()
        print(json.dumps(output, indent=2, allow_nan=False))
        return 0
    except UncertainEffect as exc:
        print(json.dumps({"error": str(exc), "status": "uncertain"}), file=sys.stderr)
        return 3
    except BusyError as exc:
        print(json.dumps({"error": str(exc), "status": "busy"}), file=sys.stderr)
        return 4
    except (ReplayError, OSError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
