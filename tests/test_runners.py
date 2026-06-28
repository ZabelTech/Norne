"""Model-runner pure helpers: structured-output parsing, Claude Code JSON
metering, the rate-limit sniffer, and the billing-safety env scrub.

No subprocesses are spawned here — only the deterministic helpers are tested.
"""
import json

from orchestrator import runners


# ── parse_structured ────────────────────────────────────────────────────────
def test_parse_structured_extracts_json_block():
    text = 'Some reasoning.\n```json\n{"status": "ready", "n": 1}\n```'
    assert runners.parse_structured(text) == {"status": "ready", "n": 1}


def test_parse_structured_takes_the_last_block():
    text = ('```json\n{"status": "draft"}\n```\n'
            'then\n```json\n{"status": "ready"}\n```')
    assert runners.parse_structured(text) == {"status": "ready"}


def test_parse_structured_returns_empty_on_no_block():
    assert runners.parse_structured("no json here") == {}


def test_parse_structured_returns_empty_on_malformed_json():
    assert runners.parse_structured('```json\n{not valid}\n```') == {}


def test_parse_structured_handles_none_and_empty():
    assert runners.parse_structured(None) == {}
    assert runners.parse_structured("") == {}


def test_parse_structured_with_nested_braces():
    payload = {"status": "ready", "specs": [{"title": "x", "items": ["a"]}]}
    text = f"```json\n{json.dumps(payload)}\n```"
    assert runners.parse_structured(text) == payload


# ── _claude_cc_json ─────────────────────────────────────────────────────────
def test_claude_cc_json_parses_result_and_usage():
    line = json.dumps({
        "result": "the answer",
        "usage": {"input_tokens": 100, "output_tokens": 30},
    })
    text, itok, otok = runners._claude_cc_json(line)
    assert text == "the answer"
    assert itok == 100
    assert otok == 30


def test_claude_cc_json_counts_cache_reads_as_input():
    line = json.dumps({
        "result": "x",
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 90,
                  "output_tokens": 5},
    })
    _, itok, otok = runners._claude_cc_json(line)
    assert itok == 100  # 10 + 90
    assert otok == 5


def test_claude_cc_json_uses_last_line():
    stdout = "noise\n" + json.dumps({"result": "final", "usage": {}})
    text, itok, otok = runners._claude_cc_json(stdout)
    assert text == "final"
    assert (itok, otok) == (0, 0)


def test_claude_cc_json_falls_back_on_garbage():
    text, itok, otok = runners._claude_cc_json("not json at all")
    assert text == "not json at all"
    assert (itok, otok) == (0, 0)


def test_claude_cc_json_handles_empty():
    text, itok, otok = runners._claude_cc_json("")
    assert (text, itok, otok) == ("", 0, 0)


def test_claude_cc_json_prefers_text_key_when_no_result():
    line = json.dumps({"text": "fallback", "usage": {"output_tokens": 2}})
    text, _, otok = runners._claude_cc_json(line)
    assert text == "fallback"
    assert otok == 2


# ── rate-limit sniffer ──────────────────────────────────────────────────────
def test_rl_hint_matches_common_phrasings():
    for phrase in ("Rate limit exceeded", "HTTP 429", "model overloaded",
                   "quota exceeded for today", "usage limit reached"):
        assert runners._RL_HINT.search(phrase), phrase


def test_rl_hint_ignores_unrelated_text():
    assert runners._RL_HINT.search("completed successfully") is None


# ── billing safety: ANTHROPIC_API_KEY must never reach a runner subprocess ───
def test_base_env_strips_anthropic_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    env = runners._base_env()
    assert "ANTHROPIC_API_KEY" not in env


def test_base_env_preserves_other_vars(monkeypatch):
    monkeypatch.setenv("SOME_OTHER_VAR", "keep-me")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env = runners._base_env()
    assert env["SOME_OTHER_VAR"] == "keep-me"


def test_get_runner_returns_matching_family():
    assert runners.get_runner("claude").family == "claude"
    assert runners.get_runner("glm").family == "glm"


# ── effort wiring ────────────────────────────────────────────────────────────
def test_apply_effort_high_prepends_directive_and_raises_turns():
    from orchestrator import config
    prompt, max_turns = runners._apply_effort("DO THE THING", "high")
    assert prompt.endswith("DO THE THING")          # directive prepended, prompt last
    assert prompt != "DO THE THING"                  # something was prepended
    assert max_turns == config.EFFORT_TUNING["high"]["max_turns"]


def test_apply_effort_low_adds_no_directive():
    prompt, max_turns = runners._apply_effort("PROMPT", "low")
    assert prompt == "PROMPT"                         # low has an empty directive
    assert max_turns == 25


def test_apply_effort_unknown_effort_defaults_to_medium():
    _, max_turns = runners._apply_effort("P", "bogus")
    from orchestrator import config
    assert max_turns == config.EFFORT_TUNING["medium"]["max_turns"]
