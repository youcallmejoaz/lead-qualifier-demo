"""Loads and validates the grading rubric that the client's team edits in Airtable."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from pydantic import ValidationError

from .models import Kind, Pillar, RubricRow

RUBRIC_FIELDS = ["Name", "Kind", "Pillar", "Definition", "Weight", "Active"]


class RubricError(ValueError):
    """Raised when active rubric rows cannot be used. `problems` is a list of (row name, message)."""

    def __init__(self, problems: list[tuple[str, str]]):
        self.problems = problems
        super().__init__("; ".join(f"{name}: {msg}" for name, msg in problems))


@dataclass(frozen=True)
class Rubric:
    rows: tuple[RubricRow, ...]  # active rows only

    @property
    def version(self) -> str:
        return rubric_version(self.rows)

    def triggers(self, pillar: Pillar) -> list[RubricRow]:
        return [r for r in self.rows if r.kind is Kind.trigger and r.pillar is pillar]

    @property
    def exclusions(self) -> list[RubricRow]:
        return [r for r in self.rows if r.kind is Kind.exclusion]

    def pillar_weight(self, pillar: Pillar) -> RubricRow:
        return next(r for r in self.rows if r.kind is Kind.weight and r.pillar is pillar)

    @property
    def general_weights(self) -> list[RubricRow]:
        return [r for r in self.rows if r.kind is Kind.weight and r.pillar is Pillar.general]

    def by_name(self, name: str) -> RubricRow | None:
        key = name.strip().casefold()
        return next((r for r in self.rows if r.name.casefold() == key), None)

    def to_json(self) -> str:
        return json.dumps([r.fingerprint() for r in self.rows], sort_keys=True)

    @classmethod
    def from_json(cls, data: str) -> "Rubric":
        return cls(tuple(RubricRow(**row) for row in json.loads(data)))


def rubric_version(rows: Iterable[RubricRow]) -> str:
    canonical = json.dumps(sorted((r.fingerprint() for r in rows), key=lambda r: r["name"]), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def _row_from_record(record: dict[str, Any]) -> dict[str, Any]:
    f = record.get("fields", {})
    return {
        "name": f.get("Name") or "",
        "kind": f.get("Kind"),
        "pillar": f.get("Pillar"),
        "definition": f.get("Definition") or "",
        "weight": f.get("Weight"),
        "active": bool(f.get("Active", False)),
        "airtable_id": record.get("id"),
    }


def _friendly(err: ValidationError) -> str:
    msgs = []
    for e in err.errors():
        field = ".".join(str(p) for p in e["loc"])
        if field == "weight" and e["type"] in ("float_type", "missing"):
            msgs.append("Weight is missing")
        elif field == "weight":
            msgs.append("Weight must be a number from 0 to 10")
        elif field == "definition":
            msgs.append("Definition is missing or too short (write at least one plain English sentence)")
        elif field == "name":
            msgs.append("Name is missing")
        elif field == "kind":
            msgs.append("Kind must be trigger, exclusion or weight")
        elif field == "pillar":
            msgs.append("Pillar must be offer_works, founder_bottleneck or general")
        else:
            msgs.append(f"{field}: {e['msg']}")
    return "; ".join(msgs)


def parse_rubric(records: list[dict[str, Any]]) -> Rubric:
    """Validate raw Airtable records. Inactive rows are ignored, even if they are invalid."""
    problems: list[tuple[str, str]] = []
    rows: list[RubricRow] = []
    for i, record in enumerate(records, start=1):
        raw = _row_from_record(record)
        if not raw["active"]:
            continue
        label = raw["name"] or f"(row {i}, no name)"
        try:
            row = RubricRow(**raw)
        except ValidationError as err:
            problems.append((label, _friendly(err)))
            continue
        if row.kind is Kind.trigger and row.pillar is Pillar.general:
            problems.append((label, "A trigger must belong to offer_works or founder_bottleneck, not general"))
            continue
        rows.append(row)

    seen: dict[str, int] = {}
    for r in rows:
        seen[r.name.casefold()] = seen.get(r.name.casefold(), 0) + 1
    for name, count in seen.items():
        if count > 1:
            problems.append((name, f"{count} active rows share this Name; names must be unique"))

    for pillar in (Pillar.offer_works, Pillar.founder_bottleneck):
        weights = [r for r in rows if r.kind is Kind.weight and r.pillar is pillar]
        if len(weights) != 1:
            problems.append(
                (f"{pillar.value} pillar weight", f"Need exactly 1 active weight row for {pillar.value}, found {len(weights)}")
            )
        if not [r for r in rows if r.kind is Kind.trigger and r.pillar is pillar]:
            problems.append((f"{pillar.value} triggers", f"Need at least 1 active trigger for {pillar.value}"))

    if problems:
        raise RubricError(problems)
    return Rubric(tuple(sorted(rows, key=lambda r: (r.kind.value, r.pillar.value, r.name))))


def load_rubric(client) -> Rubric:
    """Read every row from the Airtable Rubric table and validate it. Called on every run."""
    return parse_rubric(client.rubric_records())


# Starting rows for an empty Rubric table. The team edits these in Airtable afterwards.
SEED_ROWS: list[dict[str, Any]] = [
    # offer_works triggers
    {"Name": "Repeat client wins", "Kind": "trigger", "Pillar": "offer_works", "Weight": 7,
     "Definition": "Posts or comments show the same clients getting results again or buying a second time, e.g. returning clients sharing a new milestone."},
    {"Name": "Testimonials naming results", "Kind": "trigger", "Pillar": "offer_works", "Weight": 8,
     "Definition": "Clients describe a specific, concrete result (numbers, money, time, a clear before and after), not just 'she is amazing'."},
    {"Name": "Buyers asking about next round", "Kind": "trigger", "Pillar": "offer_works", "Weight": 6,
     "Definition": "Commenters ask when the next round, cohort or intake opens, or ask to be put on the waitlist."},
    {"Name": "Sold out launches", "Kind": "trigger", "Pillar": "offer_works", "Weight": 6,
     "Definition": "The coach states that a launch, cohort or programme sold out, filled up, or closed early because spots were gone."},
    # founder_bottleneck triggers
    {"Name": "Every sale through a 1:1 call", "Kind": "trigger", "Pillar": "founder_bottleneck", "Weight": 8,
     "Definition": "Every route to buying goes through booking a personal call with the founder (e.g. a Calendly link); there is no checkout or sales team."},
    {"Name": "Manual hand raiser loop", "Kind": "trigger", "Pillar": "founder_bottleneck", "Weight": 8,
     "Definition": "Sales depend on 'comment X' posts (e.g. 'comment READY') where the owner replies by hand, often hours later."},
    {"Name": "Slow manual replies", "Kind": "trigger", "Pillar": "founder_bottleneck", "Weight": 6,
     "Definition": "The owner personally answers comments or DMs, often many hours later, apologising for the delay or saying they are catching up."},
    {"Name": "Posting gaps after launches", "Kind": "trigger", "Pillar": "founder_bottleneck", "Weight": 5,
     "Definition": "Posting stops for weeks after a launch because the founder is busy delivering, often followed by an apology post."},
    {"Name": "Founder does all delivery", "Kind": "trigger", "Pillar": "founder_bottleneck", "Weight": 7,
     "Definition": "The founder personally delivers every call, session, review or message for every client, with no other coaches involved."},
    # exclusions
    {"Name": "Already has a team", "Kind": "exclusion", "Pillar": "general", "Weight": 10,
     "Definition": "The account already has a team, other coaches, a community manager, an assistant or an operations person handling sales or delivery."},
    {"Name": "MLM or income claims", "Kind": "exclusion", "Pillar": "general", "Weight": 10,
     "Definition": "The offer is a multi level marketing opportunity, recruits a 'downline', or makes guaranteed income claims."},
    {"Name": "Under minimum followers", "Kind": "exclusion", "Pillar": "general", "Weight": 10,
     "Definition": "The account has fewer than 3,000 followers (use the follower count in the metrics)."},
    # weights
    {"Name": "Offer works pillar", "Kind": "weight", "Pillar": "offer_works", "Weight": 10,
     "Definition": "How much the 'does the offer genuinely work' pillar counts towards the total score."},
    {"Name": "Founder bottleneck pillar", "Kind": "weight", "Pillar": "founder_bottleneck", "Weight": 10,
     "Definition": "How much the 'is the founder the bottleneck' pillar counts towards the total score."},
    {"Name": "Follower range", "Kind": "weight", "Pillar": "general", "Weight": 3,
     "Definition": "Score 10 for 5,000 to 150,000 followers, falling off outside that range; score 0 under 3,000."},
    {"Name": "Engagement rate", "Kind": "weight", "Pillar": "general", "Weight": 3,
     "Definition": "Score from the average engagement rate in the metrics: 10 at 4% or more, 7 at 2 to 4%, 4 at 1 to 2%, 1 below 1%."},
]
for _row in SEED_ROWS:
    _row["Active"] = True
