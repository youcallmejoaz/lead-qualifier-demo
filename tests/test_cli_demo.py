"""Runs the real CLI commands end to end with fake Claude and Airtable clients."""

import pytest
from typer.testing import CliRunner

from src import cli
from src.config import Settings
from tests.conftest import ACCOUNTS
from tests.fakes import FakeAirtable, FakeClaude

runner = CliRunner()


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    claude, airtable = FakeClaude(), FakeAirtable()
    settings = Settings(anthropic_api_key="test", anthropic_model="claude-sonnet-5-5", airtable_api_key="test",
                        airtable_base_id="appTEST", rubric_table="Rubric", leads_table="Leads",
                        db_path=tmp_path / "test.db", accounts_dir=ACCOUNTS)
    deps = cli.Deps(settings=lambda: settings, claude=lambda s: claude, airtable=lambda s: airtable,
                    sleep=lambda s: None)
    monkeypatch.setattr(cli, "DEPS", deps)
    monkeypatch.setattr(cli.display.console, "width", 160)
    yield claude, airtable, deps
    deps.get("store").close()


def invoke(*args):
    result = runner.invoke(cli.app, list(args))
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    return result


def test_demo_runs_end_to_end(fakes):
    claude, airtable, deps = fakes
    result = invoke("demo")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Demo complete" in out
    assert "PASS: updated, not duplicated" in out
    assert "invented or misquoted, dropped" in out  # the fake's invented quote was caught
    # exactly one Airtable record per account, with the original handles restored
    by_handle = {r["fields"]["Handle"]: r["fields"] for r in airtable.leads.values()}
    assert sorted(by_handle) == ["sample_qualified", "sample_systemised", "sample_weak_offer"]
    assert by_handle["sample_qualified"]["Verdict"] == "qualified"
    assert by_handle["sample_weak_offer"]["Verdict"] == "rejected"
    assert by_handle["sample_systemised"]["Verdict"] == "rejected"
    assert by_handle["sample_systemised"]["Exclusions Hit"].startswith("[link_in_bio]")
    assert "I personally coach all 140 clients" not in by_handle["sample_qualified"]["Evidence"]
    assert "[p02 · 2026-08-07] \"Round 4 SOLD OUT in 48 hours\" (Sold out launches)" in \
        by_handle["sample_qualified"]["Evidence"]
    # every Claude call used the rubric from Airtable and no forced tool_choice
    assert all(c["tool_choice"] == {"type": "auto"} for c in claude.calls)
    assert "Manual hand raiser loop" in claude.calls[0]["system"][0]["text"]


def test_preview_reflects_airtable_weight_change_without_saving(fakes):
    claude, airtable, deps = fakes
    assert invoke("score").exit_code == 0
    calls = len(claude.calls)
    before = deps.get("store").get_result("17841400000000002").total

    airtable.set_weight("Founder does all delivery", 0)
    result = invoke("preview")
    assert result.exit_code == 0, result.output
    assert "weight 7 → 0" in result.output
    assert "Nothing was saved" in result.output
    assert len(claude.calls) == calls  # preview reuses stored judgments
    assert deps.get("store").get_result("17841400000000002").total == before


def test_rename_test_leaves_one_record(fakes):
    claude, airtable, deps = fakes
    invoke("score")
    invoke("sync")
    result = invoke("rename-test", "--keep")
    assert result.exit_code == 0, result.output
    records = airtable.find_leads("17841400000000001")
    assert len(records) == 1
    assert records[0]["fields"]["Handle"] == "sample_qualified_rebrand"
    assert len(airtable.leads) == 3


def test_score_single_handle(fakes):
    claude, airtable, deps = fakes
    result = invoke("score", "--handle", "sample_systemised")
    assert result.exit_code == 0, result.output
    assert len(claude.calls) == 1
    assert "Already has a team" in result.output


def test_bad_rubric_stops_scoring(fakes):
    claude, airtable, deps = fakes
    airtable.set_weight("Slow manual replies", 15)
    result = invoke("score")
    assert result.exit_code == 1
    assert "Rubric problems" in result.output and "Slow manual replies" in result.output
    assert claude.calls == []


def test_setup_reports_missing_fields_and_creates_them(fakes):
    claude, airtable, deps = fakes
    del airtable.schema["Leads"]["Evidence"]
    airtable.schema["Rubric"] = None
    result = invoke("setup-airtable")
    assert result.exit_code == 1
    assert "add a field Evidence" in result.output.replace("\n", " ") or "Evidence" in result.output
    result = invoke("setup-airtable", "--create-missing")
    assert result.exit_code == 0, result.output
    assert "Evidence" in airtable.schema["Leads"]
    assert airtable.schema["Rubric"] is not None
