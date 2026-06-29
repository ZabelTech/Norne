"""Stage-level helpers that don't need GitHub or subprocesses: spec rendering,
usage metering per family, and the route->run->meter path including the
budget-parked failure mode.
"""
import concurrent.futures
import math
import threading
import time

import pytest

from orchestrator import config, stages
from orchestrator.runners import RunResult, RateLimited


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
        self.reserves = []
        self.reconciles = []

    def record(self, pool, tokens=0, prompts=0):
        self.calls.append((pool, tokens, prompts))

    def reserve(self, pool, tokens=0, prompts=0):
        self.reserves.append((pool, tokens, prompts))
        return True  # Always succeed for tests

    def reconcile(self, pool, reserved_tokens=0, reserved_prompts=0,
                  actual_tokens=0, actual_prompts=0):
        self.reconciles.append((pool, reserved_tokens, reserved_prompts,
                               actual_tokens, actual_prompts))


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

    def run(self, prompt, cwd, write=False, model=None, effort="medium", schema=None):
        self.seen = {"prompt": prompt, "cwd": cwd, "write": write,
                     "model": model, "effort": effort, "schema": schema}
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
    # With the new reserve/reconcile API:
    # - reserve is called with the estimate (CLAUDE_RESERVE_TOKENS)
    # - reconcile is called with the actual usage
    assert len(led.reserves) == 1
    assert led.reserves[0] == ("claude", config.CLAUDE_RESERVE_TOKENS, 0)
    assert len(led.reconciles) == 1
    assert led.reconciles[0] == ("claude", config.CLAUDE_RESERVE_TOKENS, 0, 15, 0)


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
    # effort is derived from the chosen model (glm here), not the stage
    assert runner.seen["effort"] == config.effort_for(config.GLM_MODEL_BY_STAGE["summarize"])


def test_run_raises_budget_parked_when_no_family(monkeypatch):
    led = RecordingLedger()
    monkeypatch.setattr(stages.router, "choose_family", lambda *a, **k: None)
    with pytest.raises(stages.BudgetParked):
        stages._run("review", led, "P", cwd="/w")
    assert led.calls == []  # nothing metered when parked


def test_run_raises_budget_parked_when_reserve_returns_false(monkeypatch):
    """Failover then park: reserve() returning False makes _run skip that family
    and try the next; when no family is left it raises BudgetParked.

    This tests the path where the pool exhausts between router choice and
    reservation — exactly where the blocking bug hides.
    """
    led = RecordingLedger()

    def reserve_returns_false(pool, tokens=0, prompts=0):
        # Still record the call for test verification
        led.reserves.append((pool, tokens, prompts))
        return False

    led.reserve = reserve_returns_false

    # Router offers claude once, then (skipped) has nothing left -> park.
    def choose(stage, ledger, exclude_family=None, skip=None):
        return None if (skip and "claude" in skip) else "claude"

    monkeypatch.setattr(stages.router, "choose_family", choose)
    monkeypatch.setattr(stages.runners, "get_runner",
                        lambda fam: StubRunner("claude", RunResult(ok=True, text="")))

    with pytest.raises(stages.BudgetParked):
        stages._run("review", led, "P", cwd="/w")

    # Reserve was called but returned False, so BudgetParked was raised
    assert led.reserves == [("claude", config.CLAUDE_RESERVE_TOKENS, 0)]
    # Nothing was recorded because the run never happened
    assert led.calls == []
    # No reconcile because reserve failed
    assert led.reconciles == []


def test_run_forwards_exclude_family(monkeypatch):
    led = RecordingLedger()
    captured = {}

    def fake_choose(stage, ledger, exclude_family=None, skip=None):
        captured["exclude"] = exclude_family
        return "glm"

    monkeypatch.setattr(stages.router, "choose_family", fake_choose)
    monkeypatch.setattr(stages.runners, "get_runner",
                        lambda fam: StubRunner("glm", RunResult(ok=True, text="")))
    stages._run("review", led, "P", cwd="/w", exclude_family="claude")
    assert captured["exclude"] == "claude"


class _RLRunner:
    """A runner that raises RateLimited for named families, else returns ok."""
    def __init__(self, family, rate_limited):
        self.family = family
        self._rl = rate_limited

    def run(self, prompt, cwd, write=False, model=None, effort="medium", schema=None):
        if self.family in self._rl:
            raise stages.RateLimited(self.family)
        return RunResult(ok=True, text="", input_tokens=1, output_tokens=1)


