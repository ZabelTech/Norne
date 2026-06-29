"""Git working-copy helpers. One persistent checkout per issue on the volume.

Level 2 concurrency: per-spec worktrees let implement/review fan out to process
multiple spec branches of one issue concurrently, each in its own isolated tree.
"""
import os
import subprocess
import threading
from . import config

REMOTE = f"https://x-access-token:{config.GH_TOKEN}@github.com/{config.GH_REPO}.git"

# ── Per-issue git lock registry ───────────────────────────────────────────────
_repo_locks = {}
_repo_locks_lock = threading.Lock()


def _repo_lock_for(n):
    """Get or create a lock for this specific issue's repo operations.

    Uses RLock (reentrant) so a function holding the lock can call another
    function that needs the same lock (e.g., ensure_worktree -> remove_worktree).
    """
    with _repo_locks_lock:
        if n not in _repo_locks:
            _repo_locks[n] = threading.RLock()
        return _repo_locks[n]


def _git(args, cwd, check=True, capture_output=True):
    """Run a git command. Returns the CompletedProcess instance."""
    return subprocess.run(["git"] + args, cwd=cwd, check=check,
                         capture_output=capture_output, text=True)


def workdir(n):
    return os.path.join(config.WORKDIR_ROOT, f"issue-{n}")


def workdir_spec(n, slug):
    """Path for a spec-branch worktree — a sibling container, not nested.

    The approved summary wrote `/data/work/issue-N/<slug>`, but that would nest
    the worktree inside the primary checkout, making the primary's own `git add -A`
    and `git status` (used by commit_all/dirty_files) treat the worktree as
    untracked junk. Using `worktrees/issue-N/<slug>` keeps each spec tree cleanly
    isolated outside the primary while preserving the approved intent.
    """
    return os.path.join(config.WORKDIR_ROOT, "worktrees", f"issue-{n}", slug)


def ensure_repo(n):
    """Clone if missing; return the checkout path."""
    path = workdir(n)
    if not os.path.isdir(os.path.join(path, ".git")):
        os.makedirs(config.WORKDIR_ROOT, exist_ok=True)
        subprocess.run(["git", "clone", REMOTE, path], capture_output=True, text=True)
        _git(["config", "user.name", "pipeline-bot"], path)
        _git(["config", "user.email", "pipeline-bot@users.noreply.github.com"], path)
    return path


def checkout_branch(path, branch, base):
    _git(["fetch", "origin"], path)
    _git(["checkout", base], path)
    _git(["pull", "origin", base], path)
    # create from base, or switch to it if it already exists
    if _git(["checkout", "-b", branch, f"origin/{base}"], path).returncode != 0:
        _git(["checkout", branch], path)
        _git(["pull", "origin", branch], path)


def write_spec(path, n, spec, idx=0):
    """Write ONE spec under specs/<n>/<slug>.md and commit it — each spec lives on
    its own branch, so the branch carries only its own spec file. Returns the slug."""
    d = os.path.join(path, "specs", str(n))
    os.makedirs(d, exist_ok=True)
    slug = spec.get("slug") or f"spec-{idx + 1}"
    body = spec.get("body", "")
    items = "\n".join(f"- [ ] {wi.get('title','')}" for wi in spec.get("work_items", []))
    with open(os.path.join(d, f"{slug}.md"), "w") as f:
        f.write(f"# {spec.get('title','')}\n\n{body}\n\n## Work items\n{items}\n")
    commit_all(path, f"spec '{slug}' for #{n}")
    return slug


def dirty_files(path):
    """Paths with uncommitted changes (git porcelain) — the work an agent left
    behind in the checkout when it stopped before we committed."""
    out = _git(["status", "--porcelain"], path).stdout.strip()
    return [ln[3:] for ln in out.splitlines() if ln.strip()] if out else []


def commit_all(path, msg):
    _git(["add", "-A"], path)
    # commit only if there's something staged
    if _git(["diff", "--cached", "--quiet"], path).returncode != 0:
        _git(["commit", "-m", msg], path)


def push(path, branch):
    return _git(["push", "-u", "origin", branch], path)


def ensure_worktree(n, slug, branch, base):
    """Ensure a clean worktree for this spec branch exists and return its path.

    Under the per-issue git lock: ensure the primary clone exists, fetch, detach
    the primary onto the base ref (so NO spec branch is checked out there — the
    load-bearing fix that prevents 'branch already checked out' errors), prune
    stale worktrees, then add or reset the worktree. Idempotent across crashes.
    """
    lock = _repo_lock_for(n)
    with lock:
        primary = ensure_repo(n)                      # primary checkout hosts worktrees
        _git(["fetch", "origin"], primary)

        # Move the primary OFF the spec branch — otherwise `git worktree add`
        # refuses with "branch already checked out in another worktree".
        _git(["checkout", "--detach", f"origin/{base}"], primary)

        # Clean up any stale worktree registration.
        _git(["worktree", "prune"], primary)

        path = workdir_spec(n, slug)
        # If path exists, remove it first (idempotent).
        remove_worktree(n, path)  # runs inside the same lock

        # Create (or reset) the worktree from the branch.
        # `-B` creates or resets the local branch from origin — authoritative
        # since every commit is pushed immediately.
        _git(["worktree", "add", "-B", branch, path, f"origin/{branch}"], primary)
        return path


def remove_worktree(n, path):
    """Remove a worktree, ignoring if it doesn't exist (bounded cleanup)."""
    lock = _repo_lock_for(n)
    with lock:
        primary = ensure_repo(n)
        # Prune first to clear stale registrations.
        _git(["worktree", "prune"], primary)
        # Force-remove the worktree; ignore errors if path already gone.
        _git(["worktree", "remove", "--force", path], primary,
             check=False, capture_output=True)
