"""Sequential durable dispatch with explicit uncertain-effect handling."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Protocol, cast

from .model import (
    JSON,
    ReplayError,
    State,
    UncertainEffect,
    canonical,
    name,
    plan_value,
    replay,
    request_key,
)
from .storage import EventStore, writer_lock


class Tool(Protocol):
    identity: str
    idempotent: bool

    def invoke(self, key: str, args: dict[str, JSON]) -> JSON: ...


class Runtime:
    def __init__(self, path: Path, tools: Mapping[str, Tool]):
        self.store = EventStore(path)
        self.tools = dict(tools)

    def submit(self, run: str, plan: JSON) -> State:
        name(run)
        steps = plan_value(json.loads(canonical(plan)))
        bindings: dict[str, JSON] = {}
        for step in steps:
            tool = self.tools.get(cast(str, step["tool"]))
            if tool is None or tool.idempotent is not step["idempotent"]:
                raise ReplayError("Missing tool or capability mismatch")
            bindings[cast(str, step["tool"])] = name(tool.identity)
        with writer_lock(self.store.path):
            if self.store.exists(run):
                state = self.inspect(run)
                if (
                    canonical(cast(JSON, state.plan)) != canonical(cast(JSON, steps))
                    or state.bindings != bindings
                ):
                    raise ReplayError("Run already exists with a different plan or tool binding")
                return state
            self.store.append(
                run,
                "created",
                {"run": run, "plan": cast(JSON, steps), "bindings": bindings, "version": 1},
            )
            return self.inspect(run)

    def inspect(self, run: str) -> State:
        return replay(self.store.events(run))

    def resume(self, run: str, hook: Callable[[str], None] | None = None) -> State:
        def checkpoint(stage: str) -> None:
            if hook:
                hook(stage)

        with writer_lock(self.store.path):
            state = self.inspect(run)
            for step in state.plan:
                tool = self.tools.get(cast(str, step["tool"]))
                if (
                    tool is None
                    or tool.identity != state.bindings[cast(str, step["tool"])]
                    or tool.idempotent is not step["idempotent"]
                ):
                    raise ReplayError("Tool binding or capability changed")
            for step in state.plan[len(state.results) :]:
                sid = cast(str, step["id"])
                tool = self.tools[cast(str, step["tool"])]
                request: dict[str, JSON] = {"step": sid, "key": request_key(run, sid)}
                if state.pending is not None:
                    if not tool.idempotent:
                        raise UncertainEffect(f"Step {sid} requires external reconciliation")
                    self.store.append(run, "retried", request)
                else:
                    self.store.append(run, "requested", request)
                checkpoint("after_request")
                result = tool.invoke(cast(str, request["key"]), cast(dict[str, JSON], step["args"]))
                checkpoint("after_effect")
                self.store.append(run, "completed", {"step": sid, "result": result})
                checkpoint("after_ack")
                state = self.inspect(run)
            if not state.finished:
                self.store.append(run, "finished", {})
            return self.inspect(run)

    def reconcile(self, run: str, result: JSON, evidence: str) -> State:
        name(evidence)
        canonical(result)
        with writer_lock(self.store.path):
            state = self.inspect(run)
            if state.pending is None:
                raise ReplayError("No pending request to reconcile")
            self.store.append(
                run,
                "resolved",
                {"step": state.pending["step"], "result": result, "evidence": evidence},
            )
            return self.inspect(run)
