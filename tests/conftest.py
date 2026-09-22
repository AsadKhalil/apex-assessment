import os

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("APP_FAKE_NOW", "2026-10-05T09:00:00+03:00")
os.environ.setdefault("LOG_HMAC_KEY", "test-hmac-key")
os.environ.setdefault("LLM_PROVIDER", "scripted")

from app.config import Settings  # noqa: E402
from app.state import Store  # noqa: E402


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings.from_env(db_path=str(tmp_path / "test.db"), tool_timeout_s=0.3)


@pytest.fixture
def store(settings) -> Store:
    s = Store(settings)
    s.init_schema()
    s.seed()
    return s
