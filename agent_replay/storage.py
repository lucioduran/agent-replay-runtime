"""SQLite event storage and a process-lifetime single-writer lock."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from .model import JSON, ReplayError, digest, name, replay


class BusyError(ReplayError):
    """Another writer holds this runtime database."""


@contextmanager
def writer_lock(path: Path) -> Iterator[None]:
    with Path(str(path.resolve()) + ".lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BusyError("Runtime database already has a writer") from exc
        try:
            yield
        finally:
            if sys.platform == "win32":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def connection(path: Path) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(path, timeout=5)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        yield db
    finally:
        db.close()


class EventStore:
    def __init__(self, path: Path):
        self.path = path.resolve()
        with writer_lock(self.path), connection(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS events "
                "(run TEXT NOT NULL, seq INTEGER NOT NULL, event TEXT NOT NULL, "
                "PRIMARY KEY(run, seq))"
            )
            db.commit()

    def events(self, run: str) -> list[dict[str, JSON]]:
        name(run)
        with connection(self.path) as db:
            rows = db.execute("SELECT event FROM events WHERE run=? ORDER BY seq", (run,))
            try:
                events = [cast(dict[str, JSON], json.loads(row[0])) for row in rows]
                state = replay(events)
                if state.run != run:
                    raise ReplayError("Run identity mismatch")
                return events
            except (TypeError, KeyError, json.JSONDecodeError) as exc:
                raise ReplayError("Malformed event storage") from exc

    def exists(self, run: str) -> bool:
        with connection(self.path) as db:
            return db.execute("SELECT 1 FROM events WHERE run=?", (run,)).fetchone() is not None

    def append(self, run: str, kind: str, data: dict[str, JSON]) -> None:
        """Caller must hold writer_lock for the entire read/dispatch/write cycle."""
        events = self.events(run) if self.exists(run) else []
        event: dict[str, JSON] = {
            "seq": len(events),
            "kind": kind,
            "data": data,
            "previous": events[-1]["hash"] if events else "0" * 64,
        }
        event["hash"] = digest(event)
        replay([*events, event])
        with connection(self.path) as db:
            with db:
                db.execute(
                    "INSERT INTO events(run, seq, event) VALUES (?, ?, ?)",
                    (run, len(events), json.dumps(event, sort_keys=True, allow_nan=False)),
                )