def test_run_falls_back_to_another_family_when_one_is_rate_limited(monkeypatch):
    # glm is rate-limited by the provider -> cool it down, fall back to claude.
    led = RecordingLedger()
    cooled = []
    led.cool_down = lambda pool, seconds: cooled.append((pool, seconds))
    order = ["glm", "claude"]

    def choose(stage, ledger, exclude_family=None, skip=None):
        for f in order:
            if f not in (skip or set()):
                return f
        return None

    monkeypatch.setattr(stages.router, "choose_family", choose)
    monkeypatch.setattr(stages.runners, "get_runner",
                        lambda fam: _RLRunner(fam, {"glm"}))
    fam, res = stages._run("implement", led, "P", cwd="/w", write=True)
    assert fam == "claude"                               # fell back
    assert res.ok
    assert cooled and cooled[0][0] == "glm"              # glm cooled down


def test_run_parks_only_when_all_families_rate_limited(monkeypatch):
    led = RecordingLedger()
    led.cool_down = lambda pool, seconds: None
    order = ["glm", "claude"]

    def choose(stage, ledger, exclude_family=None, skip=None):
        for f in order:
            if f not in (skip or set()):
                return f
        return None

    monkeypatch.setattr(stages.router, "choose_family", choose)
    monkeypatch.setattr(stages.runners, "get_runner",
                        lambda fam: _RLRunner(fam, {"glm", "claude"}))
    with pytest.raises(stages.BudgetParked):
        stages._run("implement", led, "P", cwd="/w", write=True)


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
    assert "claude-opus-4-8" in p and config.effort_for("claude-opus-4-8") in p
    assert "12ktok" in p


# ── escalation reason (parse-failure surfacing) ──────────────────────────────
def test_failure_reason_prefers_the_models_own_reason():
    res = RunResult(ok=True, text="x", data={"status": "needs_human", "reason": "need a call"})
    assert stages._failure_reason(res, "default") == "need a call"


def test_failure_reason_surfaces_raw_text_when_output_did_not_parse():
    res = RunResult(ok=True, text="I dug in but I'm stuck on the loader path.", data={})
    r = stages._failure_reason(res, "default")
    assert "didn't parse" in r and "stuck on the loader path" in r


def test_failure_reason_falls_back_to_default_when_nothing_useful():
    assert stages._failure_reason(RunResult(ok=True, text="", data={}), "DEF") == "DEF"
    # parsed something but no reason -> default, not a raw dump
    assert stages._failure_reason(
        RunResult(ok=True, text="x", data={"status": "weird"}), "DEF") == "DEF"


def test_implement_escalation_reports_a_timeout(monkeypatch):
    monkeypatch.setattr(stages.repo, "dirty_files", lambda path: [])
    res = RunResult(ok=False, text="", data={}, raw="timeout")
    r = stages._implement_escalation(res, "/w")
    assert "time limit" in r


def test_implement_escalation_lists_uncommitted_work(monkeypatch):
    # The key signal: the agent wrote a feature but never reported `done`, so the
    # work is sitting uncommitted. The human must be told what's there.
    monkeypatch.setattr(stages.repo, "dirty_files",
                        lambda path: ["orchestrator/instructions.py",
                                      "tests/test_instructions.py"])
    res = RunResult(ok=True, text="", data={"status": "needs_human"})
    r = stages._implement_escalation(res, "/w")
    assert "2 uncommitted file(s)" in r
    assert "orchestrator/instructions.py" in r


def test_implement_escalation_truncates_a_long_file_list(monkeypatch):
    monkeypatch.setattr(stages.repo, "dirty_files",
                        lambda path: [f"f{i}.py" for i in range(25)])
    r = stages._implement_escalation(RunResult(ok=True, text="", data={}), "/w")
    assert "…and 5 more" in r


def test_implement_escalation_includes_the_models_reason(monkeypatch):
    monkeypatch.setattr(stages.repo, "dirty_files", lambda path: [])
    res = RunResult(ok=True, text="x",
                    data={"status": "needs_human", "reason": "Spec is ambiguous."})
    r = stages._implement_escalation(res, "/w")
    assert "Spec is ambiguous." in r


def test_implement_escalation_falls_back_when_nothing_known(monkeypatch):
    monkeypatch.setattr(stages.repo, "dirty_files", lambda path: [])
    r = stages._implement_escalation(RunResult(ok=True, text="", data={}), "/w")
    assert r == "Implementation hit a blocker."


def test_implement_escalation_surfaces_subprocess_output_on_crash(monkeypatch):
    # A non-zero exit (not a timeout) must include the subprocess's actual output
    # so the crash is diagnosable — this is the #5 blind spot.
    monkeypatch.setattr(stages.repo, "dirty_files", lambda path: [])
    res = RunResult(ok=False, text="", data={},
                    raw="...\nz.ai: 503 upstream error\nfatal: stream closed")
    r = stages._implement_escalation(res, "/w")
    assert "exited with an error" in r
    assert "z.ai: 503 upstream error" in r               # raw output surfaced
    assert "Subprocess output" in r


