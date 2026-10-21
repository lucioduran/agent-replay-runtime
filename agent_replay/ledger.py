"""An independent local effect store for crash and retry experiments."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from .model import JSON, ReplayError, canonical, name
from .storage import connection


class LedgerTool:
    def __init__(self, path: Path, *, idempotent: bool = True):
        self.path = path.resolve()
        self.idempotent = idempotent
        self.identity = f"ledger/v1:{self.path}:{idempotent}"
        with connection(self.path) as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS effects "
                "(id INTEGER PRIMARY KEY, request_key TEXT NOT NULL, payload TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS receipts "
                "(request_key TEXT PRIMARY KEY, payload TEXT NOT NULL, result TEXT NOT NULL);"
            )

    def invoke(self, key: str, args: dict[str, JSON]) -> JSON:
        name(key)
        payload = canonical(args)
        with connection(self.path) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                if self.idempotent:
                    row = db.execute(
                        "SELECT payload, result FROM receipts WHERE request_key=?", (key,)
                    ).fetchone()
                    if row:
                        if row[0] != payload:
                            raise ReplayError("Idempotency key reused with different arguments")
                        return cast(JSON, json.loads(row[1]))
                cursor = db.execute(
                    "INSERT INTO effects(request_key, payload) VALUES (?, ?)", (key, payload)
                )
                result: dict[str, JSON] = {"effect_id": cursor.lastrowid, "payload": args}
                if self.idempotent:
                    db.execute(
                        "INSERT INTO receipts VALUES (?, ?, ?)", (key, payload, canonical(result))
                    )
                return result

    def effects(self) -> list[dict[str, JSON]]:
        with connection(self.path) as db:
            return [
                {"effect_id": row[0], "key": row[1], "payload": json.loads(row[2])}
                for row in db.execute("SELECT id, request_key, payload FROM effects ORDER BY id")
            ]
