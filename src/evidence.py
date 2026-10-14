"""Checks that every quote Claude cites really appears in the account's source data.

Nothing invented: a quote that cannot be found is dropped, and the pillar's
confidence falls. Bio quotes are verified like any other but never count as
evidence, because the brief says never to judge from the bio alone.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel

from .models import Account, Assessment, EvidenceQuote, Pillar
from .rubric import Rubric

BIO = "bio"
LINK = "link_in_bio"
METRICS = "metrics"


class QuoteStatus(str, Enum):
    verified = "verified"  # found where Claude said it was
    corrected = "corrected"  # found, but in a different source; source fixed
    dropped = "dropped"  # not found anywhere; discarded


class QuoteCheck(BaseModel):
    quote: str
    claimed_source: str
    source_id: str | None  # where it was actually found
    status: QuoteStatus
    date: str | None = None  # post date, if it came from a post

    @property
    def found(self) -> bool:
        return self.status is not QuoteStatus.dropped

    @property
    def counts(self) -> bool:
        """A quote counts as evidence if it was found and is not from the bio."""
        return self.found and self.source_id != BIO


class TriggerCheck(BaseModel):
    trigger: str
    strength: float
    known: bool  # the trigger name exists in the rubric used for this run
    quotes: list[QuoteCheck]

    @property
    def matched(self) -> bool:
        return self.known and any(q.counts for q in self.quotes)


class PillarCheck(BaseModel):
    pillar: Pillar
    claude_score: float
    reason: str
    triggers: list[TriggerCheck]

    @property
    def claimed(self) -> int:
        return sum(len(t.quotes) for t in self.triggers)

    @property
    def verified(self) -> int:
        return sum(1 for t in self.triggers for q in t.quotes if q.found)

    @property
    def confidence(self) -> float:
        return self.verified / self.claimed if self.claimed else 0.0

    @property
    def has_evidence(self) -> bool:
        return any(q.counts for t in self.triggers for q in t.quotes)


class ExclusionCheck(BaseModel):
    exclusion: str
    known: bool
    quotes: list[QuoteCheck]

    @property
    def hit(self) -> bool:
        return self.known and any(q.counts for q in self.quotes)


class VerifiedAssessment(BaseModel):
    offer_works: PillarCheck
    founder_bottleneck: PillarCheck
    exclusions: list[ExclusionCheck]
    general_scores: dict[str, float]
    general_reasons: dict[str, str]
    summary: str

    def pillar(self, pillar: Pillar) -> PillarCheck:
        return self.offer_works if pillar is Pillar.offer_works else self.founder_bottleneck

    def all_quotes(self) -> list[QuoteCheck]:
        out = [q for p in (self.offer_works, self.founder_bottleneck) for t in p.triggers for q in t.quotes]
        out += [q for e in self.exclusions for q in e.quotes]
        return out

    @property
    def exclusions_hit(self) -> list[ExclusionCheck]:
        return [e for e in self.exclusions if e.hit]


# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    source_id: str
    text: str  # normalised
    date: str | None


_QUOTE_MAP = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "–": "-", "—": "-", "−": "-", " ": " ",
})
_ELLIPSIS = re.compile(r"\.\.\.+|…")


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_QUOTE_MAP).casefold()
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text)  # 12,500 -> 12500
    return re.sub(r"\s+", " ", text).strip()


def metrics_text(account: Account) -> str:
    return f"followers: {account.followers} | avg engagement rate: {account.avg_engagement_rate}%"


def build_corpus(account: Account) -> dict[str, Source]:
    corpus = {
        BIO: Source(BIO, normalise(account.bio), None),
        LINK: Source(LINK, normalise(account.link_in_bio_text), None),
        METRICS: Source(METRICS, normalise(metrics_text(account)), None),
    }
    for post in account.posts:
        parts = [post.caption]
        for c in post.comments:
            parts.append(c.text)
            if c.reply_from_owner:
                parts.append(c.reply_from_owner)
        # Join with a separator so a quote can't straddle two different comments.
        corpus[post.post_id] = Source(post.post_id, " ␞ ".join(normalise(p) for p in parts), post.date)
    return corpus


def _fragments(quote: str) -> list[str]:
    frags = [normalise(f).strip(" \"'.,") for f in _ELLIPSIS.split(quote)]
    return [f for f in frags if len(f) >= 3]


def _contains(source: Source, frags: list[str]) -> bool:
    pos = 0
    for frag in frags:
        idx = source.text.find(frag, pos)
        if idx < 0:
            return False
        pos = idx + len(frag)
    return True


def verify_quote(quote: EvidenceQuote, corpus: dict[str, Source]) -> QuoteCheck:
    frags = _fragments(quote.quote)
    claimed = quote.source_id.strip()
    base = {"quote": quote.quote.strip(), "claimed_source": claimed}
    if not frags:
        return QuoteCheck(**base, source_id=None, status=QuoteStatus.dropped)

    src = corpus.get(claimed)
    if src and _contains(src, frags):
        return QuoteCheck(**base, source_id=src.source_id, status=QuoteStatus.verified, date=src.date)

    # Look elsewhere, preferring posts and the link page over the bio.
    order = sorted(corpus.values(), key=lambda s: s.source_id == BIO)
    for other in order:
        if _contains(other, frags):
            return QuoteCheck(**base, source_id=other.source_id, status=QuoteStatus.corrected, date=other.date)
    return QuoteCheck(**base, source_id=None, status=QuoteStatus.dropped)


def verify_assessment(assessment: Assessment, account: Account, rubric: Rubric) -> VerifiedAssessment:
    corpus = build_corpus(account)

    def pillar_check(pillar: Pillar) -> PillarCheck:
        pa = assessment.pillar(pillar)
        known = {r.name.casefold() for r in rubric.triggers(pillar)}
        triggers = []
        for m in pa.matched_triggers:
            row = rubric.by_name(m.trigger)
            triggers.append(TriggerCheck(
                trigger=row.name if row else m.trigger,
                strength=m.strength,
                known=m.trigger.strip().casefold() in known,
                quotes=[verify_quote(q, corpus) for q in m.quotes],
            ))
        return PillarCheck(pillar=pillar, claude_score=pa.score, reason=pa.reason, triggers=triggers)

    exclusion_names = {r.name.casefold() for r in rubric.exclusions}
    exclusions = []
    for e in assessment.exclusions_hit:
        row = rubric.by_name(e.exclusion)
        exclusions.append(ExclusionCheck(
            exclusion=row.name if row else e.exclusion,
            known=e.exclusion.strip().casefold() in exclusion_names,
            quotes=[verify_quote(q, corpus) for q in e.quotes],
        ))

    general_scores, general_reasons = {}, {}
    for g in assessment.general_scores:
        row = rubric.by_name(g.name)
        if row and row in rubric.general_weights:
            general_scores[row.name] = g.score
            general_reasons[row.name] = g.reason

    return VerifiedAssessment(
        offer_works=pillar_check(Pillar.offer_works),
        founder_bottleneck=pillar_check(Pillar.founder_bottleneck),
        exclusions=exclusions,
        general_scores=general_scores,
        general_reasons=general_reasons,
        summary=assessment.summary,
    )