def test_implement_escalation_reports_committed_work(monkeypatch):
    # When the agent committed a feature but the run died before `done`, say so —
    # don't imply nothing was written.
    monkeypatch.setattr(stages.repo, "dirty_files", lambda path: ["tests/test_x.py"])
    monkeypatch.setattr(stages.repo, "local_commits",
                        lambda path, base: ["21ba79b Implement the feature",
                                            "WIP tests"])
    res = RunResult(ok=False, text="", data={}, raw="boom")
    r = stages._implement_escalation(res, "/w", base="main")
    assert "2 commit(s)" in r and "Implement the feature" in r
    assert "1 uncommitted file(s)" in r                  # both states reported


def test_implement_escalation_timeout_skips_raw_dump(monkeypatch):
    monkeypatch.setattr(stages.repo, "dirty_files", lambda path: [])
    r = stages._implement_escalation(RunResult(ok=False, text="", data={}, raw="timeout"), "/w")
    assert "time limit" in r and "Subprocess output" not in r


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


class _SpecGH:
    def __init__(self):
        self.comments = []

    def comment(self, n, body, **k):    # capture the per-round checkpoint comment
        self.comments.append(body)


def _spec_env(monkeypatch, seq):
    """Stub handle_spec's deps; meta persists across ticks (cross-tick loop)."""
    it = iter(seq)
    meta = {}
    runs = []

    def fake_run(stage, ledger, prompt, cwd=None, **k):
        runs.append((stage, prompt))
        return next(it)

    monkeypatch.setattr(stages, "_run", fake_run)
    monkeypatch.setattr(stages.repo, "ensure_repo", lambda n: "/p")
    monkeypatch.setattr(stages, "_discussion", lambda *a, **k: "D")
    monkeypatch.setattr(stages, "_last_bot_summary", lambda gh, n: "S")
    monkeypatch.setattr(stages, "issue_meta", lambda n: dict(meta))
    monkeypatch.setattr(stages, "update_issue_meta", lambda n, **kw: meta.update(kw))
    out = {"meta": meta, "runs": runs}
    monkeypatch.setattr(stages, "_publish_specs",
                        lambda gh, n, path, specs, round_note="":
                        out.update(published=specs, note=round_note))
    monkeypatch.setattr(stages, "_escalate_spec",
                        lambda gh, n, specs, reason, round_note="":
                        out.update(escalated=(specs, reason), note=round_note))
    return out


def _tick():
    gh = _SpecGH()
    stages.handle_spec(gh, None, {"number": 1, "title": "T"})
    return gh


def test_spec_publishes_first_round_when_reviewer_happy(monkeypatch):
    out = _spec_env(monkeypatch, [
        ("claude", _R({"status": "ready", "specs": [{"title": "A"}]})),
        ("glm", _R({"verdict": "ok", "concerns": []})),
    ])
    _tick()
    assert out.get("published") == [{"title": "A"}] and "escalated" not in out
    assert "claude-opus-4-8" in out["note"] and "glm-5.2" in out["note"]
    assert "round 1" in out["note"]
    assert out["meta"].get("spec_round") == 0           # loop reset on publish


def test_spec_checkpoints_on_concerns_then_publishes_next_tick(monkeypatch):
    out = _spec_env(monkeypatch, [
        ("claude", _R({"status": "ready", "specs": [{"title": "v1"}]})),  # tick1 author
        ("glm", _R({"verdict": "concerns", "concerns": ["c1"]})),         # tick1 review
        ("claude", _R({"status": "ready", "specs": [{"title": "v2"}]})),  # tick2 revise
        ("glm", _R({"verdict": "ok", "concerns": []})),                   # tick2 happy
    ])
    gh = _tick()                                         # round 1 -> checkpoint
    assert "published" not in out and "escalated" not in out
    assert out["meta"]["spec_round"] == 1
    assert "v1" in out["meta"]["spec_feedback"]          # draft carried forward
    assert any("c1" in b for b in gh.comments)           # the raised concern is noted down
    _tick()                                              # round 2 -> publish revised
    assert out.get("published") == [{"title": "v2"}]
    assert "round 2" in out["note"]
    assert out["meta"]["spec_round"] == 0


def test_spec_escalates_after_max_rounds(monkeypatch):
    monkeypatch.setattr(stages.config, "MAX_SPEC_ROUNDS", 2)
    out = _spec_env(monkeypatch, [
        ("claude", _R({"status": "ready", "specs": [{"title": "x"}]})),
        ("glm", _R({"verdict": "concerns", "concerns": ["c"]})),
        ("claude", _R({"status": "ready", "specs": [{"title": "x"}]})),
        ("glm", _R({"verdict": "concerns", "concerns": ["c"]})),
    ])
    _tick()                                              # round 1 -> checkpoint
    assert "escalated" not in out and out["meta"]["spec_round"] == 1
    _tick()                                              # round 2 (== max) -> escalate
    assert "escalated" in out and "published" not in out
    assert out["meta"]["spec_round"] == 0


