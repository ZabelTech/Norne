"""Per-stage logic + the flow dispatcher.

Each handler reads the issue's current flow:* state, runs the right model
(chosen by the router under the budget gate), takes GitHub actions, and
transitions the flow label. Per-issue detail (branch, PR#, review round,
implementer family, paused guidance) lives in store.issue_meta.
"""
import math
import time
from . import config, instructions, prompts, repo, runners, router
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


def _implement_escalation(res, path, base=None):
    """A precise reason when an implement/fix run didn't report `done`. Tells the
    human *how* it failed (timed out vs. the subprocess crashed — with the actual
    error output) and what state it left behind: commits the agent made on its
    branch AND any still-uncommitted files, so the work isn't silently stranded."""
    parts = []
    raw = (res.raw or "").strip()
    crashed = not res.ok
    if crashed:
        parts.append("The implement run did not finish cleanly" + (
            " — it hit the time limit." if raw == "timeout"
            else " — the agent process exited with an error."))
    # Work the agent committed on its branch (whether or not it was pushed) — so we
    # don't misreport "nothing was committed" when a whole feature is sitting there.
    committed = repo.local_commits(path, base) if base else []
    if committed:
        shown = "\n".join(f"- {c}" for c in committed[:10])
        more = f"\n…and {len(committed) - 10} more" if len(committed) > 10 else ""
        parts.append(f"It had already committed **{len(committed)} commit(s)** on the "
                     f"branch (unpushed — the run never reported `done`):\n{shown}{more}")
    changed = repo.dirty_files(path)
    if changed:
        shown = "\n".join(f"- `{f}`" for f in changed[:20])
        more = f"\n…and {len(changed) - 20} more" if len(changed) > 20 else ""
        parts.append(f"It also left **{len(changed)} uncommitted file(s)**:\n{shown}{more}")
    # On a crash, surface the subprocess's own output so the failure is diagnosable
    # (the orchestrator otherwise only logs a one-line ok=False summary).
    if crashed and raw and raw != "timeout":
        parts.append("Subprocess output (tail):\n```\n" + raw[-1200:] + "\n```")
    detail = _failure_reason(res, "")
    if detail:
        parts.append(detail)
    return "\n\n".join(parts) or "Implementation hit a blocker."


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
    """Simple post-charge path (legacy; still used where no reservation)."""
    if fam == "claude":
        ledger.record("claude", tokens=res.input_tokens + res.output_tokens)
    else:
        ledger.record("glm", prompts=math.ceil(config.GLM_QUOTA_MULTIPLIER))


