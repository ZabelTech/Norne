"""Prompt template rendering — token substitution must not disturb the JSON
contract blocks the templates carry."""
from orchestrator import prompts


def test_render_substitutes_tokens():
    out = prompts.render("Hello <<<NAME>>>", NAME="world")
    assert out == "Hello world"


def test_render_replaces_all_occurrences():
    out = prompts.render("<<<X>>> and <<<X>>>", X="z")
    assert out == "z and z"


def test_render_coerces_non_strings():
    out = prompts.render("issue #<<<NUM>>>", NUM=42)
    assert out == "issue #42"


def test_render_leaves_unknown_tokens_untouched():
    out = prompts.render("<<<A>>> <<<B>>>", A="x")
    assert out == "x <<<B>>>"


def test_render_does_not_touch_json_braces():
    # The literal JSON schema block must survive rendering intact.
    out = prompts.render(prompts.SUMMARIZE, NUM=1, TITLE="t", BODY="b",
                         CLARIFICATIONS="(none)")
    assert '{"status": "clarify" | "ready",' in out
    assert "<<<" not in out  # all placeholders consumed


def test_summarize_template_fills_every_placeholder():
    out = prompts.render(prompts.SUMMARIZE, NUM=5, TITLE="Add feature",
                         BODY="please", CLARIFICATIONS="(none)")
    assert "ISSUE #5: Add feature" in out
    assert "please" in out


def test_implement_template_references_spec_path():
    out = prompts.render(prompts.IMPLEMENT, NUM=12)
    assert "specs/12/" in out
    assert "<<<" not in out


def test_fix_template_includes_round_and_feedback():
    out = prompts.render(prompts.FIX, NUM=3, ROUND=2, FEEDBACK="fix the bug")
    assert "round 2" in out
    assert "fix the bug" in out


def test_review_template_carries_diff_and_specs():
    out = prompts.render(prompts.REVIEW, NUM=9, TITLE="T", SUMMARY="S",
                         SPECS="SPECTEXT", DIFF="DIFFTEXT")
    assert "SPECTEXT" in out
    assert "DIFFTEXT" in out
    assert "<<<" not in out


def test_every_stage_template_ends_with_a_json_contract():
    for tpl in (prompts.SUMMARIZE, prompts.SPEC, prompts.SPEC_REVIEW,
                prompts.IMPLEMENT, prompts.FIX, prompts.REVIEW):
        assert "```json" in tpl
