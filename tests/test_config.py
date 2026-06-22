"""Central RM_* config helpers (resume_matcher/config.py)."""
from resume_matcher.config import DemoConfig, env_flag, env_int, env_str


def test_env_int_parses_and_falls_back(monkeypatch):
    monkeypatch.setenv("X_INT", "7")
    assert env_int("X_INT", 1) == 7
    monkeypatch.setenv("X_INT", "not-a-number")
    assert env_int("X_INT", 1) == 1  # invalid -> default
    monkeypatch.setenv("X_INT", "   ")
    assert env_int("X_INT", 5) == 5  # blank -> default
    monkeypatch.delenv("X_INT", raising=False)
    assert env_int("X_INT", 3) == 3


def test_env_flag_and_str(monkeypatch):
    monkeypatch.setenv("X_FLAG", "Yes")
    assert env_flag("X_FLAG", False) is True
    monkeypatch.setenv("X_FLAG", "0")
    assert env_flag("X_FLAG", True) is False
    monkeypatch.delenv("X_FLAG", raising=False)
    assert env_flag("X_FLAG", True) is True  # unset -> default
    assert env_str("X_MISSING", "fallback") == "fallback"


def test_demo_config_snapshot(monkeypatch):
    monkeypatch.setenv("RM_DEMO_MAX_RESUMES", "3")
    monkeypatch.setenv("RM_DEMO_SEND_FILE", "0")
    cfg = DemoConfig.from_env()
    assert cfg.max_resumes == 3 and cfg.send_file is False
    assert cfg.concurrency >= 1  # always floored to >=1
