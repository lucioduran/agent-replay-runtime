"""Fixed process-crash matrix, not a throughput benchmark."""

import argparse
import hashlib
import json
import os
import platform
import queue
import sqlite3
import subprocess
import sys
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

from agent_replay import Runtime, replay
from agent_replay.ledger import LedgerTool

ROOT = Path(__file__).resolve().parents[1]


def command(root, unsafe=False, stop=None, baseline=False):
    args = [sys.executable, "-m", "experiments.worker", str(root)]
    if unsafe:
        args += ["--unsafe"]
    if stop:
        args += ["--stop", stop]
    if baseline:
        args += ["--baseline"]
    return args


def kill_at_checkpoint(args, expected):
    with subprocess.Popen(
        args,
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as child:
        messages = queue.Queue()
        reader = threading.Thread(target=lambda: messages.put(child.stdout.readline()), daemon=True)
        reader.start()
        try:
            line = messages.get(timeout=20)
            if not line or json.loads(line) != {"checkpoint": expected}:
                raise AssertionError(f"Worker did not reach checkpoint: {line!r}")
            child.kill()
            stdout, stderr = child.communicate(timeout=20)
            reader.join(timeout=1)
            if child.returncode == 0 or stderr:
                raise AssertionError(f"Unexpected killed worker status: {stderr}")
            return {
                "checkpoint": expected,
                "returncode": child.returncode,
                "handshake": json.loads(line),
                "remaining_stdout": stdout,
                "stderr": stderr,
            }
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=20)


def run_worker(args, expected=0):
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=20)
    if result.returncode != expected:
        raise AssertionError(f"Worker exit {result.returncode}: {result.stderr} {result.stdout}")
    return {
        "returncode": result.returncode,
        "stdout": json.loads(result.stdout),
        "stderr": result.stderr,
    }


def case(root, stage, unsafe):
    root.mkdir()
    termination = kill_at_checkpoint(command(root, unsafe, stage), stage)
    tool = LedgerTool(root / "effects.db", idempotent=not unsafe)
    runtime = Runtime(root / "runtime.db", {"ledger": tool})
    before = runtime.store.events("experiment")
    before_effects = tool.effects()
    blocked = unsafe and stage != "after_ack"
    recovery = run_worker(command(root, unsafe), 3 if blocked else 0)
    reconciliation = None
    if blocked and before_effects:
        effect = before_effects[0]
        result = {"effect_id": effect["effect_id"], "payload": effect["payload"]}
        note = "Experiment verified effect 1 in the independent SQLite ledger"
        runtime.reconcile("experiment", result, note)
        reconciliation = {"result": result, "evidence": note}
        recovery = [recovery, run_worker(command(root, unsafe))]
    after = runtime.store.events("experiment")
    state = runtime.inspect("experiment")
    expected_effects = 0 if unsafe and stage == "after_request" else 1
    assert len(tool.effects()) == expected_effects
    assert state.finished == (not (unsafe and stage == "after_request"))
    assert replay(after) == state
    if state.finished:
        run_worker(command(root, unsafe))
        assert runtime.store.events("experiment") == after
        assert len(tool.effects()) == expected_effects
    return {
        "mode": "unsafe" if unsafe else "idempotent",
        "stage": stage,
        "termination": termination,
        "events_before": before,
        "effects_before": before_effects,
        "recovery": recovery,
        "reconciliation": reconciliation,
        "events_after": after,
        "effects_after": tool.effects(),
        "final_state": state.as_json(),
        "replay_consistent": True,
        "repeat_resume_unchanged": state.finished,
    }


def metadata():
    hardware = {
        "machine": platform.machine(),
        "processor": platform.processor(),
        "logical_cpus": os.cpu_count(),
    }
    if sys.platform == "darwin":
        for key in ("machdep.cpu.brand_string", "hw.memsize", "hw.ncpu"):
            hardware[key] = subprocess.check_output(["sysctl", "-n", key], text=True).strip()
    files = sorted([*ROOT.glob("agent_replay/*.py"), *ROOT.glob("experiments/*.py")])
    return {
        "timestamp_utc": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "hardware": hardware,
        "revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "source_sha256": {
            p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in files
        },
        "api_calls": 0,
        "api_cost_usd": 0,
        "inference": "none",
        "seed": None,
        "threads_per_worker": 1,
        "worker_concurrency": 1,
        "limits": "Fixed correctness matrix; no throughput, power-loss, remote-service or security claim",
    }


def reproduce():
    with tempfile.TemporaryDirectory(prefix="agent-replay-") as tmp:
        root = Path(tmp)
        cases = [
            case(root / f"{unsafe}-{stage}", stage, unsafe)
            for unsafe in (False, True)
            for stage in ("after_request", "after_effect", "after_ack")
        ]
        baseline = root / "baseline"
        baseline.mkdir()
        termination = kill_at_checkpoint(
            command(baseline, True, "after_effect", True), "after_effect"
        )
        recovery = run_worker(command(baseline, True, baseline=True))
        effects = LedgerTool(baseline / "effects.db", idempotent=False).effects()
        assert len(effects) == 2
        control = root / "control"
        control.mkdir()
        run_worker(command(control))
        assert len(LedgerTool(control / "effects.db").effects()) == 1
        return {
            "schema_version": 1,
            "metadata": metadata(),
            "cases": cases,
            "baseline": {
                "description": "Blindly retry unsafe effect after lost acknowledgment",
                "termination": termination,
                "recovery": recovery,
                "effects": effects,
            },
            "control": {"uninterrupted_effects": 1},
            "summary": {
                "crash_cases": len(cases),
                "idempotent_recovered_without_duplicates": 3,
                "unsafe_blocked": 2,
                "unsafe_reconciled_from_evidence": 1,
                "baseline_effects": len(effects),
            },
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = reproduce()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"]))


if __name__ == "__main__":
    main()
