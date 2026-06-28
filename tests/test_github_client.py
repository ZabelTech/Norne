"""Bot-comment marker: emission + detection, and the human/bot split it drives.

The marker is what lets the pipeline recognise its OWN comments even when it
posts under the same GitHub account as the human (shared-identity token) — the
failure mode that caused the clarify loop to re-summarise forever.
"""
from orchestrator.github_client import GitHub, is_bot_comment, bot_marker


def _c(cid, body, login="someone"):
    return {"id": cid, "body": body, "user": {"login": login}}


def test_bot_marker_format():
    assert bot_marker("glm-4.7", "low") == "`[norne-glm-4.7-low]`"
    assert bot_marker() == "`[norne-orchestrator-na]`"


def test_is_bot_comment_detects_marker():
    assert is_bot_comment(_c(1, "Hi\n\n`[norne-glm-5.2-high]`")) is True
    assert is_bot_comment(_c(2, "a plain human reply")) is False
    assert is_bot_comment({"body": None}) is False
    assert is_bot_comment({}) is False


def test_comment_appends_marker(monkeypatch):
    gh = GitHub()
    captured = {}

    class FakeResp:
        def raise_for_status(self):
            pass

    def fake_post(url, json=None, **kw):
        captured["body"] = json["body"]
        return FakeResp()

    monkeypatch.setattr(gh.s, "post", fake_post)
    gh.comment(5, "Hello", model="glm-4.7", effort="low")
    assert captured["body"].startswith("Hello")
    assert captured["body"].rstrip().endswith("`[norne-glm-4.7-low]`")
    # round-trips: a comment we posted reads back as a bot comment
    assert is_bot_comment({"body": captured["body"]}) is True


def test_latest_human_comment_skips_bot_even_with_shared_login(monkeypatch):
    # Regression: bot and human are BOTH "alice" (bot posts under the owner's
    # token). The bot's drafts must still be skipped by marker.
    gh = GitHub()
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
    gh = GitHub()
    monkeypatch.setattr(gh, "list_comments",
                        lambda n: [_c(1, "draft\n\n`[norne-glm-4.7-low]`")])
    assert gh.latest_human_comment(7) is None
