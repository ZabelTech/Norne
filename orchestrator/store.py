"""Tiny JSON stores on the Fly Volume.

Two files:
  - ledger.json : the budget ledger (see Ledger below)
  - issues.json : per-issue metadata the labels can't hold
                  (branch name, PR number, review round count, paused stage,
                  last-seen comment id for the clarify loop)

Thread-safe: per-path locks protect whole read-modify-write cycles under
concurrency (Level 1 — multiple issues processed at once).
"""
import json
import os
import threading
import time
from . import config

os.makedirs(config.DATA_DIR, exist_ok=True)
LEDGER_PATH = os.path.join(config.DATA_DIR, "ledger.json")
ISSUES_PATH = os.path.join(config.DATA_DIR, "issues.json")

_FIVE_H = 5 * 3600
_WEEK = 7 * 24 * 3600

# ── Per-path lock registry ────────────────────────────────────────────────────
_path_locks = {}
_path_locks_lock = threading.Lock()


def _lock_for(path):
    """Get or create a lock for this specific path (identity-based)."""
    with _path_locks_lock:
        if path not in _path_locks:
            _path_locks[path] = threading.Lock()
        return _path_locks[path]


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
def issue_meta(key):
    """Get metadata for an issue by its namespaced key (e.g. 'owner/repo#5')."""
    lock = _lock_for(ISSUES_PATH)
    with lock:
        data = _load(ISSUES_PATH, {})
        return data.get(key, {})


def update_issue_meta(key, **fields):
    """Update metadata for an issue by its namespaced key (e.g. 'owner/repo#5')."""
    lock = _lock_for(ISSUES_PATH)
    with lock:
        data = _load(ISSUES_PATH, {})
        m = data.get(key, {})
        m.update(fields)
        data[key] = m
        _save(ISSUES_PATH, data)
        return m


def migrate_legacy_keys(repo_slug):
    """One-time migration: convert bare integer keys to namespaced keys.

    Called at startup to prevent data loss when upgrading from single-repo
    to multi-repo deployments. Keys like '5' become 'owner/repo#5'.
    Keys already containing '#' are left untouched (idempotent).
    If repo_slug is None and bare keys exist, logs a warning and leaves them.

    Args:
        repo_slug: The repo slug to attribute legacy keys to (e.g. 'owner/repo').
                   If None, bare keys are left in place with a warning.
    """
    import re
    lock = _lock_for(ISSUES_PATH)
    with lock:
        data = _load(ISSUES_PATH, {})
        migrated = []
        skipped = []
        for key in list(data.keys()):
            if "#" in key:
                # Already namespaced — skip
                skipped.append(key)
                continue
            if not re.match(r"^\d+$", key):
                # Not a bare integer — skip
                skipped.append(key)
                continue
            # Bare integer key — migrate
            if repo_slug is None:
                import logging
                logging.warning(
                    f"Cannot migrate legacy bare key '{key}' without repo_slug. "
                    f"Set GH_REPO to enable migration. Leaving key untouched."
                )
                continue
            new_key = f"{repo_slug}#{key}"
            value = data[key]
            data[new_key] = value
            del data[key]
            migrated.append((key, new_key))
        if migrated:
            _save(ISSUES_PATH, data)
        return migrated, skipped


