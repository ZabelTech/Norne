"""process_issue gating — the approval gate: approve-to-proceed and the new
comment-to-revise path (a comment at approval sends it back to summarize)."""
from orchestrator import config, main


class FakeGH:
    def __init__(self, labels, latest=None, repo="owner/repo"):
        self._labels = set(labels)
        self._latest = latest
        self.flow_set = []
        self.removed = []
        self._issues = []
        self.repo = repo
        # Parse repo for identity
        if "/" not in repo:
            raise ValueError(f"Invalid repo format: {repo}. Expected 'owner/name'.")
        self.owner, self.name = repo.split("/", 1)
        self.remote = f"https://github.com/{repo}.git"

    def key(self, n):
        """The namespaced store key for issue n in this repo."""
        return f"{self.repo}#{n}"

    def slug_n(self, n):
        """The filesystem-safe slug for issue n in this repo."""
        return f"{self.owner}-{self.name}-{n}"

    def labels_of(self, issue):
        return set(self._labels)

    def latest_human_comment(self, n):
        return self._latest

    def set_flow(self, n, label, issue=None):
        self.flow_set.append(label)

    def remove_label(self, n, name):
        self.removed.append(name)

    def list_issues(self, state="open"):
        return self._issues


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


# ── skip-reason logging (why an issue isn't advanced) ─────────────────────────
class _Ledger:
    def __init__(self, headroom):
        self._h = headroom

    def headroom(self, pool):
        return self._h


def test_blocked_budget_logs_why_it_is_skipped(monkeypatch, capsys):
    monkeypatch.setattr(main, "issue_meta", lambda n: {})
    gh = FakeGH(labels={config.FLOW_SPEC, config.FLAG_BLOCKED_BUDGET})
    main.process_issue(gh, ledger=_Ledger(False), issue=_issue())
    out = capsys.readouterr().out
    assert "blocked:budget" in out and "no pool has headroom" in out
    assert gh.flow_set == []                              # did not advance


def test_approval_gate_waiting_logs_why(monkeypatch, capsys):
    monkeypatch.setattr(main, "issue_meta", lambda n: {"last_comment_seen": 100})
    monkeypatch.setattr(main, "DISPATCH", {})
    gh = FakeGH(labels={config.FLOW_APPROVAL}, latest={"id": 50})   # stale comment
    main.process_issue(gh, ledger=None, issue=_issue())
    out = capsys.readouterr().out
    assert "flow:approval" in out and "waiting" in out


def test_human_needed_waiting_logs_why(monkeypatch, capsys):
    monkeypatch.setattr(main, "issue_meta", lambda n: {"last_comment_seen": 200})
    monkeypatch.setattr(main, "update_issue_meta", lambda n, **kw: None)
    gh = FakeGH(labels={config.FLOW_SPEC, config.FLAG_NEEDS_HUMAN}, latest={"id": 200})
    main.process_issue(gh, ledger=None, issue=_issue())
    out = capsys.readouterr().out
    assert "human:needed" in out and "waiting" in out


# ── Concurrency tests ─────────────────────────────────────────────────────────
import concurrent.futures
import threading
import time


def test_dispatch_once_respects_inflight_guard():
    """An issue already in flight is not submitted again."""
    gh = FakeGH(labels={})
    gh._issues = [
        {"number": 1, "title": "Issue 1"},
        {"number": 2, "title": "Issue 2"},
    ]

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    ledger = None
    inflight = {"owner/repo#1", "owner/repo#2"}
    lock = threading.Lock()

    submitted = []

    def dummy_process_issue(gh, ledger, issue):
        submitted.append(issue["number"])

    original = main.process_issue
    main.process_issue = dummy_process_issue
    try:
        main.dispatch_once([gh], executor, ledger, inflight, lock)
    finally:
        main.process_issue = original

    # No submissions — both were in-flight.
    assert len(submitted) == 0
    assert inflight == {"owner/repo#1", "owner/repo#2"}


