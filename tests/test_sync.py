import pytest

from src.store import Store
from src.sync import (BATCH_SIZE, MAX_RETRIES, RATE_LIMIT_WAIT_SECONDS, SyncWorker, Throttle)
from tests.fakes import FakeAirtable, FakeClock
from tests.test_store import add_result


def _store_with(n: int) -> Store:
    s = Store(":memory:")
    for i in range(n):
        add_result(s, account_id=f"acct{i:03d}", handle=f"coach_{i:03d}")
    return s


def _worker(store, client, clock):
    return SyncWorker(store, client, throttle=Throttle(4, clock=clock, sleep=clock.sleep),
                      sleep=clock.sleep, clock=clock)


def test_25_records_become_3_requests():
    store, client, clock = _store_with(25), FakeAirtable(), FakeClock()
    report = _worker(store, client, clock).run()
    assert [len(r) for r in client.requests] == [10, 10, 5]
    assert report.requests == 3 and report.synced == 25 and report.failed == 0
    assert len(client.leads) == 25
    assert store.pending_sync() == []
    assert BATCH_SIZE == 10


def test_resync_is_a_no_op():
    store, client, clock = _store_with(3), FakeAirtable(), FakeClock()
    _worker(store, client, clock).run()
    report = _worker(store, client, clock).run()
    assert report.requests == 0 and len(client.requests) == 1


def test_throttle_spaces_requests_at_least_250ms():
    clock = FakeClock()
    starts = []
    t = Throttle(4, clock=clock, sleep=clock.sleep)
    for _ in range(9):
        t.wait()
        starts.append(clock())
        clock.now += 0.05  # each request takes 50 ms
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(g >= 0.25 - 1e-9 for g in gaps)
    # never more than 4 request starts inside any 1 second window
    for i, s in enumerate(starts):
        assert sum(1 for x in starts[i:] if x < s + 1.0) <= 4


def test_throttle_does_not_wait_when_requests_are_slow():
    clock = FakeClock()
    t = Throttle(4, clock=clock, sleep=clock.sleep)
    t.wait()
    clock.now += 0.4
    assert t.wait() == 0
    assert clock.sleeps == []


def test_429_waits_30s_and_retries_the_same_batch():
    store, clock = _store_with(12), FakeClock()
    client = FakeAirtable(errors=[429])
    report = _worker(store, client, clock).run()
    assert RATE_LIMIT_WAIT_SECONDS in clock.sleeps
    assert client.requests[0] == client.requests[1]  # same batch retried
    assert report.batches[0].rate_limited == 1 and report.batches[0].retries == 1
    assert report.synced == 12 and report.failed == 0
    assert len(client.leads) == 12


def test_other_errors_retry_3_times_then_stay_queued():
    store, clock = _store_with(4), FakeClock()
    client = FakeAirtable(errors=[500] * (MAX_RETRIES + 1))
    report = _worker(store, client, clock).run()
    assert len(client.requests) == MAX_RETRIES + 1
    assert [s for s in clock.sleeps if s >= 1] == [2, 4, 8]  # exponential backoff
    assert report.failed == 4 and report.synced == 0
    assert store.sync_counts() == {"failed": 4}
    # nothing dropped: the next run picks the same records up and succeeds
    report2 = _worker(store, client, clock).run()
    assert report2.synced == 4 and store.sync_counts() == {"synced": 4}


def test_transient_error_then_success():
    store, clock = _store_with(2), FakeClock()
    client = FakeAirtable(errors=[503])
    report = _worker(store, client, clock).run()
    assert report.synced == 2 and report.batches[0].retries == 1


def test_records_missing_from_response_stay_queued():
    store, clock = _store_with(3), FakeClock()

    class Partial(FakeAirtable):
        def upsert_leads(self, records):
            out = super().upsert_leads(records)
            out["records"] = out["records"][:2]
            return out

    report = _worker(store, Partial(), clock).run()
    assert report.synced == 2 and report.batches[0].status == "partial"
    assert len(store.pending_sync()) == 1


@pytest.mark.parametrize("n", [1, 10, 11])
def test_batch_sizes(n):
    store, client, clock = _store_with(n), FakeAirtable(), FakeClock()
    _worker(store, client, clock).run()
    assert sum(len(r) for r in client.requests) == n
    assert all(len(r) <= 10 for r in client.requests)
