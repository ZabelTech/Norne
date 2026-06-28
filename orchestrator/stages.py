"""Per-stage logic + the flow dispatcher.

Each handler reads the issue's current flow:* state, runs the right model
(chosen by the router under the budget gate), takes GitHub actions, and
transitions the flow label. Per-issue detail (branch, PR#, review round,
implementer family, paused guidance) lives in store.issue_meta.
"""
import math
import time
from . import config, prompts, repo, runners, router
from .github_client import is_bot_comment
from .log import log
from .runners import RateLimited
from .store import issue_meta, update_issue_meta


class BudgetParked(Exception):
    """No pool has headroom for this stage right now."""


def _model_for(stage, fam):
    """The model id for this stage+family — used both for the run and the marker."""
    table = config.GLM_MODEL_BY_STAGE if fam == "glm" else config.CLAUDE_MODEL_BY_STAGE
    return table[stage]


def _fmt_tokens(n):
    """Token total rounded to 2 significant figures, human-readable (e.g. 69ktok)."""
    n = int(n)
    if n <= 0:
        return "0tok"
    f = 10 ** (math.floor(math.log10(n)) - 1)        # round to 2 significant figures
    n = int(round(n / f) * f)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "Mtok"
    if n >= 1_000:
        return f"{n / 1_000:.1f}".rstrip("0").rstrip(".") + "ktok"
    return f"{n}tok"


def _participant(stage, fam, res):
    """`**model** (effort, Ntok)` for one stage run — for the review-round note."""
    model = _model_for(stage, fam)
    return (f"**{model}** ({config.effort_for(model)}, "
            f"{_fmt_tokens(res.input_tokens + res.output_tokens)})")


def _failure_reason(res, default):
    """A human-useful reason for an escalation. Prefer the model's own `reason`;
    if its output didn't parse into the json contract (no result block), surface
    its actual final message so the human sees what happened — not a blank
    'needs input'. Fall back to `default` only when there's genuinely nothing."""
    if res.data.get("reason"):
        return res.data["reason"]
    if not res.data:                              # nothing parsed out of the reply
        tail = (res.text or "").strip()[-1500:]
        if tail:
            return ("The agent finished without a valid result block — its output "
                    "didn't parse. Its final message:\n\n> " + tail.replace("\n", "\n> "))
    return default


def _discussion(gh, n, issue=None, pr_number=None):
    """The full discussion a stage should weigh: the issue description and every
    issue comment, plus the PR description and its conversation + inline review
    comments when a PR exists. Each comment is tagged (human) — the source of
    truth — or (bot) — prior pipeline output."""
    def fmt(cs):
        return "\n".join(
            f"- ({'bot' if is_bot_comment(c) else 'human'}) {(c.get('body') or '').strip()}"
            for c in cs)

    iss = issue or gh.get_issue(n)
    parts = [f"ISSUE #{n}: {iss.get('title', '')}\n{iss.get('body') or '(no description)'}"]
    issue_comments = gh.list_comments(n)
    if issue_comments:
        parts.append("ISSUE COMMENTS (oldest first):\n" + fmt(issue_comments))
    if pr_number:
        pr = gh.get_pull(pr_number)
        parts.append(f"PULL REQUEST #{pr_number}: {pr.get('title', '')}\n"
                     f"{pr.get('body') or '(no description)'}")
        pr_comments = gh.list_comments(pr_number) + gh.pull_review_comments(pr_number)
        if pr_comments:
            parts.append("PR COMMENTS:\n" + fmt(pr_comments))
    return "\n\n---\n\n".join(parts)


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
        log(f"  {stage}: no model family has budget headroom -> parking")
        raise BudgetParked()
    model = _model_for(stage, fam)
    effort = config.effort_for(model)
    log(f"  {stage}: calling {fam}/{model} ({effort})…")
    t0 = time.time()
    runner = runners.get_runner(fam)
    res = runner.run(prompt, cwd=cwd, write=write, model=model, effort=effort)
    _record(ledger, fam, res)
    log(f"  {stage}: {model} returned in {int(time.time() - t0)}s "
        f"({_fmt_tokens(res.input_tokens + res.output_tokens)}, ok={res.ok})")
    return fam, res


