import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from agent_replay import Runtime
from agent_replay.cli import main
from agent_replay.ledger import LedgerTool
from agent_replay.storage import writer_lock


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "runtime.db"
        self.ledger = self.root / "effects.db"
        self.base = ["--db", str(self.db), "--ledger", str(self.ledger)]
        self.plan = self.root / "plan.json"
        self.plan.write_text(
            json.dumps([{"id": "one", "tool": "ledger.v1", "args": {}, "idempotent": True}])
        )

    def call(self, args, expected=0):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = main(self.base + args)
        self.assertEqual(status, expected, err.getvalue())
        return json.loads(out.getvalue() if status == 0 else err.getvalue())

    def test_submit_resume_inspect_trace(self):
        self.assertFalse(self.call(["submit", "run", str(self.plan)])["finished"])
        self.assertTrue(self.call(["resume", "run"])["finished"])
        self.assertTrue(self.call(["inspect", "run"])["finished"])
        self.assertEqual(len(self.call(["trace", "run"])), 4)

    def test_bad_inputs_and_busy(self):
        self.call(["inspect", "missing"], 2)
        self.plan.write_text("not json")
        self.call(["submit", "run", str(self.plan)], 2)
        self.call(["submit", "run", str(self.root / "missing")], 2)
        with writer_lock(self.db):
            self.assertEqual(self.call(["inspect", "run"], 4)["status"], "busy")
        self.base = ["--db", str(self.db), "--ledger", str(self.db)]
        self.call(["inspect", "run"], 2)

    def test_reconcile_pending_unsafe(self):
        tool = LedgerTool(self.ledger, idempotent=False)
        runtime = Runtime(self.db, {"unsafe-ledger.v1": tool})
        runtime.submit(
            "run", [{"id": "one", "tool": "unsafe-ledger.v1", "args": {}, "idempotent": False}]
        )

        def interrupt(stage):
            if stage == "after_effect":
                raise TimeoutError("Lost response")

        with self.assertRaises(TimeoutError):
            runtime.resume("run", interrupt)
        self.assertEqual(self.call(["resume", "run"], 3)["status"], "uncertain")
        result = {"effect_id": 1, "payload": {}}
        self.call(
            [
                "reconcile",
                "run",
                "--result",
                json.dumps(result),
                "--evidence",
                "External ledger effect 1 verified",
            ]
        )
        self.assertTrue(self.call(["resume", "run"])["finished"])

    def test_module_entrypoint(self):
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-m", "agent_replay", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("reconcile", result.stdout)
