"""JSON contracts and deterministic event replay."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TypeAlias, cast

JSON: TypeAlias = "None | bool | int | float | str | list[JSON] | dict[str, JSON]"


class ReplayError(ValueError):
    """Invalid input, incompatible history, or corrupt events."""


class UncertainEffect(ReplayError):
    """External evidence is required before execution can continue."""


def canonical(value: JSON) -> str:
    def check(item: JSON) -> None:
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ReplayError("JSON object keys must be strings")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ReplayError("Value is not JSON data")

    try:
        check(value)
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ReplayError("Value is not finite JSON data") from exc


def digest(value: JSON) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def object_value(value: JSON) -> dict[str, JSON]:
    if not isinstance(value, dict):
        raise ReplayError("Expected a JSON object")
    return value


def name(value: JSON) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ReplayError("Names must be nonempty strings of at most 512 characters")
    return value


def plan_value(value: JSON) -> list[dict[str, JSON]]:
    canonical(value)
    if not isinstance(value, list) or not value:
        raise ReplayError("Plan must contain at least one step")
    ids = set()
    result = []
    for entry in value:
        step = object_value(entry)
        if set(step) != {"id", "tool", "args", "idempotent"}:
            raise ReplayError("Step requires id, tool, args and idempotent")
        sid = name(step["id"])
        name(step["tool"])
        object_value(step["args"])
        if type(step["idempotent"]) is not bool or sid in ids:
            raise ReplayError("Duplicate step ID or invalid idempotent flag")
        ids.add(sid)
        result.append(step)
    return result


def request_key(run: str, step: str) -> str:
    return digest(["agent-replay/v1", run, step])


@dataclass
class State:
    run: str
    plan: list[dict[str, JSON]]
    bindings: dict[str, JSON]
    results: dict[str, JSON] = field(default_factory=dict)
    pending: dict[str, JSON] | None = None
    finished: bool = False

    def as_json(self) -> dict[str, JSON]:
        return {
            "run": self.run,
            "results": self.results,
            "pending": self.pending,
            "finished": self.finished,
        }


def replay(events: list[dict[str, JSON]]) -> State:
    if not events:
        raise ReplayError("Run does not exist")
    state: State | None = None
    previous = "0" * 64
    for index, event in enumerate(events):
        if not isinstance(event, dict) or set(event) != {"seq", "kind", "data", "previous", "hash"}:
            raise ReplayError("Invalid event envelope")
        body = {key: value for key, value in event.items() if key != "hash"}
        if type(event["seq"]) is not int or event["seq"] != index or event["previous"] != previous:
            raise ReplayError("Event sequence or hash chain mismatch")
        if digest(body) != event["hash"]:
            raise ReplayError("Event hash mismatch")
        previous = cast(str, event["hash"])
        kind, data = event["kind"], object_value(event["data"])
        if index == 0:
            if kind != "created" or set(data) != {"run", "plan", "bindings", "version"}:
                raise ReplayError("History must start with created")
            if type(data["version"]) is not int or data["version"] != 1:
                raise ReplayError("Unsupported history version")
            state = State(
                name(data["run"]), plan_value(data["plan"]), object_value(data["bindings"])
            )
            if set(state.bindings) != {cast(str, step["tool"]) for step in state.plan}:
                raise ReplayError("Tool bindings do not match plan")
            for identity in state.bindings.values():
                name(identity)
            continue
        assert state is not None
        if state.finished:
            raise ReplayError("Event after finished")
        if kind == "finished":
            if data or state.pending is not None or len(state.results) != len(state.plan):
                raise ReplayError("Premature finished event")
            state.finished = True
            continue
        if len(state.results) >= len(state.plan):
            raise ReplayError("Unexpected event after last result")
        step = state.plan[len(state.results)]
        sid = cast(str, step["id"])
        if kind == "requested":
            expected: dict[str, JSON] = {"step": sid, "key": request_key(state.run, sid)}
            if state.pending is not None or data != expected:
                raise ReplayError("Invalid request")
            state.pending = data
        elif kind == "retried":
            if state.pending is None or data != state.pending or not step["idempotent"]:
                raise ReplayError("Invalid retry")
        elif kind in ("completed", "resolved"):
            fields = {"step", "result"} | ({"evidence"} if kind == "resolved" else set())
            if state.pending is None or set(data) != fields or data["step"] != sid:
                raise ReplayError("Result without matching request")
            if kind == "resolved":
                name(data["evidence"])
            state.results[sid] = data["result"]
            state.pending = None
        else:
            raise ReplayError("Unknown event kind")
    assert state is not None
    return state
