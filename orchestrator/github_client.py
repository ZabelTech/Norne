"""Thin GitHub REST client (stdlib + requests). Only what the pipeline needs."""
import re

import requests
from . import config

API = "https://api.github.com"

# Every comment the bot posts is tagged with this marker so it can recognise its
# OWN comments — independent of which account/token posted them. This is what
# lets the clarify loop tell a human reply from its own draft even when the bot
# authenticates with the repo owner's personal token (shared identity).
_BOT_MARKER = re.compile(r"\[norne-[^\]]*\]")


def bot_marker(model="orchestrator", effort="na", tokens=None):
    """The trailing tag stamped on bot comments: `[norne-{model}-{effort}]`,
    with a `-{N}tok` suffix when the comment came from a metered model run."""
    tok = f"-{tokens}tok" if tokens is not None else ""
    return f"`[norne-{model}-{effort}{tok}]`"


def is_bot_comment(comment):
    """True if this comment carries the bot marker (i.e. the pipeline wrote it)."""
    return bool(_BOT_MARKER.search(comment.get("body") or ""))


class GitHub:
    def __init__(self, repo=config.GH_REPO, token=config.GH_TOKEN):
        self.repo = repo
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })

    def _u(self, path):
        return f"{API}/repos/{self.repo}{path}"

    def _get(self, path, **kw):
        r = self.s.get(self._u(path), **kw)
        r.raise_for_status()
        return r.json()

    # ── Issues ────────────────────────────────────────────────────────────
    def list_issues(self, labels=None, state="open"):
        params = {"state": state, "per_page": 100}
        if labels:
            params["labels"] = ",".join(labels)
        out, page = [], 1
        while True:
            params["page"] = page
            batch = self._get("/issues", params=params)
            # /issues returns PRs too; drop them.
            out += [i for i in batch if "pull_request" not in i]
            if len(batch) < 100:
                break
            page += 1
        return out

    def get_issue(self, n):
        return self._get(f"/issues/{n}")

    def create_issue(self, title, body):
        r = self.s.post(self._u("/issues"), json={"title": title, "body": body})
        r.raise_for_status()
        return r.json()

    def add_sub_issue(self, parent_number, sub_issue_id):
        """Link an existing issue (by its numeric id) as a sub-issue of parent."""
        r = self.s.post(self._u(f"/issues/{parent_number}/sub_issues"),
                        json={"sub_issue_id": sub_issue_id})
        r.raise_for_status()
        return r.json()

    def close_issue(self, n):
        """Close an issue (used once every spec PR for it has merged)."""
        r = self.s.patch(self._u(f"/issues/{n}"), json={"state": "closed"})
        r.raise_for_status()
        return r.json()

    def labels_of(self, issue):
        return {l["name"] for l in issue.get("labels", [])}

    def add_labels(self, n, names):
        self.s.post(self._u(f"/issues/{n}/labels"),
                    json={"labels": names}).raise_for_status()

    def remove_label(self, n, name):
        r = self.s.delete(self._u(f"/issues/{n}/labels/{name}"))
        if r.status_code not in (200, 404):
            r.raise_for_status()

    def set_flow(self, n, flow_label, issue=None):
        """Move to a single flow:* state, clearing any other flow:* label.

        Always reads labels fresh (the passed-in issue dict may be stale after
        earlier label edits in the same tick)."""
        current = self.labels_of(self.get_issue(n))
        for l in current & config.ALL_FLOW:
            if l != flow_label:
                self.remove_label(n, l)
        if flow_label not in current:
            self.add_labels(n, [flow_label])

    def comment(self, n, body, model="orchestrator", effort="na", tokens=None):
        """Post a comment, tagged with the bot marker so we can recognise our own."""
        body = f"{body}\n\n{bot_marker(model, effort, tokens)}"
        self.s.post(self._u(f"/issues/{n}/comments"),
                    json={"body": body}).raise_for_status()

    def list_comments(self, n):
        return self._get(f"/issues/{n}/comments", params={"per_page": 100})

    def pull_review_comments(self, number):
        """Inline review comments on a PR's diff (separate from conversation comments)."""
        return self._get(f"/pulls/{number}/comments", params={"per_page": 100})

    def latest_human_comment(self, n):
        """Most recent comment NOT written by the bot. None if last word was ours.

        Identifies the bot by its comment marker (see is_bot_comment), so this
        is correct even when the bot posts under the same account as the human."""
        best = None
        for c in self.list_comments(n):
            if is_bot_comment(c):
                continue
            if best is None or c["id"] > best["id"]:
                best = c
        return best

    # ── Pull requests ─────────────────────────────────────────────────────
    def create_pull(self, title, head, base, body):
        r = self.s.post(self._u("/pulls"),
                        json={"title": title, "head": head, "base": base, "body": body})
        r.raise_for_status()
        return r.json()

    def get_pull(self, number):
        return self._get(f"/pulls/{number}")

    def pull_for_branch(self, head_branch):
        owner = self.repo.split("/")[0]
        res = self._get("/pulls", params={"head": f"{owner}:{head_branch}", "state": "all"})
        return res[0] if res else None

    def pull_diff(self, number):
        r = self.s.get(self._u(f"/pulls/{number}"),
                       headers={"Accept": "application/vnd.github.v3.diff"})
        r.raise_for_status()
        return r.text

    def default_branch(self):
        return self._get("")["default_branch"]

    def merge_pull(self, number, method="squash"):
        r = self.s.put(self._u(f"/pulls/{number}/merge"), json={"merge_method": method})
        r.raise_for_status()
        return r.json()
