"""Family selection under the budget gate."""
from orchestrator import config, router


class FakeLedger:
    """Reports headroom from a fixed map of family -> bool."""

    def __init__(self, headroom_map):
        self._h = headroom_map

    def headroom(self, family):
        return self._h[family]


def test_picks_first_family_with_headroom():
    led = FakeLedger({"claude": True, "glm": True})
    # spec prefers claude
    assert router.choose_family("spec", led) == "claude"
    # implement prefers glm
    assert router.choose_family("implement", led) == "glm"


def test_fails_over_when_preferred_pool_is_tapped():
    led = FakeLedger({"claude": False, "glm": True})
    assert router.choose_family("spec", led) == "glm"

    led = FakeLedger({"claude": True, "glm": False})
    assert router.choose_family("implement", led) == "claude"


def test_returns_none_when_no_pool_has_headroom():
    led = FakeLedger({"claude": False, "glm": False})
    assert router.choose_family("review", led) is None


def test_exclude_family_enforces_a_different_reviewer():
    led = FakeLedger({"claude": True, "glm": True})
    # review prefers claude, but if claude implemented, pick the other family.
    assert router.choose_family("review", led, exclude_family="claude") == "glm"
    assert router.choose_family("review", led, exclude_family="glm") == "claude"


def test_exclude_family_returns_none_if_only_excluded_has_headroom():
    # The implementer's family is the only one with budget -> cannot cross-check.
    led = FakeLedger({"claude": True, "glm": False})
    assert router.choose_family("review", led, exclude_family="claude") is None


def test_exclude_family_does_not_block_unrelated_choice():
    led = FakeLedger({"claude": True, "glm": True})
    # Excluding glm from a claude-first stage still yields claude.
    assert router.choose_family("spec", led, exclude_family="glm") == "claude"


# ── single-family ("no-Claude" / GLM-only) deployment ───────────────────────
def test_unconfigured_family_is_skipped(monkeypatch):
    # No Claude token -> Claude is never eligible even with budget headroom;
    # every stage falls through to GLM.
    monkeypatch.setattr(config, "CLAUDE_CODE_OAUTH_TOKEN", "")
    led = FakeLedger({"claude": True, "glm": True})
    assert router.choose_family("summarize", led) == "glm"
    assert router.choose_family("spec", led) == "glm"
    assert router.choose_family("implement", led) == "glm"


def test_review_degrades_to_same_family_when_only_one_configured(monkeypatch):
    # Claude-less box: GLM implemented, so review would exclude GLM and find no
    # other configured family. Rather than deadlock, allow a same-family review.
    monkeypatch.setattr(config, "CLAUDE_CODE_OAUTH_TOKEN", "")
    led = FakeLedger({"claude": True, "glm": True})
    assert router.choose_family("review", led, exclude_family="glm") == "glm"


def test_review_does_not_degrade_when_other_family_only_lacks_budget():
    # Both families configured but the partner is out of budget -> we must NOT
    # silently fall back to same-family review; park as blocked:budget instead.
    led = FakeLedger({"claude": False, "glm": True})
    assert router.choose_family("review", led, exclude_family="glm") is None


def test_no_family_configured_returns_none(monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_CODE_OAUTH_TOKEN", "")
    monkeypatch.setattr(config, "ZAI_AUTH_TOKEN", "")
    led = FakeLedger({"claude": True, "glm": True})
    assert router.choose_family("spec", led) is None
    assert router.choose_family("review", led, exclude_family="glm") is None
