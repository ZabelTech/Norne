"""Budget ledger + per-issue metadata store.

These exercise the heart of the soft budget gate: how usage is tallied across
the rolling 5h/weekly windows, when a pool runs out of headroom, when windows
roll over, and the blocked:budget retry hint.
"""
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


def test_claude_headroom_respects_safety_fraction(fresh_ledger):
    # Budget 1000 * safety 0.85 = 850 soft ceiling for the 5h window.
    fresh_ledger.record("claude", tokens=849)
    assert fresh_ledger.headroom("claude") is True
    fresh_ledger.record("claude", tokens=1)  # now at 850, not < 850
    assert fresh_ledger.headroom("claude") is False


def test_claude_weekly_window_can_block_independently(fresh_ledger):
    # Spend under the 5h ceiling but over the weekly one by faking the weekly tally.
    fresh_ledger.d["claude"]["wk"]["tokens"] = 9000  # > 10000 * 0.85
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
    assert store.issue_meta(42) == {}
    store.update_issue_meta(42, branch="pipeline/issue-42", review_round=0)
    m = store.issue_meta(42)
    assert m["branch"] == "pipeline/issue-42"
    assert m["review_round"] == 0


def test_update_issue_meta_merges_fields(ledger_paths):
    store.update_issue_meta(7, branch="b", review_round=0)
    store.update_issue_meta(7, review_round=2, pr_number=99)
    m = store.issue_meta(7)
    assert m == {"branch": "b", "review_round": 2, "pr_number": 99}


def test_issue_meta_is_keyed_per_issue(ledger_paths):
    store.update_issue_meta(1, branch="one")
    store.update_issue_meta(2, branch="two")
    assert store.issue_meta(1)["branch"] == "one"
    assert store.issue_meta(2)["branch"] == "two"
