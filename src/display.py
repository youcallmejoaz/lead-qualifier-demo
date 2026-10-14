"""Rich terminal output. Everything the camera sees is rendered here."""

from __future__ import annotations

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .evidence import BIO, QuoteCheck, QuoteStatus, VerifiedAssessment
from .models import PILLAR_LABELS, Account, Pillar, Verdict
from .rubric import Rubric, RubricError
from .scoring import QUALIFY_THRESHOLD, ScoreResult
from .sync import BatchLog, SyncReport

console = Console()

VERDICT_STYLE = {Verdict.qualified: "bold green", Verdict.rejected: "bold red", Verdict.review: "bold yellow"}


def verdict_text(verdict: Verdict | str) -> Text:
    v = Verdict(verdict)
    return Text(v.value.upper(), style=VERDICT_STYLE[v])


def score_style(score: float) -> str:
    return "green" if score >= QUALIFY_THRESHOLD else "yellow" if score >= 4 else "red"


def step(title: str) -> None:
    console.print()
    console.print(Rule(Text(title, style="bold cyan"), style="cyan"))


def rubric_header(rubric: Rubric, source: str = "Airtable") -> None:
    counts = {k: 0 for k in ("trigger", "exclusion", "weight")}
    for r in rubric.rows:
        counts[r.kind.value] += 1
    console.print(
        f"[bold]Rubric[/] loaded from {source}: {counts['trigger']} triggers · {counts['exclusion']} exclusions · "
        f"{counts['weight']} weights · version [bold magenta]{rubric.version}[/]"
    )


def rubric_errors(err: RubricError) -> None:
    t = Table(title="Rubric problems: fix these rows in Airtable", box=box.ROUNDED, title_style="bold red",
              border_style="red")
    t.add_column("Row")
    t.add_column("Problem")
    for name, msg in err.problems:
        t.add_row(name, msg)
    console.print(t)
    console.print("[red]Scoring did not run. Inactive rows are ignored, so you can also untick Active.[/]")


# ---------------------------------------------------------------------------
# Account card
# ---------------------------------------------------------------------------


def _quote_line(q: QuoteCheck, trigger: str | None = None) -> Text:
    line = Text("    ")
    where = q.source_id or q.claimed_source
    if q.date:
        where += f" · {q.date}"
    if q.status is QuoteStatus.dropped:
        line.append("✗ ", style="bold red")
        line.append(f'"{q.quote}"', style="red strike")
        line.append(f"  not found in source data ({q.claimed_source}), dropped", style="red")
        return line
    if q.source_id == BIO:
        line.append("• ", style="yellow")
        line.append(f'"{q.quote}"', style="dim")
        line.append("  bio, context only, not counted", style="yellow")
        return line
    if q.status is QuoteStatus.corrected:
        line.append("✓ ", style="bold yellow")
        line.append(f'"{q.quote}"')
        line.append(f"  {where} (Claude said {q.claimed_source}, corrected)", style="yellow")
    else:
        line.append("✓ ", style="bold green")
        line.append(f'"{q.quote}"')
        line.append(f"  {where}", style="cyan")
    if trigger:
        line.append(f"  {trigger}", style="dim")
    return line


def _pillar_block(pillar: Pillar, va: VerifiedAssessment, result: ScoreResult) -> list:
    ps = result.pillar(pillar)
    check = va.pillar(pillar)
    head = Text()
    head.append(f"{PILLAR_LABELS[pillar]}  ", style="bold")
    head.append(f"{ps.score:.1f}/10", style=f"bold {score_style(ps.score)}")
    head.append(f"   evidence {check.verified}/{check.claimed} verified", style="dim")
    parts: list = [head, Text("  " + ps.math, style="dim")]
    if check.reason:
        parts.append(Text("  " + check.reason, style="italic"))
    weights = {m.name: m for m in ps.matched}
    for t in check.triggers:
        label = Text("  ▸ ")
        label.append(t.trigger, style="bold")
        if t.trigger in weights:
            m = weights[t.trigger]
            label.append(f"  strength {m.strength:g} × weight {m.weight:g} = {m.points:.1f} pts", style="dim")
        elif not t.known:
            label.append("  not an active rubric trigger, ignored", style="yellow")
        else:
            label.append("  no verified evidence, not counted", style="yellow")
        parts.append(label)
        parts += [_quote_line(q) for q in t.quotes]
    if not check.triggers:
        parts.append(Text("  ▸ no triggers matched", style="dim"))
    return parts


