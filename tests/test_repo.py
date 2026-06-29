"""Tests for repo.py worktree support.

Tests worktree path isolation, primary-detach-before-add behavior, and worktree
cleanup. Hermetic — no real git calls (git commands are captured/stubbed).
"""
import os
from unittest.mock import MagicMock
from orchestrator import config, repo
from orchestrator.github_client import GitHub


# Fake GitHub client for testing
class FakeGH:
    def __init__(self, repo_slug="owner/repo"):
        self.repo = repo_slug
        if "/" not in repo_slug:
            raise ValueError(f"Invalid repo format: {repo_slug}. Expected 'owner/name'.")
        self.owner, self.name = repo_slug.split("/", 1)
        self.remote = f"https://github.com/{repo_slug}.git"

    def slug_n(self, n):
        return f"{self.owner}-{self.name}-{n}"


def test_workdir_spec_returns_isolated_sibling_paths():
    """workdir_spec returns distinct paths per slug, outside the primary checkout."""
    gh = FakeGH("owner/repo")
    root = config.WORKDIR_ROOT
    assert repo.workdir_spec(gh, 5, "implement") == os.path.join(root, "worktrees", "owner-repo-5", "implement")
    assert repo.workdir_spec(gh, 5, "review") == os.path.join(root, "worktrees", "owner-repo-5", "review")
    # Different issues get different containers.
    assert repo.workdir_spec(gh, 7, "implement") == os.path.join(root, "worktrees", "owner-repo-7", "implement")


def test_workdir_spec_does_not_nest_inside_primary():
    """worktree paths are siblings to the primary, not nested under it.

    The approved summary wrote `<root>/issue-N/<slug>`, but that would nest
    worktrees inside the primary checkout, making git operations on the primary
    treat worktrees as untracked junk. Using `<root>/worktrees/issue-N/<slug>`
    keeps each spec tree cleanly isolated.
    """
    gh = FakeGH("owner/repo")
    primary = repo.workdir(gh, 5)                      # <root>/owner-repo-5
    worktree = repo.workdir_spec(gh, 5, "implement")   # <root>/worktrees/owner-repo-5/implement

    # worktree is NOT a descendant of the primary.
    assert not worktree.startswith(primary + os.sep)
    # Both live under the same workdir root.
    assert primary.startswith(config.WORKDIR_ROOT)
    assert worktree.startswith(config.WORKDIR_ROOT)


def test_ensure_worktree_detaches_primary_before_add(monkeypatch):
    """ensure_worktree leaves the primary on a non-spec ref BEFORE worktree add.

    The load-bearing fix: after _publish_specs, the primary sits on the last
    spec branch. `git worktree add` refuses to check out a branch already
    checked out in another worktree. We must detach the primary first.
    """
    gh = FakeGH("owner/repo")
    git_calls = []

    def fake_git(args, cwd, check=True, capture_output=True):
        git_calls.append((args, cwd))

    monkeypatch.setattr(repo, "_git", fake_git)
    monkeypatch.setattr(repo, "ensure_repo", lambda gh, n: f"/data/work/owner-repo-{n}")

    path = repo.ensure_worktree(gh, 42, "my-spec", "pipeline/issue-42/my-spec", "main")

    # Sequence must be:
    # 1. fetch origin
    # 2. checkout --detach origin/<base> on the PRIMARY (this is the fix)
    # 3. worktree prune
    # 4. worktree add -B <branch> <path> origin/<branch>
    assert len(git_calls) >= 4

    # Check for the critical detach step.
    detach_calls = [call for call in git_calls if call[0] == ["checkout", "--detach", "origin/main"]]
    assert len(detach_calls) >= 1, "ensure_worktree must detach the primary before worktree add"

    # The detach must be on the PRIMARY checkout, not the worktree path.
    primary_path = "/data/work/owner-repo-42"
    detach_call = [c for c in git_calls if c[0][0] == "checkout" and "--detach" in c[0]][0]
    assert detach_call[1] == primary_path, "detach must run on the primary checkout"

    # Worktree add uses the worktree path, not the primary.
    add_calls = [call for call in git_calls if "worktree" in call[0] and "add" in call[0]]
    assert len(add_calls) >= 1
    # The worktree path should be the 4th argument (after 'worktree', 'add', '-B', branch).
    # We'll just verify the path appears somewhere in the args.
    add_call = add_calls[0]
    assert path in add_call[0], "worktree add must target the worktree path"


def test_ensure_worktree_fetches_origin_first(monkeypatch):
    """ensure_worktree fetches origin before any checkout/worktree ops."""
    gh = FakeGH("owner/repo")
    git_calls = []

    def fake_git(args, cwd, check=True, capture_output=True):
        git_calls.append((args, cwd))

    monkeypatch.setattr(repo, "_git", fake_git)
    monkeypatch.setattr(repo, "ensure_repo", lambda gh, n: f"/data/work/owner-repo-{n}")

    repo.ensure_worktree(gh, 42, "my-spec", "pipeline/issue-42/my-spec", "main")

    # First git call must be fetch.
    assert git_calls[0][0] == ["fetch", "origin"]