def test_spec_author_judgement_call_escalates(monkeypatch):
    out = _spec_env(monkeypatch, [
        ("claude", _R({"status": "needs_human", "reason": "ambiguous"})),
    ])
    _tick()
    assert out["escalated"][1] == "ambiguous"
    assert "author stopped before review" in out["note"]


def test_spec_carries_author_rebuttals_into_the_review_prompt(monkeypatch):
    out = _spec_env(monkeypatch, [
        ("claude", _R({"status": "ready", "specs": [{"title": "A"}],
                       "responses": ["disagree: concern X is wrong because Z"]})),
        ("glm", _R({"verdict": "ok", "concerns": []})),
    ])
    _tick()
    review_prompt = next(p for (s, p) in out["runs"] if s == "review")
    assert "disagree: concern X is wrong because Z" in review_prompt   # reviewer sees it


def test_spec_parse_failure_surfaces_the_agents_message(monkeypatch):
    out = _spec_env(monkeypatch, [
        ("claude", RunResult(ok=True, text="Investigated heavily; stuck on X.", data={})),
    ])
    _tick()
    assert "escalated" in out
    assert "didn't parse" in out["escalated"][1]
    assert "stuck on X" in out["escalated"][1]            # the agent's words surfaced
    assert "output didn't parse" in out["note"]           # round note distinguishes it


# ── per-spec fan-out: branch + PR per spec, issue closes when all merge ───────
class _FanGH:
    """Records the GitHub calls the per-spec handlers make, with enough behaviour
    to drive a unit through implement -> review -> merge -> close."""
    def __init__(self, default="main", pulls=None):
        self.default = default
        self.flow = None
        self.comments = []
        self.created_pulls = []
        self.merged = []
        self.closed = []
        self.labels_added = []
        self._pulls = pulls or {}                 # branch -> existing PR (or none)
        self._by_number = {}
        self.comment_calls = []                    # (target_number, body)

    def default_branch(self):
        return self.default

    def get_issue(self, n):
        return {"number": n, "title": "T"}

    def set_flow(self, n, flow, issue=None):
        self.flow = flow

    def comment(self, n, body, **k):
        self.comments.append(body)
        self.comment_calls.append((n, body))

    def add_labels(self, n, labels):
        self.labels_added.extend(labels)

    def list_comments(self, n):
        return []  # Return empty list for tests

    def pull_for_branch(self, branch):
        return self._pulls.get(branch)

    def create_pull(self, title, head, base, body):
        pr = {"number": 200 + len(self.created_pulls), "title": title,
              "head": head, "base": base, "body": body}
        self.created_pulls.append(pr)
        self._by_number[pr["number"]] = pr
        return pr

    def get_pull(self, number):
        return self._by_number.get(number, {"number": number})

    def merge_pull(self, number, method="squash"):
        self.merged.append(number)

    def close_issue(self, n):
        self.closed.append(n)


def _two_specs():
    return [{"title": "Spec A", "slug": "a", "body": "ba", "work_items": []},
            {"title": "Spec B", "slug": "b", "body": "bb", "work_items": []}]


def test_publish_specs_makes_a_branch_per_spec_no_sub_issues(monkeypatch):
    branches, written = [], []
    monkeypatch.setattr(stages.repo, "checkout_branch",
                        lambda path, branch, base: branches.append(branch))
    monkeypatch.setattr(stages.repo, "write_spec",
                        lambda path, n, s, i: written.append(s["slug"]) or s["slug"])
    monkeypatch.setattr(stages.repo, "push", lambda path, branch: None)
    monkeypatch.setattr(stages.repo, "_git", lambda *a, **k: None)  # Mock the git call
    store = {}
    monkeypatch.setattr(stages, "update_issue_meta",
                        lambda n, **kw: store.update(kw))
    gh = _FanGH()
    stages._publish_specs(gh, 1, "/p", _two_specs())
    assert branches == ["pipeline/issue-1/a", "pipeline/issue-1/b"]
    assert written == ["a", "b"]
    units = store["spec_units"]
    assert [u["branch"] for u in units] == \
        ["pipeline/issue-1/a", "pipeline/issue-1/b"]
    assert all(u["stage"] == "implement" for u in units)
    assert gh.flow == stages.config.FLOW_IMPLEMENT
    assert gh.closed == []                              # no sub-issues created/closed


def test_units_migrates_a_legacy_single_branch_issue():
    meta = {"branch": "pipeline/issue-9", "specs": [{"title": "Old", "slug": "old"}],
            "pr_number": 42, "review_round": 1, "implementer": "glm"}
    units = stages._units(meta)
    assert len(units) == 1
    u = units[0]
    assert u["branch"] == "pipeline/issue-9" and u["pr_number"] == 42
    assert u["stage"] == "review"                       # has a PR -> mid-review
    assert u["implementer"] == "glm" and u["review_round"] == 1