def _run(stage, ledger, prompt, cwd, exclude_family=None, write=False, schema=None, resume=None):
    """Route -> reserve -> run -> reconcile. Reserve pre-charges an estimate so
    the budget gate is atomic under concurrent runs; reconcile corrects to actual
    usage after. When a family is actually rate-limited by its provider, refund
    the reservation, cool it down, and fall back to any other eligible family;
    park (BudgetParked) only once EVERY family is out of budget / cooling."""
    tried = set()
    while True:
        fam = router.choose_family(stage, ledger, exclude_family=exclude_family,
                                   skip=tried)
        if fam is None:
            why = (f"all families out of budget after {', '.join(sorted(tried))} "
                   "rate-limited") if tried else "no model family has budget headroom"
            log(f"  {stage}: {why} -> parking")
            raise BudgetParked()
        model = _model_for(stage, fam)
        effort = config.effort_for(model)

        # Compute resume_id: only forward the id when the chosen family matches
        # the stored session's family. This makes failover automatically safe.
        resume_id = None
        if resume and resume.get("family") == fam and resume.get("id"):
            resume_id = resume["id"]

        # Reserve an estimate before the run (atomic gate for concurrency).
        if fam == "claude":
            reserved_tokens, reserved_prompts = config.CLAUDE_RESERVE_TOKENS, 0
        else:
            reserved_tokens, reserved_prompts = 0, config.GLM_RESERVE_PROMPTS
        if not ledger.reserve(fam, tokens=reserved_tokens, prompts=reserved_prompts):
            # Pool exhausted between router choice and reservation -> try the next.
            log(f"  {stage}: {fam} exhausted during reservation -> trying another family")
            tried.add(fam)
            continue

        log(f"  {stage}: calling {fam}/{model} ({effort})…")
        t0 = time.time()
        runner = runners.get_runner(fam)
        try:
            res = runner.run(prompt, cwd=cwd, write=write, model=model,
                             effort=effort, schema=schema, resume=resume_id)
        except RateLimited:
            # The run didn't happen: refund the reservation, cool the pool, fall back.
            ledger.reconcile(fam, reserved_tokens=reserved_tokens,
                             reserved_prompts=reserved_prompts,
                             actual_tokens=0, actual_prompts=0)
            ledger.cool_down(fam, config.RATE_LIMIT_COOLDOWN)
            tried.add(fam)
            log(f"  {stage}: {fam} rate-limited by provider -> cooling "
                f"{config.RATE_LIMIT_COOLDOWN}s, falling back to another family")
            continue

        # Reconcile the estimate to actual usage.
        if fam == "claude":
            actual_tokens, actual_prompts = res.input_tokens + res.output_tokens, 0
        else:
            actual_tokens, actual_prompts = 0, math.ceil(config.GLM_QUOTA_MULTIPLIER)
        ledger.reconcile(fam, reserved_tokens=reserved_tokens,
                         reserved_prompts=reserved_prompts,
                         actual_tokens=actual_tokens, actual_prompts=actual_prompts)

        log(f"  {stage}: {model} returned in {int(time.time() - t0)}s "
            f"({_fmt_tokens(res.input_tokens + res.output_tokens)}, ok={res.ok})")
        return fam, res