def account_card(account: Account, va: VerifiedAssessment, result: ScoreResult) -> None:
    parts: list = []
    parts += _pillar_block(Pillar.offer_works, va, result)
    parts.append(Text(""))
    parts += _pillar_block(Pillar.founder_bottleneck, va, result)
    parts.append(Text(""))

    exc = Text("Exclusions  ", style="bold")
    if result.exclusions_hit:
        exc.append(", ".join(result.exclusions_hit), style="bold red")
    else:
        exc.append("none hit", style="green")
    parts.append(exc)
    for e in va.exclusions:
        if e.exclusion not in result.exclusions_hit:
            note = Text(f"  ▸ {e.exclusion}", style="dim")
            note.append("  claimed but no verified non-bio evidence, ignored", style="yellow")
            parts.append(note)
        parts += [_quote_line(q) for q in e.quotes]

    general = Text("General     ", style="bold")
    general.append("  ·  ".join(f"{c.label} {c.score:g}" for c in result.components[2:]) or "none")
    parts.append(general)
    parts.append(Text(""))
    parts.append(Text(result.total_math, style="bold"))
    reason = Text("Verdict     ", style="bold")
    reason.append_text(verdict_text(result.verdict))
    reason.append(f"  {result.verdict_reason}", style="dim")
    parts.append(reason)
    parts.append(Text(""))
    parts.append(Text("For sales: ", style="bold").append(va.summary, style="italic"))

    quotes = va.all_quotes()
    found = sum(1 for q in quotes if q.found)
    dropped = len(quotes) - found
    check = Text("Quote check ", style="bold")
    check.append(f"{found}/{len(quotes)} quotes found verbatim in the source data", style="green" if not dropped else "yellow")
    if dropped:
        check.append(f" · {dropped} invented or misquoted, dropped", style="bold red")
    parts.append(check)

    title = Text(f" @{account.handle} ", style="bold white")
    title.append(" ")
    title.append_text(verdict_text(result.verdict))
    title.append(f"  total {result.total:.1f}/100 ", style="bold")
    console.print(Panel(Group(*parts), title=title, title_align="left", border_style=VERDICT_STYLE[result.verdict].split()[-1],
                        subtitle=f"{account.followers:,} followers · {len(account.posts)} posts · account {account.account_id}",
                        subtitle_align="right", padding=(1, 2)))


def score_summary(rows: list[tuple[Account, VerifiedAssessment, ScoreResult]], rubric: Rubric) -> None:
    t = Table(title="Scoring summary", box=box.ROUNDED, title_style="bold")
    for col, kw in [("Handle", {}), ("Verdict", {}), ("Offer works", {"justify": "right"}),
                    ("Bottleneck", {"justify": "right"}), ("Total", {"justify": "right"}),
                    ("Quotes verified", {"justify": "right"}), ("Exclusions", {})]:
        t.add_column(col, **kw)
    found_all = total_all = 0
    for account, va, r in rows:
        quotes = va.all_quotes()
        found = sum(1 for q in quotes if q.found)
        found_all += found
        total_all += len(quotes)
        t.add_row(f"@{account.handle}", verdict_text(r.verdict),
                  Text(f"{r.offer_works.score:.1f}", style=score_style(r.offer_works.score)),
                  Text(f"{r.founder_bottleneck.score:.1f}", style=score_style(r.founder_bottleneck.score)),
                  Text(f"{r.total:.1f}", style="bold"), f"{found}/{len(quotes)}",
                  ", ".join(r.exclusions_hit) or "-")
    console.print(t)
    counts = {v: sum(1 for *_, r in rows if r.verdict is v) for v in Verdict}
    console.print(
        f"[bold]{len(rows)} scored[/] · [green]{counts[Verdict.qualified]} qualified[/] · "
        f"[red]{counts[Verdict.rejected]} rejected[/] · [yellow]{counts[Verdict.review]} review[/] · "
        f"{found_all}/{total_all} quotes verified · rubric [magenta]{rubric.version}[/]"
    )


# ---------------------------------------------------------------------------
# Preview, sync, setup
# ---------------------------------------------------------------------------


def _delta(before: float, after: float) -> Text:
    d = round(after - before, 1)
    if d == 0:
        return Text("0.0", style="dim")
    return Text(f"{d:+.1f}", style="bold green" if d > 0 else "bold red")


