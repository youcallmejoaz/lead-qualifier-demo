"""Combines Claude's verified judgment with the rubric weights from Airtable.

Pillar score (0-10) = 0.5 x Claude part + 0.5 x rubric part
  Claude part = Claude's own 0-10 score x evidence confidence (verified / claimed quotes)
  Rubric part = 10 x sum(weight x strength/10 of matched triggers) / sum(weight of all active triggers)
  A pillar with no verified evidence from posts, comments or the link in bio page scores 0.

Total (0-100) = 10 x sum(component x weight) / sum(weight)
  Components: the two pillars (their pillar weight rows) and each general weight row.
"""

from __future__ import annotations

from pydantic import BaseModel

from .evidence import VerifiedAssessment
from .models import PILLAR_LABELS, Kind, Pillar, Verdict
from .rubric import Rubric

CLAUDE_SHARE = 0.5
RUBRIC_SHARE = 0.5
QUALIFY_THRESHOLD = 6.5  # both pillars must reach this to qualify
REJECT_THRESHOLD = 4.0  # either pillar below this rejects


def _fmt(x: float) -> str:
    return f"{x:.1f}".removesuffix(".0")


class TriggerPoints(BaseModel):
    name: str
    weight: float
    strength: float
    points: float


class PillarScore(BaseModel):
    pillar: Pillar
    claude_score: float
    confidence: float
    verified: int
    claimed: int
    claude_part: float
    points: float
    max_points: float
    rubric_part: float
    score: float
    has_evidence: bool
    matched: list[TriggerPoints]
    math: str


class Component(BaseModel):
    label: str
    score: float
    weight: float


class ScoreResult(BaseModel):
    offer_works: PillarScore
    founder_bottleneck: PillarScore
    components: list[Component]
    total: float
    total_math: str
    exclusions_hit: list[str]
    verdict: Verdict
    verdict_reason: str
    rubric_version: str

    def pillar(self, pillar: Pillar) -> PillarScore:
        return self.offer_works if pillar is Pillar.offer_works else self.founder_bottleneck


def score_pillar(va: VerifiedAssessment, pillar: Pillar, rubric: Rubric) -> PillarScore:
    check = va.pillar(pillar)
    label = PILLAR_LABELS[pillar]
    active = {r.name.casefold(): r for r in rubric.triggers(pillar)}
    max_points = sum(r.weight for r in active.values())

    matched: list[TriggerPoints] = []
    for t in check.triggers:
        row = active.get(t.trigger.casefold())
        if row is None or not any(q.counts for q in t.quotes):
            continue
        strength = max(0.0, min(10.0, t.strength))
        matched.append(TriggerPoints(name=row.name, weight=row.weight, strength=strength,
                                     points=row.weight * strength / 10))
    points = round(sum(m.points for m in matched), 1)
    rubric_part = round(10 * points / max_points, 1) if max_points else 0.0

    confidence = check.confidence
    claude_part = round(check.claude_score * confidence, 1)

    if not check.has_evidence:
        score = 0.0
        math = (f"{label} = 0 (no verified evidence from posts, comments or link in bio; "
                f"{check.verified}/{check.claimed} quotes verified)")
    else:
        score = round(CLAUDE_SHARE * claude_part + RUBRIC_SHARE * rubric_part, 1)
        math = (f"{label} = {CLAUDE_SHARE} × {_fmt(claude_part)} (Claude {_fmt(check.claude_score)}, "
                f"{check.verified}/{check.claimed} verified) + {RUBRIC_SHARE} × {_fmt(rubric_part)} "
                f"(rubric: {_fmt(points)} of {_fmt(max_points)} pts) = {score:.1f}")

    return PillarScore(
        pillar=pillar, claude_score=check.claude_score, confidence=confidence,
        verified=check.verified, claimed=check.claimed, claude_part=claude_part,
        points=points, max_points=max_points, rubric_part=rubric_part, score=score,
        has_evidence=check.has_evidence, matched=matched, math=math,
    )


def decide_verdict(offer: float, bottleneck: float, exclusions: list[str]) -> tuple[Verdict, str]:
    if exclusions:
        return Verdict.rejected, "Exclusion hit: " + ", ".join(exclusions)
    low = [n for n, s in (("offer works", offer), ("founder bottleneck", bottleneck)) if s < REJECT_THRESHOLD]
    if low:
        return Verdict.rejected, f"{' and '.join(low).capitalize()} below {REJECT_THRESHOLD}"
    if offer >= QUALIFY_THRESHOLD and bottleneck >= QUALIFY_THRESHOLD:
        return Verdict.qualified, f"Both pillars at or above {QUALIFY_THRESHOLD} and no exclusions"
    return Verdict.review, f"Between {REJECT_THRESHOLD} and {QUALIFY_THRESHOLD} on at least one pillar; needs a human look"


def score(va: VerifiedAssessment, rubric: Rubric) -> ScoreResult:
    offer = score_pillar(va, Pillar.offer_works, rubric)
    bottleneck = score_pillar(va, Pillar.founder_bottleneck, rubric)

    components = [
        Component(label="Offer works", score=offer.score, weight=rubric.pillar_weight(Pillar.offer_works).weight),
        Component(label="Founder bottleneck", score=bottleneck.score,
                  weight=rubric.pillar_weight(Pillar.founder_bottleneck).weight),
    ]
    for row in rubric.general_weights:
        components.append(Component(label=row.name, score=va.general_scores.get(row.name, 0.0), weight=row.weight))

    total_weight = sum(c.weight for c in components)
    weighted = sum(c.score * c.weight for c in components)
    total = round(10 * weighted / total_weight, 1) if total_weight else 0.0
    terms = " + ".join(f"{_fmt(c.score)}×{_fmt(c.weight)}" for c in components)
    total_math = f"Total = ({terms}) / {_fmt(total_weight)} × 10 = {total:.1f}"

    active_exclusions = {r.name.casefold() for r in rubric.exclusions}
    hits = [e.exclusion for e in va.exclusions
            if e.exclusion.casefold() in active_exclusions and any(q.counts for q in e.quotes)]
    verdict, reason = decide_verdict(offer.score, bottleneck.score, hits)

    return ScoreResult(
        offer_works=offer, founder_bottleneck=bottleneck, components=components, total=total,
        total_math=total_math, exclusions_hit=hits, verdict=verdict, verdict_reason=reason,
        rubric_version=rubric.version,
    )


def definition_changes(old: Rubric, new: Rubric) -> list[str]:
    """Trigger or exclusion rows that are new or whose wording changed since a stored run.

    Weight changes are applied by `preview` directly; wording changes need Claude to re-judge.
    """
    old_defs = {r.name.casefold(): r.definition for r in old.rows if r.kind is not Kind.weight}
    return [r.name for r in new.rows
            if r.kind is not Kind.weight and old_defs.get(r.name.casefold()) != r.definition]

