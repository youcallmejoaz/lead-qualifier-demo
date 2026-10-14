"""Throttled, idempotent Airtable upsert worker.

- Reads unsynced or changed results from SQLite.
- 10 records per request, upserted on "Account ID" (performUpsert), so no duplicates.
- At most 4 requests per second (Airtable allows 5 per base).
- 429: wait 30 s and retry the same batch. Other errors: back off and retry up to 3 times,
  then mark the batch failed in SQLite. It stays queued for the next run, so nothing is dropped.
- A record is marked synced only when Airtable's response confirms it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .airtable_client import ACCOUNT_ID_FIELD
from .evidence import QuoteCheck, VerifiedAssessment
from .models import PILLAR_LABELS, Account, Pillar
from .scoring import ScoreResult
from .store import StoredResult, Store

BATCH_SIZE = 10
MAX_REQUESTS_PER_SECOND = 4
RATE_LIMIT_WAIT_SECONDS = 30
MAX_RATE_LIMIT_RETRIES = 5
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 2


class LeadsClient(Protocol):
    def upsert_leads(self, records: list[dict[str, Any]]) -> dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Airtable payload
# ---------------------------------------------------------------------------


def _quote_line(q: QuoteCheck, trigger: str) -> str:
    where = q.source_id or "?"
    if q.date:
        where = f"{where} · {q.date}"
    return f'[{where}] "{q.quote}" ({trigger})'


def lead_fields(account: Account, va: VerifiedAssessment, result: ScoreResult, scored_at: str) -> dict[str, Any]:
    evidence_lines: list[str] = []
    for pillar in (Pillar.offer_works, Pillar.founder_bottleneck):
        evidence_lines.append(f"{PILLAR_LABELS[pillar].upper()} ({result.pillar(pillar).score:.1f}/10)")
        quotes = [(q, t.trigger) for t in va.pillar(pillar).triggers for q in t.quotes if q.counts]
        evidence_lines += [_quote_line(q, t) for q, t in quotes] or ["(no verified evidence)"]
        evidence_lines.append("")

    hit = set(result.exclusions_hit)
    exclusion_lines = [
        _quote_line(q, e.exclusion) for e in va.exclusions if e.exclusion in hit for q in e.quotes if q.counts
    ]
    return {
        ACCOUNT_ID_FIELD: account.account_id,
        "Handle": account.handle,
        "Verdict": result.verdict.value,
        "Total Score": result.total,
        "Offer Works Score": result.offer_works.score,
        "Bottleneck Score": result.founder_bottleneck.score,
        "Summary": va.summary,
        "Evidence": "\n".join(evidence_lines).strip(),
        "Exclusions Hit": "\n".join(exclusion_lines) if exclusion_lines else "None",
        "Rubric Version": result.rubric_version,
        "Last Scored": scored_at,
    }


# ---------------------------------------------------------------------------
# Throttle and worker
# ---------------------------------------------------------------------------


class Throttle:
    """Guarantees a minimum gap between request starts (0.25 s for 4 requests per second)."""

    def __init__(self, max_per_second: float = MAX_REQUESTS_PER_SECOND,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.min_interval = 1.0 / max_per_second
        self.clock = clock
        self.sleep = sleep
        self._last: float | None = None

    def wait(self) -> float:
        """Block until a request may start. Returns the seconds waited."""
        waited = 0.0
        if self._last is not None:
            gap = self.min_interval - (self.clock() - self._last)
            if gap > 0:
                self.sleep(gap)
                waited = gap
        self._last = self.clock()
        return waited


def http_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) if response is not None else None
    return status if status is not None else getattr(exc, "status_code", None)


@dataclass
class BatchLog:
    number: int
    size: int
    created: int = 0
    updated: int = 0
    confirmed: int = 0
    ms: float = 0.0
    retries: int = 0
    rate_limited: int = 0
    status: str = "pending"  # ok | failed | partial
    error: str | None = None


@dataclass
class SyncReport:
    batches: list[BatchLog] = field(default_factory=list)
    requests: int = 0

    @property
    def records(self) -> int:
        return sum(b.size for b in self.batches)

    @property
    def synced(self) -> int:
        return sum(b.confirmed for b in self.batches)

    @property
    def failed(self) -> int:
        return sum(b.size for b in self.batches if b.status == "failed")

    @property
    def retries(self) -> int:
        return sum(b.retries for b in self.batches)


class SyncWorker:
    def __init__(self, store: Store, client: LeadsClient, *, throttle: Throttle | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.perf_counter,
                 on_batch: Callable[[BatchLog], None] | None = None, on_wait: Callable[[str], None] | None = None,
                 batch_size: int = BATCH_SIZE):
        self.store = store
        self.client = client
        self.throttle = throttle or Throttle()
        self.sleep = sleep
        self.clock = clock
        self.on_batch = on_batch or (lambda b: None)
        self.on_wait = on_wait or (lambda msg: None)
        self.batch_size = batch_size

    def run(self) -> SyncReport:
        pending = self.store.pending_sync()
        report = SyncReport()
        for i in range(0, len(pending), self.batch_size):
            batch = pending[i:i + self.batch_size]
            log = self._push(len(report.batches) + 1, batch, report)
            report.batches.append(log)
            self.on_batch(log)
        return report

    def _push(self, number: int, batch: list[StoredResult], report: SyncReport) -> BatchLog:
        log = BatchLog(number=number, size=len(batch))
        records = [r.payload for r in batch]
        errors = 0
        while True:
            self.throttle.wait()
            start = self.clock()
            report.requests += 1
            try:
                response = self.client.upsert_leads(records)
            except Exception as exc:  # noqa: BLE001 - every failure is handled below, nothing is dropped
                log.ms = (self.clock() - start) * 1000
                status = http_status(exc)
                if status == 429:
                    log.rate_limited += 1
                    if log.rate_limited > MAX_RATE_LIMIT_RETRIES:
                        return self._fail(log, batch, f"429 rate limited {log.rate_limited} times")
                    log.retries += 1
                    self.on_wait(f"batch {number}: 429 rate limited, waiting {RATE_LIMIT_WAIT_SECONDS}s "
                                 f"then retrying the same batch")
                    self.sleep(RATE_LIMIT_WAIT_SECONDS)
                    continue
                errors += 1
                if errors > MAX_RETRIES:
                    return self._fail(log, batch, f"{status or type(exc).__name__}: {exc}")
                log.retries += 1
                delay = BACKOFF_BASE_SECONDS ** errors
                self.on_wait(f"batch {number}: error {status or type(exc).__name__}, retry {errors}/{MAX_RETRIES} "
                             f"in {delay}s")
                self.sleep(delay)
                continue

            log.ms = (self.clock() - start) * 1000
            log.created = len(response.get("createdRecords", []))
            log.updated = len(response.get("updatedRecords", []))
            confirmed = {
                rec.get("fields", {}).get(ACCOUNT_ID_FIELD): rec.get("id") for rec in response.get("records", [])
            }
            for row in batch:
                record_id = confirmed.get(row.account_id)
                if record_id:
                    self.store.mark_synced(row.account_id, record_id, row.payload_hash)
                    log.confirmed += 1
            log.status = "ok" if log.confirmed == log.size else "partial"
            if log.status == "partial":
                log.error = f"{log.size - log.confirmed} record(s) not confirmed; left queued"
            return log

    def _fail(self, log: BatchLog, batch: list[StoredResult], error: str) -> BatchLog:
        self.store.mark_failed([r.account_id for r in batch], error)
        log.status = "failed"
        log.error = error
        return log
