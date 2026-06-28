"""Poll loop. One Machine, one writer over the GitHub state machine.

Each tick: find triggered/in-flight issues and advance each one step. The
budget gate (ledger) and the 429 backstop both funnel into a `blocked:budget`
park that auto-retries when a usage window resets.
"""
import time
import traceback

from . import config
from .github_client import GitHub
from .store import Ledger, issue_meta, update_issue_meta
from .runners import RateLimited
from .stages import DISPATCH, BudgetParked


def process_issue(gh, ledger, issue):
    n = issue["number"]
    labels = gh.labels_of(issue)
    flow = next((l for l in labels if l in config.ALL_FLOW), None)

    # Parked on budget — resume when any pool frees up.
    if config.FLAG_BLOCKED_BUDGET in labels:
        if ledger.headroom("claude") or ledger.headroom("glm"):
            gh.remove_label(n, config.FLAG_BLOCKED_BUDGET)
            labels.discard(config.FLAG_BLOCKED_BUDGET)
        else:
            return

    # Paused on a human judgement call — resume only after you say so.
    if config.FLAG_NEEDS_HUMAN in labels:
        if config.SIG_RESOLVED in labels:
            latest = gh.latest_human_comment(n)
            if latest:
                update_issue_meta(n, human_guidance=latest["body"])
            gh.remove_label(n, config.FLAG_NEEDS_HUMAN)
            gh.remove_label(n, config.SIG_RESOLVED)
        else:
            return

    # New issue entering via the trigger label.
    if flow is None:
        if config.TRIGGER_LABEL in labels:
            gh.set_flow(n, config.FLOW_SUMMARIZE)
            flow = config.FLOW_SUMMARIZE
        else:
            return

    if flow == config.FLOW_DONE:
        return

    # Approval gate. Add the `approve` label to proceed to spec; OR just comment
    # to ask for changes — a new human comment sends it back to summarize so the
    # feedback is folded into a revised summary.
    if flow == config.FLOW_APPROVAL:
        if config.SIG_APPROVE in labels:
            gh.remove_label(n, config.SIG_APPROVE)
            gh.set_flow(n, config.FLOW_SPEC)
            flow = config.FLOW_SPEC
        else:
            latest = gh.latest_human_comment(n)
            if latest and latest["id"] > issue_meta(n).get("last_comment_seen", 0):
                gh.set_flow(n, config.FLOW_SUMMARIZE, issue)
            return

    handler = DISPATCH.get(flow)
    if not handler:
        return
    try:
        handler(gh, ledger, issue)
    except (RateLimited, BudgetParked) as e:
        gh.add_labels(n, [config.FLAG_BLOCKED_BUDGET])
        print(f"[#{n}] parked (budget: {getattr(e, 'pool', 'all pools tapped')})")
    except Exception as e:                       # never let one issue kill the loop
        print(f"[#{n}] ERROR: {e}")
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


def main():
    gh = GitHub()
    print(f"orchestrator up — repo={config.GH_REPO} glm_runner={config.GLM_RUNNER} "
          f"tier={config.GLM_TIER} auto_merge={config.AUTO_MERGE}")
    while True:
        try:
            ledger = Ledger()
            for issue in candidates(gh):
                process_issue(gh, ledger, issue)
        except Exception as e:
            print(f"loop error: {e}")
            traceback.print_exc()
        time.sleep(config.POLL_INTERVAL)


if __name__ == "__main__":
    main()