def _build_prompt(template, step, path, **tokens):
    """Build a prompt with merged general + stage-specific instructions.

    Args:
        template: The prompt template constant from prompts.py
        step: The logical pipeline step (e.g., "summarize", "spec", "implement", "review")
        path: The target repo checkout path (for reading CLAUDE.md)
        **tokens: Other template tokens (NUM, TITLE, BODY, etc.)

    Returns:
        The rendered prompt with instructions injected.
    """
    return prompts.render(template, INSTRUCTIONS=instructions.load(step, path), **tokens)


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
    # Capture the checkout path BEFORE rendering so the general layer is read from the real checkout
    path = repo.ensure_repo(n)
    prompt = _build_prompt(prompts.SUMMARIZE, "summarize", path, NUM=n, TITLE=issue["title"],
                            BODY=issue.get("body") or "(no description)",
                            CLARIFICATIONS=human)
    # Run inside a checkout so the model can investigate the repo, not just /data.
    fam, res = _run("summarize", ledger, prompt, cwd=path,
                    schema=prompts.SUMMARIZE_SCHEMA)
    model = _model_for("summarize", fam)
    effort = config.effort_for(model)
    d = res.data
    summary = d.get("summary", "")
    questions = d.get("questions") or []
    if not summary and not questions:
        # The triage produced nothing usable — typically a long agentic run that
        # ended in prose without the trailing json contract. Don't post an empty
        # "Summary (draft) … - (clarify)" placeholder with no actual questions:
        # surface what the model really said and pause for a human.
        log(f"[#{n}] summarize: no usable summary/questions parsed -> escalate (human:needed)")
        _pause(gh, n, _failure_reason(
            res, "Summarize produced no usable summary or questions — try again or "
                 "add detail to the issue."))
        return
    if d.get("status") == "ready":
        gh.comment(n, f"📋 **Summary**\n\n{summary}\n\n"
                      f"If this looks right, add the `{config.SIG_APPROVE}` label to approve "
                      f"— or just comment to ask for changes.",
                   model=model, effort=effort, tokens=res.input_tokens + res.output_tokens)
        gh.set_flow(n, config.FLOW_APPROVAL, issue)
        log(f"[#{n}] summarize -> ready, flow:approval")
    else:
        qs = "\n".join(f"- {q}" for q in questions) or "- (clarify)"
        gh.comment(n, f"📋 **Summary (draft)**\n\n{summary}\n\n"
                      f"**A few questions before I spec this:**\n{qs}",
                   model=model, effort=effort, tokens=res.input_tokens + res.output_tokens)
        gh.set_flow(n, config.FLOW_CLARIFY, issue)
        log(f"[#{n}] summarize -> {len(questions)} question(s), flow:clarify")


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
    tick; a clean verdict publishes (a branch per spec -> implement); reaching
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

    # Build resume dict for author: resume from round 0's session when rnd > 0
    author_resume = meta.get("spec_author_session") if rnd > 1 else None

    fam, res = _run("spec", ledger, _build_prompt(prompts.SPEC, "spec", path, NUM=n,
                    TITLE=issue["title"], SUMMARY=summary, DISCUSSION=discussion,
                    FEEDBACK=feedback), cwd=path, schema=prompts.SPEC_SCHEMA,
                    resume=author_resume)
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
    # Persist the author session for resumption on subsequent rounds
    if res.session_id:
        update_issue_meta(n, spec_author_session={"family": fam, "id": res.session_id})
    # the author's replies to last round's concerns (resolved or rebutted)
    responses = "\n".join(f"- {r}" for r in (d.get("responses") or [])) \
        or "(none — first review)"
    # diversity-of-thought: a DIFFERENT family peer-reviews the specs, weighing
    # the author's responses so a sound rebuttal isn't re-raised.
    # Build resume dict for reviewer: resume from previous reviewer session when looping
    reviewer_resume = meta.get("spec_reviewer_session") if rnd > 1 else None
    rfam, rev = _run("review", ledger, _build_prompt(prompts.SPEC_REVIEW,
                     "spec_review", path, SPECS=_specs_text(specs),
                     AUTHOR_RESPONSES=responses, DISCUSSION=discussion),
                     cwd=path, exclude_family=fam, schema=prompts.SPEC_REVIEW_SCHEMA,
                     resume=reviewer_resume)
    reviewer = _participant("review", rfam, rev)
    note = f"🔎 Spec round {rnd} — author {author} · reviewer {reviewer}"
    # Persist the reviewer session for resumption on subsequent rounds
    if rev.session_id:
        update_issue_meta(n, spec_reviewer_session={"family": rfam, "id": rev.session_id})
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
    update_issue_meta(n, spec_round=0, spec_feedback=None,
                    spec_author_session=None, spec_reviewer_session=None)


def _slug(spec, idx):
    return spec.get("slug") or f"spec-{idx + 1}"


_FLOW_STAGE = {"implement": "implement", "review": "review", "merge": "merge"}


def _units(meta, flow=None):
    """The per-spec work units. Each is one spec on its own branch, carried through
    implement -> review -> merge independently:
        {slug, title, branch, spec, stage, pr_number, review_round,
         last_feedback, implementer}
    stage ∈ {implement, review, merge, done}. Migrates a legacy single-branch
    issue (pre per-spec fan-out) into one unit on first read so in-flight work
    keeps moving — placing the unit at the stage matching the issue's current flow
    label (passed in by the handler), falling back to a PR-presence heuristic."""
    units = meta.get("spec_units")
    if units:
        return units
    specs = meta.get("specs") or []
    branch = meta.get("branch")
    if not (specs and branch):
        return []
    stage = _FLOW_STAGE.get(flow) or \
        ("review" if meta.get("pr_number") else "implement")
    return [{
        "slug": _slug(specs[0], 0), "title": specs[0].get("title", ""),
        "branch": branch, "spec": specs[0], "stage": stage,
        "pr_number": meta.get("pr_number"), "review_round": meta.get("review_round", 0),
        "last_feedback": meta.get("last_feedback"), "implementer": meta.get("implementer"),
    }]


