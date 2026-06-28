"""process_issue gating — the approval gate: approve-to-proceed and the new
comment-to-revise path (a comment at approval sends it back to summarize)."""
from orchestrator import config, main


class FakeGH:
    def __init__(self, labels, latest=None):
        self._labels = set(labels)
        self._latest = latest
        self.flow_set = []
        self.removed = []

    def labels_of(self, issue):
        return set(self._labels)

    def latest_human_comment(self, n):
        return self._latest

    def set_flow(self, n, label, issue=None):
        self.flow_set.append(label)

    def remove_label(self, n, name):
        self.removed.append(name)


def _issue(n=1):
    return {"number": n}


def test_approval_new_comment_revises(monkeypatch):
    # A human comment newer than last_comment_seen -> back to summarize.
    monkeypatch.setattr(main, "issue_meta", lambda n: {"last_comment_seen": 100})
    gh = FakeGH(labels={config.FLOW_APPROVAL}, latest={"id": 200})
    main.process_issue(gh, ledger=None, issue=_issue())
    assert gh.flow_set == [config.FLOW_SUMMARIZE]


def test_approval_stale_comment_waits(monkeypatch):
    # Nothing new since we summarized -> stay parked at approval.
    monkeypatch.setattr(main, "issue_meta", lambda n: {"last_comment_seen": 200})
    gh = FakeGH(labels={config.FLOW_APPROVAL}, latest={"id": 200})
    main.process_issue(gh, ledger=None, issue=_issue())
    assert gh.flow_set == []


def test_approval_no_comment_waits(monkeypatch):
    monkeypatch.setattr(main, "issue_meta", lambda n: {})
    gh = FakeGH(labels={config.FLOW_APPROVAL}, latest=None)
    main.process_issue(gh, ledger=None, issue=_issue())
    assert gh.flow_set == []


def test_approve_label_advances_to_spec(monkeypatch):
    monkeypatch.setattr(main, "issue_meta", lambda n: {})
    monkeypatch.setattr(main, "DISPATCH", {})  # don't actually run handle_spec
    gh = FakeGH(labels={config.FLOW_APPROVAL, config.SIG_APPROVE})
    main.process_issue(gh, ledger=None, issue=_issue())
    assert config.SIG_APPROVE in gh.removed
    assert config.FLOW_SPEC in gh.flow_set


# ── human:needed gate — resume on a new comment OR the resolved label ─────────
def test_human_needed_resumes_on_a_new_comment(monkeypatch):
    monkeypatch.setattr(main, "issue_meta", lambda n: {"last_comment_seen": 100})
    captured = {}
    monkeypatch.setattr(main, "update_issue_meta", lambda n, **kw: captured.update(kw))
    monkeypatch.setattr(main, "DISPATCH", {})
    gh = FakeGH(labels={config.FLOW_SPEC, config.FLAG_NEEDS_HUMAN},
                latest={"id": 200, "body": "do X"})
    main.process_issue(gh, ledger=None, issue=_issue())
    assert config.FLAG_NEEDS_HUMAN in gh.removed          # resumed
    assert captured.get("human_guidance") == "do X"       # comment = guidance


def test_human_needed_waits_without_new_comment_or_label(monkeypatch):
    monkeypatch.setattr(main, "issue_meta", lambda n: {"last_comment_seen": 200})
    monkeypatch.setattr(main, "update_issue_meta", lambda n, **kw: None)
    monkeypatch.setattr(main, "DISPATCH", {})
    gh = FakeGH(labels={config.FLOW_SPEC, config.FLAG_NEEDS_HUMAN},
                latest={"id": 200, "body": "stale"})       # not newer than seen
    main.process_issue(gh, ledger=None, issue=_issue())
    assert gh.removed == []                                # stayed paused


def test_human_needed_still_resumes_on_resolved_label(monkeypatch):
    monkeypatch.setattr(main, "issue_meta", lambda n: {"last_comment_seen": 10})
    captured = {}
    monkeypatch.setattr(main, "update_issue_meta", lambda n, **kw: captured.update(kw))
    monkeypatch.setattr(main, "DISPATCH", {})
    gh = FakeGH(labels={config.FLOW_SPEC, config.FLAG_NEEDS_HUMAN, config.SIG_RESOLVED},
                latest={"id": 5, "body": "decided"})        # stale, but label present
    main.process_issue(gh, ledger=None, issue=_issue())
    assert config.FLAG_NEEDS_HUMAN in gh.removed
    assert config.SIG_RESOLVED in gh.removed
