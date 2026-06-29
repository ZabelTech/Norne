"""Budget ledger + per-issue metadata store.

These exercise the heart of the soft budget gate: how usage is tallied across
the rolling 5h/weekly windows, when a pool runs out of headroom, when windows
roll over, and the blocked:budget retry hint.

Includes concurrency tests: store operations and ledger accounting are
correct under concurrent access.
"""
import threading
import time
from orchestrator import config, store

_FIVE_H = 5 * 3600
_WEEK = 7 * 24 * 3600


def test_fresh_ledger_has_full_headroom(fresh_ledger):
    assert fresh_ledger.headroom("claude") is True
    assert fresh_ledger.headroom("glm") is True


def test_record_claude_tokens_accumulate_in_both_windows(fresh_ledger):
    fresh_ledger.record("claude", tokens=100)
    fresh_ledger.record("claude", tokens=50)
    assert fresh_ledger.d["claude"]["w5"]["tokens"] == 150
    assert fresh_ledger.d["claude"]["wk"]["tokens"] == 150


def test_record_glm_prompts_accumulate(fresh_ledger):
    fresh_ledger.record("glm", prompts=3)
    assert fresh_ledger.d["glm"]["w5"]["prompts"] == 3
    assert fresh_ledger.d["glm"]["wk"]["prompts"] == 3


def test_claude_5h_window_respects_its_safety_fraction(fresh_ledger):
    # Budget 1000 * CLAUDE_5H_SAFETY_FRACTION 0.90 = 900 soft 5h ceiling.
    fresh_ledger.record("claude", tokens=899)
    assert fresh_ledger.headroom("claude") is True
    fresh_ledger.record("claude", tokens=1)  # now at 900, not < 900
    assert fresh_ledger.headroom("claude") is False


def test_claude_weekly_window_uses_its_own_lower_fraction(fresh_ledger):
    # Weekly cap is protected harder: 10000 * CLAUDE_WEEK_SAFETY_FRACTION 0.80 = 8000.
    fresh_ledger.d["claude"]["wk"]["tokens"] = 7999
    fresh_ledger._save()
    assert fresh_ledger.headroom("claude") is True       # under the weekly ceiling
    fresh_ledger.d["claude"]["wk"]["tokens"] = 8000      # at the ceiling -> blocked
    fresh_ledger._save()
    assert fresh_ledger.headroom("claude") is False


def test_glm_headroom_uses_tier_limits(fresh_ledger):
    lim = config.GLM_TIER_LIMITS["lite"]["per5h"]  # 80
    ceiling = int(lim * config.BUDGET_SAFETY_FRACTION)  # 68
    fresh_ledger.record("glm", prompts=ceiling - 1)
    assert fresh_ledger.headroom("glm") is True
    fresh_ledger.record("glm", prompts=5)
    assert fresh_ledger.headroom("glm") is False


def test_five_hour_window_rolls_over(fresh_ledger, clock):
    fresh_ledger.record("claude", tokens=900)
    assert fresh_ledger.headroom("claude") is False
    clock.advance(_FIVE_H + 1)
    # _roll() runs inside headroom(); the 5h bucket should reset.
    assert fresh_ledger.headroom("claude") is True
    assert fresh_ledger.d["claude"]["w5"]["tokens"] == 0


def test_weekly_window_survives_a_five_hour_roll(fresh_ledger, clock):
    fresh_ledger.record("claude", tokens=400)
    clock.advance(_FIVE_H + 1)
    fresh_ledger.headroom("claude")  # triggers the 5h roll only
    assert fresh_ledger.d["claude"]["w5"]["tokens"] == 0
    assert fresh_ledger.d["claude"]["wk"]["tokens"] == 400  # weekly persists


def test_cool_down_blocks_headroom_until_it_expires(fresh_ledger, clock):
    assert fresh_ledger.headroom("glm") is True
    fresh_ledger.cool_down("glm", 600)
    assert fresh_ledger.cooling("glm") is True
    assert fresh_ledger.headroom("glm") is False         # backed off despite empty counters
    assert fresh_ledger.headroom("claude") is True       # other pool unaffected
    clock.advance(601)
    assert fresh_ledger.cooling("glm") is False
    assert fresh_ledger.headroom("glm") is True          # auto-recovers after the window


def test_cool_down_persists_across_ledger_instances(fresh_ledger, clock, ledger_paths):
    fresh_ledger.cool_down("glm", 600)
    # A fresh Ledger view (as the worker creates per issue) still sees the cooldown.
    assert store.Ledger().headroom("glm") is False


def test_next_reset_points_at_the_soonest_window(fresh_ledger, clock):
    nxt = fresh_ledger.next_reset()
    # The 5h window opened at clock start (== now), so it frees up first.
    assert nxt == clock.now + _FIVE_H
    assert nxt < clock.now + _WEEK


def test_snapshot_reports_current_usage_and_limits(fresh_ledger):
    fresh_ledger.record("claude", tokens=120)
    fresh_ledger.record("glm", prompts=4)
    snap = fresh_ledger.snapshot()
    assert snap["claude_5h_tokens"] == 120
    assert snap["claude_5h_budget"] == config.CLAUDE_5H_TOKEN_BUDGET
    assert snap["glm_5h_prompts"] == 4
    assert snap["glm_5h_limit"] == config.GLM_TIER_LIMITS["lite"]["per5h"]


