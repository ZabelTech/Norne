"""Config parsing + routing-table invariants."""
import importlib
import pytest

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


def test_claude_model_map_covers_every_stage():
    assert set(config.CLAUDE_MODEL_BY_STAGE) == set(config.ROUTING)


def test_review_uses_each_familys_best_model():
    # Review must always run the strongest model of whichever family does it
    # (incl. a budget-forced same-family review).
    assert config.CLAUDE_MODEL_BY_STAGE["review"] == "claude-opus-4-8"
    assert config.GLM_MODEL_BY_STAGE["review"] == "glm-5.2"


def test_opus_and_glm_run_at_medium_effort():
    assert config.effort_for("claude-opus-4-8") == "medium"
    assert config.effort_for("glm-5.2") == "medium"
    assert config.effort_for("glm-4.7") == "medium"


def test_effort_for_defaults_to_medium_on_unknown_model():
    assert config.effort_for("some-future-model") == "medium"


def test_spec_round_cap_is_a_positive_int():
    assert isinstance(config.MAX_SPEC_ROUNDS, int) and config.MAX_SPEC_ROUNDS >= 1


def test_claude_has_split_per_window_safety_fractions():
    # 90% on the fast 5h window, 80% on the protective weekly cap.
    assert config.CLAUDE_5H_SAFETY_FRACTION == 0.90
    assert config.CLAUDE_WEEK_SAFETY_FRACTION == 0.80
    assert config.CLAUDE_WEEK_SAFETY_FRACTION < config.CLAUDE_5H_SAFETY_FRACTION


def test_cache_reads_are_weighted_below_full():
    # Cache reads must be discounted (the over-count fix), not counted at 1x.
    assert 0 <= config.CLAUDE_CACHE_READ_WEIGHT < 1


def test_every_models_effort_is_tunable():
    # Each per-model effort label must resolve to a real tuning entry.
    for effort in config.EFFORT_BY_MODEL.values():
        assert effort in config.EFFORT_TUNING


def test_effort_covers_every_routed_model():
    # Every model the router can pick must have an effort entry (else it would
    # silently default; an explicit entry keeps the marker honest).
    routed = set(config.GLM_MODEL_BY_STAGE.values()) | set(config.CLAUDE_MODEL_BY_STAGE.values())
    assert routed <= set(config.EFFORT_BY_MODEL)


def test_effort_tuning_entries_well_formed():
    for label, t in config.EFFORT_TUNING.items():
        assert "directive" in t and "max_turns" in t
        assert isinstance(t["max_turns"], int) and t["max_turns"] > 0


def test_effort_tuning_defaults_to_medium_on_unknown():
    assert config.effort_tuning("nope") is config.EFFORT_TUNING["medium"]


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


# ── Multi-repo configuration validation ────────────────────────────────────────

def test_validate_config_raises_when_neither_owner_nor_repo_set(monkeypatch):
    monkeypatch.setattr(config, "GH_OWNER", None)
    monkeypatch.setattr(config, "GH_REPO", None)
    with pytest.raises(RuntimeError, match="GH_OWNER"):
        config.validate_config()


def test_validate_config_passes_with_gh_owner_only(monkeypatch):
    monkeypatch.setattr(config, "GH_OWNER", "ZabelTech")
    monkeypatch.setattr(config, "GH_REPO", None)
    config.validate_config()   # must not raise


def test_validate_config_passes_with_gh_repo_only(monkeypatch):
    monkeypatch.setattr(config, "GH_OWNER", None)
    monkeypatch.setattr(config, "GH_REPO", "owner/repo")
    config.validate_config()   # must not raise


def test_validate_config_passes_with_both_set(monkeypatch):
    monkeypatch.setattr(config, "GH_OWNER", "ZabelTech")
    monkeypatch.setattr(config, "GH_REPO", "owner/repo")
    config.validate_config()   # must not raise


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
