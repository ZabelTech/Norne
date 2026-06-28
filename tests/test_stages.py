"""Stage-level helpers that don't need GitHub or subprocesses: spec rendering,
usage metering per family, and the route->run->meter path including the
budget-parked failure mode.
"""
import math

import pytest

from orchestrator import config, stages
from orchestrator.runners import RunResult


# ── _specs_text ─────────────────────────────────────────────────────────────
class FakeGH:
    """Minimal gh for _discussion: serves issue body + comments + PR data."""
    def __init__(self, issue, comments, pr=None, pr_comments=None, pr_review=None):
        self._issue = issue
        self._comments = comments
        self._pr = pr
        self._pr_comments = pr_comments or []
        self._pr_review = pr_review or []

    def get_issue(self, n):
        return self._issue

    def list_comments(self, n):
        return self._pr_comments if (self._pr and n == self._pr["number"]) else self._comments

    def get_pull(self, number):
        return self._pr

    def pull_review_comments(self, number):
        return self._pr_review


def _cm(body, marker=False):
    b = body + ("\n\n`[norne-glm-4.7-low]`" if marker else "")
    return {"id": 1, "body": b, "user": {"login": "x"}}


def test_discussion_includes_issue_body_and_all_comments():
    gh = FakeGH(
        issue={"title": "T", "body": "the description"},
        comments=[_cm("human asks"), _cm("bot replied", marker=True)],
    )
    out = stages._discussion(gh, 1, issue={"title": "T", "body": "the description"})
    assert "the description" in out
    assert "(human) human asks" in out
    assert "(bot) bot replied" in out          # bot comment labelled, not dropped
    assert "PULL REQUEST" not in out           # no PR -> no PR section


def test_discussion_includes_pr_description_and_comments():
    gh = FakeGH(
        issue={"title": "T", "body": "desc"},
        comments=[_cm("issue comment")],
        pr={"number": 7, "title": "PR", "body": "pr body"},
        pr_comments=[_cm("pr convo comment")],
        pr_review=[_cm("inline review note")],
    )
    out = stages._discussion(gh, 1, issue={"title": "T", "body": "desc"}, pr_number=7)
    assert "PULL REQUEST #7" in out
    assert "pr body" in out
    assert "pr convo comment" in out
    assert "inline review note" in out


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
    # claude stage runs with its pinned per-stage Claude model
    assert runner.seen["model"] == config.CLAUDE_MODEL_BY_STAGE["spec"]
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


# ── review-round attribution ────────────────────────────────────────────────
def test_fmt_tokens_rounds_to_two_significant_figures():
    assert stages._fmt_tokens(69088) == "69ktok"
    assert stages._fmt_tokens(1234) == "1.2ktok"
    assert stages._fmt_tokens(8523) == "8.5ktok"
    assert stages._fmt_tokens(950) == "950tok"
    assert stages._fmt_tokens(12) == "12tok"
    assert stages._fmt_tokens(0) == "0tok"
    assert stages._fmt_tokens(1_260_000) == "1.3Mtok"


def test_participant_notes_model_effort_and_tokens():
    res = RunResult(ok=True, text="", input_tokens=10000, output_tokens=2000)
    p = stages._participant("spec", "claude", res)
    assert "claude-opus-4-8" in p and "high" in p and "12ktok" in p


def test_pause_records_comment_baseline(monkeypatch):
    posted = {}

    class GH:
        def add_labels(self, n, names):
            pass

        def comment(self, n, body):
            posted["body"] = body

        def list_comments(self, n):
            return [{"id": 7}, {"id": 42}]

    store = {}
    monkeypatch.setattr(stages, "update_issue_meta", lambda n, **kw: store.update(kw))
    stages._pause(GH(), 1, "because")
    assert store.get("last_comment_seen") == 42   # so a later comment is "fresh"
    assert "because" in posted["body"]


# ── spec author<->reviewer loop ──────────────────────────────────────────────
def _R(data):
    return RunResult(ok=True, text="", data=data)


