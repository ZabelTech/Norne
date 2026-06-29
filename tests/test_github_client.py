"""Bot-comment marker: emission + detection, and the human/bot split it drives.

The marker is what lets the pipeline recognise its OWN comments even when it
posts under the same GitHub account as the human (shared-identity token) — the
failure mode that caused the clarify loop to re-summarise forever.
"""
import pytest
from orchestrator.github_client import GitHub, is_bot_comment, bot_marker, discover_repos


def _c(cid, body, login="someone"):
    return {"id": cid, "body": body, "user": {"login": login}}


def test_bot_marker_format():
    assert bot_marker("glm-4.7", "low") == "`[norne-glm-4.7-low]`"
    assert bot_marker() == "`[norne-orchestrator-na]`"


def test_bot_marker_includes_model_and_tokens():
    # Shows the real model id and the token count of the run that produced it.
    assert bot_marker("claude-opus-4-8", "high", tokens=12345) == \
        "`[norne-claude-opus-4-8-high-12345tok]`"
    # No token suffix for non-metered (orchestrator status) comments.
    assert "tok" not in bot_marker("claude-opus-4-8", "high")


def test_is_bot_comment_detects_marker():
    assert is_bot_comment(_c(1, "Hi\n\n`[norne-glm-5.2-high]`")) is True
    assert is_bot_comment(_c(2, "a plain human reply")) is False
    assert is_bot_comment({"body": None}) is False
    assert is_bot_comment({}) is False


def test_comment_appends_marker(monkeypatch):
    gh = GitHub("owner/repo")
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

    def fake_post(url, json=None, **kw):
        captured["body"] = json["body"]
        return FakeResp()

    monkeypatch.setattr(gh.s, "post", fake_post)
    gh.comment(5, "Hello", model="claude-opus-4-8", effort="high", tokens=999)
    assert captured["body"].startswith("Hello")
    assert captured["body"].rstrip().endswith("`[norne-claude-opus-4-8-high-999tok]`")
    # round-trips: a comment we posted reads back as a bot comment
    assert is_bot_comment({"body": captured["body"]}) is True


def test_latest_human_comment_skips_bot_even_with_shared_login(monkeypatch):
    # Regression: bot and human are BOTH "alice" (bot posts under the owner's
    # token). The bot's drafts must still be skipped by marker.
    gh = GitHub("owner/repo")
    comments = [
        _c(1, "human asks something", login="alice"),
        _c(2, "📋 Summary (draft)\n\n`[norne-glm-4.7-low]`", login="alice"),
        _c(3, "human reply", login="alice"),
        _c(4, "📋 Summary (draft)\n\n`[norne-glm-4.7-low]`", login="alice"),
    ]
    monkeypatch.setattr(gh, "list_comments", lambda n: comments)
    latest = gh.latest_human_comment(7)
    assert latest["id"] == 3  # newest UNMARKED comment, not the later bot draft


def test_latest_human_comment_none_when_all_bot(monkeypatch):
    gh = GitHub("owner/repo")
    monkeypatch.setattr(gh, "list_comments",
                        lambda n: [_c(1, "draft\n\n`[norne-glm-4.7-low]`")])
    assert gh.latest_human_comment(7) is None


def test_create_issue_and_add_sub_issue_hit_the_right_endpoints(monkeypatch):
    gh = GitHub("owner/repo")
    calls = []

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"number": 7, "id": 555}

    def fake_post(url, json=None, **kw):
        calls.append((url, json))
        return FakeResp()

    monkeypatch.setattr(gh.s, "post", fake_post)
    child = gh.create_issue("Title", "Body")
    assert child == {"number": 7, "id": 555}
    assert calls[-1][0].endswith("/issues")
    assert calls[-1][1] == {"title": "Title", "body": "Body"}

    gh.add_sub_issue(3, 555)
    assert calls[-1][0].endswith("/issues/3/sub_issues")
    assert calls[-1][1] == {"sub_issue_id": 555}


def test_close_issue_patches_state_closed(monkeypatch):
    gh = GitHub("owner/repo")
    calls = []

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"number": 9, "state": "closed"}

    def fake_patch(url, json=None, **kw):
        calls.append((url, json))
        return FakeResp()

    monkeypatch.setattr(gh.s, "patch", fake_patch)
    out = gh.close_issue(9)
    assert out["state"] == "closed"
    assert calls[-1][0].endswith("/issues/9")
    assert calls[-1][1] == {"state": "closed"}


# ── Per-repo identity (multi-repo support) ────────────────────────────────────

def test_github_client_derives_owner_name_remote():
    gh = GitHub("ZabelTech/foo")
    assert gh.owner == "ZabelTech"
    assert gh.name == "foo"
    assert gh.repo == "ZabelTech/foo"
    assert "ZabelTech/foo" in gh.remote
    assert gh.remote.startswith("https://")
    assert gh.remote.endswith(".git")


def test_github_client_key_returns_namespaced_key():
    gh = GitHub("ZabelTech/foo")
    assert gh.key(5) == "ZabelTech/foo#5"
    assert gh.key(1) == "ZabelTech/foo#1"


def test_github_client_slug_n_returns_filesystem_safe_slug():
    gh = GitHub("ZabelTech/foo")
    assert gh.slug_n(5) == "ZabelTech-foo-5"
    assert gh.slug_n(1) == "ZabelTech-foo-1"


def test_github_client_two_repos_same_issue_distinct_keys():
    gh_a = GitHub("org/repo-a")
    gh_b = GitHub("org/repo-b")
    # Same issue number → different store keys and filesystem slugs
    assert gh_a.key(5) != gh_b.key(5)
    assert gh_a.slug_n(5) != gh_b.slug_n(5)
    assert gh_a.key(5) == "org/repo-a#5"
    assert gh_b.key(5) == "org/repo-b#5"


def test_github_client_rejects_invalid_repo_format():
    with pytest.raises(ValueError):
        GitHub("noslash")


def test_discover_repos_paginates_and_filters_archived(monkeypatch):
    """discover_repos returns non-archived repos from all pages."""
    page1 = [
        {"full_name": "ZabelTech/active-1", "archived": False},
        {"full_name": "ZabelTech/archived-1", "archived": True},
    ]
    # 100 items on page1 signals there may be more; only 1 on page2 signals last page.
    page1_full = page1 + [{"full_name": f"ZabelTech/repo-{i}", "archived": False}
                           for i in range(98)]  # total 100 items
    page2 = [{"full_name": "ZabelTech/active-2", "archived": False}]

    responses = [page1_full, page2]
    call_count = [0]

    class FakeResp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    class FakeHeaders(dict):
        """A dict subclass whose 'update' attribute can be replaced."""
        pass

    class FakeSession:
        def __init__(self):
            self.headers = FakeHeaders()

        def get(self, url, params=None, **kw):
            idx = call_count[0]
            call_count[0] += 1
            return FakeResp(responses[idx])

    import orchestrator.github_client as gc
    fake_session = FakeSession()
    monkeypatch.setattr(gc.requests, "Session", lambda: fake_session)

    result = discover_repos("ZabelTech", "fake-token")

    # Should have 2 pages of results (page1_full has 100 items → triggers page 2)
    assert call_count[0] == 2
    # archived-1 is dropped; active-1, 98 active repos from page1, and active-2 from page2
    assert "ZabelTech/archived-1" not in result
    assert "ZabelTech/active-1" in result
    assert "ZabelTech/active-2" in result
    # Total: 99 from page1 (100 - 1 archived) + 1 from page2 = 100
    assert len(result) == 100