def _save_units(n, units):
    update_issue_meta(n, spec_units=units)


def _update_unit(n, slug, **changes):
    """Update a single unit's fields atomically — for concurrent workers.

    Under the store path lock, reload spec_units, update ONLY the unit matching
    `slug`, and save. Each worker persists its own unit without clobbering
    siblings. Uses _load/_save directly to avoid re-acquiring the non-reentrant
    lock (issue_meta/update_issue_meta each acquire the same lock).
    """
    from . import store
    lock = store._lock_for(store.ISSUES_PATH)
    with lock:
        # Load, modify, save inline — do NOT call issue_meta() or update_issue_meta()
        # since they re-acquire the same non-reentrant lock and would deadlock.
        data = store._load(store.ISSUES_PATH, {})
        issue_data = data.get(str(n), {})
        units = issue_data.get("spec_units", [])
        updated = []
        for u in units:
            if u.get("slug") == slug:
                updated_u = u.copy()
                updated_u.update(changes)
                updated.append(updated_u)
            else:
                updated.append(u)
        issue_data["spec_units"] = updated
        data[str(n)] = issue_data
        store._save(store.ISSUES_PATH, data)


def _advance(gh, n, units, issue=None):
    """Set the issue's aggregate flow from the per-unit stages — implement until
    every spec has an open PR, then review, then merge — and close the issue once
    every spec PR is merged."""
    if any(u["stage"] == "implement" for u in units):
        gh.set_flow(n, config.FLOW_IMPLEMENT, issue)
    elif any(u["stage"] == "review" for u in units):
        gh.set_flow(n, config.FLOW_REVIEW, issue)
    elif any(u["stage"] != "done" for u in units):       # approved, waiting to merge
        gh.set_flow(n, config.FLOW_MERGE, issue)
    else:                                                 # all spec PRs merged
        gh.comment(n, f"🎉 All {len(units)} spec PR(s) merged. Closing #{n}.")
        gh.set_flow(n, config.FLOW_DONE, issue)
        gh.close_issue(n)
        log(f"[#{n}] all {len(units)} spec PR(s) merged -> closed, flow:done")


def _publish_specs(gh, n, path, specs, round_note=""):
    """Reviewer is happy: give each spec its OWN branch with just that spec
    committed (no sub-issues), then move to implement. Leaves the primary on
    a detached HEAD so `ensure_worktree` never hits 'branch already checked out'."""
    base = gh.default_branch()
    units = []
    for i, s in enumerate(specs):
        slug = _slug(s, i)
        branch = f"pipeline/issue-{n}/{slug}"
        repo.checkout_branch(path, branch, base)
        repo.write_spec(path, n, s, i)
        repo.push(path, branch)
        units.append({"slug": slug, "title": s.get("title", ""), "branch": branch,
                      "spec": s, "stage": "implement", "pr_number": None,
                      "review_round": 0, "last_feedback": None, "implementer": None})
    # Leave the primary on a non-spec ref so `ensure_worktree` never fails with
    # 'branch already checked out in another worktree'.
    repo._git(["checkout", "--detach", f"origin/{base}"], path)
    update_issue_meta(n, spec_units=units, specs=specs, branch=None,
                      review_round=0, human_guidance=None, pending_specs=None)
    gh.set_flow(n, config.FLOW_IMPLEMENT, gh.get_issue(n))
    lines = "\n".join(f"- `{u['branch']}` — {u['title']}" for u in units)
    body = (f"🛠️ Spec'd into {len(units)} spec(s), one branch each:\n{lines}\n\n"
            "Implementing each on its own branch; the issue closes when every PR merges.")
    if round_note:
        body += f"\n\n{round_note}"
    gh.comment(n, body)


