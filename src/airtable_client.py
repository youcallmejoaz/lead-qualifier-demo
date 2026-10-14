"""Thin wrapper around pyairtable so the rest of the code (and the tests) see a small interface."""

from __future__ import annotations

from typing import Any

from pyairtable import Api

from .rubric import SEED_ROWS

ACCOUNT_ID_FIELD = "Account ID"

_SELECT = "singleSelect"
_CHECKBOX_OPTS = {"icon": "check", "color": "greenBright"}
_DATETIME_OPTS = {"dateFormat": {"name": "iso"}, "timeFormat": {"name": "24hour"}, "timeZone": "utc"}


def _choices(*names: str) -> dict[str, Any]:
    return {"choices": [{"name": n} for n in names]}


# name -> (Airtable field type, options for creation, human readable type for instructions)
RUBRIC_SCHEMA: dict[str, tuple[str, dict | None, str]] = {
    "Name": ("singleLineText", None, "Single line text (primary field)"),
    "Kind": (_SELECT, _choices("trigger", "exclusion", "weight"), "Single select: trigger, exclusion, weight"),
    "Pillar": (_SELECT, _choices("offer_works", "founder_bottleneck", "general"),
               "Single select: offer_works, founder_bottleneck, general"),
    "Definition": ("multilineText", None, "Long text"),
    "Weight": ("number", {"precision": 1}, "Number (0 to 10, 1 decimal)"),
    "Active": ("checkbox", _CHECKBOX_OPTS, "Checkbox"),
}

LEADS_SCHEMA: dict[str, tuple[str, dict | None, str]] = {
    "Account ID": ("singleLineText", None, "Single line text (primary field, the merge key)"),
    "Handle": ("singleLineText", None, "Single line text"),
    "Verdict": (_SELECT, _choices("qualified", "rejected", "review"), "Single select: qualified, rejected, review"),
    "Total Score": ("number", {"precision": 1}, "Number (1 decimal)"),
    "Offer Works Score": ("number", {"precision": 1}, "Number (1 decimal)"),
    "Bottleneck Score": ("number", {"precision": 1}, "Number (1 decimal)"),
    "Summary": ("multilineText", None, "Long text"),
    "Evidence": ("multilineText", None, "Long text"),
    "Exclusions Hit": ("multilineText", None, "Long text"),
    "Rubric Version": ("singleLineText", None, "Single line text"),
    "Last Scored": ("dateTime", _DATETIME_OPTS, "Date with time (GMT/UTC)"),
}

# Airtable reports some compatible types under different names.
_COMPATIBLE = {"singleLineText": {"singleLineText", "email", "url", "phoneNumber"},
               "multilineText": {"multilineText", "richText"},
               "number": {"number", "percent", "decimal"},
               "dateTime": {"dateTime", "date"}}


def type_ok(expected: str, actual: str) -> bool:
    return actual in _COMPATIBLE.get(expected, {expected})


class AirtableClient:
    def __init__(self, api_key: str, base_id: str, rubric_table: str = "Rubric", leads_table: str = "Leads"):
        # retry_strategy=None: our sync worker owns retries, so a 429 reaches it instead of being hidden.
        self.api = Api(api_key, retry_strategy=None)
        self.base = self.api.base(base_id)
        self.rubric_table_name = rubric_table
        self.leads_table_name = leads_table

    @property
    def rubric(self):
        return self.base.table(self.rubric_table_name)

    @property
    def leads(self):
        return self.base.table(self.leads_table_name)

    # -- rubric ------------------------------------------------------------

    def rubric_records(self) -> list[dict[str, Any]]:
        return self.rubric.all()

    def seed_rubric(self) -> int:
        self.rubric.batch_create([dict(r) for r in SEED_ROWS], typecast=True)
        return len(SEED_ROWS)

    # -- leads -------------------------------------------------------------

    def upsert_leads(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """One HTTP request: callers pass at most 10 records."""
        assert len(records) <= 10, "Airtable accepts at most 10 records per request"
        return self.leads.batch_upsert(
            [{"fields": r} for r in records], key_fields=[ACCOUNT_ID_FIELD], typecast=True
        )

    def find_leads(self, account_id: str) -> list[dict[str, Any]]:
        safe = account_id.replace("\\", "\\\\").replace("'", "\\'")
        return self.leads.all(formula=f"{{{ACCOUNT_ID_FIELD}}} = '{safe}'")

    # -- schema (setup-airtable) -------------------------------------------

    def table_fields(self) -> dict[str, dict[str, str] | None]:
        """Map of table name -> {field name: field type}, or None if the table does not exist."""
        schema = self.base.schema(force=True)
        by_name = {t.name: {f.name: f.type for f in t.fields} for t in schema.tables}
        return {name: by_name.get(name) for name in (self.rubric_table_name, self.leads_table_name)}

    def create_table(self, name: str, spec: dict[str, tuple[str, dict | None, str]]) -> None:
        fields = []
        for fname, (ftype, opts, _) in spec.items():
            f: dict[str, Any] = {"name": fname, "type": ftype}
            if opts:
                f["options"] = opts
            fields.append(f)
        self.base.create_table(name, fields)

    def create_field(self, table: str, name: str, spec: tuple[str, dict | None, str]) -> None:
        ftype, opts, _ = spec
        self.base.table(table).create_field(name, ftype, options=opts)
