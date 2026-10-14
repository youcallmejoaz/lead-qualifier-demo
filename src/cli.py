"""Command line interface: python -m src.cli <command>."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import typer
from pydantic import ValidationError

from . import display
from .airtable_client import LEADS_SCHEMA, RUBRIC_SCHEMA, AirtableClient, type_ok
from .config import ConfigError, Settings, load_settings
from .display import console
from .evidence import VerifiedAssessment, verify_assessment
from .models import Account, Verdict
from .qualifier import Qualifier, QualifierError
from .rubric import Rubric, RubricError, load_rubric
from .scoring import ScoreResult, definition_changes, score
from .store import Store, now_iso
from .sync import SyncWorker, Throttle, lead_fields

app = typer.Typer(add_completion=False, no_args_is_help=True, rich_markup_mode="rich",
                  help="Lead Qualifier Demo: qualify Instagram coaches with Claude, tune in Airtable, sync safely.")


# ---------------------------------------------------------------------------
# Dependencies (swapped for fakes in tests)
# ---------------------------------------------------------------------------


def _claude(settings: Settings):
    import anthropic

    return anthropic.Anthropic(api_key=settings.require_anthropic())


def _airtable(settings: Settings):
    key, base = settings.require_airtable()
    return AirtableClient(key, base, settings.rubric_table, settings.leads_table)


@dataclass
class Deps:
    settings: Callable[[], Settings] = load_settings
    claude: Callable[[Settings], object] = _claude
    airtable: Callable[[Settings], object] = _airtable
    store: Callable[[Settings], Store] = lambda s: Store(s.db_path)
    sleep: Callable[[float], None] = time.sleep
    _cache: dict = field(default_factory=dict)

    def get(self, name: str):
        if name not in self._cache:
            s = self._cache.setdefault("settings", self.settings())
            self._cache[name] = s if name == "settings" else getattr(self, name)(s)
        return self._cache[name]

    def reset(self) -> None:
        self._cache.clear()


DEPS = Deps()


def _fail(msg: str) -> None:
    console.print(f"[bold red]✗ {msg}[/]")
    raise typer.Exit(1)


def _load_rubric() -> Rubric:
    try:
        rubric = load_rubric(DEPS.get("airtable"))
    except RubricError as err:
        display.rubric_errors(err)
        raise typer.Exit(1)
    display.rubric_header(rubric)
    return rubric


def _account_files(settings: Settings, handle: str | None) -> list[Account]:
    files = sorted(Path(settings.accounts_dir).glob("*.json"))
    accounts: list[Account] = []
    for f in files:
        try:
            account = Account.model_validate_json(f.read_text(encoding="utf-8"))
        except (ValidationError, ValueError) as err:
            console.print(f"[red]✗ {f.name} is not a valid account file, skipped:[/] {err}")
            continue
        if handle and handle.lstrip("@").lower() not in (account.handle, f.stem.lower()):
            continue
        accounts.append(account)
    return accounts


# ---------------------------------------------------------------------------
# Shared steps
# ---------------------------------------------------------------------------


def score_and_store(accounts: list[Account], rubric: Rubric) -> list[tuple[Account, VerifiedAssessment, ScoreResult]]:
    settings: Settings = DEPS.get("settings")
    store: Store = DEPS.get("store")
    qualifier = Qualifier(DEPS.get("claude"), settings.anthropic_model, rubric)
    run_id = store.start_run(rubric.version, rubric.to_json(), settings.anthropic_model)
    console.print(f"[dim]Run {run_id} · model {settings.anthropic_model}[/]")

    rows = []
    for account in accounts:
        store.upsert_account(account)
        try:
            with console.status(f"Claude is reading @{account.handle}'s posts, comments and link in bio page..."):
                assessment = qualifier.assess(account)
        except QualifierError as err:
            console.print(f"[red]✗ {err}. Skipped; the other accounts still run.[/]")
            continue
        va = verify_assessment(assessment, account, rubric)
        result = score(va, rubric)
        scored_at = now_iso()
        store.save_result(
            run_id=run_id, account=account, verdict=result.verdict.value, total=result.total,
            offer=result.offer_works.score, bottleneck=result.founder_bottleneck.score,
            rubric_version=rubric.version, assessment_json=va.model_dump_json(),
            score_json=result.model_dump_json(), payload=lead_fields(account, va, result, scored_at),
            scored_at=scored_at,
        )
        display.account_card(account, va, result)
        rows.append((account, va, result))
    return rows


def run_sync():
    store: Store = DEPS.get("store")
    pending = len(store.pending_sync())
    console.print(f"{display._n(pending, 'result')} queued for Airtable · batches of 10 · max 4 requests per second")
    # One throttle per process, so back to back syncs (e.g. in rename-test) share the 4 req/s budget.
    throttle = DEPS._cache.setdefault("throttle", Throttle(sleep=DEPS.sleep))
    worker = SyncWorker(store, DEPS.get("airtable"), throttle=throttle, sleep=DEPS.sleep, on_batch=display.batch_line,
                        on_wait=lambda m: console.print(f"  [yellow]⏳ {m}[/]"))
    report = worker.run()
    display.sync_summary(report, store.sync_counts())
    return report


def check_airtable(create_missing: bool) -> bool:
    client = DEPS.get("airtable")
    settings: Settings = DEPS.get("settings")
    try:
        tables = client.table_fields()
    except Exception as err:  # noqa: BLE001
        _fail(f"Could not read the base schema ({err}). The token needs the schema.bases:read scope.")
    all_ok = True
    instructions: list[str] = []
    for table, spec in ((settings.rubric_table, RUBRIC_SCHEMA), (settings.leads_table, LEADS_SCHEMA)):
        fields = tables.get(table)
        if fields is None:
            if create_missing:
                client.create_table(table, spec)
                console.print(f"[green]✓ Created table {table} with all fields[/]")
                fields = {n: s[0] for n, s in spec.items()}
            else:
                all_ok = False
                instructions.append(f"Create a table named [bold]{table}[/] with the fields below "
                                    f"(the first one is the primary field).")
                display.setup_table(table, [(n, s[2], "[red]✗ table missing[/]") for n, s in spec.items()])
                continue
        rows = []
        for name, s in spec.items():
            actual = fields.get(name)
            if actual is None and create_missing:
                client.create_field(table, name, s)
                rows.append((name, s[2], "[green]✓ created[/]"))
            elif actual is None:
                all_ok = False
                rows.append((name, s[2], "[red]✗ missing[/]"))
                instructions.append(f"In [bold]{table}[/], add a field [bold]{name}[/] of type {s[2]}.")
            elif not type_ok(s[0], actual):
                all_ok = False
                rows.append((name, s[2], f"[yellow]⚠ is {actual}[/]"))
                instructions.append(f"In [bold]{table}[/], change [bold]{name}[/] to type {s[2]} (it is {actual}).")
            else:
                rows.append((name, s[2], "[green]✓[/]"))
        display.setup_table(table, rows)

    if instructions:
        console.print("[bold yellow]To fix (nothing was deleted or changed):[/]")
        for i, line in enumerate(instructions, 1):
            console.print(f"  {i}. {line}")
        console.print("  Or run [bold]python -m src.cli setup-airtable --create-missing[/] to add them automatically "
                      "(needs the schema.bases:write scope).")
        return False

    if all_ok:
        records = client.rubric_records()
        if not records:
            n = client.seed_rubric()
            console.print(f"[green]✓ Rubric table was empty, seeded {n} starting rows[/]")
        else:
            console.print(f"[green]✓ Rubric table has {len(records)} rows (not seeded again)[/]")
    return all_ok


def _resolve_account_id(handle: str) -> str:
    store: Store = DEPS.get("store")
    h = handle.lstrip("@").lower()
    for r in store.results():
        if r.handle in (h, f"{h}_rebrand") or r.account_id == handle:
            return r.account_id
    for account in _account_files(DEPS.get("settings"), h):
        return account.account_id
    _fail(f"No account found for {handle}. Add data/accounts/{h}.json first.")
    return ""


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command("setup-airtable")
def setup_airtable(create_missing: bool = typer.Option(False, "--create-missing",
                                                      help="Create missing tables and fields (never deletes).")):
    """Check the Rubric and Leads tables, print fixes for anything missing, seed the rubric if empty."""
    display.step("Airtable setup check")
    if not check_airtable(create_missing):
        raise typer.Exit(1)
    console.print("[bold green]Airtable base is ready.[/]")


@app.command("score")
def score_cmd(handle: str = typer.Option(None, "--handle", help="Score only this account (handle or file name).")):
    """Score accounts in data/accounts with Claude and the current Airtable rubric."""
    display.step("Scoring accounts")
    rubric = _load_rubric()
    accounts = _account_files(DEPS.get("settings"), handle)
    if not accounts:
        _fail(f"No account files found{f' for {handle}' if handle else ''} in {DEPS.get('settings').accounts_dir}")
    rows = score_and_store(accounts, rubric)
    if rows:
        display.score_summary(rows, rubric)
    if len(rows) < len(accounts):
        raise typer.Exit(1)


@app.command("preview")
def preview():
    """Rescore stored results with the CURRENT Airtable rubric and show before vs after (nothing saved)."""
    display.step("Preview with the current Airtable rubric")
    rubric = _load_rubric()
    store: Store = DEPS.get("store")
    stored = store.results()
    if not stored:
        _fail("No stored results yet. Run `python -m src.cli score` first.")
    rows, reword = [], set()
    old_rubric = Rubric.from_json(stored[0].rubric_json)
    for r in stored:
        va = VerifiedAssessment.model_validate_json(r.assessment_json)
        before = ScoreResult.model_validate_json(r.score_json)
        after = score(va, rubric)
        reword.update(definition_changes(Rubric.from_json(r.rubric_json), rubric))
        rows.append((r.handle, before, after))
    display.rubric_diff(old_rubric, rubric)
    display.preview_table(rows)
    if reword:
        console.print(f"[yellow]⚠ New or reworded rows: {', '.join(sorted(reword))}. Preview reuses Claude's stored "
                      f"judgment, so run `score` to have Claude re-judge with the new wording.[/]")
    console.print("[dim]Nothing was saved. Run `score` then `sync` to apply.[/]")


@app.command("sync")
def sync_cmd():
    """Push unsynced or changed results to Airtable (10 per request, max 4 requests per second)."""
    display.step("Syncing to Airtable")
    report = run_sync()
    if report.failed:
        raise typer.Exit(1)


@app.command("rename-test")
def rename_test(handle: str = typer.Option("sample_qualified", "--handle"),
                keep: bool = typer.Option(False, "--keep", help="Keep the new handle instead of restoring it.")):
    """Simulate a handle change, rescore and sync, then prove Airtable updated the record instead of duplicating it."""
    display.step("Rename test: same account, new handle")
    store: Store = DEPS.get("store")
    client = DEPS.get("airtable")
    account_id = _resolve_account_id(handle)

    if store.get_result(account_id) is None:
        console.print("Account not scored yet, scoring it first.")
        rubric = _load_rubric()
        account = next(a for a in _account_files(DEPS.get("settings"), None) if a.account_id == account_id)
        score_and_store([account], rubric)
    if store.pending_sync():
        run_sync()

    before = client.find_leads(account_id)
    old_handle = store.get_result(account_id).handle
    original = old_handle.removesuffix("_rebrand")
    new_handle = f"{original}_rebrand" if old_handle == original else original
    console.print(f"Before: Airtable has [bold]{display._n(len(before), 'record')}[/] for Account ID {account_id}"
                  + (f" · record {before[0]['id']} · handle @{before[0]['fields'].get('Handle')}" if before else ""))

    console.print(f"Changing handle [bold]@{old_handle}[/] → [bold]@{new_handle}[/] (account_id unchanged)")
    store.change_handle(account_id, new_handle)
    rubric = _load_rubric()
    score_and_store([store.get_account(account_id)], rubric)
    run_sync()

    after = client.find_leads(account_id)
    local_rows = sum(1 for r in store.results() if r.account_id == account_id)
    same_record = bool(before) and len(after) == 1 and after[0]["id"] == before[0]["id"]
    handle_ok = len(after) == 1 and after[0]["fields"].get("Handle") == new_handle
    passed = len(after) == 1 and handle_ok and (same_record or not before) and local_rows == 1

    lines = [
        f"Airtable records for Account ID {account_id}: [bold]{len(before)} → {len(after)}[/]",
        f"Record ID: {before[0]['id'] if before else '(none)'} → {after[0]['id'] if after else '(none)'}"
        + ("  [green](same record, updated in place)[/]" if same_record else ""),
        f"Handle in Airtable: @{after[0]['fields'].get('Handle') if after else '?'}",
        f"Local SQLite rows for this account: {local_rows}",
    ]
    display.console.print(display.Panel("\n".join(lines), title="[bold green]PASS: updated, not duplicated[/]" if passed
                                        else "[bold red]FAIL[/]", border_style="green" if passed else "red"))

    if not keep and new_handle != original:
        console.print(f"Restoring the original handle @{original} for the next run")
        store.change_handle(account_id, original)
        run_sync()
        restored = client.find_leads(account_id)
        console.print(f"Airtable records for Account ID {account_id} after restore: [bold]{len(restored)}[/]")
        passed = passed and len(restored) == 1
    if not passed:
        raise typer.Exit(1)


@app.command("demo")
def demo():
    """Run the full flow for the recording: setup check, score, sync, rename test."""
    display.step("1/4  Airtable setup check")
    if not check_airtable(create_missing=False):
        raise typer.Exit(1)
    display.step("2/4  Score the sample accounts")
    rubric = _load_rubric()
    accounts = _account_files(DEPS.get("settings"), None)
    rows = score_and_store(accounts, rubric)
    display.score_summary(rows, rubric)
    display.step("3/4  Sync to Airtable")
    report = run_sync()
    rename_test(handle="sample_qualified", keep=False)

    display.step("Done")
    counts = {v: sum(1 for *_, r in rows if r.verdict is v) for v in Verdict}
    console.print(display.Panel(
        f"{len(rows)} accounts scored · {counts[Verdict.qualified]} qualified · {counts[Verdict.rejected]} rejected · "
        f"{counts[Verdict.review]} review\n"
        f"Every quote checked against the source data · rubric [magenta]{rubric.version}[/] read live from Airtable\n"
        f"{report.records} records synced in {report.requests} request(s) · {report.failed} failed · "
        f"one Airtable record per account",
        title="[bold green]Demo complete[/]", border_style="green"))
    if len(rows) < len(accounts) or report.failed:
        raise typer.Exit(1)


def main() -> None:
    try:
        app()
    except ConfigError as err:
        console.print(f"[bold red]✗ {err}[/]")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
