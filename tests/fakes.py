"""In-memory stand-ins for Claude and Airtable so the tests (and the CLI demo test) run without keys."""

from __future__ import annotations

import copy
import itertools
import re
from types import SimpleNamespace
from typing import Any

from src.airtable_client import ACCOUNT_ID_FIELD, LEADS_SCHEMA, RUBRIC_SCHEMA
from src.rubric import SEED_ROWS


def q(quote: str, source_id: str) -> dict[str, str]:
    return {"quote": quote, "source_id": source_id}


def trig(name: str, strength: float, *quotes: dict) -> dict[str, Any]:
    return {"trigger": name, "strength": strength, "quotes": list(quotes)}


# Canned assessments built from the real quotes in data/accounts. The qualified one also
# contains one invented quote and one with the wrong post ID, so the evidence check has work to do.
ASSESSMENTS: dict[str, dict[str, Any]] = {
    "sample_qualified": {
        "offer_works": {
            "score": 9,
            "reason": "Named client results, repeat buyers and a 48 hour sell out show the offer works.",
            "matched_triggers": [
                trig("Testimonials naming results", 9,
                     q("doubled my monthly revenue to £22k", "p04"),
                     q("She went from 3 to 9 patients a day", "p01")),
                trig("Sold out launches", 9, q("Round 4 SOLD OUT in 48 hours", "p02")),
                trig("Buyers asking about next round", 8,
                     q("when does round 5 open?", "p02"),
                     q("Can I get on the waitlist for the next round please?", "p02")),
                trig("Repeat client wins", 8,
                     q("Aisha is back for round 5 after going from 40 to 110 patients a month", "p06"),
                     q("He's back with me in round 4 to build his team.", "p03")),  # really p04
            ],
        },
        "founder_bottleneck": {
            "score": 9,
            "reason": "Every sale goes through her own call link and she delivers and replies to everything by hand.",
            "matched_triggers": [
                trig("Every sale through a 1:1 call", 9,
                     q("The only way in is a 1:1 strategy call with me", "link_in_bio"),
                     q("No checkout, just book a call with me through the link in bio", "p06")),
                trig("Manual hand raiser loop", 9,
                     q("Comment READY below and I'll send it to you personally", "p05"),
                     q("Sent! Sorry it took a day, I'm doing these by hand", "p05")),
                trig("Slow manual replies", 8, q("sorry for the slow reply, been on calls all day", "p01")),
                trig("Posting gaps after launches", 8, q("Sorry for going quiet for three weeks, friends.", "p03")),
                trig("Founder does all delivery", 9,
                     q("I write every client's weekly review myself", "p03"),
                     q("I personally run every strategy call and every coaching session", "link_in_bio"),
                     q("I personally coach all 140 clients every week", "p03")),  # invented
            ],
        },
        "exclusions_hit": [],
        "general_scores": [
            {"name": "Follower range", "score": 10, "reason": "18,400 followers is inside the sweet spot."},
            {"name": "Engagement rate", "score": 7, "reason": "3.11% average engagement."},
        ],
        "summary": "Dana is a physio turned clinic coach whose rounds sell out and whose clients report concrete "
                   "results. Every sale runs through her own Calendly and she delivers every session and reply herself. "
                   "Lead with freeing her from the call-only sales loop.",
    },
    "sample_weak_offer": {
        "offer_works": {
            "score": 2,
            "reason": "Only hype and a guarantee in the bio; no client results or buyer demand in posts.",
            "matched_triggers": [
                trig("Testimonials naming results", 2, q("Life changing results guaranteed", "bio")),
            ],
        },
        "founder_bottleneck": {
            "score": 6,
            "reason": "Sales run through her own discovery calls and hand sent DMs.",
            "matched_triggers": [
                trig("Every sale through a 1:1 call", 7, q("Book a free discovery call with me", "link_in_bio")),
                trig("Manual hand raiser loop", 6,
                     q("Comment ABUNDANCE and I'll send you my free alignment guide", "p03")),
                trig("Slow manual replies", 5, q("DM me and we can hop on a call", "p02")),
            ],
        },
        "exclusions_hit": [],
        "general_scores": [
            {"name": "Follower range", "score": 9, "reason": "9,800 followers."},
            {"name": "Engagement rate", "score": 1, "reason": "0.98% average engagement."},
        ],
        "summary": "Sienna sells a mindset mentorship with lots of energy but no visible client results. "
                   "A commenter could not find any reviews. Not a fit until there is proof the offer works.",
    },
    "sample_systemised": {
        "offer_works": {
            "score": 9,
            "reason": "Sold out rounds, named client results and repeat buyers.",
            "matched_triggers": [
                trig("Sold out launches", 9, q("Round 7 of Scale Studio sold out in 5 days", "p01")),
                trig("Testimonials naming results", 9,
                     q("went from 6 in person clients to 31 online clients in 4 months", "p02")),
                trig("Buyers asking about next round", 8, q("When does round 8 open? I need in", "p01")),
                trig("Repeat client wins", 7, q("second time through and I just hit 40 clients", "p02")),
            ],
        },
        "founder_bottleneck": {
            "score": 1,
            "reason": "A team, a checkout and DM automation handle sales and delivery.",
            "matched_triggers": [],
        },
        "exclusions_hit": [
            {"exclusion": "Already has a team", "quotes": [
                q("Our community manager Jess and the support team reply within one hour", "link_in_bio"),
                q("Six certified coaches, a success manager and Jess running our community", "p04"),
            ]},
        ],
        "general_scores": [
            {"name": "Follower range", "score": 10, "reason": "64,000 followers."},
            {"name": "Engagement rate", "score": 7, "reason": "3.3% average engagement."},
        ],
        "summary": "Scale Studio clearly works, but it is already systemised with six coaches, a community manager, "
                   "a checkout and DM automation. The founder is not the bottleneck.",
    },
}


