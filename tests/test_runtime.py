import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_replay import BusyError, ReplayError, Runtime, UncertainEffect, replay
from agent_replay.ledger import LedgerTool
from agent_replay.model import canonical, digest, plan_value
from agent_replay.storage import writer_lock


def plan(safe=True):
    return [{"id": "first", "tool": "ledger", "args": {"n": 1}, "idempotent": safe}]


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tool = LedgerTool(self.root / "effects.db")
        self.runtime = Runtime(self.root / "runtime.db", {"ledger": self.tool})

    def test_roundtrip_multiple_steps_and_repeat(self):
        steps = plan() + [{**plan()[0], "id": "second"}]
        self.runtime.submit("run", steps)
        state = self.runtime.resume("run")
        self.assertTrue(state.finished)
        self.assertEqual(list(state.results), ["first", "second"])
        before = self.runtime.store.events("run")
        self.assertEqual(self.runtime.resume("run"), state)
        self.assertEqual(self.runtime.store.events("run"), before)
        self.assertEqual(replay(before), state)
        self.assertEqual(len(self.tool.effects()), 2)
        self.assertEqual(self.runtime.submit("run", steps), state)

    def test_plan_and_binding_cannot_change(self):
        self.runtime.submit("run", plan())
        with self.assertRaises(ReplayError):
            self.runtime.submit("run", [{**plan()[0], "args": {"n": 2}}])
        self.tool.identity = "other-endpoint"
        with self.assertRaises(ReplayError):
            self.runtime.resume("run")
        with self.assertRaises(ReplayError):
            self.runtime.submit("run", plan())
        self.assertEqual(self.tool.effects(), [])

    def test_capability_or_missing_tool(self):
        for steps in (plan(False), [{**plan()[0], "tool": "missing"}]):
            with self.assertRaises(ReplayError):
                self.runtime.submit("run", steps)
        self.runtime.submit("run", plan())
        self.tool.idempotent = False
        with self.assertRaises(ReplayError):
            self.runtime.resume("run")

    def test_unknown_run(self):
        with self.assertRaises(ReplayError):
            self.runtime.inspect("missing")

    def test_writer_exclusion(self):
        self.runtime.submit("run", plan())
        with writer_lock(self.runtime.store.path):
            with self.assertRaises(BusyError):
                self.runtime.resume("run")
        self.assertTrue(self.runtime.resume("run").finished)

    def test_exception_after_effect_retries_same_key(self):
        original = self.tool.invoke

        def broken(key, args):
            original(key, args)
            raise TimeoutError("Lost response")

        self.tool.invoke = broken
        self.runtime.submit("run", plan())
        with self.assertRaises(TimeoutError):
            self.runtime.resume("run")
        self.assertIsNotNone(self.runtime.inspect("run").pending)
        self.tool.invoke = original
        self.assertTrue(self.runtime.resume("run").finished)
        self.assertEqual(len(self.tool.effects()), 1)
        self.assertIn("retried", [e["kind"] for e in self.runtime.store.events("run")])

    def test_unsafe_requires_evidence(self):
        self.tool.idempotent = False
        self.runtime.submit("run", plan(False))

        def stop(stage):
            if stage == "after_effect":
                raise TimeoutError("Lost response")

        with self.assertRaises(TimeoutError):
            self.runtime.resume("run", stop)
        for _ in range(2):
            with self.assertRaises(UncertainEffect):
                self.runtime.resume("run")
        with self.assertRaises(ReplayError):
            self.runtime.reconcile("run", None, " ")
        effect = self.tool.effects()[0]
        result = {"effect_id": effect["effect_id"], "payload": effect["payload"]}
        self.runtime.reconcile("run", result, "Verified effect 1 in external ledger")
        self.assertTrue(self.runtime.resume("run").finished)
        self.assertEqual(len(self.tool.effects()), 1)
        with self.assertRaises(ReplayError):
            self.runtime.reconcile("run", result, "No pending request")

    def test_invalid_result_stays_pending(self):
        self.tool.invoke = lambda key, args: float("nan")
        self.runtime.submit("run", plan())
        with self.assertRaises(ReplayError):
            self.runtime.resume("run")
        self.assertIsNotNone(self.runtime.inspect("run").pending)

    def test_null_result_is_completed(self):
        self.tool.invoke = lambda key, args: None
        self.runtime.submit("run", plan())
        self.assertEqual(self.runtime.resume("run").results, {"first": None})

    def test_run_namespace_isolation(self):
        for run in ("a", "b"):
            self.runtime.submit(run, plan())
            self.runtime.resume(run)
        self.assertEqual(len(self.tool.effects()), 2)

    def test_key_conflict_rejected_atomically(self):
        result = self.tool.invoke("key", {"n": 1})
        self.assertEqual(self.tool.invoke("key", {"n": 1}), result)
        with self.assertRaises(ReplayError):
            self.tool.invoke("key", {"n": 2})
        self.assertEqual(len(self.tool.effects()), 1)

    def test_unsafe_adapter_duplicates_if_caller_retries(self):
        self.tool.idempotent = False
        self.tool.invoke("key", {})
        self.tool.invoke("key", {})
        self.assertEqual(len(self.tool.effects()), 2)

    def test_bad_storage_rejected(self):
        self.runtime.submit("run", plan())
        with sqlite3.connect(self.runtime.store.path) as db:
            db.execute("UPDATE events SET event='not json'")
        with self.assertRaises(ReplayError):
            self.runtime.inspect("run")

    def test_corrupt_hash_rejected(self):
        self.runtime.submit("run", plan())
        events = self.runtime.store.events("run")
        events[0]["data"]["run"] = "changed"
        with self.assertRaises(ReplayError):
            replay(events)


