"""Tests for repo.py worktree support.

Tests worktree path isolation, primary-detach-before-add behavior, and worktree
cleanup. Hermetic — no real git calls (git commands are captured/stubbed).
"""
from unittest.mock import MagicMock
from orchestrator import repo


def test_workdir_spec_returns_isolated_sibling_paths():
    """workdir_spec returns distinct paths per slug, outside the primary checkout."""
    assert repo.workdir_spec(5, "implement") == "/data/work/worktrees/issue-5/implement"
    assert repo.workdir_spec(5, "review") == "/data/work/worktrees/issue-5/review"
    # Different issues get different containers.
    assert repo.workdir_spec(7, "implement") == "/data/work/worktrees/issue-7/implement"


def test_workdir_spec_does_not_nest_inside_primary():
    """worktree paths are siblings to the primary, not nested under it.

    The approved summary wrote `/data/work/issue-N/<slug>`, but that would
    nest worktrees inside the primary checkout, making git operations on the
    primary treat worktrees as untracked junk. Using `worktrees/issue-N/<slug>`
    keeps each spec tree cleanly isolated.
    """
    primary = repo.workdir(5)  # /data/work/issue-5
    worktree = repo.workdir_spec(5, "implement")  # /data/work/worktrees/issue-5/implement

    # worktree is NOT a descendant of the primary.
    assert not worktree.startswith(primary)
    # Both share the same WORKDIR_ROOT parent.
    assert primary.startswith("/data/work")
    assert worktree.startswith("/data/work")


def test_ensure_worktree_detaches_primary_before_add(monkeypatch):
    """ensure_worktree leaves the primary on a non-spec ref BEFORE worktree add.

    The load-bearing fix: after _publish_specs, the primary sits on the last
    spec branch. `git worktree add` refuses to check out a branch already
    checked out in another worktree. We must detach the primary first.
    """
    git_calls = []

    def fake_git(args, cwd, check=True, capture_output=True):
        git_calls.append((args, cwd))

    monkeypatch.setattr(repo, "_git", fake_git)
    monkeypatch.setattr(repo, "ensure_repo", lambda n: f"/data/work/issue-{n}")

    path = repo.ensure_worktree(42, "my-spec", "pipeline/issue-42/my-spec", "main")

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
    primary_path = "/data/work/issue-42"
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
    git_calls = []

    def fake_git(args, cwd, check=True, capture_output=True):
        git_calls.append((args, cwd))

    monkeypatch.setattr(repo, "_git", fake_git)
    monkeypatch.setattr(repo, "ensure_repo", lambda n: f"/data/work/issue-{n}")

    repo.ensure_worktree(42, "my-spec", "pipeline/issue-42/my-spec", "main")

    # First git call must be fetch.
    assert git_calls[0][0] == ["fetch", "origin"]


def test_ensure_worktree_prunes_before_add(monkeypatch):
    """ensure_worktree calls 'worktree prune' before 'worktree add'."""
    git_calls = []

    def fake_git(args, cwd, check=True, capture_output=True):
        git_calls.append((args, cwd))

    monkeypatch.setattr(repo, "_git", fake_git)
    monkeypatch.setattr(repo, "ensure_repo", lambda n: f"/data/work/issue-{n}")

    repo.ensure_worktree(42, "my-spec", "pipeline/issue-42/my-spec", "main")

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
    git_calls = []

    def fake_git(args, cwd, check=True, capture_output=True):
        git_calls.append((args, cwd))

    monkeypatch.setattr(repo, "_git", fake_git)
    monkeypatch.setattr(repo, "ensure_repo", lambda n: f"/data/work/issue-{n}")

    repo.remove_worktree(42, "/data/work/worktrees/issue-42/my-spec")

    # Should prune first, then remove.
    assert any("prune" in call[0] for call in git_calls)
    remove_calls = [call for call in git_calls if "worktree" in call[0] and "remove" in call[0]]
    assert len(remove_calls) >= 1
    # The remove call must include --force and the path.
    remove_call = remove_calls[0]
    assert "--force" in remove_call[0]
    assert "/data/work/worktrees/issue-42/my-spec" in remove_call[0]


def test_per_issue_git_lock_isolation():
    """Each issue gets its own lock — concurrent ops on different issues don't block."""
    import threading
    import time

    lock1 = repo._repo_lock_for(1)
    lock2 = repo._repo_lock_for(2)

    # Different issues should have different locks.
    assert lock1 is not lock2
    assert id(lock1) != id(lock2)

    # Same issue should get the same lock (cached).
    lock1_again = repo._repo_lock_for(1)
    assert lock1 is lock1_again
