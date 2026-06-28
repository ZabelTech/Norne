"""Config parsing + routing-table invariants."""
import importlib

from orchestrator import config


def test_bool_parser_truthy(monkeypatch):
    for val in ("1", "true", "TRUE", "Yes", " yes "):
        monkeypatch.setenv("SOME_FLAG", val)
        assert config._b("SOME_FLAG") is True


def test_bool_parser_falsy(monkeypatch):
    for val in ("0", "false", "no", "", "nope"):
        monkeypatch.setenv("SOME_FLAG", val)
        assert config._b("SOME_FLAG") is False


def test_bool_parser_default_when_unset(monkeypatch):
    monkeypatch.delenv("MISSING_FLAG", raising=False)
    assert config._b("MISSING_FLAG") is False
    assert config._b("MISSING_FLAG", default="true") is True


def test_all_flow_contains_every_flow_label():
    expected = {
        config.FLOW_SUMMARIZE, config.FLOW_CLARIFY, config.FLOW_APPROVAL,
        config.FLOW_SPEC, config.FLOW_IMPLEMENT, config.FLOW_REVIEW,
        config.FLOW_MERGE, config.FLOW_DONE,
    }
    assert config.ALL_FLOW == expected


def test_flow_labels_are_unique():
    assert len(config.ALL_FLOW) == 8


def test_routing_covers_every_stage():
    # Every stage the dispatcher meters must have a routing entry.
    for stage in ("summarize", "spec", "implement", "review"):
        assert stage in config.ROUTING
        assert config.ROUTING[stage], f"{stage} has no families"


def test_routing_families_are_known():
    known = {"claude", "glm"}
    for stage, fams in config.ROUTING.items():
        assert set(fams) <= known, f"{stage} routes to unknown family"


def test_review_can_pick_a_partner_after_excluding_implementer():
    # Diversity-of-thought: review must still have a family left once the
    # implementer's family is excluded — i.e. it lists both families.
    assert set(config.ROUTING["review"]) == {"claude", "glm"}


def test_implement_prefers_glm_for_subscription_maxing():
    # Bulk implement should reach for cheap GLM quota first.
    assert config.ROUTING["implement"][0] == "glm"


def test_spec_prefers_claude():
    assert config.ROUTING["spec"][0] == "claude"


def test_glm_model_map_covers_every_stage():
    assert set(config.GLM_MODEL_BY_STAGE) == set(config.ROUTING)


def test_family_available_tracks_credential_presence(monkeypatch):
    monkeypatch.setattr(config, "CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-x")
    monkeypatch.setattr(config, "ZAI_AUTH_TOKEN", "zai-x")
    assert config.family_available("claude") is True
    assert config.family_available("glm") is True

    monkeypatch.setattr(config, "CLAUDE_CODE_OAUTH_TOKEN", "")
    assert config.family_available("claude") is False
    assert config.family_available("glm") is True  # GLM still configured

    monkeypatch.setattr(config, "ZAI_AUTH_TOKEN", "")
    assert config.family_available("glm") is False


def test_family_available_unknown_family_is_false():
    assert config.family_available("gpt") is False


def test_glm_tier_limits_have_all_tiers():
    for tier in ("lite", "pro", "max"):
        lim = config.GLM_TIER_LIMITS[tier]
        assert lim["per5h"] > 0 and lim["perweek"] > 0


def test_env_overrides_apply_on_reimport(monkeypatch):
    monkeypatch.setenv("MAX_REVIEW_ROUNDS", "7")
    monkeypatch.setenv("AUTO_MERGE", "true")
    reloaded = importlib.reload(config)
    try:
        assert reloaded.MAX_REVIEW_ROUNDS == 7
        assert reloaded.AUTO_MERGE is True
    finally:
        # Restore module-level state for other tests.
        monkeypatch.undo()
        importlib.reload(config)