def _pause(gh, n, reason):
    gh.add_labels(n, [config.FLAG_NEEDS_HUMAN])
    gh.comment(n, f"⏸️ **Needs a human call.**\n\n{reason}\n\n"
                  f"Just reply with your decision to resume (or add the "
                  f"`{config.SIG_RESOLVED}` label).")
    # Baseline: any comment you post AFTER this is detected as fresh input that
    # resumes the paused stage — see process_issue's human:needed gate.
    update_issue_meta(n, last_comment_seen=max(
        (c["id"] for c in gh.list_comments(n)), default=0))


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
    model = _model_for("summarize", fam)
    effort = config.effort_for(model)
    d = res.data
    if d.get("status") == "ready":
        gh.comment(n, f"📋 **Summary**\n\n{d.get('summary','')}\n\n"
                      f"If this looks right, add the `{config.SIG_APPROVE}` label to approve "
                      f"— or just comment to ask for changes.",
                   model=model, effort=effort, tokens=res.input_tokens + res.output_tokens)
        gh.set_flow(n, config.FLOW_APPROVAL, issue)
        log(f"[#{n}] summarize -> ready, flow:approval")
    else:
        qs = "\n".join(f"- {q}" for q in d.get("questions", [])) or "- (clarify)"
        gh.comment(n, f"📋 **Summary (draft)**\n\n{d.get('summary','')}\n\n"
                      f"**A few questions before I spec this:**\n{qs}",
                   model=model, effort=effort, tokens=res.input_tokens + res.output_tokens)
        gh.set_flow(n, config.FLOW_CLARIFY, issue)
        log(f"[#{n}] summarize -> {len(d.get('questions', []))} question(s), flow:clarify")


def handle_clarify(gh, ledger, issue):
    """Re-summarize once the human replies."""
    n = issue["number"]
    latest = gh.latest_human_comment(n)
    seen = issue_meta(n).get("last_comment_seen", 0)
    if latest and latest["id"] > seen:
        gh.set_flow(n, config.FLOW_SUMMARIZE, issue)   # re-run next tick with the reply
        log(f"[#{n}] clarify: new reply -> flow:summarize")
    else:
        log(f"[#{n}] clarify: waiting for your reply")


def handle_spec(gh, ledger, issue):
    """ONE author->peer-review round per tick (cross-tick loop). On concerns it
    checkpoints the feedback in meta and stays at flow:spec to revise on the next
    tick; a clean verdict publishes (sub-issues + branch -> implement); reaching
    MAX_SPEC_ROUNDS without convergence escalates to a human. Running one round
    per tick (not all rounds inline) keeps the worker free between rounds so the
    orchestrator stays responsive and a paused issue can resume promptly."""
    n = issue["number"]
    path = repo.ensure_repo(n)
    meta = issue_meta(n)
    summary = _last_bot_summary(gh, n)
    guidance = meta.get("human_guidance")
    if guidance:
        summary += f"\n\nHUMAN GUIDANCE:\n{guidance}"
    discussion = _discussion(gh, n, issue=issue)
    rnd = meta.get("spec_round", 0) + 1
    feedback = meta.get("spec_feedback") or "(none)"

    fam, res = _run("spec", ledger, prompts.render(prompts.SPEC, NUM=n,
                    TITLE=issue["title"], SUMMARY=summary, DISCUSSION=discussion,
                    FEEDBACK=feedback), cwd=path)
    author = _participant("spec", fam, res)
    d = res.data
    if d.get("status") != "ready" or not d.get("specs"):
        # author stopped before review — a real judgement call, OR output that
        # didn't parse into the json contract (common on long agentic runs).
        _reset_spec_loop(n)
        why = "output didn't parse" if not d else "author stopped before review"
        log(f"[#{n}] spec round {rnd}: author stopped ({why}) -> escalate (human:needed)")
        _escalate_spec(gh, n, d.get("specs"),
                       _failure_reason(res, "Spec generation needs input."),
                       round_note=f"🔎 Spec round {rnd} — author {author} ({why}).")
        return
    specs = d["specs"]
    # the author's replies to last round's concerns (resolved or rebutted)
    responses = "\n".join(f"- {r}" for r in (d.get("responses") or [])) \
        or "(none — first review)"
    # diversity-of-thought: a DIFFERENT family peer-reviews the specs, weighing
    # the author's responses so a sound rebuttal isn't re-raised.
    rfam, rev = _run("review", ledger, prompts.render(prompts.SPEC_REVIEW,
                     SPECS=_specs_text(specs), AUTHOR_RESPONSES=responses,
                     DISCUSSION=discussion),
                     cwd=path, exclude_family=fam)
    reviewer = _participant("review", rfam, rev)
    note = f"🔎 Spec round {rnd} — author {author} · reviewer {reviewer}"
    concerns = rev.data.get("concerns") or []
    if rev.data.get("verdict") != "concerns" or not concerns:
        _reset_spec_loop(n)
        log(f"[#{n}] spec round {rnd}: reviewer approved -> publishing "
            f"{len(specs)} spec(s), flow:implement")
        _publish_specs(gh, n, path, specs, round_note=note)        # reviewer happy
        return
    if rnd >= config.MAX_SPEC_ROUNDS:
        _reset_spec_loop(n)
        log(f"[#{n}] spec round {rnd}: still {len(concerns)} concern(s) after "
            f"MAX_SPEC_ROUNDS -> escalate (human:needed):")
        for c in concerns:
            log(f"    • {str(c)[:200]}")
        _escalate_spec(gh, n, specs,
                       f"Spec review still has concerns after {config.MAX_SPEC_ROUNDS} "
                       "rounds — your call:\n" + "\n".join(f"- {c}" for c in concerns),
                       round_note=note)
        return
    # checkpoint: store the draft + concerns and revise on the next tick
    fb = (f"YOUR PREVIOUS DRAFT:\n{_specs_text(specs)}\n\n"
          f"PEER-REVIEW CONCERNS (round {rnd}) to resolve:\n"
          + "\n".join(f"- {c}" for c in concerns))
    update_issue_meta(n, spec_round=rnd, spec_feedback=fb)
    concern_list = "\n".join(f"- {c}" for c in concerns)
    gh.comment(n, f"🔁 **Peer review raised {len(concerns)} concern(s)** "
                  f"(round {rnd}) — revising next round:\n{concern_list}\n\n{note}")
    log(f"[#{n}] spec round {rnd}: {len(concerns)} concern(s) raised -> checkpoint, revising:")
    for c in concerns:
        log(f"    • {str(c)[:200]}")
    # stays at flow:spec -> the next tick runs round rnd+1


