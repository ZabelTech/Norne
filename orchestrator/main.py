"""Poll loop. One Machine, one writer over the GitHub state machine.

The poll loop never blocks on a model call: it just discovers triggered/in-flight
issues and hands each to a bounded worker pool (Level 1 concurrency). The
shared Ledger + per-path store locks preserve correctness under concurrency
(the budget reservation gate prevents window overrun, and the inflight guard
ensures at-most-one-worker-per-issue). The 429 backstop funnels into a
`blocked:budget` park.
"""
import concurrent.futures
import threading
import time
import traceback

from . import config
from .github_client import GitHub, discover_repos
from .store import Ledger, issue_meta, update_issue_meta, migrate_legacy_keys
from .runners import RateLimited
from .stages import DISPATCH, BudgetParked
from .log import log


def process_issue(gh, ledger, issue):
    n = issue["number"]
    labels = gh.labels_of(issue)
    flow = next((l for l in labels if l in config.ALL_FLOW), None)
    key = gh.key(n)

    # Parked on budget — resume when any pool frees up.
    if config.FLAG_BLOCKED_BUDGET in labels:
        if ledger.headroom("claude") or ledger.headroom("glm"):
            gh.remove_label(n, config.FLAG_BLOCKED_BUDGET)
            labels.discard(config.FLAG_BLOCKED_BUDGET)
            log(f"[{key}] budget freed -> resuming")
        else:
            log(f"[{key}] skip: blocked:budget (no pool has headroom)")
            return

    # Paused on a human judgement call. Resume when you EITHER add the
    # `human:resolved` label OR simply post a new comment — your latest comment
    # becomes the guidance fed into the re-run (the next round).
    if config.FLAG_NEEDS_HUMAN in labels:
        latest = gh.latest_human_comment(n)
        new_comment = bool(latest) and latest["id"] > issue_meta(key).get("last_comment_seen", 0)
        if config.SIG_RESOLVED in labels or new_comment:
            if latest:
                update_issue_meta(key, human_guidance=latest["body"])
            gh.remove_label(n, config.FLAG_NEEDS_HUMAN)
            if config.SIG_RESOLVED in labels:
                gh.remove_label(n, config.SIG_RESOLVED)
            log(f"[{key}] human:needed resolved ({'label' if config.SIG_RESOLVED in labels else 'comment'})"
                f" -> resuming {flow}")
        else:
            log(f"[{key}] skip: human:needed (waiting for your reply or human:resolved)")
            return

    # New issue entering via the trigger label.
    if flow is None:
        if config.TRIGGER_LABEL in labels:
            gh.set_flow(n, config.FLOW_SUMMARIZE)
            flow = config.FLOW_SUMMARIZE
            log(f"[{key}] triggered -> flow:summarize")
        else:
            return

    if flow == config.FLOW_DONE:
        log(f"[{key}] skip: flow:done")
        return

    # Approval gate. Add the `approve` label to proceed to spec; OR just comment
    # to ask for changes — a new human comment sends it back to summarize so the
    # feedback is folded into a revised summary.
    if flow == config.FLOW_APPROVAL:
        if config.SIG_APPROVE in labels:
            gh.remove_label(n, config.SIG_APPROVE)
            gh.set_flow(n, config.FLOW_SPEC)
            flow = config.FLOW_SPEC
            log(f"[{key}] approved -> flow:spec")
        else:
            latest = gh.latest_human_comment(n)
            if latest and latest["id"] > issue_meta(key).get("last_comment_seen", 0):
                gh.set_flow(n, config.FLOW_SUMMARIZE, issue)
                log(f"[{key}] approval: new comment -> back to flow:summarize")
            else:
                log(f"[{key}] skip: flow:approval (waiting for `approve` or a comment)")
            return

    handler = DISPATCH.get(flow)
    if not handler:
        log(f"[{key}] skip: no handler for {flow}")
        return
    log(f"[{key}] {flow} — start")
    try:
        handler(gh, ledger, issue)
    except (RateLimited, BudgetParked) as e:
        gh.add_labels(n, [config.FLAG_BLOCKED_BUDGET])
        log(f"[{key}] parked: blocked:budget ({getattr(e, 'pool', 'all pools tapped')})")
    except Exception as e:                       # never let one issue kill the loop
        log(f"[{key}] ERROR in {flow}: {e}")
        traceback.print_exc()


def candidates(gh):
    """Open issues that are triggered or already in flight."""
    out = []
    for issue in gh.list_issues(state="open"):
        labels = gh.labels_of(issue)
        in_flight = labels & (config.ALL_FLOW - {config.FLOW_DONE})
        if config.TRIGGER_LABEL in labels or in_flight:
            out.append(issue)
    return out