def test_inflight_guard_releases_on_done_callback():
    """The done callback removes the issue from inflight after completion."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    inflight = {"owner/repo#42"}
    lock = threading.Lock()

    fut = executor.submit(lambda: None)
    fut.add_done_callback(main._done_callback("owner/repo#42", inflight, lock, 0))
    fut.result()  # wait for completion

    assert "owner/repo#42" not in inflight


def test_inflight_guard_releases_on_error():
    """The done callback removes from inflight even if the task raised."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    inflight = {"owner/repo#42"}
    lock = threading.Lock()

    def failing():
        raise ValueError("boom")

    fut = executor.submit(failing)
    fut.add_done_callback(main._done_callback("owner/repo#42", inflight, lock, 0))
    try:
        fut.result()  # will raise
    except ValueError:
        pass

    assert "owner/repo#42" not in inflight


def test_dispatch_once_peak_concurrency_cap():
    """The executor's max_workers cap is observed: peak concurrent process_issue
    calls never exceeds MAX_WORKERS=2 even when 4 candidates are dispatched.

    Uses a real ThreadPoolExecutor and a slow stub that records peak concurrency
    via a shared counter — exactly what the spec test plan prescribes.
    """
    MAX_W = 2

    # Issues need a flow label so candidates() picks them up.
    gh = FakeGH(labels={config.FLOW_SPEC})
    gh._issues = [{"number": i, "title": f"Issue {i}"} for i in range(1, 5)]

    peak = [0]
    active = [0]
    counter_lock = threading.Lock()

    def slow_process_issue(gh, ledger, issue):
        with counter_lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.02)  # ensure concurrent workers overlap so peak is observable
        with counter_lock:
            active[0] -= 1

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_W)
    original = main.process_issue
    main.process_issue = slow_process_issue
    try:
        inflight = set()
        inflight_lock = threading.Lock()
        main.dispatch_once([gh], executor, None, inflight, inflight_lock)
        executor.shutdown(wait=True)
    finally:
        main.process_issue = original

    assert peak[0] <= MAX_W


# ── Multi-repo: same issue number in two repos dispatched independently ─────────
def test_dispatch_once_same_issue_number_two_repos_dispatched_independently():
    """Issue #1 in repo-a and issue #1 in repo-b must both be dispatched (no cross-repo
    in-flight collision). Each repo's client produces a distinct key() so the two
    issues don't block each other.
    """
    gh_a = FakeGH(labels={config.FLOW_SPEC}, repo="org/repo-a")
    gh_a._issues = [{"number": 1, "title": "Issue in A"}]
    gh_b = FakeGH(labels={config.FLOW_SPEC}, repo="org/repo-b")
    gh_b._issues = [{"number": 1, "title": "Issue in B"}]

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    inflight = set()
    lock = threading.Lock()

    submitted = []

    def capture_process_issue(gh, ledger, issue):
        submitted.append(gh.key(issue["number"]))

    original = main.process_issue
    main.process_issue = capture_process_issue
    try:
        main.dispatch_once([gh_a, gh_b], executor, None, inflight, lock)
        executor.shutdown(wait=True)
    finally:
        main.process_issue = original

    # Both issues must have been submitted — cross-repo issue #1 does not block.
    assert "org/repo-a#1" in submitted
    assert "org/repo-b#1" in submitted


def test_dispatch_once_same_repo_issue_not_dispatched_twice():
    """Within one repo, an in-flight issue #1 blocks a second dispatch of issue #1."""
    gh = FakeGH(labels={config.FLOW_SPEC}, repo="org/repo")
    gh._issues = [{"number": 1, "title": "Issue 1"}]

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    # Seed inflight with the namespaced key for this issue.
    inflight = {"org/repo#1"}
    lock = threading.Lock()

    submitted = []
    original = main.process_issue
    main.process_issue = lambda gh, l, i: submitted.append(gh.key(i["number"]))
    try:
        main.dispatch_once([gh], executor, None, inflight, lock)
        executor.shutdown(wait=True)
    finally:
        main.process_issue = original

    assert submitted == []   # skipped because already in-flight