def _spec_env(monkeypatch, seq):
    """Stub everything handle_spec touches except its loop/escalation logic."""
    it = iter(seq)
    monkeypatch.setattr(stages, "_run", lambda *a, **k: next(it))
    monkeypatch.setattr(stages.repo, "ensure_repo", lambda n: "/p")
    monkeypatch.setattr(stages, "_discussion", lambda *a, **k: "D")
    monkeypatch.setattr(stages, "_last_bot_summary", lambda gh, n: "S")
    monkeypatch.setattr(stages, "issue_meta", lambda n: {})
    out = {}
    monkeypatch.setattr(stages, "_publish_specs",
                        lambda gh, n, path, specs, round_note="":
                        out.update(published=specs, note=round_note))
    monkeypatch.setattr(stages, "_escalate_spec",
                        lambda gh, n, specs, reason, round_note="":
                        out.update(escalated=(specs, reason), note=round_note))
    return out


def test_spec_publishes_when_reviewer_happy(monkeypatch):
    out = _spec_env(monkeypatch, [
        ("claude", _R({"status": "ready", "specs": [{"title": "A"}]})),
        ("glm", _R({"verdict": "ok", "concerns": []})),
    ])
    stages.handle_spec(object(), None, {"number": 1, "title": "T"})
    assert out.get("published") == [{"title": "A"}]
    assert "escalated" not in out
    # the round note names BOTH participants (author + reviewer model)
    assert "claude-opus-4-8" in out["note"]      # spec author
    assert "glm-5.2" in out["note"]              # peer reviewer
    assert "round 1" in out["note"]


def test_spec_loops_then_publishes_the_revised_specs(monkeypatch):
    out = _spec_env(monkeypatch, [
        ("claude", _R({"status": "ready", "specs": [{"title": "v1"}]})),  # author r1
        ("glm", _R({"verdict": "concerns", "concerns": ["c1"]})),         # review r1
        ("claude", _R({"status": "ready", "specs": [{"title": "v2"}]})),  # author r2 revises
        ("glm", _R({"verdict": "ok", "concerns": []})),                   # review r2 happy
    ])
    stages.handle_spec(object(), None, {"number": 1, "title": "T"})
    assert out.get("published") == [{"title": "v2"}]   # the revised set, not v1


def test_spec_escalates_after_max_rounds(monkeypatch):
    monkeypatch.setattr(stages.config, "MAX_SPEC_ROUNDS", 2)
    out = _spec_env(monkeypatch, [
        ("claude", _R({"status": "ready", "specs": [{"title": "x"}]})),
        ("glm", _R({"verdict": "concerns", "concerns": ["c"]})),
        ("claude", _R({"status": "ready", "specs": [{"title": "x"}]})),
        ("glm", _R({"verdict": "concerns", "concerns": ["c"]})),
    ])
    stages.handle_spec(object(), None, {"number": 1, "title": "T"})
    assert "escalated" in out and "published" not in out


def test_spec_author_judgement_call_escalates(monkeypatch):
    out = _spec_env(monkeypatch, [
        ("claude", _R({"status": "needs_human", "reason": "ambiguous"})),
    ])
    stages.handle_spec(object(), None, {"number": 1, "title": "T"})
    assert out["escalated"][1] == "ambiguous"


def test_spec_sub_issues_creates_once_then_dedupes(monkeypatch):
    created = []

    class GH:
        def create_issue(self, title, body):
            created.append(title)
            return {"number": 100 + len(created), "id": 9000 + len(created)}

        def add_sub_issue(self, parent, sub_id):
            pass

    store = {}
    monkeypatch.setattr(stages, "issue_meta", lambda n: store.get(n, {}))
    monkeypatch.setattr(stages, "update_issue_meta",
                        lambda n, **kw: store.setdefault(n, {}).update(kw))
    specs = [{"title": "A", "work_items": [{"title": "wi"}]}, {"title": "B"}]
    assert stages._spec_sub_issues(GH(), 1, specs) == [101, 102]
    assert created == ["A", "B"]
    # idempotent: a second call posts nothing new
    assert stages._spec_sub_issues(GH(), 1, specs) == [101, 102]
    assert created == ["A", "B"]