def _done_callback(key, inflight, lock, t0):
    """Called when a process_issue task completes: remove from inflight and log."""
    def callback(fut):
        try:
            fut.result()  # re-raises if the task raised
            log(f"[{key}] worker done ({int(time.time() - t0)}s)")
        except Exception as e:
            log(f"[{key}] worker error: {e}")
            traceback.print_exc()
        finally:
            with lock:
                inflight.discard(key)
    return callback


def dispatch_once(gh_clients, executor, ledger, inflight, lock):
    """One poll iteration: find candidates across all repos, dispatch up to MAX_WORKERS, log.

    Extracted as a testable unit — the tests drive ONE iteration without
    the infinite loop.

    Args:
        gh_clients: List of GitHub client instances (one per repo)
        executor: ThreadPoolExecutor for running workers
        ledger: Shared Ledger instance
        inflight: Set of issue keys currently being processed
        lock: Threading lock for inflight set
    """
    cands = []
    for gh in gh_clients:
        for issue in candidates(gh):
            cands.append((gh, issue))
    dispatched, busy = [], []
    for gh, issue in cands:
        n = issue["number"]
        key = gh.key(n)
        with lock:
            if key in inflight:                # already queued or in progress
                busy.append(key)
                continue
            inflight.add(key)
        t0 = time.time()
        fut = executor.submit(process_issue, gh, ledger, issue)
        fut.add_done_callback(_done_callback(key, inflight, lock, t0))
        dispatched.append(key)
    if cands:                                # only log polls that saw work
        candidates_str = [f'{gh.key(i["number"])}' for gh, i in cands]
        log(f"poll: candidates={candidates_str} "
            f"dispatched={dispatched} in-flight(skipped)={busy}")


def main():
    # Validate configuration at startup
    config.validate_config()

    # Run one-time migration of legacy bare-integer keys to namespaced keys
    migrated, skipped = migrate_legacy_keys(config.GH_REPO)
    if migrated:
        log(f"Migration: renamed {len(migrated)} legacy key(s) to namespaced format")
        for old_key, new_key in migrated:
            log(f"  '{old_key}' -> '{new_key}'")
    if skipped:
        log(f"Migration: skipped {len(skipped)} already-namespaced key(s)")

    # Build repo list: either auto-discover via GH_OWNER or fall back to GH_REPO
    repo_slugs = []
    last_discovered = []  # cache for discovery failures

    if config.GH_OWNER:
        try:
            repo_slugs = discover_repos(config.GH_OWNER, config.GH_TOKEN)
            last_discovered = list(repo_slugs)  # cache successful discovery
            log(f"Discovered {len(repo_slugs)} repos in org {config.GH_OWNER}")
        except Exception as e:
            log(f"Discovery failed for org {config.GH_OWNER}: {e}. "
                "Using last successful discovery list if available, otherwise skipping cycle.")
            if last_discovered:
                repo_slugs = last_discovered
                log(f"Using cached repo list: {len(repo_slugs)} repos")
            else:
                log("No cached repo list available — skipping this cycle")
    elif config.GH_REPO:
        repo_slugs = [config.GH_REPO]
        log(f"Single-repo mode: {config.GH_REPO}")
    else:
        # This should not happen due to validate_config(), but be defensive
        log("ERROR: Neither GH_OWNER nor GH_REPO is set. Cannot start.")
        return

    # Create one GitHub client per repo (caches the requests.Session per client)
    gh_clients = [GitHub(repo, config.GH_TOKEN) for repo in repo_slugs]

    log(f"orchestrator up — repos={repo_slugs} glm_runner={config.GLM_RUNNER} "
        f"tier={config.GLM_TIER} auto_merge={config.AUTO_MERGE} poll={config.POLL_INTERVAL}s "
        f"max_workers={config.MAX_WORKERS}")

    # One shared ledger for all workers — the reservation gate prevents races.
    ledger = Ledger()
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=config.MAX_WORKERS,
        thread_name_prefix="worker"
    )
    inflight = set()
    lock = threading.Lock()
    while True:
        try:
            # Re-discover repos each cycle (cheap, makes repo list runtime-configurable)
            if config.GH_OWNER:
                try:
                    repo_slugs = discover_repos(config.GH_OWNER, config.GH_TOKEN)
                    last_discovered = list(repo_slugs)
                    # Rebuild clients if the repo list changed
                    if len(repo_slugs) != len(gh_clients):
                        gh_clients = [GitHub(repo, config.GH_TOKEN) for repo in repo_slugs]
                        log(f"Repo list changed: now {len(repo_slugs)} repos")
                except Exception as e:
                    log(f"Discovery failed: {e}. Using cached list.")
                    if last_discovered and len(last_discovered) != len(gh_clients):
                        gh_clients = [GitHub(repo, config.GH_TOKEN) for repo in last_discovered]

            dispatch_once(gh_clients, executor, ledger, inflight, lock)
        except Exception as e:
            log(f"loop error: {e}")
            traceback.print_exc()
        time.sleep(config.POLL_INTERVAL)


if __name__ == "__main__":
    main()
