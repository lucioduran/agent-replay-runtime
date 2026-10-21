"""A controllable real process, killed by the experiment parent at a handshake."""

import argparse
import json
import sys

from agent_replay import Runtime, UncertainEffect
from agent_replay.ledger import LedgerTool


def main():
    from pathlib import Path

    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--unsafe", action="store_true")
    parser.add_argument("--stop", choices=["after_request", "after_effect", "after_ack"])
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    tool = LedgerTool(args.root / "effects.db", idempotent=not args.unsafe)

    def hook(stage):
        if stage == args.stop:
            print(json.dumps({"checkpoint": stage}), flush=True)
            sys.stdin.readline()
            raise RuntimeError("Checkpoint must be terminated by the parent")

    if args.baseline:
        tool.invoke("baseline-key", {"message": "effect"})
        hook("after_effect")
        print(json.dumps({"finished": True}), flush=True)
        return 0
    runtime = Runtime(args.root / "runtime.db", {"ledger": tool})
    runtime.submit(
        "experiment",
        [
            {
                "id": "effect",
                "tool": "ledger",
                "args": {"message": "effect"},
                "idempotent": not args.unsafe,
            }
        ],
    )
    try:
        state = runtime.resume("experiment", hook)
        print(json.dumps(state.as_json()), flush=True)
        return 0
    except UncertainEffect as exc:
        print(json.dumps({"uncertain": str(exc)}), flush=True)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
