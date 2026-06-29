"""Model-runner pure helpers: structured-output parsing, Claude Code JSON
metering, the rate-limit sniffer, and the billing-safety env scrub.

No subprocesses are spawned here — only the deterministic helpers are tested.
"""
import json
import subprocess

from orchestrator import config
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


# ── tolerant fallback: recover JSON the run emitted but didn't fence cleanly ──
def test_parse_structured_recovers_unfenced_trailing_object():
    # The model ended in prose and just printed the object — no ```json fence.
    text = ('I dug through the repo and here is my result:\n'
            '{"status": "done", "summary": "did the thing"}\n'
            'Let me know if you need anything else.')
    assert runners.parse_structured(text) == {"status": "done",
                                              "summary": "did the thing"}


def test_parse_structured_recovers_when_closing_fence_is_missing():
    # Truncated/garbled fence: opener present, closing ``` lost.
    text = 'reasoning…\n```json\n{"status": "ready", "n": 2}'
    assert runners.parse_structured(text) == {"status": "ready", "n": 2}


def test_parse_structured_takes_last_object_ignoring_prose_braces():
    # A brace-y aside earlier, the real answer last.
    text = ('I considered returning {status: done} but reconsidered.\n'
            '{"status": "request_changes", "comments": ["fix {the} lock"]}')
    assert runners.parse_structured(text) == {
        "status": "request_changes", "comments": ["fix {the} lock"]}


def test_parse_structured_tolerates_trailing_commas():
    assert runners.parse_structured('```json\n{"a": 1, "b": [2, 3,],}\n```') == \
        {"a": 1, "b": [2, 3]}


def test_parse_structured_fenced_beats_unfenced():
    # When both exist, the contract fence wins over a stray earlier object.
    text = '{"status": "stale"}\n```json\n{"status": "fresh"}\n```'
    assert runners.parse_structured(text) == {"status": "fresh"}


def test_parse_structured_still_empty_on_pure_prose():
    assert runners.parse_structured("no json, just words and a } stray brace") == {}


# ── _claude_cc_json ─────────────────────────────────────────────────────────
def test_claude_cc_json_parses_result_and_usage():
    line = json.dumps({
        "result": "the answer",
        "usage": {"input_tokens": 100, "output_tokens": 30},
    })
    text, itok, otok, structured = runners._claude_cc_json(line)
    assert text == "the answer"
    assert itok == 100
    assert otok == 30
    assert structured is None                            # no --json-schema run


def test_claude_cc_json_discounts_cache_reads_and_counts_cache_writes():
    # Effective input = fresh input + cache creation (full) + 0.1 * cache reads.
    line = json.dumps({
        "result": "x",
        "usage": {"input_tokens": 10, "cache_creation_input_tokens": 20,
                  "cache_read_input_tokens": 900, "output_tokens": 5},
    })
    _, itok, otok, _ = runners._claude_cc_json(line)
    assert itok == 10 + 20 + int(0.1 * 900)              # 10 + 20 + 90 = 120
    assert otok == 5


def test_claude_cc_json_cache_read_heavy_run_is_not_over_counted():
    # The bug: a long agentic run re-reads a big cached context every turn. Those
    # cache reads must NOT dominate the budget tally.
    line = json.dumps({
        "result": "ok",
        "usage": {"input_tokens": 1000, "cache_read_input_tokens": 5_000_000,
                  "output_tokens": 2000},
    })
    _, itok, _, _ = runners._claude_cc_json(line)
    assert itok == 1000 + int(0.1 * 5_000_000)           # 501000, not 5_001_000
    assert itok < 600_000


def test_claude_cc_json_uses_last_line():
    stdout = "noise\n" + json.dumps({"result": "final", "usage": {}})
    text, itok, otok, _ = runners._claude_cc_json(stdout)
    assert text == "final"
    assert (itok, otok) == (0, 0)


def test_claude_cc_json_falls_back_on_garbage():
    text, itok, otok, structured = runners._claude_cc_json("not json at all")
    assert text == "not json at all"
    assert (itok, otok) == (0, 0)
    assert structured is None


def test_claude_cc_json_handles_empty():
    text, itok, otok, structured = runners._claude_cc_json("")
    assert (text, itok, otok, structured) == ("", 0, 0, None)


def test_claude_cc_json_prefers_text_key_when_no_result():
    line = json.dumps({"text": "fallback", "usage": {"output_tokens": 2}})
    text, _, otok, _ = runners._claude_cc_json(line)
    assert text == "fallback"
    assert otok == 2


def test_claude_cc_json_returns_structured_output_when_present():
    # A --json-schema run: the envelope carries a parsed `structured_output`.
    line = json.dumps({
        "result": '{"status": "ok", "n": 3}',
        "structured_output": {"status": "ok", "n": 3},
        "usage": {"input_tokens": 5, "output_tokens": 2},
    })
    text, _, _, structured = runners._claude_cc_json(line)
    assert text == '{"status": "ok", "n": 3}'
    assert structured == {"status": "ok", "n": 3}


# ── _data_from: structured_output wins, text parse is the fallback ───────────
def test_data_from_prefers_structured_output():
    structured = {"status": "ready", "n": 1}
    # Even with a different (stale) fenced block in the text, structured wins.
    text = '```json\n{"status": "stale"}\n```'
    assert runners._data_from(text, structured) == structured


def test_data_from_falls_back_to_text_when_no_structured():
    text = '```json\n{"status": "ready"}\n```'
    assert runners._data_from(text, None) == {"status": "ready"}


