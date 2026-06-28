"""Tiny JSON stores on the Fly Volume.

Two files:
  - ledger.json : the budget ledger (see Ledger below)
  - issues.json : per-issue metadata the labels can't hold
                  (branch name, PR number, review round count, paused stage,
                  last-seen comment id for the clarify loop)

Single writer (one Machine), so plain read-modify-write is safe.
"""
import json
import os
import time
from . import config

os.makedirs(config.DATA_DIR, exist_ok=True)
LEDGER_PATH = os.path.join(config.DATA_DIR, "ledger.json")
ISSUES_PATH = os.path.join(config.DATA_DIR, "issues.json")

_FIVE_H = 5 * 3600
_WEEK = 7 * 24 * 3600


def _load(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def _save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ── Per-issue metadata ────────────────────────────────────────────────────
def issue_meta(n):
    data = _load(ISSUES_PATH, {})
    return data.get(str(n), {})


def update_issue_meta(n, **fields):
    data = _load(ISSUES_PATH, {})
    m = data.get(str(n), {})
    m.update(fields)
    data[str(n)] = m
    _save(ISSUES_PATH, data)
    return m


# ── Budget ledger ─────────────────────────────────────────────────────────
class Ledger:
    """Tracks two pools against rolling 5h + weekly windows.

    Claude pool is token-counted (we read exact usage from Claude Code's JSON
    output per call). GLM pool is prompt-counted (z.ai is prompt-quota'd), with
    a safety multiplier applied per call.
    """

    def __init__(self):
        self.d = _load(LEDGER_PATH, {})
        for pool in ("claude", "glm"):
            self.d.setdefault(pool, {
                "w5": {"start": time.time(), "tokens": 0, "prompts": 0},
                "wk": {"start": time.time(), "tokens": 0, "prompts": 0},
            })
        self._roll()

    def _roll(self):
        now = time.time()
        for pool in self.d.values():
            if now - pool["w5"]["start"] >= _FIVE_H:
                pool["w5"] = {"start": now, "tokens": 0, "prompts": 0}
            if now - pool["wk"]["start"] >= _WEEK:
                pool["wk"] = {"start": now, "tokens": 0, "prompts": 0}
        self._save()

    def _save(self):
        _save(LEDGER_PATH, self.d)

    def record(self, pool, tokens=0, prompts=0):
        p = self.d[pool]
        for w in ("w5", "wk"):
            p[w]["tokens"] += tokens
            p[w]["prompts"] += prompts
        self._save()

    def headroom(self, pool):
        """True if this pool has room for another step in both windows."""
        self._roll()
        f = config.BUDGET_SAFETY_FRACTION
        p = self.d[pool]
        if pool == "claude":
            return (p["w5"]["tokens"] < config.CLAUDE_5H_TOKEN_BUDGET * f and
                    p["wk"]["tokens"] < config.CLAUDE_WEEK_TOKEN_BUDGET * f)
        lim = config.GLM_TIER_LIMITS[config.GLM_TIER]
        return (p["w5"]["prompts"] < lim["per5h"] * f and
                p["wk"]["prompts"] < lim["perweek"] * f)

    def next_reset(self):
        """Earliest time any window frees up — for the blocked:budget retry."""
        now = time.time()
        soonest = None
        for p in self.d.values():
            for w in ("w5", "wk"):
                span = _FIVE_H if w == "w5" else _WEEK
                t = p[w]["start"] + span
                if t > now and (soonest is None or t < soonest):
                    soonest = t
        return soonest

    def snapshot(self):
        self._roll()
        lim = config.GLM_TIER_LIMITS[config.GLM_TIER]
        return {
            "claude_5h_tokens": self.d["claude"]["w5"]["tokens"],
            "claude_5h_budget": config.CLAUDE_5H_TOKEN_BUDGET,
            "glm_5h_prompts": self.d["glm"]["w5"]["prompts"],
            "glm_5h_limit": lim["per5h"],
        }
