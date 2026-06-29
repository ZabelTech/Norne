"""Git working-copy helpers. One persistent checkout per issue on the volume."""
import os
import subprocess
from . import config

REMOTE = f"https://x-access-token:{config.GH_TOKEN}@github.com/{config.GH_REPO}.git"


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=True)


def workdir(n):
    return os.path.join(config.WORKDIR_ROOT, f"issue-{n}")


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
