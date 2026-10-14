from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.models import Account, Assessment
from src.rubric import SEED_ROWS, parse_rubric
from src.store import Store
from tests.fakes import ASSESSMENTS

ROOT = Path(__file__).resolve().parent.parent
ACCOUNTS = ROOT / "data" / "accounts"


def seed_records(rows=None):
    return [{"id": f"rec{i}", "fields": dict(r)} for i, r in enumerate(rows or SEED_ROWS)]


@pytest.fixture
def rubric():
    return parse_rubric(seed_records())


@pytest.fixture
def load_account():
    def _load(name: str) -> Account:
        return Account.model_validate_json((ACCOUNTS / f"{name}.json").read_text(encoding="utf-8"))
    return _load


@pytest.fixture
def load_assessment():
    def _load(name: str) -> Assessment:
        return Assessment.model_validate(ASSESSMENTS[name])
    return _load


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()