def _reset_spec_loop(n):
    update_issue_meta(n, spec_round=0, spec_feedback=None)


def _spec_sub_issues(gh, n, specs):
    """Post each spec as a sub-issue of #n (once). Best-effort; returns numbers."""
    meta = issue_meta(n)
    if meta.get("spec_sub_issues") is not None or not specs:
        return meta.get("spec_sub_issues") or []
    numbers = []
    for s in specs:
        items = "\n".join(f"- [ ] {wi.get('title','')}" for wi in s.get("work_items", []))
        body = f"{s.get('body','')}\n\n## Work items\n{items}\n\n_Spec for #{n}._"
        try:
            child = gh.create_issue(s.get("title") or f"Spec for #{n}", body)
            try:
                gh.add_sub_issue(n, child["id"])       # link as a sub-issue
            except Exception:
                pass                                    # created; linking is best-effort
            numbers.append(child["number"])
        except Exception:
            pass                                        # never let this break the stage
    update_issue_meta(n, spec_sub_issues=numbers)
    return numbers


def _publish_specs(gh, n, path, specs, round_note=""):
    """Reviewer is happy: post sub-issues, commit specs + branch, go implement."""
    subs = _spec_sub_issues(gh, n, specs)
    _commit_specs_and_branch(gh, n, path, specs, subs, round_note)


def _escalate_spec(gh, n, specs, reason, round_note=""):
    """Judgement call: post the proposed specs as sub-issues and pause for a human."""
    subs = _spec_sub_issues(gh, n, specs)
    if specs:
        update_issue_meta(n, pending_specs=specs)
    if subs:
        reason += ("\n\nProposed specs posted as sub-issues: "
                   + ", ".join(f"#{m}" for m in subs))
    if round_note:
        reason += f"\n\n{round_note}"
    _pause(gh, n, reason)


def _commit_specs_and_branch(gh, n, path, specs, subs=None, round_note=""):
    base = gh.default_branch()
    branch = f"pipeline/issue-{n}"
    repo.checkout_branch(path, branch, base)
    repo.write_specs(path, n, specs)
    repo.push(path, branch)
    update_issue_meta(n, branch=branch, specs=specs, review_round=0,
                      human_guidance=None, pending_specs=None)
    gh.set_flow(n, config.FLOW_IMPLEMENT, gh.get_issue(n))
    sub_txt = (" Sub-issues: " + ", ".join(f"#{m}" for m in subs)) if subs else ""
    body = f"🛠️ Spec'd into {len(specs)} spec(s) on `{branch}`.{sub_txt} Implementing."
    if round_note:
        body += f"\n\n{round_note}"
    gh.comment(n, body)