def test_data_from_falls_back_when_structured_is_empty():
    # An empty dict isn't a usable result — parse the text instead.
    text = 'prose\n{"status": "done"}'
    assert runners._data_from(text, {}) == {"status": "done"}


def test_data_from_ignores_non_dict_structured():
    text = '{"status": "done"}'
    assert runners._data_from(text, ["not", "a", "dict"]) == {"status": "done"}


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


def test_apply_effort_write_stage_gets_the_turn_floor():
    # A code-writing stage must get at least the write floor, well above what a
    # read-and-reason medium run would allow — this is the fix for implement
    # running out of turns before reporting `done`.
    _, read_turns = runners._apply_effort("P", "medium")
    _, write_turns = runners._apply_effort("P", "medium", write=True)
    assert read_turns == config.EFFORT_TUNING["medium"]["max_turns"]
    assert write_turns == runners.WRITE_TURN_FLOOR
    assert write_turns > read_turns


def test_apply_effort_write_never_lowers_a_higher_effort_budget():
    # The floor is a floor, not a cap: if effort already grants more turns than
    # the floor, keep the larger number.
    from orchestrator import config
    config.EFFORT_TUNING["huge"] = {"directive": "", "max_turns": 999}
    try:
        _, turns = runners._apply_effort("P", "huge", write=True)
        assert turns == 999
    finally:
        del config.EFFORT_TUNING["huge"]


# ── timeout handling ─────────────────────────────────────────────────────────
def test_run_returns_sentinel_on_timeout(monkeypatch):
    # A wall-clock kill must come back as (None, "timeout"), distinct from a
    # clean process exit — callers branch on it.
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
    monkeypatch.setattr(runners.subprocess, "run", boom)
    res, blob = runners._run(["claude"], cwd=".", env={}, pool="claude")
    assert res is None
    assert blob == "timeout"


def test_claude_runner_survives_a_timeout(monkeypatch):
    # The runner must not crash unpacking the sentinel — it returns a clean
    # ok=False result the dispatcher can escalate on.
    monkeypatch.setattr(runners, "_run", lambda *a, **k: (None, "timeout"))
    monkeypatch.setattr(config, "CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-x")
    res = runners.ClaudeCodeRunner().run("PROMPT", cwd=".", model="claude-opus-4-8")
    assert res.ok is False
    assert res.data == {}
    assert res.raw == "timeout"


def test_glm_runner_survives_a_timeout(monkeypatch):
    monkeypatch.setattr(runners, "_run", lambda *a, **k: (None, "timeout"))
    monkeypatch.setattr(config, "ZAI_AUTH_TOKEN", "zai-x")
    res = runners.GlmClaudeCodeRunner().run("PROMPT", cwd=".", model="glm-4.7")
    assert res.ok is False
    assert res.raw == "timeout"


# ── --json-schema wiring ─────────────────────────────────────────────────────
class _FakeProc:
    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _capture_run(captured):
    """A fake runners._run that records the cmd and returns a canned envelope
    carrying a parsed structured_output."""
    envelope = json.dumps({
        "result": '{"status": "ready"}',
        "structured_output": {"status": "ready"},
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })

    def fake(cmd, cwd, env, pool):
        captured["cmd"] = cmd
        captured["env"] = env
        return _FakeProc(envelope), "blob"
    return fake


def test_claude_runner_passes_json_schema_inline(monkeypatch):
    captured = {}
    monkeypatch.setattr(runners, "_run", _capture_run(captured))
    monkeypatch.setattr(config, "CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-x")
    schema = {"type": "object", "properties": {"status": {"type": "string"}}}
    res = runners.ClaudeCodeRunner().run("PROMPT", cwd=".", model="claude-opus-4-8",
                                         schema=schema)
    cmd = captured["cmd"]
    assert "--json-schema" in cmd
    # The schema is passed INLINE as a JSON string (not a file path).
    inline = cmd[cmd.index("--json-schema") + 1]
    assert json.loads(inline) == schema
    # structured_output flows through to RunResult.data.
    assert res.data == {"status": "ready"}


def test_claude_runner_omits_json_schema_when_none(monkeypatch):
    captured = {}
    monkeypatch.setattr(runners, "_run", _capture_run(captured))
    monkeypatch.setattr(config, "CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-x")
    runners.ClaudeCodeRunner().run("PROMPT", cwd=".", model="claude-opus-4-8")
    assert "--json-schema" not in captured["cmd"]


def test_claude_runner_accepts_prebuilt_schema_string(monkeypatch):
    captured = {}
    monkeypatch.setattr(runners, "_run", _capture_run(captured))
    monkeypatch.setattr(config, "CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat-x")
    runners.ClaudeCodeRunner().run("PROMPT", cwd=".", schema='{"type":"object"}')
    cmd = captured["cmd"]
    assert cmd[cmd.index("--json-schema") + 1] == '{"type":"object"}'


def test_glm_runner_never_forwards_json_schema(monkeypatch):
    # z.ai may not honor the flag — GLM relies on the tolerant parser, so the
    # schema must NOT reach the cmd even when one is passed.
    captured = {}
    monkeypatch.setattr(runners, "_run", _capture_run(captured))
    monkeypatch.setattr(config, "ZAI_AUTH_TOKEN", "zai-x")
    res = runners.GlmClaudeCodeRunner().run("PROMPT", cwd=".", model="glm-4.7",
                                            schema={"type": "object"})
    assert "--json-schema" not in captured["cmd"]
    # It still reads structured_output if the endpoint happens to return it.
    assert res.data == {"status": "ready"}