def test_implement_opens_pr_linking_issue_without_closing(monkeypatch):
    units = [{"slug": "a", "title": "Spec A", "branch": "pipeline/issue-1/a",
              "spec": _two_specs()[0], "stage": "implement", "pr_number": None,
              "review_round": 0, "last_feedback": None, "implementer": None}]
    monkeypatch.setattr(stages, "issue_meta", lambda n: {"spec_units": units})
    saved = {}
    monkeypatch.setattr(stages, "update_issue_meta", lambda n, **kw: saved.update(kw))
    # Properly mock _update_unit to actually update the unit in the list
    def mock_update_unit(n, slug, **kw):
        for u in units:
            if u["slug"] == slug:
                u.update(kw)
                break
    monkeypatch.setattr(stages, "_update_unit", mock_update_unit)
    monkeypatch.setattr(stages.repo, "ensure_worktree", lambda *a: "/p")
    monkeypatch.setattr(stages.repo, "remove_worktree", lambda *a: None)
    monkeypatch.setattr(stages.repo, "commit_all", lambda *a: None)
    monkeypatch.setattr(stages.repo, "push", lambda *a: None)
    monkeypatch.setattr(stages, "_discussion", lambda *a, **k: "D")
    monkeypatch.setattr(stages, "_run", lambda *a, **k:
                        ("glm", RunResult(ok=True, text="",
                                          data={"status": "done", "summary": "did it"})))
    gh = _FanGH()
    monkeypatch.setattr(stages, "_advance", lambda *a: None)  # Mock _advance to avoid further processing
    stages.handle_implement(gh, None, {"number": 1, "title": "T"})
    pr = gh.created_pulls[0]
    assert "Part of #1" in pr["body"] and "Closes #1" not in pr["body"]
    assert pr["head"] == "pipeline/issue-1/a"
    # Check that the unit was updated via _update_unit
    assert units[0]["stage"] == "review"
    assert units[0]["pr_number"] == pr["number"]


def test_merge_closes_issue_only_when_every_pr_is_merged(monkeypatch):
    units = [
        {"slug": "a", "branch": "b/a", "spec": {}, "stage": "merge", "pr_number": 201,
         "title": "A", "review_round": 0, "last_feedback": None, "implementer": "glm"},
        {"slug": "b", "branch": "b/b", "spec": {}, "stage": "merge", "pr_number": 202,
         "title": "B", "review_round": 0, "last_feedback": None, "implementer": "glm"},
    ]
    saved = {}
    monkeypatch.setattr(stages, "issue_meta", lambda n: {"spec_units": units})
    monkeypatch.setattr(stages, "update_issue_meta", lambda n, **kw: saved.update(kw))
    monkeypatch.setattr(stages.config, "AUTO_MERGE", False)

    # First tick: only PR 201 is merged -> issue stays open.
    gh = _FanGH()
    gh._by_number = {201: {"number": 201, "merged": True},
                     202: {"number": 202, "merged": False}}
    stages.handle_merge(gh, None, {"number": 1, "title": "T"})
    assert gh.closed == []                               # one still open
    assert saved["spec_units"][0]["stage"] == "done"
    assert saved["spec_units"][1]["stage"] == "merge"

    # Second tick: 202 now merged too -> close the issue.
    gh2 = _FanGH()
    gh2._by_number = {201: {"number": 201, "merged": True},
                      202: {"number": 202, "merged": True}}
    units2 = saved["spec_units"]
    monkeypatch.setattr(stages, "issue_meta", lambda n: {"spec_units": units2})
    stages.handle_merge(gh2, None, {"number": 1, "title": "T"})
    assert gh2.closed == [1]
    assert gh2.flow == stages.config.FLOW_DONE


def test_review_approve_moves_unit_to_merge(monkeypatch):
    units = [{"slug": "a", "title": "A", "branch": "b/a", "spec": _two_specs()[0],
              "stage": "review", "pr_number": 201, "review_round": 0,
              "last_feedback": None, "implementer": "glm"}]
    saved = {}
    monkeypatch.setattr(stages, "issue_meta", lambda n: {"spec_units": units})
    # Properly mock _update_unit to actually update the unit in the list
    def mock_update_unit(n, slug, **kw):
        for u in units:
            if u["slug"] == slug:
                u.update(kw)
                break
    monkeypatch.setattr(stages, "_update_unit", mock_update_unit)
    monkeypatch.setattr(stages.repo, "ensure_worktree", lambda *a: "/p")
    monkeypatch.setattr(stages.repo, "remove_worktree", lambda *a: None)
    monkeypatch.setattr(stages, "_discussion", lambda *a, **k: "D")
    monkeypatch.setattr(stages, "_last_bot_summary", lambda gh, n: "S")
    monkeypatch.setattr(stages, "_run", lambda *a, **k:
                        ("claude", RunResult(ok=True, text="",
                                             data={"status": "approve", "summary": "ok"})))
    gh = _FanGH()
    monkeypatch.setattr(stages, "_advance", lambda *a: None)  # Mock _advance to avoid further processing
    stages.handle_review(gh, None, {"number": 1, "title": "T"})
    assert units[0]["stage"] == "merge"
    # The review verdict is posted on the PR (#201), never on the issue (#1).
    targets = [t for t, _ in gh.comment_calls]
    assert 201 in targets and 1 not in targets