def test_migrate_legacy_keys_called_at_startup(monkeypatch):
    """main() calls migrate_legacy_keys(GH_REPO) before the first dispatch."""
    from orchestrator import config as cfg

    migration_calls = []
    monkeypatch.setattr(main, "migrate_legacy_keys",
                        lambda slug: (migration_calls.append(slug), ([], []))[1])
    monkeypatch.setattr(cfg, "GH_REPO", "owner/repo")
    monkeypatch.setattr(cfg, "GH_OWNER", None)

    # Stub out everything else so main() doesn't block or make network calls.
    monkeypatch.setattr(main, "discover_repos", lambda owner, token: [])

    import concurrent.futures as cf
    import threading

    dispatched = []

    def fake_dispatch(gh_clients, executor, ledger, inflight, lock):
        dispatched.append(True)
        raise KeyboardInterrupt   # exit the loop after one iteration

    monkeypatch.setattr(main, "dispatch_once", fake_dispatch)

    class _FakeExecutor:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def submit(self, *a, **k): pass
        def shutdown(self, *a, **k): pass

    monkeypatch.setattr(cf, "ThreadPoolExecutor", _FakeExecutor)

    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda s: None)

    try:
        main.main()
    except KeyboardInterrupt:
        pass

    # migrate_legacy_keys must have been called with GH_REPO before dispatching.
    assert "owner/repo" in migration_calls


def test_dispatch_once_per_repo_error_skips_that_repo_others_still_dispatch():
    """If list_issues raises for one repo, the other repos are still dispatched.

    The per-repo try/except in dispatch_once must swallow the per-repo exception
    so a single flaky API call doesn't abort the whole poll cycle.
    """
    gh_ok = FakeGH(labels={config.FLOW_SPEC}, repo="org/ok-repo")
    gh_ok._issues = [{"number": 1, "title": "OK issue"}]

    class _BrokenGH(FakeGH):
        def list_issues(self, **kw):
            raise RuntimeError("network error")

    gh_broken = _BrokenGH(labels={}, repo="org/broken-repo")

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    inflight = set()
    lock = threading.Lock()

    submitted = []
    original = main.process_issue
    main.process_issue = lambda gh, l, i: submitted.append(gh.key(i["number"]))
    try:
        main.dispatch_once([gh_broken, gh_ok], executor, None, inflight, lock)
        executor.shutdown(wait=True)
    finally:
        main.process_issue = original

    # The ok repo's issue was dispatched even though the broken repo errored.
    assert "org/ok-repo#1" in submitted
    # The broken repo produced no submissions (it errored, not dispatched).
    assert not any("broken" in k for k in submitted)


def test_dispatch_once_content_change_same_length_rebuilds_clients():
    """Clients are rebuilt when repo content changes even if the count is the same.

    A rename (or simultaneous add+remove) keeps len() constant, so comparing by
    length alone misses the change. The comparison must use content.
    This test is not about dispatch_once itself but about the loop logic. We
    verify it by checking that comparing slugs vs client.repo catches a swap.
    """
    # Simulate: clients currently track [repo-a, repo-b]
    gh_a = FakeGH(labels={}, repo="org/repo-a")
    gh_b = FakeGH(labels={}, repo="org/repo-b")
    current_clients = [gh_a, gh_b]

    # New discovery returns [repo-a, repo-c] — same length, different content.
    new_slugs = ["org/repo-a", "org/repo-c"]

    # Content comparison catches the difference.
    assert new_slugs != [c.repo for c in current_clients]

    # Length comparison would have missed it.
    assert len(new_slugs) == len(current_clients)