class FakeMessages:
    def __init__(self, parent: "FakeClaude"):
        self.parent = parent

    def create(self, **kwargs):
        self.parent.calls.append(kwargs)
        text = kwargs["messages"][0]["content"]
        handle = re.search(r'handle="@([^"]+)"', text).group(1)
        key = handle.removesuffix("_rebrand")
        data = copy.deepcopy(self.parent.assessments[key])
        block = SimpleNamespace(type="tool_use", name="submit_assessment", id=f"toolu_{len(self.parent.calls)}",
                                input=data)
        return SimpleNamespace(stop_reason="tool_use", content=[block])


class FakeClaude:
    def __init__(self, assessments: dict[str, dict] | None = None):
        self.assessments = assessments or ASSESSMENTS
        self.calls: list[dict] = []
        self.beta = SimpleNamespace(messages=FakeMessages(self))


class HTTPErrorWithStatus(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.response = SimpleNamespace(status_code=status)


class FakeAirtable:
    """Implements the AirtableClient interface in memory, with upsert keyed on Account ID."""

    def __init__(self, rubric_rows: list[dict] | None = None, errors: list[int] | None = None):
        rows = rubric_rows if rubric_rows is not None else [dict(r) for r in SEED_ROWS]
        self._ids = itertools.count(1)
        self.rubric = [{"id": f"recR{next(self._ids):04d}", "fields": dict(r)} for r in rows]
        self.leads: dict[str, dict] = {}  # record id -> record
        self.requests: list[list[dict]] = []
        self.errors = list(errors or [])  # status codes to raise on the next upsert calls
        self.schema: dict[str, dict[str, str] | None] = {
            "Rubric": {n: s[0] for n, s in RUBRIC_SCHEMA.items()},
            "Leads": {n: s[0] for n, s in LEADS_SCHEMA.items()},
        }

    # rubric
    def rubric_records(self):
        return copy.deepcopy(self.rubric)

    def seed_rubric(self):
        self.rubric = [{"id": f"recR{next(self._ids):04d}", "fields": dict(r)} for r in SEED_ROWS]
        return len(self.rubric)

    def set_weight(self, name: str, weight: float) -> None:
        for r in self.rubric:
            if r["fields"]["Name"] == name:
                r["fields"]["Weight"] = weight
                return
        raise KeyError(name)

    # leads
    def upsert_leads(self, records):
        assert len(records) <= 10
        self.requests.append(copy.deepcopy(records))
        if self.errors:
            raise HTTPErrorWithStatus(self.errors.pop(0))
        out = {"createdRecords": [], "updatedRecords": [], "records": []}
        for fields in records:
            existing = next((r for r in self.leads.values() if r["fields"][ACCOUNT_ID_FIELD] == fields[ACCOUNT_ID_FIELD]),
                            None)
            if existing:
                existing["fields"].update(fields)
                out["updatedRecords"].append(existing["id"])
                rec = existing
            else:
                rec = {"id": f"recL{next(self._ids):04d}", "fields": dict(fields)}
                self.leads[rec["id"]] = rec
                out["createdRecords"].append(rec["id"])
            out["records"].append(copy.deepcopy(rec))
        return out

    def find_leads(self, account_id):
        return [copy.deepcopy(r) for r in self.leads.values() if r["fields"][ACCOUNT_ID_FIELD] == account_id]

    # schema
    def table_fields(self):
        return copy.deepcopy(self.schema)

    def create_table(self, name, spec):
        self.schema[name] = {n: s[0] for n, s in spec.items()}

    def create_field(self, table, name, spec):
        self.schema[table][name] = spec[0]


class FakeClock:
    """A clock whose sleep() advances time instantly, recording every sleep."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
