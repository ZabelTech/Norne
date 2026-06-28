"""Shared test setup.

`orchestrator.config` reads several env vars *at import time* (GH_TOKEN, GH_REPO)
and `orchestrator.store` runs `os.makedirs(DATA_DIR)` at import. So we populate a
hermetic environment here, before pytest imports any test module (and therefore
before the orchestrator package is first imported). Nothing here talks to the
network — these are placeholders that satisfy module-load, not real credentials.
"""
import os
import tempfile

# A throwaway data dir so store's import-time makedirs() never touches /data.
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="norne-tests-")

os.environ.setdefault("GH_TOKEN", "test-token")
os.environ.setdefault("GH_REPO", "owner/repo")
os.environ.setdefault("BOT_LOGIN", "pipeline-bot")
# Both model families configured by default, so routing treats each as
# available (see config.family_available); Claude-less tests clear the token.
os.environ.setdefault("CLAUDE_CODE_OAUTH_TOKEN", "test-claude-oauth")
os.environ.setdefault("ZAI_AUTH_TOKEN", "test-zai-token")
os.environ.setdefault("DATA_DIR", _TEST_DATA_DIR)
os.environ.setdefault("WORKDIR_ROOT", os.path.join(_TEST_DATA_DIR, "work"))
# Deterministic budget/tier knobs so the ledger tests assert against known numbers.
os.environ.setdefault("CLAUDE_5H_TOKEN_BUDGET", "1000")
os.environ.setdefault("CLAUDE_WEEK_TOKEN_BUDGET", "10000")
os.environ.setdefault("GLM_TIER", "lite")
os.environ.setdefault("GLM_QUOTA_MULTIPLIER", "2.0")
os.environ.setdefault("BUDGET_SAFETY_FRACTION", "0.85")

import pytest

from orchestrator import store


class FakeClock:
    """A monkeypatchable stand-in for time.time() used by the ledger."""

    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(store.time, "time", c)
    return c


@pytest.fixture
def ledger_paths(tmp_path, monkeypatch):
    """Point the ledger + issue stores at per-test temp files."""
    ledger = tmp_path / "ledger.json"
    issues = tmp_path / "issues.json"
    monkeypatch.setattr(store, "LEDGER_PATH", str(ledger))
    monkeypatch.setattr(store, "ISSUES_PATH", str(issues))
    return ledger, issues


@pytest.fixture
def fresh_ledger(clock, ledger_paths):
    """A Ledger with isolated storage and a controllable clock."""
    return store.Ledger()