def test_ensure_worktree_prunes_before_add(monkeypatch):
    """ensure_worktree calls 'worktree prune' before 'worktree add'."""
    gh = FakeGH("owner/repo")
    git_calls = []

    def fake_git(args, cwd, check=True, capture_output=True):
        git_calls.append((args, cwd))

    monkeypatch.setattr(repo, "_git", fake_git)
    monkeypatch.setattr(repo, "ensure_repo", lambda gh, n: f"/data/work/owner-repo-{n}")

    repo.ensure_worktree(gh, 42, "my-spec", "pipeline/issue-42/my-spec", "main")

    prune_idx = None
    add_idx = None
    for i, (args, cwd) in enumerate(git_calls):
        if "prune" in args:
            prune_idx = i
        if "worktree" in args and "add" in args:
            add_idx = i

    assert prune_idx is not None, "worktree prune must be called"
    assert add_idx is not None, "worktree add must be called"
    assert prune_idx < add_idx, "prune must come before add"


def test_remove_worktree_issues_force_remove(monkeypatch):
    """remove_worktree calls 'worktree remove --force' on the path."""
    gh = FakeGH("owner/repo")
    git_calls = []

    def fake_git(args, cwd, check=True, capture_output=True):
        git_calls.append((args, cwd))

    monkeypatch.setattr(repo, "_git", fake_git)
    monkeypatch.setattr(repo, "ensure_repo", lambda gh, n: f"/data/work/owner-repo-{n}")

    repo.remove_worktree(gh, 42, "/data/work/worktrees/owner-repo-42/my-spec")

    # Should prune first, then remove.
    assert any("prune" in call[0] for call in git_calls)
    remove_calls = [call for call in git_calls if "worktree" in call[0] and "remove" in call[0]]
    assert len(remove_calls) >= 1
    # The remove call must include --force and the path.
    remove_call = remove_calls[0]
    assert "--force" in remove_call[0]
    assert "/data/work/worktrees/owner-repo-42/my-spec" in remove_call[0]


def test_two_repos_same_issue_number_have_distinct_workdirs():
    """Two repos with issue #5 must produce distinct workdir and worktree paths."""
    gh_a = FakeGH("org/repo-a")
    gh_b = FakeGH("org/repo-b")

    wd_a = repo.workdir(gh_a, 5)
    wd_b = repo.workdir(gh_b, 5)
    assert wd_a != wd_b
    assert "org-repo-a-5" in wd_a
    assert "org-repo-b-5" in wd_b

    wt_a = repo.workdir_spec(gh_a, 5, "my-spec")
    wt_b = repo.workdir_spec(gh_b, 5, "my-spec")
    assert wt_a != wt_b
    assert "org-repo-a-5" in wt_a
    assert "org-repo-b-5" in wt_b


def test_per_issue_git_lock_isolation():
    """Each issue gets its own lock — concurrent ops on different issues don't block."""
    import threading
    import time

    gh1 = FakeGH("owner/repo")
    gh2 = FakeGH("other/repo")
    gh3 = FakeGH("owner/repo")

    lock1 = repo._repo_lock_for(gh1, 1)
    lock2 = repo._repo_lock_for(gh2, 1)
    lock3 = repo._repo_lock_for(gh3, 1)

    # Same issue number in different repos should have different locks.
    assert lock1 is not lock2
    assert id(lock1) != id(lock2)

    # Same issue number in same repo should get the same lock (cached).
    lock1_again = repo._repo_lock_for(gh1, 1)
    assert lock1 is lock1_again

    # Same issue number in same repo (different client instance) should get the same lock.
    assert lock1 is lock3


# ── commit_all: something to commit path doesn't raise ─────────────────────────
def test_commit_all_with_something_to_commit(monkeypatch, tmp_path):
    """commit_all when there are staged changes must not raise.

    git diff --cached --quiet exits 1 when there ARE staged changes. With
    check=False (the default), this should not raise CalledProcessError.
    """
    from subprocess import CompletedProcess
    import os

    # Fake work path
    work_path = str(tmp_path / "work")
    os.makedirs(work_path)

    git_calls = []

    def fake_git(args, cwd, check=False, capture_output=True, text=True):
        git_calls.append((args, cwd, check))
        if "diff" in args and "--cached" in args and "--quiet" in args:
            # git diff --cached --quiet exits 1 when there ARE staged changes
            return CompletedProcess(["git"] + args, returncode=1, stdout="", stderr="")
        elif "commit" in args:
            return CompletedProcess(["git"] + args, returncode=0, stdout="", stderr="")
        return CompletedProcess(["git"] + args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(repo.subprocess, "run", fake_git)

    # This should NOT raise — check=False means returncode is inspected, not asserted.
    repo.commit_all(work_path, "test commit")

    # Verify the calls were made with check=False (default).
    diff_calls = [c for c in git_calls if "diff" in c[0] and "--cached" in c[0]]
    assert len(diff_calls) == 1
    # The check parameter should be False (not the check=True that would raise).
    assert diff_calls[0][2] is False

    # Verify commit was called (because there was something to commit).
    commit_calls = [c for c in git_calls if "commit" in c[0]]
    assert len(commit_calls) == 1
