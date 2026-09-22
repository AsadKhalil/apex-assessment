"""Environment-driven settings and the (optionally fixed) clock."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, fields
from datetime import datetime, timedelta, timezone

# Asia/Riyadh is UTC+3 with no DST; a fixed offset avoids a tz database dependency.
RIYADH = timezone(timedelta(hours=3), "Asia/Riyadh")

_ENV_MAP = {
    "app_env": "APP_ENV",
    "openai_api_key": "OPENAI_API_KEY",
    "openai_model": "OPENAI_MODEL",
    "openai_reasoning_effort": "OPENAI_REASONING_EFFORT",
    "llm_provider": "LLM_PROVIDER",
    "log_hmac_key": "LOG_HMAC_KEY",
    "db_path": "DB_PATH",
    "fake_now": "APP_FAKE_NOW",
    "mock_fault": "MOCK_FAULT",
    "kb_dir": "KB_DIR",
}


@dataclass(frozen=True)
class Settings:
    app_env: str = "local"
    openai_api_key: str | None = None
    openai_model: str = "gpt-5.6-luna"
    openai_reasoning_effort: str | None = None
    llm_provider: str = "openai"
    log_hmac_key: str = "dev-only-change-me"
    db_path: str = "data/assistant.db"
    fake_now: str | None = None
    mock_fault: str | None = None
    kb_dir: str = "kb"
    tool_timeout_s: float = 2.0
    pending_ttl_s: int = 600
    max_tool_calls: int = 6
    max_iterations: int = 4

    @classmethod
    def from_env(cls, **overrides) -> Settings:
        values = {}
        for f in fields(cls):
            env_name = _ENV_MAP.get(f.name)
            raw = os.environ.get(env_name) if env_name else None
            if raw is not None:
                # .env files copied from .env.example may carry inline comments; drop them.
                raw = re.sub(r"\s+#.*$", "", raw).strip()
                if raw != "":
                    values[f.name] = raw
        values.update(overrides)
        return cls(**values)

    @property
    def faults_enabled(self) -> bool:
        return self.app_env != "production"


def now(settings: Settings) -> datetime:
    if settings.fake_now:
        return datetime.fromisoformat(settings.fake_now).astimezone(RIYADH)
    return datetime.now(RIYADH)
