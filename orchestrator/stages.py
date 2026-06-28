"""Per-stage logic + the flow dispatcher.

Each handler reads the issue's current flow:* state, runs the right model
(chosen by the router under the budget gate), takes GitHub actions, and
transitions the flow label. Per-issue detail (branch, PR#, review round,
implementer family, paused guidance) lives in store.issue_meta.
"""
import math
from . import config, prompts, repo, runners, router
from .github_client import is_bot_comment
from .runners import RateLimited
from .store import issue_meta, update_issue_meta


class BudgetParked(Exception):
    """No pool has headroom for this stage right now."""


def _model_label(stage, fam):
    """The model name for a stage's bot-comment marker."""
    return config.GLM_MODEL_BY_STAGE[stage] if fam == "glm" else "claude"


def _specs_text(specs):
    parts = []
    for s in specs:
        items = "\n".join(f"- {wi.get('title','')}" for wi in s.get("work_items", []))
        parts.append(f"### {s.get('title','')}\n{s.get('body','')}\n{items}")
    return "\n\n".join(parts)


def _record(ledger, fam, res):
    if fam == "claude":
        ledger.record("claude", tokens=res.input_tokens + res.output_tokens)
    else:
        ledger.record("glm", prompts=math.ceil(config.GLM_QUOTA_MULTIPLIER))


def _run(stage, ledger, prompt, cwd, exclude_family=None, write=False):
    """Route -> run -> meter. Raises BudgetParked if nothing has headroom."""
    fam = router.choose_family(stage, ledger, exclude_family=exclude_family)
    if fam is None:
        raise BudgetParked()
    runner = runners.get_runner(fam)
    model = config.GLM_MODEL_BY_STAGE[stage] if fam == "glm" else None
    res = runner.run(prompt, cwd=cwd, write=write, model=model,
                     effort=config.EFFORT_BY_STAGE[stage])
    _record(ledger, fam, res)
    return fam, res


def _pause(gh, n, reason):
    gh.add_labels(n, [config.FLAG_NEEDS_HUMAN])
    gh.comment(n, f"⏸️ **Needs a human call.**\n\n{reason}\n\n"
                  f"Reply with your decision, then add the `{config.SIG_RESOLVED}` "
                  f"label to resume.")


# ── Stage handlers ─────────────────────────────────────────────────────────
def handle_summarize(gh, ledger, issue):
    n = issue["number"]
    comments = gh.list_comments(n)
    human = "\n---\n".join(c["body"] for c in comments
                           if not is_bot_comment(c)) or "(none)"
    # Mark every comment seen so far as acted-on, so a NEW human comment later —
    # at the clarify OR the approval gate — is detected as fresh input to revise.
    update_issue_meta(n, last_comment_seen=max((c["id"] for c in comments), default=0))
    prompt = prompts.render(prompts.SUMMARIZE, NUM=n, TITLE=issue["title"],
                            BODY=issue.get("body") or "(no description)",
                            CLARIFICATIONS=human)
    # Run inside a checkout so the model can investigate the repo, not just /data.
    fam, res = _run("summarize", ledger, prompt, cwd=repo.ensure_repo(n))
    model, effort = _model_label("summarize", fam), config.EFFORT_BY_STAGE["summarize"]
    d = res.data
    if d.get("status") == "ready":
        gh.comment(n, f"📋 **Summary**\n\n{d.get('summary','')}\n\n"
                      f"If this looks right, add the `{config.SIG_APPROVE}` label to approve "
                      f"— or just comment to ask for changes.",
                   model=model, effort=effort)
        gh.set_flow(n, config.FLOW_APPROVAL, issue)
    else:
        qs = "\n".join(f"- {q}" for q in d.get("questions", [])) or "- (clarify)"
        gh.comment(n, f"📋 **Summary (draft)**\n\n{d.get('summary','')}\n\n"
                      f"**A few questions before I spec this:**\n{qs}",
                   model=model, effort=effort)
        gh.set_flow(n, config.FLOW_CLARIFY, issue)


def handle_clarify(gh, ledger, issue):
    """Re-summarize once the human replies."""
    n = issue["number"]
    latest = gh.latest_human_comment(n)
    seen = issue_meta(n).get("last_comment_seen", 0)
    if latest and latest["id"] > seen:
        gh.set_flow(n, config.FLOW_SUMMARIZE, issue)   # re-run next tick with the reply


def handle_spec(gh, ledger, issue):
    n = issue["number"]
    path = repo.ensure_repo(n)
    summary = _last_bot_summary(gh, n)
    guidance = issue_meta(n).get("human_guidance")
    if guidance:
        summary += f"\n\nHUMAN GUIDANCE:\n{guidance}"
    fam, res = _run("spec", ledger, prompts.render(prompts.SPEC, NUM=n,
                    TITLE=issue["title"], SUMMARY=summary), cwd=path)
    d = res.data
    if d.get("status") != "ready" or not d.get("specs"):
        _pause(gh, n, d.get("reason") or "Spec generation needs input.")
        return
    specs = d["specs"]
    # diversity-of-thought: a different family reviews the spec
    _, rev = _run("review", ledger, prompts.render(prompts.SPEC_REVIEW,
                  SPECS=_specs_text(specs)), cwd=path, exclude_family=fam)
    concerns = rev.data.get("concerns") or []
    if rev.data.get("verdict") == "concerns" and concerns:
        _pause(gh, n, "Spec review raised concerns:\n" +
               "\n".join(f"- {c}" for c in concerns))
        update_issue_meta(n, pending_specs=specs)
        return
    _commit_specs_and_branch(gh, n, path, specs)


