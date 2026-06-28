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
    out = prompts.render(prompts.IMPLEMENT, NUM=12, DISCUSSION="(discussion)")
    assert "specs/12/" in out
    assert "<<<" not in out


def test_fix_template_includes_round_and_feedback():
    out = prompts.render(prompts.FIX, NUM=3, ROUND=2, FEEDBACK="fix the bug",
                         DISCUSSION="(d)")
    assert "round 2" in out
    assert "fix the bug" in out
    assert "<<<" not in out


def test_review_template_carries_specs_branches_and_discussion():
    out = prompts.render(prompts.REVIEW, NUM=9, TITLE="T", SUMMARY="S",
                         SPECS="SPECTEXT", BRANCH="pipeline/issue-9", BASE="main",
                         DISCUSSION="DISCTEXT")
    assert "SPECTEXT" in out
    assert "pipeline/issue-9" in out                 # PR branch named
    assert "git diff main...pipeline/issue-9" in out  # self-diff instruction wired
    assert "DISCTEXT" in out
    assert "<<<" not in out


def test_context_carrying_templates_have_a_discussion_slot():
    # Every stage that acts on an issue/PR must thread the full discussion in.
    for tpl in (prompts.SPEC, prompts.SPEC_REVIEW, prompts.IMPLEMENT,
                prompts.FIX, prompts.REVIEW):
        assert "<<<DISCUSSION>>>" in tpl


def test_spec_template_has_feedback_slot_for_the_loop():
    assert "<<<FEEDBACK>>>" in prompts.SPEC
    out = prompts.render(prompts.SPEC, NUM=1, TITLE="T", SUMMARY="S",
                         DISCUSSION="D", FEEDBACK="(none)")
    assert "<<<" not in out


def test_spec_template_has_a_responses_field_for_rebuttals():
    assert '"responses"' in prompts.SPEC


def test_spec_review_weighs_author_responses():
    assert "<<<AUTHOR_RESPONSES>>>" in prompts.SPEC_REVIEW
    out = prompts.render(prompts.SPEC_REVIEW, SPECS="S",
                         AUTHOR_RESPONSES="RTEXT", DISCUSSION="D")
    assert "RTEXT" in out and "<<<" not in out


def test_every_stage_template_ends_with_a_json_contract():
    for tpl in (prompts.SUMMARIZE, prompts.SPEC, prompts.SPEC_REVIEW,
                prompts.IMPLEMENT, prompts.FIX, prompts.REVIEW):
        assert "```json" in tpl


def test_every_template_has_exactly_one_instructions_placeholder():
    """Each template contains exactly one <<<INSTRUCTIONS>>> placeholder."""
    for tpl in (prompts.SUMMARIZE, prompts.SPEC, prompts.SPEC_REVIEW,
                prompts.IMPLEMENT, prompts.FIX, prompts.REVIEW):
        assert tpl.count("<<<INSTRUCTIONS>>>") == 1


def test_instructions_placeholder_before_json_contract():
    """The <<<INSTRUCTIONS>>> placeholder appears before the trailing json contract."""
    for tpl in (prompts.SUMMARIZE, prompts.SPEC, prompts.SPEC_REVIEW,
                prompts.IMPLEMENT, prompts.FIX, prompts.REVIEW):
        instructions_idx = tpl.index("<<<INSTRUCTIONS>>>")
        contract_idx = tpl.index("```json")
        assert instructions_idx < contract_idx, "INSTRUCTIONS placeholder must come before the json contract"


def test_render_without_instructions_leaves_no_residue():
    """Rendering a template WITHOUT INSTRUCTIONS consumes the placeholder to ''."""
    out = prompts.render(prompts.SUMMARIZE, NUM=1, TITLE="t", BODY="b",
                         CLARIFICATIONS="(none)")
    assert "<<<" not in out
    assert "INSTRUCTIONS" not in out


def test_render_with_instructions_injects_text():
    """Rendering with INSTRUCTIONS='GUIDE' places GUIDE in the output and keeps the contract intact."""
    out = prompts.render(prompts.SUMMARIZE, NUM=1, TITLE="t", BODY="b",
                         CLARIFICATIONS="(none)", INSTRUCTIONS="Follow these rules.")
    assert "Follow these rules." in out
    assert "```json" in out  # Contract intact
    assert '{"status": "clarify" | "ready",' in out


def test_render_instructions_applied_last_avoids_collision():
    """Instruction text containing <<<SPECS>>> survives verbatim (collision-safe)."""
    out = prompts.render(prompts.REVIEW, NUM=9, TITLE="T", SUMMARY="S",
                         SPECS="real-specs", BRANCH="b", BASE="main",
                         DISCUSSION="D", INSTRUCTIONS="see <<<SPECS>>> above")
    assert "see <<<SPECS>>> above" in out  # Not re-expanded
    assert "real-specs" in out  # Original token expanded correctly


def test_render_default_instructions_is_empty_string_not_none():
    """Rendering without INSTRUCTIONS defaults to '' (not the literal 'None')."""
    out = prompts.render(prompts.SUMMARIZE, NUM=1, TITLE="t", BODY="b",
                         CLARIFICATIONS="(none)")
    assert "None" not in out
    assert "INSTRUCTIONS" not in out
