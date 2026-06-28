"""Stage-level helpers that don't need GitHub or subprocesses: spec rendering,
usage metering per family, and the route->run->meter path including the
budget-parked failure mode.
"""
import math

import pytest

from orchestrator import config, stages
from orchestrator.runners import RunResult


# ── _specs_text ─────────────────────────────────────────────────────────────
def test_specs_text_renders_titles_bodies_and_items():
    specs = [{
        "title": "Add login",
        "body": "Context here.",
        "work_items": [{"title": "form"}, {"title": "session"}],
    }]
    out = stages._specs_text(specs)
    assert "### Add login" in out
    assert "Context here." in out
    assert "- form" in out
    assert "- session" in out


def test_specs_text_tolerates_missing_fields():
    out = stages._specs_text([{}])
    assert isinstance(out, str)  # no KeyError on absent title/body/work_items


def test_specs_text_joins_multiple_specs():
    out = stages._specs_text([{"title": "A"}, {"title": "B"}])
    assert "### A" in out and "### B" in out


# ── _record (metering) ──────────────────────────────────────────────────────
class RecordingLedger:
    def __init__(self):
        self.calls = []

    def record(self, pool, tokens=0, prompts=0):
        self.calls.append((pool, tokens, prompts))


def test_record_claude_sums_input_and_output_tokens():
    led = RecordingLedger()
    res = RunResult(ok=True, text="", input_tokens=80, output_tokens=20)
    stages._record(led, "claude", res)
    assert led.calls == [("claude", 100, 0)]


def test_record_glm_charges_prompts_by_multiplier():
    led = RecordingLedger()
    res = RunResult(ok=True, text="")
    stages._record(led, "glm", res)
    expected = math.ceil(config.GLM_QUOTA_MULTIPLIER)
    assert led.calls == [("glm", 0, expected)]


# ── _run (route -> run -> meter) ────────────────────────────────────────────
class StubRunner:
    def __init__(self, family, result):
        self.family = family
        self._result = result
        self.seen = {}

    def run(self, prompt, cwd, write=False, model=None, effort="medium"):
        self.seen = {"prompt": prompt, "cwd": cwd, "write": write,
                     "model": model, "effort": effort}
        return self._result


def test_run_routes_records_and_returns_family(monkeypatch):
    led = RecordingLedger()
    result = RunResult(ok=True, text="ok", input_tokens=10, output_tokens=5)
    runner = StubRunner("claude", result)
    monkeypatch.setattr(stages.router, "choose_family", lambda *a, **k: "claude")
    monkeypatch.setattr(stages.runners, "get_runner", lambda fam: runner)

    fam, res = stages._run("spec", led, "PROMPT", cwd="/work")
    assert fam == "claude"
    assert res is result
    assert runner.seen["prompt"] == "PROMPT"
    assert runner.seen["cwd"] == "/work"
    # claude stage runs with no GLM model override
    assert runner.seen["model"] is None
    # and the call was metered against the claude pool
    assert led.calls == [("claude", 15, 0)]


def test_run_passes_glm_model_for_glm_family(monkeypatch):
    led = RecordingLedger()
    runner = StubRunner("glm", RunResult(ok=True, text=""))
    monkeypatch.setattr(stages.router, "choose_family", lambda *a, **k: "glm")
    monkeypatch.setattr(stages.runners, "get_runner", lambda fam: runner)

    stages._run("implement", led, "P", cwd="/w", write=True)
    assert runner.seen["model"] == config.GLM_MODEL_BY_STAGE["implement"]
    assert runner.seen["write"] is True


def test_run_passes_stage_effort_to_runner(monkeypatch):
    led = RecordingLedger()
    runner = StubRunner("glm", RunResult(ok=True, text=""))
    monkeypatch.setattr(stages.router, "choose_family", lambda *a, **k: "glm")
    monkeypatch.setattr(stages.runners, "get_runner", lambda fam: runner)

    stages._run("summarize", led, "P", cwd="/w")
    assert runner.seen["effort"] == config.EFFORT_BY_STAGE["summarize"]  # "high"


def test_run_raises_budget_parked_when_no_family(monkeypatch):
    led = RecordingLedger()
    monkeypatch.setattr(stages.router, "choose_family", lambda *a, **k: None)
    with pytest.raises(stages.BudgetParked):
        stages._run("review", led, "P", cwd="/w")
    assert led.calls == []  # nothing metered when parked


def test_run_forwards_exclude_family(monkeypatch):
    led = RecordingLedger()
    captured = {}

    def fake_choose(stage, ledger, exclude_family=None):
        captured["exclude"] = exclude_family
        return "glm"

    monkeypatch.setattr(stages.router, "choose_family", fake_choose)
    monkeypatch.setattr(stages.runners, "get_runner",
                        lambda fam: StubRunner("glm", RunResult(ok=True, text="")))
    stages._run("review", led, "P", cwd="/w", exclude_family="claude")
    assert captured["exclude"] == "claude"