def handle_implement(gh, ledger, issue):
    n = issue["number"]
    meta = issue_meta(n)
    path = repo.ensure_repo(n)
    repo.checkout_branch(path, meta["branch"], gh.default_branch())
    rnd = meta.get("review_round", 0)
    discussion = _discussion(gh, n, issue=issue, pr_number=meta.get("pr_number"))
    if rnd > 0 and meta.get("last_feedback"):           # fix iteration
        prompt = prompts.render(prompts.FIX, NUM=n, ROUND=rnd,
                                FEEDBACK=meta["last_feedback"], DISCUSSION=discussion)
    else:
        prompt = prompts.render(prompts.IMPLEMENT, NUM=n, DISCUSSION=discussion)
    log(f"[#{n}] implement: {'fix round ' + str(rnd) if rnd else 'first pass'}")
    fam, res = _run("implement", ledger, prompt, cwd=path, write=True)
    if res.data.get("status") != "done":
        log(f"[#{n}] implement: not done -> escalate (human:needed)")
        _pause(gh, n, _failure_reason(res, "Implementation hit a blocker."))
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
    log(f"[#{n}] implement: done by {fam} -> PR #{pr['number']}, flow:review")


def handle_review(gh, ledger, issue):
    n = issue["number"]
    meta = issue_meta(n)
    base = gh.default_branch()
    branch = meta["branch"]
    # Check the PR branch out (base fetched + pulled too) so the reviewer can run
    # git diff itself against the target branch and inspect the whole tree.
    path = repo.ensure_repo(n)
    repo.checkout_branch(path, branch, base)
    summary = _last_bot_summary(gh, n)
    discussion = _discussion(gh, n, issue=issue, pr_number=meta["pr_number"])
    prompt = prompts.render(prompts.REVIEW, NUM=n, TITLE=issue["title"],
                            SUMMARY=summary, SPECS=_specs_text(meta.get("specs", [])),
                            BRANCH=branch, BASE=base, DISCUSSION=discussion)
    # cross-check: review with the OTHER family than implemented
    fam, res = _run("review", ledger, prompt, cwd=path,
                    exclude_family=meta.get("implementer"))
    model = _model_for("review", fam)
    effort = config.effort_for(model)
    tok = res.input_tokens + res.output_tokens
    status = res.data.get("status")
    if status == "approve":
        gh.comment(n, f"✅ **Review passed** (by {fam}).\n\n{res.data.get('summary','')}",
                   model=model, effort=effort, tokens=tok)
        gh.set_flow(n, config.FLOW_MERGE, issue)
        log(f"[#{n}] review: approved by {fam} -> flow:merge")
    elif status == "request_changes":
        rnd = meta.get("review_round", 0) + 1
        if rnd > config.MAX_REVIEW_ROUNDS:
            log(f"[#{n}] review: still failing after {config.MAX_REVIEW_ROUNDS} rounds "
                f"-> escalate (human:needed)")
            _pause(gh, n, f"Still failing review after {config.MAX_REVIEW_ROUNDS} rounds. "
                          "Take a look.")
            return
        fb = "\n".join(f"- {c}" for c in res.data.get("comments", [])) or \
             res.data.get("summary", "")
        gh.comment(n, f"🔁 **Changes requested** (round {rnd}, by {fam}):\n{fb}",
                   model=model, effort=effort, tokens=tok)
        update_issue_meta(n, review_round=rnd, last_feedback=fb)
        gh.set_flow(n, config.FLOW_IMPLEMENT, issue)
        log(f"[#{n}] review: changes requested (round {rnd}, by {fam}) -> flow:implement")
    else:
        log(f"[#{n}] review: needs_human -> escalate")
        _pause(gh, n, _failure_reason(res, "Review flagged a judgement call."))


def handle_merge(gh, ledger, issue):
    n = issue["number"]
    pr = gh.get_pull(issue_meta(n)["pr_number"])
    if pr.get("merged"):
        gh.set_flow(n, config.FLOW_DONE, issue)
        gh.comment(n, "🎉 Merged. Done.")
        log(f"[#{n}] merge: PR #{pr.get('number')} merged -> flow:done")
        return
    if config.AUTO_MERGE and pr.get("mergeable") and pr.get("mergeable_state") == "clean":
        gh.merge_pull(pr["number"])
        gh.set_flow(n, config.FLOW_DONE, issue)
        gh.comment(n, "🎉 Auto-merged on green. Done.")
        log(f"[#{n}] merge: auto-merged -> flow:done")
    else:
        log(f"[#{n}] merge: waiting for you to click merge "
            f"(mergeable={pr.get('mergeable')}, state={pr.get('mergeable_state')})")


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
