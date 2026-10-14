"""Pydantic schemas for input data, the rubric and Claude's assessment."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ---------------------------------------------------------------------------
# Input data (one JSON file per Instagram account)
# ---------------------------------------------------------------------------


class Comment(BaseModel):
    author: str
    text: str
    reply_from_owner: str | None = None
    reply_delay_hours: float | None = None


class Post(BaseModel):
    post_id: str
    date: str
    caption: str
    likes: int = 0
    comments_count: int = 0
    comments: list[Comment] = Field(default_factory=list)


class Account(BaseModel):
    account_id: str = Field(min_length=1)
    handle: str = Field(min_length=1)
    bio: str = ""
    followers: int = Field(ge=0)
    link_in_bio_text: str = ""
    posts: list[Post] = Field(default_factory=list)

    @field_validator("account_id")
    @classmethod
    def _strip_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("account_id must not be empty")
        return v

    @field_validator("handle")
    @classmethod
    def _clean_handle(cls, v: str) -> str:
        return v.strip().lstrip("@").lower()

    def post(self, post_id: str) -> Post | None:
        return next((p for p in self.posts if p.post_id == post_id), None)

    @property
    def avg_engagement_rate(self) -> float:
        """Average (likes + comments) per post divided by followers, as a percentage."""
        if not self.posts or not self.followers:
            return 0.0
        per_post = sum(p.likes + p.comments_count for p in self.posts) / len(self.posts)
        return round(per_post / self.followers * 100, 2)


# ---------------------------------------------------------------------------
# Rubric (lives in Airtable)
# ---------------------------------------------------------------------------


class Kind(str, Enum):
    trigger = "trigger"
    exclusion = "exclusion"
    weight = "weight"


class Pillar(str, Enum):
    offer_works = "offer_works"
    founder_bottleneck = "founder_bottleneck"
    general = "general"


PILLAR_LABELS = {
    Pillar.offer_works: "Offer works",
    Pillar.founder_bottleneck: "Founder bottleneck",
    Pillar.general: "General",
}


class RubricRow(BaseModel):
    name: str = Field(min_length=1)
    kind: Kind
    pillar: Pillar
    definition: str = Field(min_length=10)
    weight: float = Field(ge=0, le=10)
    active: bool = True
    airtable_id: str | None = None

    @field_validator("name", "definition")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    def fingerprint(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "pillar": self.pillar.value,
            "definition": self.definition,
            "weight": self.weight,
        }


# ---------------------------------------------------------------------------
# Claude's assessment (structured output of the submit_assessment tool)
# ---------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceQuote(_Strict):
    quote: str = Field(description="Exact words copied verbatim from the source. No paraphrasing.")
    source_id: str = Field(
        description='Where the quote comes from: a post ID such as "p01", "link_in_bio", "bio" or "metrics".'
    )


class TriggerMatch(_Strict):
    trigger: str = Field(description="Exact Name of the rubric trigger row that matched.")
    strength: float = Field(ge=0, le=10, description="How strongly the evidence shows this trigger, 0 to 10.")
    quotes: list[EvidenceQuote]


class PillarAssessment(_Strict):
    score: float = Field(ge=0, le=10, description="Overall judgment for this pillar, 0 to 10.")
    matched_triggers: list[TriggerMatch]
    reason: str = Field(description="One sentence explaining the score.")


class ExclusionHit(_Strict):
    exclusion: str = Field(description="Exact Name of the rubric exclusion row that was hit.")
    quotes: list[EvidenceQuote]


class GeneralScore(_Strict):
    name: str = Field(description="Exact Name of the general weight row.")
    score: float = Field(ge=0, le=10)
    reason: str


class Assessment(_Strict):
    offer_works: PillarAssessment
    founder_bottleneck: PillarAssessment
    exclusions_hit: list[ExclusionHit]
    general_scores: list[GeneralScore]
    summary: str = Field(description="2 to 3 sentences for a salesperson.")

    def pillar(self, pillar: Pillar) -> PillarAssessment:
        if pillar is Pillar.offer_works:
            return self.offer_works
        if pillar is Pillar.founder_bottleneck:
            return self.founder_bottleneck
        raise ValueError(pillar)


class Verdict(str, Enum):
    qualified = "qualified"
    rejected = "rejected"
    review = "review"
