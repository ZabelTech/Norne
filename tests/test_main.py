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
