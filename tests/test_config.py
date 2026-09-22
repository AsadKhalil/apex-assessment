from datetime import datetime, timedelta, timezone

from app.config import RIYADH, Settings, now


def test_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    s = Settings.from_env(db_path="x.db")
    assert s.openai_model == "gpt-5.6-luna"
    assert s.db_path == "x.db"
    assert s.max_tool_calls == 6 and s.max_iterations == 4 and s.pending_ttl_s == 600


def test_env_is_read(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-terra")
    monkeypatch.setenv("APP_ENV", "production")
    s = Settings.from_env()
    assert s.openai_model == "gpt-5.6-terra"
    assert s.faults_enabled is False


def test_fixed_clock():
    s = Settings.from_env(fake_now="2026-10-05T09:00:00+03:00")
    t = now(s)
    assert t == datetime(2026, 10, 5, 9, 0, tzinfo=timezone(timedelta(hours=3)))
    assert t.tzinfo is not None


def test_real_clock_is_riyadh():
    s = Settings.from_env(fake_now=None)
    assert now(s).utcoffset() == timedelta(hours=3)
    assert RIYADH.utcoffset(None) == timedelta(hours=3)