def _review_fanout_env(monkeypatch, units, run_result):
    """Wire the fan-out handle_review: worktrees + in-place _update_unit + a
    stubbed _run, with _advance neutralised so we assert the per-unit transition."""
    monkeypatch.setattr(stages, "issue_meta", lambda n: {"spec_units": units})

    def mock_update_unit(n, slug, **kw):
        for u in units:
            if u["slug"] == slug:
                u.update(kw)
                break
    monkeypatch.setattr(stages, "_update_unit", mock_update_unit)
    monkeypatch.setattr(stages.repo, "ensure_worktree", lambda *a: "/p")
    monkeypatch.setattr(stages.repo, "remove_worktree", lambda *a: None)
    monkeypatch.setattr(stages, "_discussion", lambda *a, **k: "D")
    monkeypatch.setattr(stages, "_last_bot_summary", lambda gh, n: "S")
    monkeypatch.setattr(stages, "_run", lambda *a, **k: ("claude", run_result))
    monkeypatch.setattr(stages, "_advance", lambda *a: None)


def test_review_request_changes_comments_on_the_pr(monkeypatch):
    units = [{"slug": "a", "title": "A", "branch": "b/a", "spec": _two_specs()[0],
              "stage": "review", "pr_number": 201, "review_round": 0,
              "last_feedback": None, "implementer": "glm"}]
    _review_fanout_env(monkeypatch, units, RunResult(
        ok=True, text="", data={"status": "request_changes",
                                "comments": ["fix the lock"]}))
    gh = _FanGH()
    stages.handle_review(gh, None, {"number": 1, "title": "T"})
    target, body = gh.comment_calls[-1]
    assert target == 201                                 # on the PR, not issue #1
    assert "Changes requested" in body and "fix the lock" in body
    assert units[0]["stage"] == "implement"              # bounced back to implement


# ── summarize: don't post an empty "(clarify)" placeholder ────────────────────
class _SumGH:
    def __init__(self):
        self.flow = None
        self.labels = []
        self.comments = []

    def list_comments(self, n):
        return []

    def comment(self, n, body, **k):
        self.comments.append(body)

    def set_flow(self, n, flow, issue=None):
        self.flow = flow

    def add_labels(self, n, names):
        self.labels += names


def _sum_env(monkeypatch, res):
    monkeypatch.setattr(stages, "_run", lambda *a, **k: ("claude", res))
    monkeypatch.setattr(stages.repo, "ensure_repo", lambda n: "/p")
    monkeypatch.setattr(stages.instructions, "load", lambda step, path: "")
    monkeypatch.setattr(stages, "update_issue_meta", lambda n, **kw: None)


def test_summarize_escalates_when_output_does_not_parse(monkeypatch):
    # The exact #5 failure: a long run that ended in prose, nothing parsed.
    _sum_env(monkeypatch, RunResult(ok=True, text="I dug through the repo... here's my take.",
                                    data={}))
    gh = _SumGH()
    stages.handle_summarize(gh, None, {"number": 5, "title": "T"})
    assert gh.flow != stages.config.FLOW_CLARIFY          # NOT an empty clarify
    assert stages.config.FLAG_NEEDS_HUMAN in gh.labels    # paused for a human
    body = "\n".join(gh.comments)
    assert "(clarify)" not in body                        # no useless placeholder
    assert "here's my take" in body                       # surfaces the real output


def test_summarize_escalates_on_empty_clarify(monkeypatch):
    # Parsed, but status=clarify with neither summary nor questions -> still useless.
    _sum_env(monkeypatch, RunResult(ok=True, text="x",
                                    data={"status": "clarify", "questions": []}))
    gh = _SumGH()
    stages.handle_summarize(gh, None, {"number": 5, "title": "T"})
    assert stages.config.FLAG_NEEDS_HUMAN in gh.labels
    assert "- (clarify)" not in "\n".join(gh.comments)


def test_summarize_still_posts_real_questions(monkeypatch):
    _sum_env(monkeypatch, RunResult(ok=True, text="",
                                    data={"status": "clarify", "summary": "S",
                                          "questions": ["Which tier?", "Worktrees ok?"]}))
    gh = _SumGH()
    stages.handle_summarize(gh, None, {"number": 5, "title": "T"})
    assert gh.flow == stages.config.FLOW_CLARIFY
    body = "\n".join(gh.comments)
    assert "Which tier?" in body and "Worktrees ok?" in body


