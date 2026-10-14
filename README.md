# Lead Qualifier Demo

A small working proof of concept for a lead pipeline. It finds Instagram coaches whose offer genuinely works but who are stuck running everything themselves as solo operators, instead of running the business as a CEO with systems. Lead quality over volume.

## What this demo proves

1. **Deep behavioural qualification.** Claude judges each account from its captions, comments, the owner's replies and the link in bio page. It never judges from the bio alone. Every verdict cites the exact posts, and a program checks that **every quote really exists in the source data**. Invented quotes are dropped on screen.
2. **No code configuration.** Grading weights, diagnostic triggers and exclusions live in an Airtable table that your team edits. The script reads that table on every run. Change a weight in Airtable, run `preview`, and the scores change.
3. **Safe Airtable sync.** Results are stored locally first (SQLite), then pushed to Airtable at most 4 requests per second, 10 records per request. Records are upserted on `Account ID`, so no record is dropped and none is duplicated, even when an account changes its handle.

No scraping in this version. The input is collected by hand into JSON files.

---

## Setup in under 5 minutes

**1. Install** (Python 3.12+)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

**2. Fill in `.env`**

| Variable | What it is |
|---|---|
| `ANTHROPIC_API_KEY` | Your Claude API key |
| `ANTHROPIC_MODEL` | Optional. Defaults to `claude-sonnet-5-5` |
| `AIRTABLE_API_KEY` | An Airtable personal access token with the scopes `data.records:read`, `data.records:write`, `schema.bases:read` and, for automatic setup, `schema.bases:write`. Give it access to your base |
| `AIRTABLE_BASE_ID` | The `app...` part of your base URL |

**3. Prepare the Airtable base.** Create an empty base, then run:

```bash
python -m src.cli setup-airtable --create-missing
```

This creates the **Rubric** and **Leads** tables with the right fields and seeds the rubric with sensible starting rows. It never deletes anything. Without `--create-missing`, it only checks the base and prints exactly what to add by hand.

**4. Run the demo** on the three sample accounts:

```bash
python -m src.cli demo
```

## Commands

```
python -m src.cli setup-airtable     # check tables, seed the rubric if empty (--create-missing to add fields)
python -m src.cli score              # score all accounts in data/accounts
python -m src.cli score --handle X   # score one account
python -m src.cli preview            # rescore stored results with the CURRENT Airtable rubric, before vs after, nothing saved
python -m src.cli sync               # push results to Airtable
python -m src.cli rename-test        # simulate a handle change, rescore, sync, prove one record was updated not duplicated
python -m src.cli demo               # the full flow for the recording
python -m pytest                     # run the tests (no API keys needed)
```

## Sample accounts

`data/accounts/` ships with three fictional coaches so the demo works before any real data is added:

| File | What it shows | Expected verdict |
|---|---|---|
| `sample_qualified.json` | Sold out rounds, named client results and repeat buyers. Every sale goes through her own Calendly, plus "comment READY" posts answered by hand a day later, a three week posting gap after launch, and she delivers everything herself | qualified |
| `sample_weak_offer.json` | Hype, a "results guaranteed" claim in the bio only, no client results, and a commenter who can't find any reviews | rejected (offer) |
| `sample_systemised.json` | A strong offer, but six coaches, a community manager, a checkout and DM automation | rejected (bottleneck, plus the team exclusion) |

## Adding a real account

Create `data/accounts/<handle>.json` in this format:

```json
{
  "account_id": "17841400000000000",
  "handle": "example_handle",
  "bio": "...",
  "followers": 12500,
  "link_in_bio_text": "Text copied from the link in bio page, including offers and booking links",
  "posts": [
    {
      "post_id": "p01",
      "date": "2026-09-30",
      "caption": "...",
      "likes": 210,
      "comments_count": 34,
      "comments": [
        { "author": "user1", "text": "...", "reply_from_owner": "...", "reply_delay_hours": 6 }
      ]
    }
  ]
}
```

Tips:

- `account_id` is the permanent Instagram ID. It is the merge key everywhere. Never reuse it, and never change it when the handle changes.
- Copy captions, comments and the link in bio page **exactly**, because quotes are checked word for word.
- Leave `reply_from_owner` as `null` when the owner didn't reply. Unanswered comments are evidence too.
- 6 to 12 recent posts, with their comment threads, is plenty.
- Run `python -m src.cli score --handle <handle>`.

## How the rubric works (for the team)

Everything Claude grades against lives in the **Rubric** table in Airtable. Python never needs to change. Each row is one rule:

| Column | What it means |
|---|---|
| **Name** | A short, unique label, e.g. *Manual hand raiser loop*. Claude cites rows by this name |
| **Kind** | `trigger` = a signal to look for. `exclusion` = an automatic no. `weight` = how much a score counts |
| **Pillar** | `offer_works` (does the offer genuinely work?), `founder_bottleneck` (is the founder the bottleneck?) or `general` |
| **Definition** | Plain English that Claude reads as is. Be specific: *"Sales depend on 'comment X' posts where the owner replies by hand, often hours later"* |
| **Weight** | 0 to 10. How much this row matters |
| **Active** | Untick to switch a row off without deleting it |

**How a score is built** (the terminal shows every step):

- **Pillar score (0 to 10)** = half Claude's own judgment + half the rubric.
  - Claude's part is its 0 to 10 judgment × the share of its quotes that were verified. Invented quotes pull this down.
  - The rubric part is the weighted share of triggers matched with verified evidence: `sum(weight × strength) ÷ sum(weight of all active triggers in that pillar)`.
  - A pillar with no verified evidence from posts, comments or the link in bio page scores **0**.
- **Total (0 to 100)** is the weighted average of the two pillars (their `weight` rows) and each general weight row (follower range, engagement rate).
- **Verdict:**
  - **qualified** = both pillars at least 6.5 and no exclusion.
  - **rejected** = any exclusion hit, or either pillar below 4.
  - **review** = everything in between.

**Raising a trigger's weight** makes accounts that show that signal score higher, and accounts that don't score lower. **Rewording a definition** changes what Claude looks for. `preview` warns you to run `score` so Claude can re-judge. The rubric version (a hash of all active rows) is stored with every score, so you can always see which rubric produced which result.

If a row is broken (a weight of 12, an empty definition, an unknown kind), the run stops and prints a table of exactly which rows to fix.

## How the sync protects against rate limits and duplicates

- **Local first.** Every result is saved to SQLite before anything touches Airtable. The sync only reads results that are new or have changed since the last confirmed push.
- **Batches of 10, at most 4 requests per second.** That is the Airtable maximum per request, and under its limit of 5 requests per second per base.
- **Upsert on `Account ID`** (`performUpsert` with `fieldsToMergeOn: ["Account ID"]`). Sending the same account twice updates the same row. A new handle updates the existing record instead of creating a second one. `rename-test` proves this live.
- **429 Too Many Requests:** wait 30 seconds and retry the same batch.
- **Other errors:** back off for 2 s, 4 s, then 8 s. After 3 retries the batch is marked failed in SQLite and stays queued for the next `sync`. **Nothing is dropped.**
- **Confirmed or nothing.** A record is marked synced only when it appears in Airtable's response.
- Each batch is logged in the terminal with its batch number, record count, response time, retries and created/updated counts.

## How this grows into the full pipeline

| Today (demo) | Full version |
|---|---|
| Hand collected JSON | Instagram Graph API **Business Discovery** for profile, posts and comment counts. **Playwright** for the link in bio page and comment threads the API doesn't expose. Same `Account` schema |
| SQLite | **Postgres**. All SQL lives in `src/store.py` behind one small `Store` class, so it is a contained swap |
| CLI on a laptop | **Docker** image deployed on **Render**: a background **worker** that scrapes, scores and syncs from a queue, plus a **cron job** that queues accounts for re-checks (e.g. weekly) |
| One Claude call per account | The same call, run in parallel workers with caching, plus a cheap pre-filter for obvious no's |
| Thresholds in code | Thresholds and the Claude/rubric blend become rows in Airtable too |

## Project layout

```
src/
  cli.py             commands
  models.py          pydantic schemas (accounts, rubric rows, Claude's assessment)
  rubric.py          loads and validates the rubric from Airtable, seed rows, version hash
  qualifier.py       the Claude call (strict tool use, validated with pydantic)
  evidence.py        checks every quote exists in the source data
  scoring.py         combines verified evidence with rubric weights, verdict rules
  store.py           SQLite storage, keyed by account_id
  sync.py            throttled, batched Airtable upsert worker
  airtable_client.py thin pyairtable wrapper (retries are owned by sync.py)
  display.py         rich terminal output
  config.py          .env loading
tests/               pytest suite with fake Claude and Airtable clients
data/accounts/       one JSON file per account
```

## Before you record

- Run `python -m pytest`. It needs no API keys.
- Run `python -m src.cli demo` once against your real base and read every card. **Check every verdict and quote yourself, and never show a verdict about a real account that you have not verified.**
- For the rubric segment: the sample qualified coach matches nearly every trigger at similar strength, so nudging one of her trigger weights barely moves her rounded pillar score. The "now:" math lines under the preview table still show the points change. For a big visible change, edit a weight that separates the accounts:
  - **Engagement rate** 3 → 8 moves every total by a few points, in different directions.
  - **Founder does all delivery** 7 → 0 lifts the weak offer coach's bottleneck score.
- `rename-test` restores the original handle at the end, so you can run it again. Use `--keep` to leave the new handle in Airtable.
