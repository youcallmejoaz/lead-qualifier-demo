"""Environment configuration. Secrets come from .env, never from code."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_MODEL = "claude-sonnet-5-5"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str | None
    anthropic_model: str
    airtable_api_key: str | None
    airtable_base_id: str | None
    rubric_table: str
    leads_table: str
    db_path: Path
    accounts_dir: Path

    def require_anthropic(self) -> str:
        if not self.anthropic_api_key:
            raise ConfigError("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.")
        return self.anthropic_api_key

    def require_airtable(self) -> tuple[str, str]:
        if not self.airtable_api_key or not self.airtable_base_id:
            raise ConfigError(
                "AIRTABLE_API_KEY and AIRTABLE_BASE_ID must be set. Copy .env.example to .env and fill them in."
            )
        return self.airtable_api_key, self.airtable_base_id


def load_settings() -> Settings:
    load_dotenv()
    return Settings(
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
        anthropic_model=os.getenv("ANTHROPIC_MODEL") or DEFAULT_MODEL,
        airtable_api_key=os.getenv("AIRTABLE_API_KEY") or None,
        airtable_base_id=os.getenv("AIRTABLE_BASE_ID") or None,
        rubric_table=os.getenv("AIRTABLE_RUBRIC_TABLE") or "Rubric",
        leads_table=os.getenv("AIRTABLE_LEADS_TABLE") or "Leads",
        db_path=Path(os.getenv("LQ_DB_PATH") or "lead_qualifier.db"),
        accounts_dir=Path(os.getenv("LQ_ACCOUNTS_DIR") or "data/accounts"),
    )