class ValidationTests(unittest.TestCase):
    def test_invalid_plans(self):
        cases = [None, [], {}, [None], [{}], plan() * 2]
        for key, value in (("id", ""), ("tool", " "), ("args", []), ("idempotent", 1)):
            cases.append([{**plan()[0], key: value}])
        for value in cases:
            with self.subTest(value=value), self.assertRaises(ReplayError):
                plan_value(value)

    def test_non_json_values(self):
        for value in ({1: "x"}, (1, 2), float("inf"), {"x": object()}):
            with self.assertRaises(ReplayError):
                canonical(value)

    def test_replay_rejects_invalid_transitions_even_with_rehashed_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            tool = LedgerTool(Path(tmp) / "effects")
            runtime = Runtime(Path(tmp) / "runtime", {"ledger": tool})
            runtime.submit("run", plan())
            runtime.resume("run")
            events = runtime.store.events("run")
        mutations = []
        for index, key, value in (
            (0, "kind", "completed"),
            (0, "seq", 9),
            (0, "previous", "bad"),
            (0, "data", {"run": "run", "plan": plan(), "bindings": {}, "version": 1}),
            (0, "data", {"run": "run", "plan": plan(), "bindings": {"ledger": "x"}, "version": 2}),
            (1, "kind", "completed"),
            (1, "kind", "finished"),
            (1, "kind", "retried"),
            (1, "kind", "unknown"),
            (1, "data", {"step": "wrong", "key": "wrong"}),
            (2, "kind", "requested"),
            (2, "kind", "resolved"),
            (3, "kind", "completed"),
        ):
            altered = copy.deepcopy(events)
            altered[index][key] = value
            mutations.append(altered)
        mutations += [events + [events[-1]], [events[0], events[2]], []]
        extra = copy.deepcopy(events)
        extra[0]["extra"] = True
        mutations.append(extra)
        for altered in mutations:
            previous = "0" * 64
            for event in altered:
                if event["previous"] != "bad":
                    event["previous"] = previous
                event["hash"] = digest({k: v for k, v in event.items() if k != "hash"})
                previous = event["hash"]
            with self.subTest(events=json.dumps(altered)), self.assertRaises(ReplayError):
                replay(altered)
