from src.models import Account
from src.store import Store
from src.sync import SyncWorker, Throttle
from tests.fakes import FakeAirtable, FakeClock


def add_result(store: Store, *, account_id: str, handle: str, total: float = 50.0, run_id: str | None = None):
    account = Account(account_id=account_id, handle=handle, followers=1000)
    store.upsert_account(account)
    run_id = run_id or store.start_run("v1", "[]", "test-model")
    store.save_result(
        run_id=run_id, account=account, verdict="review", total=total, offer=5, bottleneck=5,
        rubric_version="v1", assessment_json="{}", score_json="{}",
        payload={"Account ID": account_id, "Handle": handle, "Total Score": total}, scored_at="2026-10-14T00:00:00Z",
    )
    return account


def _sync(store, client):
    clock = FakeClock()
    return SyncWorker(store, client, throttle=Throttle(4, clock=clock, sleep=clock.sleep),
                      sleep=clock.sleep, clock=clock).run()


def test_upsert_by_account_id_after_handle_change(store):
    add_result(store, account_id="17841400000000001", handle="old_handle")
    add_result(store, account_id="17841400000000001", handle="new_handle")
    assert store.count("results") == 1
    assert store.count("accounts") == 1
    assert store.get_result("17841400000000001").handle == "new_handle"
    assert store.count("run_results") == 2  # history keeps both runs


def test_handle_change_updates_the_same_airtable_record(store):
    client = FakeAirtable()
    add_result(store, account_id="acct1", handle="coach_old")
    _sync(store, client)
    [record_id] = client.leads

    store.change_handle("acct1", "coach_new")
    assert len(store.pending_sync()) == 1
    report = _sync(store, client)

    assert report.synced == 1
    assert list(client.leads) == [record_id]  # still exactly one record, same ID
    assert client.leads[record_id]["fields"]["Handle"] == "coach_new"
    assert store.get_result("acct1").airtable_record_id == record_id


def test_unchanged_rescore_keeps_synced_status(store):
    client = FakeAirtable()
    add_result(store, account_id="acct1", handle="coach")
    _sync(store, client)
    add_result(store, account_id="acct1", handle="coach")  # identical payload
    assert store.pending_sync() == []
    add_result(store, account_id="acct1", handle="coach", total=60)  # changed payload
    assert len(store.pending_sync()) == 1