# ── Budget ledger ─────────────────────────────────────────────────────────
class Ledger:
    """Tracks two pools against rolling 5h + weekly windows.

    Claude pool is token-counted (we read exact usage from Claude Code's JSON
    output per call). GLM pool is prompt-counted (z.ai is prompt-quota'd), with
    a safety multiplier applied per call.

    Thread-safe: internal lock protects _roll/_save/record/headroom/reserve/
    reconcile/next_reset/snapshot. Under concurrency, one shared Ledger is
    created in main() and passed to every process_issue call.
    """

    def __init__(self):
        self.d = _load(LEDGER_PATH, {})
        for pool in ("claude", "glm"):
            self.d.setdefault(pool, {
                "w5": {"start": time.time(), "tokens": 0, "prompts": 0},
                "wk": {"start": time.time(), "tokens": 0, "prompts": 0},
            })
        self._lock = threading.RLock()
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
        with _lock_for(LEDGER_PATH):
            _save(LEDGER_PATH, self.d)

    def record(self, pool, tokens=0, prompts=0):
        with self._lock:
            p = self.d[pool]
            for w in ("w5", "wk"):
                p[w]["tokens"] += tokens
                p[w]["prompts"] += prompts
            self._save()

    def cool_down(self, pool, seconds):
        """Back a pool off after a real provider rate-limit. Independent of the
        local window counters (which can't see the provider's own quota), so the
        router stops re-picking the family and the blocked:budget gate stops
        un-parking until the cooldown expires."""
        with self._lock:
            self.d[pool]["cooldown_until"] = time.time() + seconds
            self._save()

    def cooling(self, pool):
        """True while a pool is backed off from a provider rate-limit."""
        return time.time() < self.d.get(pool, {}).get("cooldown_until", 0)

    def _has_headroom(self, pool):
        """Internal: check headroom assuming the lock is held. False while a pool
        is cooling off from a provider rate-limit."""
        if self.cooling(pool):
            return False
        p = self.d[pool]
        if pool == "claude":
            # Per-window ceilings: hotter on the fast-refilling 5h window, more
            # protective on the weekly cap.
            return (p["w5"]["tokens"] < config.CLAUDE_5H_TOKEN_BUDGET * config.CLAUDE_5H_SAFETY_FRACTION and
                    p["wk"]["tokens"] < config.CLAUDE_WEEK_TOKEN_BUDGET * config.CLAUDE_WEEK_SAFETY_FRACTION)
        f = config.BUDGET_SAFETY_FRACTION
        lim = config.GLM_TIER_LIMITS[config.GLM_TIER]
        return (p["w5"]["prompts"] < lim["per5h"] * f and
                p["wk"]["prompts"] < lim["perweek"] * f)

    def headroom(self, pool):
        """True if this pool has room for another step in both windows (and isn't
        cooling off from a provider rate-limit)."""
        with self._lock:
            self._roll()
            return self._has_headroom(pool)

    def reserve(self, pool, tokens=0, prompts=0):
        """Atomically check-and-charge estimated budget before a run.

        Returns True if the pool had headroom and the estimate was charged;
        False if the pool lacked headroom (nothing is charged). This is the
        atomic gate that prevents N concurrent runners from all passing headroom()
        and then collectively busting the window.
        """
        with self._lock:
            self._roll()
            if not self._has_headroom(pool):
                return False
            p = self.d[pool]
            for w in ("w5", "wk"):
                p[w]["tokens"] += tokens
                p[w]["prompts"] += prompts
            self._save()
            return True

    def reconcile(self, pool, reserved_tokens=0, reserved_prompts=0,
                  actual_tokens=0, actual_prompts=0):
        """Adjust both windows from the estimated charge to the actual usage.

        Called after a run completes. The pool lands at its true usage
        (over-estimate -> refund, under-estimate -> top-up).
        """
        with self._lock:
            p = self.d[pool]
            for w in ("w5", "wk"):
                p[w]["tokens"] += actual_tokens - reserved_tokens
                p[w]["prompts"] += actual_prompts - reserved_prompts
            self._save()

    def next_reset(self):
        """Earliest time any window frees up — for the blocked:budget retry."""
        with self._lock:
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
        with self._lock:
            self._roll()
            lim = config.GLM_TIER_LIMITS[config.GLM_TIER]
            return {
                "claude_5h_tokens": self.d["claude"]["w5"]["tokens"],
                "claude_5h_budget": config.CLAUDE_5H_TOKEN_BUDGET,
                "glm_5h_prompts": self.d["glm"]["w5"]["prompts"],
                "glm_5h_limit": lim["per5h"],
            }
