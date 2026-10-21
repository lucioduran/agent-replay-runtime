import tempfile
import unittest
from pathlib import Path

from agent_replay import BusyError, Runtime
from agent_replay.ledger import LedgerTool
from experiments.reproduce import case, command, kill_at_checkpoint, run_worker


class ProcessTests(unittest.TestCase):
    def test_real_process_crashes_and_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            for unsafe in (False, True):
                for stage in ("after_request", "after_effect", "after_ack"):
                    with self.subTest(unsafe=unsafe, stage=stage):
                        result = case(Path(tmp) / f"{unsafe}-{stage}", stage, unsafe)
                        self.assertTrue(result["replay_consistent"])

    def test_blind_unsafe_retry_duplicates_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            kill_at_checkpoint(command(root, True, "after_effect", True), "after_effect")
            run_worker(command(root, True, baseline=True))
            self.assertEqual(len(LedgerTool(root / "effects.db").effects()), 2)

    def test_competing_process_is_rejected(self):
        import json
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool = LedgerTool(root / "effects.db")
            runtime = Runtime(root / "runtime.db", {"ledger": tool})
            child = subprocess.Popen(
                command(root, stop="after_request"),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(
                    json.loads(child.stdout.readline()), {"checkpoint": "after_request"}
                )
                with self.assertRaises(BusyError):
                    runtime.resume("experiment")
            finally:
                child.kill()
                child.communicate(timeout=20)
            self.assertTrue(runtime.resume("experiment").finished)
            self.assertEqual(len(tool.effects()), 1)