def _commit_specs_and_branch(gh, n, path, specs):
    base = gh.default_branch()
    branch = f"pipeline/issue-{n}"
    repo.checkout_branch(path, branch, base)
    repo.write_specs(path, n, specs)
    repo.push(path, branch)
    update_issue_meta(n, branch=branch, specs=specs, review_round=0,
                      human_guidance=None, pending_specs=None)
    gh.set_flow(n, config.FLOW_IMPLEMENT, gh.get_issue(n))
    gh.comment(n, f"🛠️ Spec'd into {len(specs)} spec(s) on `{branch}`. Implementing.")


def handle_implement(gh, ledger, issue):
    n = issue["number"]
    meta = issue_meta(n)
    path = repo.ensure_repo(n)
    repo.checkout_branch(path, meta["branch"], gh.default_branch())
    rnd = meta.get("review_round", 0)
    if rnd > 0 and meta.get("last_feedback"):           # fix iteration
        prompt = prompts.render(prompts.FIX, NUM=n, ROUND=rnd,
                                FEEDBACK=meta["last_feedback"])
    else:
        prompt = prompts.render(prompts.IMPLEMENT, NUM=n)
    fam, res = _run("implement", ledger, prompt, cwd=path, write=True)
    if res.data.get("status") != "done":
        _pause(gh, n, res.data.get("reason") or "Implementation hit a blocker.")
        return
    repo.commit_all(path, f"implement #{n}" + (f" (fix round {rnd})" if rnd else ""))
    repo.push(path, meta["branch"])
    pr = gh.pull_for_branch(meta["branch"])
    if not pr:
        body = (f"{res.data.get('summary','')}\n\nCloses #{n}\n\n"
                f"_Specs: `specs/{n}/`. Implemented by **{fam}**._")
        pr = gh.create_pull(title=issue["title"], head=meta["branch"],
                            base=gh.default_branch(), body=body)
    update_issue_meta(n, pr_number=pr["number"], implementer=fam)
    gh.set_flow(n, config.FLOW_REVIEW, issue)


def handle_review(gh, ledger, issue):
    n = issue["number"]
    meta = issue_meta(n)
    pr = gh.get_pull(meta["pr_number"])
    diff = gh.pull_diff(meta["pr_number"])[:60000]      # keep prompt bounded
    summary = _last_bot_summary(gh, n)
    prompt = prompts.render(prompts.REVIEW, NUM=n, TITLE=issue["title"],
                            SUMMARY=summary, SPECS=_specs_text(meta.get("specs", [])),
                            DIFF=diff)
    # cross-check: review with the OTHER family than implemented
    fam, res = _run("review", ledger, prompt, cwd=repo.workdir(n),
                    exclude_family=meta.get("implementer"))
    model, effort = _model_label("review", fam), config.EFFORT_BY_STAGE["review"]
    status = res.data.get("status")
    if status == "approve":
        gh.comment(n, f"✅ **Review passed** (by {fam}).\n\n{res.data.get('summary','')}",
                   model=model, effort=effort)
        gh.set_flow(n, config.FLOW_MERGE, issue)
    elif status == "request_changes":
        rnd = meta.get("review_round", 0) + 1
        if rnd > config.MAX_REVIEW_ROUNDS:
            _pause(gh, n, f"Still failing review after {config.MAX_REVIEW_ROUNDS} rounds. "
                          "Take a look.")
            return
        fb = "\n".join(f"- {c}" for c in res.data.get("comments", [])) or \
             res.data.get("summary", "")
        gh.comment(n, f"🔁 **Changes requested** (round {rnd}, by {fam}):\n{fb}",
                   model=model, effort=effort)
        update_issue_meta(n, review_round=rnd, last_feedback=fb)
        gh.set_flow(n, config.FLOW_IMPLEMENT, issue)
    else:
        _pause(gh, n, res.data.get("reason") or "Review flagged a judgement call.")


def handle_merge(gh, ledger, issue):
    n = issue["number"]
    pr = gh.get_pull(issue_meta(n)["pr_number"])
    if pr.get("merged"):
        gh.set_flow(n, config.FLOW_DONE, issue)
        gh.comment(n, "🎉 Merged. Done.")
        return
    if config.AUTO_MERGE and pr.get("mergeable") and pr.get("mergeable_state") == "clean":
        gh.merge_pull(pr["number"])
        gh.set_flow(n, config.FLOW_DONE, issue)
        gh.comment(n, "🎉 Auto-merged on green. Done.")
    # else: wait for a human to click merge


# ── helpers ────────────────────────────────────────────────────────────────
def _last_bot_summary(gh, n):
    for c in reversed(gh.list_comments(n)):
        if is_bot_comment(c) and "Summary" in c["body"]:
            return c["body"]
    return "(summary unavailable)"


DISPATCH = {
    config.FLOW_SUMMARIZE: handle_summarize,
    config.FLOW_CLARIFY: handle_clarify,
    config.FLOW_SPEC: handle_spec,
    config.FLOW_IMPLEMENT: handle_implement,
    config.FLOW_REVIEW: handle_review,
    config.FLOW_MERGE: handle_merge,
}