# ── _update_unit: no deadlock, persists correctly ───────────────────────────
def test_update_unit_deadlock_free_and_persists(ledger_paths):
    """Drive the real _update_unit without mocking — must return and persist.

    This is a hermetic test that exercises the actual load-modify-save cycle
    under the store lock, proving it doesn't deadlock and updates persist.
    """
    from orchestrator import store

    # Setup: start with one spec unit.
    store.update_issue_meta(7, spec_units=[
        {"slug": "alpha", "branch": "b/alpha", "spec": {}, "stage": "implement",
         "pr_number": None, "review_round": 0, "last_feedback": None, "implementer": None}
    ])

    # Call the real _update_unit (not mocked) — this acquires the store lock,
    # loads data, updates the unit matching the slug, and saves.
    stages._update_unit(7, "alpha", stage="review", pr_number=101)

    # Verify the update persisted and didn't deadlock.
    m = store.issue_meta(7)
    units = m.get("spec_units", [])
    assert len(units) == 1
    assert units[0]["slug"] == "alpha"
    assert units[0]["stage"] == "review"
    assert units[0]["pr_number"] == 101


# ── Fan-out tests with 2+ units ────────────────────────────────────────────────────
def test_implement_fan_out_all_units_processed(monkeypatch):
    """Fan-out with 3 units, MAX_SPEC_WORKERS=2: beta fails deterministically by
    worktree path; alpha and gamma succeed. Asserts: all 3 units attempted, peak
    concurrent workers <= MAX_SPEC_WORKERS=2, successful siblings persisted,
    _advance NOT called when one unit escalates, _pause called exactly once."""
    units = [
        {"slug": "alpha", "title": "A", "branch": "b/alpha", "spec": _two_specs()[0],
         "stage": "implement", "pr_number": None, "review_round": 0,
         "last_feedback": None, "implementer": None},
        {"slug": "beta", "title": "B", "branch": "b/beta", "spec": _two_specs()[1],
         "stage": "implement", "pr_number": None, "review_round": 0,
         "last_feedback": None, "implementer": None},
        {"slug": "gamma", "title": "C", "branch": "b/gamma", "spec": {"title": "C"},
         "stage": "implement", "pr_number": None, "review_round": 0,
         "last_feedback": None, "implementer": None},
    ]

    monkeypatch.setattr(stages.config, "MAX_SPEC_WORKERS", 2)
    monkeypatch.setattr(stages, "issue_meta", lambda n: {"spec_units": units})
    monkeypatch.setattr(stages, "update_issue_meta", lambda n, **kw: None)
    saved = {}

    def mock_update_unit(n, slug, **kw):
        for u in units:
            if u["slug"] == slug:
                u.update(kw)
                break
        saved[slug] = kw

    monkeypatch.setattr(stages, "_update_unit", mock_update_unit)
    # Distinct paths per slug so mock_run can identify which unit is running.
    monkeypatch.setattr(stages.repo, "ensure_worktree",
                        lambda n, slug, branch, base: f"/p/{slug}")
    monkeypatch.setattr(stages.repo, "remove_worktree", lambda n, path: None)
    monkeypatch.setattr(stages.repo, "commit_all", lambda *a: None)
    monkeypatch.setattr(stages.repo, "push", lambda *a: None)
    monkeypatch.setattr(stages.repo, "dirty_files", lambda path: [])
    monkeypatch.setattr(stages, "_discussion", lambda *a, **k: "D")

    advance_calls = []
    monkeypatch.setattr(stages, "_advance", lambda *a: advance_calls.append(a))

    # Track peak concurrency to verify MAX_SPEC_WORKERS is respected.
    peak = [0]
    active = [0]
    peak_lock = threading.Lock()

    def mock_run(stage, ledger, prompt, cwd, **k):
        with peak_lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.01)  # let concurrent workers overlap so peak is observable
        with peak_lock:
            active[0] -= 1
        if cwd == "/p/beta":  # beta fails deterministically
            return ("glm", RunResult(ok=True, text="",
                                     data={"status": "needs_human", "reason": "needs input"}))
        return ("glm", RunResult(ok=True, text="",
                                  data={"status": "done", "summary": "did it"}))

    monkeypatch.setattr(stages, "_run", mock_run)

    gh = _FanGH()
    stages.handle_implement(gh, None, {"number": 1, "title": "T"})

    # alpha and gamma succeeded; beta escalated (never reaches _update_unit)
    assert saved.get("alpha", {}).get("stage") == "review"
    assert saved.get("gamma", {}).get("stage") == "review"
    assert "beta" not in saved

    # MAX_SPEC_WORKERS=2 was respected
    assert peak[0] <= 2

    # _advance not called because one unit escalated; _pause called once
    assert len(advance_calls) == 0
    assert stages.config.FLAG_NEEDS_HUMAN in gh.labels_added
    assert any("beta" in c for c in gh.comments)


