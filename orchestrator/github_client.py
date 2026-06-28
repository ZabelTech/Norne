"""Thin GitHub REST client (stdlib + requests). Only what the pipeline needs."""
import requests
from . import config

API = "https://api.github.com"


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

    def comment(self, n, body):
        self.s.post(self._u(f"/issues/{n}/comments"),
                    json={"body": body}).raise_for_status()

    def list_comments(self, n):
        return self._get(f"/issues/{n}/comments", params={"per_page": 100})

    def latest_human_comment(self, n):
        """Most recent comment NOT authored by the bot. None if last word was ours."""
        best = None
        for c in self.list_comments(n):
            if c["user"]["login"].lower() == config.BOT_LOGIN:
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