def test_ledger_persists_across_instances(clock, ledger_paths):
    l1 = store.Ledger()
    l1.record("claude", tokens=222)
    l2 = store.Ledger()  # re-reads the same file
    assert l2.d["claude"]["w5"]["tokens"] == 222


def test_issue_meta_round_trips(ledger_paths):
    key = "owner/repo#42"
    assert store.issue_meta(key) == {}
    store.update_issue_meta(key, branch="pipeline/issue-42", review_round=0)
    m = store.issue_meta(key)
    assert m["branch"] == "pipeline/issue-42"
    assert m["review_round"] == 0


def test_update_issue_meta_merges_fields(ledger_paths):
    key = "owner/repo#7"
    store.update_issue_meta(key, branch="b", review_round=0)
    store.update_issue_meta(key, review_round=2, pr_number=99)
    m = store.issue_meta(key)
    assert m == {"branch": "b", "review_round": 2, "pr_number": 99}


def test_issue_meta_is_keyed_per_issue(ledger_paths):
    store.update_issue_meta("owner/repo#1", branch="one")
    store.update_issue_meta("owner/repo#2", branch="two")
    assert store.issue_meta("owner/repo#1")["branch"] == "one"
    assert store.issue_meta("owner/repo#2")["branch"] == "two"


# ── Concurrency tests ────────────────────────────────────────────────────────
def test_concurrent_issue_meta_updates_dont_clobber(ledger_paths):
    """Many threads updating distinct fields on the same issue — all writes persist."""
    key = "owner/repo#42"
    barriers = [threading.Barrier(3), threading.Barrier(3)]

    def worker(field, value):
        barriers[0].wait()                     # all threads arrive together
        store.update_issue_meta(key, **{field: value})
        barriers[1].wait()                     # wait for all to finish

    threads = [
        threading.Thread(target=worker, args=("a", 1)),
        threading.Thread(target=worker, args=("b", 2)),
        threading.Thread(target=worker, args=("c", 3)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    m = store.issue_meta(key)
    assert m == {"a": 1, "b": 2, "c": 3}       # no lost updates


def test_concurrent_ledger_record_produces_exact_tallies(ledger_paths, clock):
    """Many threads calling record() — final totals are exact."""
    fresh_ledger = store.Ledger()
    barriers = [threading.Barrier(5), threading.Barrier(5)]

    def worker(pool, tokens, prompts):
        barriers[0].wait()
        fresh_ledger.record(pool, tokens=tokens, prompts=prompts)
        barriers[1].wait()

    threads = [
        threading.Thread(target=worker, args=("claude", 100, 0)),
        threading.Thread(target=worker, args=("claude", 50, 0)),
        threading.Thread(target=worker, args=("glm", 0, 3)),
        threading.Thread(target=worker, args=("glm", 0, 2)),
        threading.Thread(target=worker, args=("claude", 10, 0)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert fresh_ledger.d["claude"]["w5"]["tokens"] == 160
    assert fresh_ledger.d["glm"]["w5"]["prompts"] == 5


def test_reserve_returns_false_when_pool_lacks_headroom(ledger_paths, clock):
    """Reserve when a pool is at ceiling returns False and charges nothing."""
    fresh_ledger = store.Ledger()
    # Exhaust the claude pool (5h ceiling = 1000 * CLAUDE_5H_SAFETY_FRACTION 0.90).
    fresh_ledger.record("claude", tokens=900)  # at ceiling

    assert fresh_ledger.headroom("claude") is False
    assert fresh_ledger.reserve("claude", tokens=100) is False
    # Still at 900 — nothing charged.
    assert fresh_ledger.d["claude"]["w5"]["tokens"] == 900


def test_reserve_returns_true_and_precharges_when_pool_has_headroom(ledger_paths):
    """Reserve when a pool has room returns True and charges the estimate."""
    fresh_ledger = store.Ledger()

    assert fresh_ledger.headroom("claude") is True
    assert fresh_ledger.reserve("claude", tokens=100) is True
    # Estimate charged to both windows.
    assert fresh_ledger.d["claude"]["w5"]["tokens"] == 100
    assert fresh_ledger.d["claude"]["wk"]["tokens"] == 100


def test_reconcile_corrects_overestimate(ledger_paths):
    """Reconcile with actual < reserved refunds the difference."""
    fresh_ledger = store.Ledger()
    fresh_ledger.reserve("claude", tokens=200)
    assert fresh_ledger.d["claude"]["w5"]["tokens"] == 200

    # Actual usage was only 150.
    fresh_ledger.reconcile("claude", reserved_tokens=200, actual_tokens=150)
    assert fresh_ledger.d["claude"]["w5"]["tokens"] == 150
    assert fresh_ledger.d["claude"]["wk"]["tokens"] == 150


def test_reconcile_corrects_underestimate(ledger_paths):
    """Reconcile with actual > reserved tops up the difference."""
    fresh_ledger = store.Ledger()
    fresh_ledger.reserve("claude", tokens=100)
    assert fresh_ledger.d["claude"]["w5"]["tokens"] == 100

    # Actual usage was 120.
    fresh_ledger.reconcile("claude", reserved_tokens=100, actual_tokens=120)
    assert fresh_ledger.d["claude"]["w5"]["tokens"] == 120
    assert fresh_ledger.d["claude"]["wk"]["tokens"] == 120


def test_concurrent_reserve_reconcile_produces_correct_final_tally(ledger_paths):
    """Threads reserve/reconcile in parallel — final accounting is exact."""
    fresh_ledger = store.Ledger()
    barrier = threading.Barrier(4)

    def worker(reserved, actual):
        barrier.wait()
        if fresh_ledger.reserve("claude", tokens=reserved):
            fresh_ledger.reconcile("claude", reserved_tokens=reserved, actual_tokens=actual)

    threads = [
        threading.Thread(target=worker, args=(200, 180)),
        threading.Thread(target=worker, args=(150, 150)),
        threading.Thread(target=worker, args=(300, 320)),
        threading.Thread(target=worker, args=(100, 90)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Actuals: 180 + 150 + 320 + 90 = 740
    assert fresh_ledger.d["claude"]["w5"]["tokens"] == 740
    assert fresh_ledger.d["claude"]["wk"]["tokens"] == 740


# ── Legacy key migration (multi-repo namespacing) ────────────────────────────────
def test_migrate_legacy_keys_converts_bare_integers(ledger_paths):
    """Bare integer keys become 'owner/repo#n' under the target slug."""
    # Seed with realistic in-flight meta like a pre-upgrade deployment would have.
    meta = {
        "spec_units": [{"slug": "s1", "stage": "implement", "pr_number": None}],
        "review_round": 0,
        "human_guidance": None,
    }
    store.update_issue_meta("5", **meta)

    migrated, skipped = store.migrate_legacy_keys("owner/repo")
    assert len(migrated) == 1
    assert migrated[0] == ("5", "owner/repo#5")
    assert store.issue_meta("owner/repo#5") == meta
    # Old key gone.
    assert store.issue_meta("5") == {}


def test_migrate_legacy_keys_is_idempotent(ledger_paths):
    """A second migration run makes no changes."""
    store.update_issue_meta("3", branch="pipeline/issue-3")
    store.migrate_legacy_keys("owner/repo")

    # Second run: everything already namespaced → no changes.
    migrated2, skipped2 = store.migrate_legacy_keys("owner/repo")
    assert migrated2 == []
    # Namespaced key still intact.
    assert store.issue_meta("owner/repo#3")["branch"] == "pipeline/issue-3"


def test_migrate_legacy_keys_leaves_namespaced_keys_untouched(ledger_paths):
    """Keys already containing '#' are never modified."""
    store.update_issue_meta("owner/repo#10", branch="b/10")
    store.update_issue_meta("other/repo#10", branch="b/other")

    migrated, _ = store.migrate_legacy_keys("owner/repo")
    assert migrated == []   # nothing to migrate — already namespaced
    assert store.issue_meta("owner/repo#10")["branch"] == "b/10"
    assert store.issue_meta("other/repo#10")["branch"] == "b/other"


def test_migrate_legacy_keys_without_slug_warns_and_leaves_bare_keys(ledger_paths, caplog):
    """When repo_slug is None, bare keys are left and a single consolidated warning is logged."""
    import logging
    store.update_issue_meta("7", branch="b")
    store.update_issue_meta("9", branch="c")

    with caplog.at_level(logging.WARNING, logger="root"):
        migrated, _ = store.migrate_legacy_keys(None)

    assert migrated == []
    assert store.issue_meta("7") == {"branch": "b"}   # untouched
    assert store.issue_meta("9") == {"branch": "c"}   # untouched
    # Single warning listing all affected keys (not one per key)
    warn_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warn_records) == 1
    assert "7" in warn_records[0].message
    assert "9" in warn_records[0].message


def test_migrate_two_bare_keys_both_converted(ledger_paths):
    """Multiple bare integer keys are all migrated in one pass."""
    store.update_issue_meta("1", spec_units=[])
    store.update_issue_meta("2", review_round=1)

    migrated, _ = store.migrate_legacy_keys("org/r")
    assert len(migrated) == 2
    keys_after = {m[1] for m in migrated}
    assert "org/r#1" in keys_after
    assert "org/r#2" in keys_after
    assert store.issue_meta("org/r#1") == {"spec_units": []}
    assert store.issue_meta("org/r#2") == {"review_round": 1}


def test_namespaced_keys_with_same_number_dont_clobber_each_other(ledger_paths):
    """Two repos with issue #5 keep entirely separate store entries."""
    store.update_issue_meta("org/repo-a#5", branch="b/a")
    store.update_issue_meta("org/repo-b#5", branch="b/b")
    assert store.issue_meta("org/repo-a#5")["branch"] == "b/a"
    assert store.issue_meta("org/repo-b#5")["branch"] == "b/b"