def test_implement_fan_out_re_raises_budget_parked(monkeypatch):
    """Fan-out: BudgetParked is re-raised after persisting successful siblings."""
    units = [
        {"slug": "alpha", "title": "A", "branch": "b/alpha", "spec": _two_specs()[0],
         "stage": "implement", "pr_number": None, "review_round": 0,
         "last_feedback": None, "implementer": None},
        {"slug": "beta", "title": "B", "branch": "b/beta", "spec": _two_specs()[1],
         "stage": "implement", "pr_number": None, "review_round": 0,
         "last_feedback": None, "implementer": None},
    ]

    monkeypatch.setattr(stages.config, "MAX_SPEC_WORKERS", 2)
    monkeypatch.setattr(stages, "issue_meta", lambda n: {"spec_units": units})
    saved = {}

    def mock_update_unit(n, slug, **kw):
        for u in units:
            if u["slug"] == slug:
                u.update(kw)
                break
        saved[slug] = kw

    monkeypatch.setattr(stages, "_update_unit", mock_update_unit)
    monkeypatch.setattr(stages.repo, "ensure_worktree", lambda *a: "/p")
    monkeypatch.setattr(stages.repo, "remove_worktree", lambda *a: None)
    monkeypatch.setattr(stages.repo, "commit_all", lambda *a: None)
    monkeypatch.setattr(stages.repo, "push", lambda *a: None)
    monkeypatch.setattr(stages.repo, "dirty_files", lambda path: [])
    monkeypatch.setattr(stages, "_discussion", lambda *a, **k: "D")

    call_count = {"count": 0}
    count_lock = threading.Lock()

    def mock_run(*a, **k):
        with count_lock:
            call_count["count"] += 1
            count = call_count["count"]
        if count == 2:  # second call hits budget limit (whichever unit that is)
            raise stages.BudgetParked()
        return ("glm", RunResult(ok=True, text="",
                                  data={"status": "done", "summary": "did it"}))

    monkeypatch.setattr(stages, "_run", mock_run)

    gh = _FanGH()

    # Should re-raise BudgetParked
    with pytest.raises(stages.BudgetParked):
        stages.handle_implement(gh, None, {"number": 1, "title": "T"})

    # At least one successful unit persisted its state before re-raising
    assert len(saved) >= 1
    assert any(s.get("stage") == "review" for s in saved.values())


def test_implement_fan_out_re_raises_rate_limited(monkeypatch):
    """Fan-out: RateLimited is re-raised after persisting successful siblings."""
    units = [
        {"slug": "alpha", "title": "A", "branch": "b/alpha", "spec": _two_specs()[0],
         "stage": "implement", "pr_number": None, "review_round": 0,
         "last_feedback": None, "implementer": None},
        {"slug": "beta", "title": "B", "branch": "b/beta", "spec": _two_specs()[1],
         "stage": "implement", "pr_number": None, "review_round": 0,
         "last_feedback": None, "implementer": None},
    ]

    monkeypatch.setattr(stages.config, "MAX_SPEC_WORKERS", 2)
    monkeypatch.setattr(stages, "issue_meta", lambda n: {"spec_units": units})
    saved = {}

    def mock_update_unit(n, slug, **kw):
        for u in units:
            if u["slug"] == slug:
                u.update(kw)
                break
        saved[slug] = kw

    monkeypatch.setattr(stages, "_update_unit", mock_update_unit)
    monkeypatch.setattr(stages.repo, "ensure_worktree", lambda *a: "/p")
    monkeypatch.setattr(stages.repo, "remove_worktree", lambda *a: None)
    monkeypatch.setattr(stages.repo, "commit_all", lambda *a: None)
    monkeypatch.setattr(stages.repo, "push", lambda *a: None)
    monkeypatch.setattr(stages.repo, "dirty_files", lambda path: [])
    monkeypatch.setattr(stages, "_discussion", lambda *a, **k: "D")

    call_count = {"count": 0}
    lock = threading.Lock()

    def mock_run(*a, **k):
        with lock:
            call_count["count"] += 1
            count = call_count["count"]
        if count == 2:  # beta hits rate limit
            raise RateLimited("glm")
        return ("glm", RunResult(ok=True, text="",
                                  data={"status": "done", "summary": "did it"}))

    monkeypatch.setattr(stages, "_run", mock_run)

    gh = _FanGH()

    # Should re-raise RateLimited
    with pytest.raises(RateLimited):
        stages.handle_implement(gh, None, {"number": 1, "title": "T"})

    # At least one successful unit persisted its state before re-raising
    assert len(saved) >= 1
    assert any(s.get("stage") == "review" for s in saved.values())