def _escalate_spec(gh, n, specs, reason, round_note=""):
    """Judgement call before publish: keep the proposed specs in meta and pause for
    a human (no sub-issues — the specs become branches only once approved)."""
    if specs:
        update_issue_meta(n, pending_specs=specs)
        titles = "\n".join(f"- {s.get('title', '(untitled)')}" for s in specs)
        reason += f"\n\nProposed specs:\n{titles}"
    if round_note:
        reason += f"\n\n{round_note}"
    _pause(gh, n, reason)


def handle_implement(gh, ledger, issue):
    """Implement ALL specs that still need code (or a fix round), on their own
    branches via worktrees, and open PRs linking the issue. Fan-out to process
    all eligible units concurrently, one thread + worktree per unit."""
    import concurrent.futures

    n = issue["number"]
    units = _units(issue_meta(n), "implement")
    implement_units = [u for u in units if u["stage"] == "implement"]

    if not implement_units:                             # nothing left to implement
        _advance(gh, n, units, issue)
        return

    base = gh.default_branch()

    def _implement_one(u):
        """Worker: run one implement unit in a worktree."""
        slug = u["slug"]
        path = repo.ensure_worktree(n, slug, u["branch"], base)
        try:
            rnd = u.get("review_round", 0)
            discussion = _discussion(gh, n, issue=issue, pr_number=u.get("pr_number"))
            if rnd > 0 and u.get("last_feedback"):              # fix iteration
                prompt = _build_prompt(prompts.FIX, "fix", path, NUM=n, ROUND=rnd,
                                       FEEDBACK=u["last_feedback"], DISCUSSION=discussion)
            else:
                prompt = _build_prompt(prompts.IMPLEMENT, "implement", path, NUM=n,
                                       DISCUSSION=discussion)
            log(f"[#{n}] implement {slug}: "
                f"{'fix round ' + str(rnd) if rnd else 'first pass'}")
            # Build resume dict for fix rounds: resume from the unit's first-pass session
            implement_resume = u.get("implement_session") if rnd > 0 else None
            fam, res = _run("implement", ledger, prompt, cwd=path, write=True,
                            schema=prompts.IMPLEMENT_SCHEMA, resume=implement_resume)

            if res.data.get("status") != "done":
                reason = _implement_escalation(res, path, base)
                return {"slug": slug, "status": "escalate", "reason": reason}

            # Persist the implement session for resumption on fix rounds
            if res.session_id:
                _update_unit(n, slug, implement_session={"family": fam, "id": res.session_id})

            repo.commit_all(path, f"implement #{n} {slug}"
                            + (f" (fix round {rnd})" if rnd else ""))
            repo.push(path, u["branch"])
            pr = gh.pull_for_branch(u["branch"])
            if not pr:
                body = (f"{res.data.get('summary','')}\n\nPart of #{n}\n\n"
                        f"_Spec: `specs/{n}/{slug}.md`. Implemented by **{fam}**._")
                pr = gh.create_pull(title=f"{issue['title']} — {u['title']}",
                                    head=u["branch"], base=base, body=body)
            return {"slug": slug, "status": "done", "pr_number": pr["number"],
                    "implementer": fam}
        finally:
            repo.remove_worktree(n, path)

    # Fan-out: process all implement units concurrently.
    results = []
    budget_or_rate_limited = False
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=config.MAX_SPEC_WORKERS) as executor:
        futures = {executor.submit(_implement_one, u): u for u in implement_units}
        for fut in concurrent.futures.as_completed(futures):
            try:
                results.append(fut.result())
            except (BudgetParked, RateLimited) as e:
                u = futures[fut]
                log(f"[#{n}] implement {u['slug']}: {type(e).__name__} -> will park")
                results.append({"slug": u["slug"], "status": "parked", "exception": e})
                budget_or_rate_limited = True
            except Exception as e:
                u = futures[fut]
                log(f"[#{n}] implement {u['slug']}: raised {e}")
                results.append({"slug": u["slug"], "status": "error", "reason": str(e)})

    # Persist outcomes and escalate if any unit needs human.
    escalation_reasons = []
    for r in results:
        if r["status"] == "done":
            slug = r["slug"]
            _update_unit(n, slug, pr_number=r["pr_number"],
                         implementer=r["implementer"], stage="review")
            log(f"[#{n}] implement {slug}: done by {r['implementer']} -> PR #{r['pr_number']}")
        elif r["status"] == "parked":
            # Budget/rate-limit units are already logged; don't escalate yet
            pass
        elif r["status"] in ("escalate", "error"):
            escalation_reasons.append(
                f"`{r['slug']}`: {r.get('reason', 'unknown failure')}")
            log(f"[#{n}] implement {r['slug']}: {r['status']} -> escalate (human:needed)")

    # If any unit hit budget/rate-limit, re-raise so process_issue parks the issue.
    if budget_or_rate_limited:
        # Find the first budget/rate-limit exception and re-raise it
        for r in results:
            if r["status"] == "parked":
                raise r["exception"]

    if escalation_reasons:
        combined = "\n\n".join(escalation_reasons)
        _pause(gh, n, f"One or more implement units hit a blocker:\n\n{combined}")
    else:
        _advance(gh, n, _units(issue_meta(n)), issue)


