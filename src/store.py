"""SQLite storage: the local source of truth.

Results are always keyed by `account_id`, never by handle, so an account that
changes its handle updates its existing row. Plain SQL behind one small class
keeps a later move to Postgres simple.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Account

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id  TEXT PRIMARY KEY,
    handle      TEXT NOT NULL,
    data_json   TEXT NOT NULL,
    data_hash   TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scoring_runs (
    run_id          TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    rubric_version  TEXT NOT NULL,
    rubric_json     TEXT NOT NULL,
    model           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS results (
    account_id          TEXT PRIMARY KEY REFERENCES accounts(account_id),
    handle              TEXT NOT NULL,
    run_id              TEXT NOT NULL REFERENCES scoring_runs(run_id),
    rubric_version      TEXT NOT NULL,
    verdict             TEXT NOT NULL,
    total               REAL NOT NULL,
    offer               REAL NOT NULL,
    bottleneck          REAL NOT NULL,
    assessment_json     TEXT NOT NULL,
    score_json          TEXT NOT NULL,
    payload_json        TEXT NOT NULL,
    payload_hash        TEXT NOT NULL,
    scored_at           TEXT NOT NULL,
    sync_status         TEXT NOT NULL DEFAULT 'pending',
    synced_hash         TEXT,
    sync_attempts       INTEGER NOT NULL DEFAULT 0,
    last_sync_error     TEXT,
    airtable_record_id  TEXT,
    synced_at           TEXT
);
CREATE TABLE IF NOT EXISTS run_results (
    run_id       TEXT NOT NULL REFERENCES scoring_runs(run_id),
    account_id   TEXT NOT NULL,
    handle       TEXT NOT NULL,
    verdict      TEXT NOT NULL,
    total        REAL NOT NULL,
    score_json   TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


@dataclass
class StoredResult:
    account_id: str
    handle: str
    run_id: str
    rubric_version: str
    rubric_json: str
    verdict: str
    total: float
    offer: float
    bottleneck: float
    assessment_json: str
    score_json: str
    payload: dict[str, Any]
    payload_hash: str
    scored_at: str
    sync_status: str
    synced_hash: str | None
    sync_attempts: int
    last_sync_error: str | None
    airtable_record_id: str | None


class Store:
    def __init__(self, path: str | Path = ":memory:"):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- accounts ----------------------------------------------------------

    def upsert_account(self, account: Account) -> None:
        data = account.model_dump_json()
        self.conn.execute(
            """INSERT INTO accounts (account_id, handle, data_json, data_hash, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(account_id) DO UPDATE SET
                 handle = excluded.handle, data_json = excluded.data_json,
                 data_hash = excluded.data_hash, updated_at = excluded.updated_at""",
            (account.account_id, account.handle, data, hashlib.sha256(data.encode()).hexdigest()[:16], now_iso()),
        )
        self.conn.commit()

    def get_account(self, account_id: str) -> Account | None:
        row = self.conn.execute("SELECT data_json FROM accounts WHERE account_id = ?", (account_id,)).fetchone()
        return Account.model_validate_json(row["data_json"]) if row else None

    def count(self, table: str) -> int:
        assert table in {"accounts", "results", "scoring_runs", "run_results"}
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    # -- runs and results --------------------------------------------------

    def start_run(self, rubric_version: str, rubric_json: str, model: str) -> str:
        run_id = f"run_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
        self.conn.execute(
            "INSERT INTO scoring_runs (run_id, started_at, rubric_version, rubric_json, model) VALUES (?, ?, ?, ?, ?)",
            (run_id, now_iso(), rubric_version, rubric_json, model),
        )
        self.conn.commit()
        return run_id

    def save_result(
        self, *, run_id: str, account: Account, verdict: str, total: float, offer: float, bottleneck: float,
        rubric_version: str, assessment_json: str, score_json: str, payload: dict[str, Any], scored_at: str,
    ) -> None:
        """Upsert by account_id. The row goes back to 'pending' whenever its Airtable payload changes."""
        p_hash = payload_hash(payload)
        self.conn.execute(
            """INSERT INTO results (account_id, handle, run_id, rubric_version, verdict, total, offer, bottleneck,
                   assessment_json, score_json, payload_json, payload_hash, scored_at, sync_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
               ON CONFLICT(account_id) DO UPDATE SET
                 handle = excluded.handle, run_id = excluded.run_id, rubric_version = excluded.rubric_version,
                 verdict = excluded.verdict, total = excluded.total, offer = excluded.offer,
                 bottleneck = excluded.bottleneck, assessment_json = excluded.assessment_json,
                 score_json = excluded.score_json, payload_json = excluded.payload_json,
                 payload_hash = excluded.payload_hash, scored_at = excluded.scored_at,
                 sync_status = CASE WHEN results.synced_hash = excluded.payload_hash
                                    THEN results.sync_status ELSE 'pending' END""",
            (account.account_id, account.handle, run_id, rubric_version, verdict, total, offer, bottleneck,
             assessment_json, score_json, json.dumps(payload), p_hash, scored_at),
        )
        self.conn.execute(
            "INSERT INTO run_results (run_id, account_id, handle, verdict, total, score_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, account.account_id, account.handle, verdict, total, score_json, now_iso()),
        )
        self.conn.commit()

    def change_handle(self, account_id: str, new_handle: str) -> None:
        """Used by rename-test: same account_id, new handle, so the row is updated and re-queued for sync."""
        account = self.get_account(account_id)
        if account is None:
            raise KeyError(account_id)
        self.upsert_account(account.model_copy(update={"handle": new_handle}))
        result = self.get_result(account_id)
        if result is not None:
            payload = dict(result.payload, Handle=new_handle)
            p_hash = payload_hash(payload)
            self.conn.execute(
                """UPDATE results SET handle = ?, payload_json = ?, payload_hash = ?,
                       sync_status = CASE WHEN synced_hash = ? THEN sync_status ELSE 'pending' END
                   WHERE account_id = ?""",
                (new_handle, json.dumps(payload), p_hash, p_hash, account_id),
            )
            self.conn.commit()

    _RESULT_SQL = """SELECT r.*, s.rubric_json FROM results r JOIN scoring_runs s ON s.run_id = r.run_id"""

    def _to_result(self, row: sqlite3.Row) -> StoredResult:
        d = dict(row)
        d["payload"] = json.loads(d.pop("payload_json"))
        for k in ("synced_at",):
            d.pop(k, None)
        return StoredResult(**d)

    def get_result(self, account_id: str) -> StoredResult | None:
        row = self.conn.execute(self._RESULT_SQL + " WHERE r.account_id = ?", (account_id,)).fetchone()
        return self._to_result(row) if row else None

    def results(self) -> list[StoredResult]:
        rows = self.conn.execute(self._RESULT_SQL + " ORDER BY r.handle").fetchall()
        return [self._to_result(r) for r in rows]

    # -- sync bookkeeping --------------------------------------------------

    def pending_sync(self) -> list[StoredResult]:
        rows = self.conn.execute(
            self._RESULT_SQL + " WHERE r.sync_status != 'synced' OR r.synced_hash IS NULL"
            " OR r.payload_hash != r.synced_hash ORDER BY r.handle"
        ).fetchall()
        return [self._to_result(r) for r in rows]

    def mark_synced(self, account_id: str, record_id: str, sent_hash: str) -> None:
        """Called only after Airtable confirmed the record. If the row changed meanwhile it stays pending."""
        self.conn.execute(
            """UPDATE results SET synced_hash = ?, airtable_record_id = ?, synced_at = ?, last_sync_error = NULL,
                   sync_status = CASE WHEN payload_hash = ? THEN 'synced' ELSE 'pending' END
               WHERE account_id = ?""",
            (sent_hash, record_id, now_iso(), sent_hash, account_id),
        )
        self.conn.commit()

    def mark_failed(self, account_ids: list[str], error: str) -> None:
        self.conn.executemany(
            "UPDATE results SET sync_status = 'failed', sync_attempts = sync_attempts + 1, last_sync_error = ? "
            "WHERE account_id = ?",
            [(error[:500], a) for a in account_ids],
        )
        self.conn.commit()

    def sync_counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT sync_status, COUNT(*) FROM results GROUP BY sync_status").fetchall()
        return {r[0]: r[1] for r in rows}