def rubric_diff(old: Rubric, new: Rubric) -> None:
    old_by = {r.name.casefold(): r for r in old.rows}
    new_by = {r.name.casefold(): r for r in new.rows}
    t = Table(title="Rubric changes since last score", box=box.ROUNDED, title_style="bold")
    t.add_column("Row")
    t.add_column("Change")
    for key, r in new_by.items():
        o = old_by.get(key)
        if o is None:
            t.add_row(r.name, Text("new row", style="yellow"))
        else:
            if o.weight != r.weight:
                t.add_row(r.name, Text(f"weight {o.weight:g} → {r.weight:g}", style="bold cyan"))
            if o.definition != r.definition:
                t.add_row(r.name, Text("definition reworded", style="yellow"))
    for key, o in old_by.items():
        if key not in new_by:
            t.add_row(o.name, Text("removed or deactivated", style="red"))
    if t.row_count:
        console.print(t)
    else:
        console.print("[dim]No rubric changes since these results were scored.[/]")


def preview_table(rows: list[tuple[str, ScoreResult, ScoreResult]]) -> None:
    t = Table(title="Preview: stored results rescored with the CURRENT Airtable rubric (nothing saved)",
              box=box.ROUNDED, title_style="bold")
    t.add_column("Handle")
    for col in ("Offer works", "Bottleneck", "Total"):
        t.add_column(col, justify="right")
        t.add_column("Δ", justify="right")
    t.add_column("Verdict")
    for handle, before, after in rows:
        cells: list = [f"@{handle}"]
        for b, a in ((before.offer_works.score, after.offer_works.score),
                     (before.founder_bottleneck.score, after.founder_bottleneck.score),
                     (before.total, after.total)):
            cells += [f"{b:.1f} → {a:.1f}", _delta(b, a)]
        verdict = verdict_text(before.verdict)
        if before.verdict != after.verdict:
            verdict.append(" → ").append_text(verdict_text(after.verdict))
        cells.append(verdict)
        t.add_row(*cells)
    console.print(t)
    for handle, before, after in rows:
        changed = [after.pillar(p).math for p in (Pillar.offer_works, Pillar.founder_bottleneck)
                   if after.pillar(p).math != before.pillar(p).math]
        if after.total_math != before.total_math:
            changed.append(after.total_math)
        if changed:
            console.print(f"[bold]@{handle}[/] now:")
            for line in changed:
                console.print(f"  [dim]{line}[/]")


def batch_line(b: BatchLog) -> None:
    status = {"ok": "[bold green]✓ confirmed[/]", "partial": "[bold yellow]⚠ partial[/]",
              "failed": "[bold red]✗ failed, left queued[/]"}[b.status]
    console.print(
        f"  Batch {b.number:>2} · {_n(b.size, 'record'):>10} · {b.ms:>6.0f} ms · {_n(b.retries, 'retry', 'retries')}"
        + (f" ({b.rate_limited}× 429)" if b.rate_limited else "")
        + f" · {b.created} created / {b.updated} updated · {status}"
        + (f" [dim]{b.error}[/]" if b.error else "")
    )


def _n(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else many or one + 's'}"


def sync_summary(report: SyncReport, counts: dict[str, int]) -> None:
    if not report.batches:
        console.print("[green]Nothing to sync: every stored result already matches Airtable.[/]")
        return
    style = "green" if report.failed == 0 and report.synced == report.records else "yellow"
    console.print(
        f"[bold {style}]{_n(report.records, 'record')} · {_n(report.requests, 'request')} · "
        f"{_n(report.retries, 'retry', 'retries')} · "
        f"{report.synced} confirmed · {report.failed} failed[/]  "
        f"[dim](throttle ≤ 4 req/s, 10 records per request, upsert on Account ID)[/]"
    )
    console.print(f"[dim]Local sync state: {', '.join(f'{k} {v}' for k, v in sorted(counts.items()))}[/]")


def setup_table(table: str, rows: list[tuple[str, str, str]]) -> None:
    t = Table(title=f"Airtable table: {table}", box=box.ROUNDED, title_style="bold")
    t.add_column("Field")
    t.add_column("Expected type")
    t.add_column("Status")
    for name, expected, status in rows:
        t.add_row(name, expected, status)
    console.print(t)