def handle_review(gh, ledger, issue):
    """Review ALL spec PRs awaiting review, cross-model. Fan-out to process
    all eligible units concurrently, one thread + worktree per unit."""
    import concurrent.futures

    n = issue["number"]
    units = _units(issue_meta(n), "review")
    review_units = [u for u in units if u["stage"] == "review"]

    if not review_units:                                # nothing left to review
        _advance(gh, n, units, issue)
        return

    base = gh.default_branch()
    summary = _last_bot_summary(gh, n)

    def _review_one(u):
        """Worker: run one review unit in a worktree."""
        slug = u["slug"]
        path = repo.ensure_worktree(n, slug, u["branch"], base)
        try:
            discussion = _discussion(gh, n, issue=issue, pr_number=u.get("pr_number"))
            prompt = _build_prompt(prompts.REVIEW, "review", path, NUM=n, TITLE=issue["title"],
                                   SUMMARY=summary, SPECS=_specs_text([u["spec"]]),
                                   BRANCH=u["branch"], BASE=base, DISCUSSION=discussion)
            # cross-check: review with the OTHER family than implemented
            fam, res = _run("review", ledger, prompt, cwd=path,
                            exclude_family=u.get("implementer"),
                            schema=prompts.REVIEW_SCHEMA)

            model = _model_for("review", fam)
            effort = config.effort_for(model)
            tok = res.input_tokens + res.output_tokens
            status = res.data.get("status")

            if status == "approve":
                return {"slug": slug, "status": "approve", "family": fam,
                        "summary": res.data.get("summary", ""),
                        "model": model, "effort": effort, "tokens": tok}
            elif status == "request_changes":
                rnd = u.get("review_round", 0) + 1
                if rnd > config.MAX_REVIEW_ROUNDS:
                    return {"slug": slug, "status": "escalate",
                            "reason": f"still failing after {config.MAX_REVIEW_ROUNDS} rounds"}
                fb = "\n".join(f"- {c}" for c in res.data.get("comments", [])) or \
                     res.data.get("summary", "")
                return {"slug": slug, "status": "request_changes", "round": rnd,
                        "family": fam, "feedback": fb, "model": model,
                        "effort": effort, "tokens": tok}
            else:
                return {"slug": slug, "status": "escalate",
                        "reason": _failure_reason(res, "Review flagged a judgement call.")}
        finally:
            repo.remove_worktree(n, path)

    # Fan-out: process all review units concurrently.
    results = []
    budget_or_rate_limited = False
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=config.MAX_SPEC_WORKERS) as executor:
        futures = {executor.submit(_review_one, u): u for u in review_units}
        for fut in concurrent.futures.as_completed(futures):
            try:
                results.append(fut.result())
            except (BudgetParked, RateLimited) as e:
                u = futures[fut]
                log(f"[#{n}] review {u['slug']}: {type(e).__name__} -> will park")
                results.append({"slug": u["slug"], "status": "parked", "exception": e})
                budget_or_rate_limited = True
            except Exception as e:
                u = futures[fut]
                log(f"[#{n}] review {u['slug']}: raised {e}")
                results.append({"slug": u["slug"], "status": "escalate",
                                "reason": str(e)})

    # Persist outcomes and escalate if any unit needs human.
    escalation_reasons = []
    for r in results:
        slug = r["slug"]
        pr_number = next(u["pr_number"] for u in units if u["slug"] == slug)
        if r["status"] == "approve":
            # Review feedback goes on the PR (the code under review), not the issue.
            gh.comment(pr_number, f"✅ **Review passed** (by {r['family']}).\n\n{r['summary']}",
                       model=r["model"], effort=r["effort"], tokens=r["tokens"])
            _update_unit(n, slug, stage="merge")
            log(f"[#{n}] review {slug}: approved by {r['family']}")
        elif r["status"] == "request_changes":
            # Review feedback goes on the PR (the code under review), not the issue.
            gh.comment(pr_number, f"🔁 **Changes requested** (round {r['round']}, "
                       f"by {r['family']}):\n{r['feedback']}",
                       model=r["model"], effort=r["effort"], tokens=r["tokens"])
            _update_unit(n, slug, review_round=r["round"],
                         last_feedback=r["feedback"], stage="implement")
            log(f"[#{n}] review {slug}: changes requested (round {r['round']}, by {r['family']})")
        elif r["status"] == "escalate":
            escalation_reasons.append(f"`{slug}`: {r.get('reason', 'unknown failure')}")
            log(f"[#{n}] review {slug}: escalate (human:needed)")
        elif r["status"] == "parked":
            # Budget/rate-limit units are already logged; don't escalate yet
            pass

    # If any unit hit budget/rate-limit, re-raise so process_issue parks the issue.
    if budget_or_rate_limited:
        # Find the first budget/rate-limit exception and re-raise it
        for r in results:
            if r["status"] == "parked":
                raise r["exception"]

    if escalation_reasons:
        combined = "\n\n".join(escalation_reasons)
        _pause(gh, n, f"One or more review units hit a blocker:\n\n{combined}")
    else:
        _advance(gh, n, _units(issue_meta(n)), issue)


def handle_merge(gh, ledger, issue):
    """Merge (or await) every approved spec PR; close the issue when all are in."""
    n = issue["number"]
    units = _units(issue_meta(n), "merge")
    changed = False
    for u in units:
        if u["stage"] == "done" or not u.get("pr_number"):
            continue
        pr = gh.get_pull(u["pr_number"])
        if pr.get("merged"):
            u["stage"] = "done"
            changed = True
            log(f"[#{n}] merge: PR #{pr.get('number')} ({u['slug']}) merged")
        elif config.AUTO_MERGE and pr.get("mergeable") and \
                pr.get("mergeable_state") == "clean":
            gh.merge_pull(pr["number"])
            u["stage"] = "done"
            changed = True
            log(f"[#{n}] merge: auto-merged PR #{pr.get('number')} ({u['slug']})")
    if changed:
        _save_units(n, units)
    pending = [u for u in units if u["stage"] != "done"]
    if not pending:
        _advance(gh, n, units, issue)                    # all merged -> close issue
    else:
        log(f"[#{n}] merge: waiting on {len(pending)} PR(s): "
            + ", ".join(f"#{u.get('pr_number')}" for u in pending))


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
