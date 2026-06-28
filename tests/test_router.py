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
