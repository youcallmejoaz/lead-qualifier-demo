import pytest

from src.rubric import RubricError, parse_rubric, rubric_version
from tests.conftest import seed_records


def _with(name, **changes):
    rows = seed_records()
    for r in rows:
        if r["fields"]["Name"] == name:
            r["fields"].update(changes)
            for k, v in changes.items():
                if v is None:
                    r["fields"].pop(k)
    return rows


def test_seed_rubric_is_valid():
    r = parse_rubric(seed_records())
    assert len(r.rows) == 16
    assert len(r.version) == 12


@pytest.mark.parametrize("changes,needle", [
    ({"Weight": 12}, "0 to 10"),
    ({"Weight": -1}, "0 to 10"),
    ({"Weight": None}, "Weight is missing"),
    ({"Definition": None}, "Definition is missing"),
    ({"Definition": "short"}, "Definition is missing or too short"),
    ({"Kind": "bonus"}, "Kind must be"),
])
def test_bad_rows_are_caught(changes, needle):
    with pytest.raises(RubricError) as exc:
        parse_rubric(_with("Manual hand raiser loop", **changes))
    names = [name for name, _ in exc.value.problems]
    assert "Manual hand raiser loop" in names
    assert needle in str(exc.value)


def test_trigger_cannot_be_general():
    with pytest.raises(RubricError, match="must belong to offer_works or founder_bottleneck"):
        parse_rubric(_with("Slow manual replies", Pillar="general"))


def test_duplicate_active_names_are_caught():
    rows = seed_records()
    rows.append({"id": "dup", "fields": dict(rows[0]["fields"])})
    with pytest.raises(RubricError, match="share this Name"):
        parse_rubric(rows)


def test_missing_pillar_weight_is_caught():
    with pytest.raises(RubricError, match="exactly 1 active weight row for offer_works"):
        parse_rubric(_with("Offer works pillar", Active=False))


def test_inactive_bad_rows_are_ignored():
    rows = _with("Manual hand raiser loop", Weight=99, Active=False)
    r = parse_rubric(rows)
    assert r.by_name("Manual hand raiser loop") is None


def test_version_is_stable_and_order_independent():
    a = parse_rubric(seed_records())
    b = parse_rubric(list(reversed(seed_records())))
    assert a.version == b.version == rubric_version(a.rows)
    c = parse_rubric(_with("Manual hand raiser loop", Weight=9))
    assert c.version != a.version
