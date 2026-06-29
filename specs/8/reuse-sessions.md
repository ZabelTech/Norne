# Reuse Claude Code sessions on pipeline loop-backs

## Context

When the pipeline loops a model stage back to itself — spec revision rounds (`handle_spec`) and implement fix rounds (`handle_implement`) — every iteration starts a cold Claude Code session. The model re-explores the repo and re-derives reasoning it already did last round. Claude Code already emits a `session_id` in its `--output-format json` envelope and supports `claude -p --resume <session_id>` to continue an existing conversation. We currently parse that envelope in `_claude_cc_json` (orchestrator/runners.py:148) but drop `session_id` on the floor.

This spec captures `session_id`, threads it through the runners and the central `stages._run` router, persists it per stage/unit in `issue_meta`, and passes `--resume <session_id>` on subsequent same-stage iterations so revisions are coherent and targeted. The payoff is **context quality, not token cost** — resumption does not change prompt-cache billing, so the budget ledger is untouched.

This is one dependency chain (capture → thread → persist → resume), so it is a single spec. It must ship on one branch: the repo implements each spec on its own branch concurrently and merges independently (see `_publish_specs`), so splitting this dependent work across branches would leave a branch that cannot build against the other.

### Key facts verified in the repo
- `stages._run` (orchestrator/stages.py:139) is the single choke point for every model run. It routes → reserves budget → runs → reconciles, and **fails over across families** on rate-limits. A `session_id` is therefore *family-specific*: a Claude session id is meaningless to a GLM run. Resume must only fire when the family chosen for this iteration matches the family that produced the stored session.
- Implement/review run in worktrees at the **stable** path `worktrees/issue-<n>/<slug>` (orchestrator/repo.py:41-49). Claude Code stores session transcripts under `~/.claude/projects/<encoded-cwd>/`, **not** inside the worktree, so `repo.remove_worktree` between rounds does not delete the session — `--resume <id>` still resolves on the next fix round. No change to worktree lifecycle is needed.
- `handle_spec` runs **one author→reviewer round per tick** and checkpoints `spec_round`/`spec_feedback` in meta, staying at `flow:spec` to revise next tick. `_reset_spec_loop` clears that state on convergence/escalation.
- GLM goes through either `GlmClaudeCodeRunner` (Claude Code pointed at z.ai — `--resume` is structurally available) or `GlmPiRunner` (Pi — no resume equivalent).

## Approach

**1. Capture.** Add `session_id` to `_claude_cc_json`'s return tuple (read `obj.get("session_id")`) and to the `RunResult` dataclass (default `None`). All three runners populate it (`GlmPiRunner` leaves it `None`).

**2. Resume param on runners.** Add `resume=None` to `ClaudeCodeRunner.run` and `GlmClaudeCodeRunner.run`. When `resume` is a non-empty string, append `--resume <session_id>` to the `claude` command. `GlmPiRunner.run` accepts `resume` for a uniform signature but ignores it.

**3. Graceful stale-session fallback.** A resumed session can be expired/evicted; `claude --resume <gone-id>` then exits non-zero. When `resume` was passed and the run fails non-zero, is **not** a `RateLimited`, and the output matches a conservative session-not-found pattern, retry the run **once** without `--resume` (a fresh, full-prompt session) and use that result. This keeps fallback silent and never double-runs on unrelated failures.

**4. Thread resume through `stages._run`.** Add a `resume=None` parameter that takes a `{"family": <fam>, "id": <session_id>}` dict (or `None`). Inside the router loop, after a family is chosen, compute `resume_id = resume["id"] if resume and resume.get("family") == fam and resume.get("id") else None` and forward it to `runner.run`. This makes failover automatically safe: when the router picks a different family than the stored session's, no resume is attempted. `_run` already returns `(fam, res)`, and `res.session_id` lets callers persist the new id.

**5. Persist + resume per stage.**
- *Spec author*: after the author `_run("spec", …)`, persist `spec_author_session={"family": fam, "id": res.session_id}` (only when a non-empty id is present) wherever the loop continues (the checkpoint path and any path that stays at `flow:spec`). Build the resume dict from `meta.get("spec_author_session")` and pass it to the author `_run` on rounds ≥ 1. Clear it in `_reset_spec_loop`.
- *Spec reviewer* (optional but in-scope): same pattern with `spec_reviewer_session` for the reviewer `_run("review", …)`, so a reviewer that loops across ticks resumes too. Cleared in `_reset_spec_loop`.
- *Implement fix rounds*: store the session on the **unit** (per slug) — after a successful first-pass run, `_update_unit(n, slug, implement_session={"family": fam, "id": res.session_id})`. On fix rounds (`rnd > 0`), build the resume dict from `u.get("implement_session")` and pass it to the implement `_run`. Because units already carry per-slug state and `_update_unit` is lock-safe for concurrent workers, this is naturally fan-out-safe.

**6. GLM.** Resumption is opt-in by construction: `session_id` is only stored when the runner actually returns one. `GlmClaudeCodeRunner` will resume iff z.ai echoed a `session_id`; `GlmPiRunner` never produces one, so the resume dict's id stays `None` and nothing happens. No GLM-specific branching needed beyond "resume only when an id exists."

## Non-goals (from the approved summary)
- **Clarify loop** — human replies arrive hours/days later; sessions are stale. No resume.
- **Cross-stage session sharing** — only loop-back-to-same-stage reuse.
- **Token/budget changes** — resumption does not alter cache billing; the ledger is untouched.
- **Session pre-warming/pinning** beyond what `--resume` provides.

## Acceptance criteria
- `_claude_cc_json` returns `session_id` from the envelope (and the existing `(text, itok, otok, structured)` callers are updated to the new arity); `RunResult` carries `session_id` (default `None`).
- `ClaudeCodeRunner`/`GlmClaudeCodeRunner` add `--resume <id>` to the command when and only when a non-empty `resume` is passed; `GlmPiRunner` ignores it without error.
- A stale-session resume (non-zero exit matching the session-not-found pattern, not a rate-limit) transparently retries once without `--resume`; the caller sees a normal result. Unrelated non-zero exits are **not** retried.
- `stages._run` accepts `resume` and only forwards an id when the chosen family matches the stored session's family; a family mismatch (failover) silently runs cold.
- Spec author rounds ≥ 1 resume round 0's author session (same family); the spec reviewer resumes its prior session when it loops; implement fix rounds (`rnd > 0`) resume the unit's first-pass implement session. All stored ids are cleared by `_reset_spec_loop` (spec) and never leak across distinct units (implement).
- A run that returns no `session_id` stores nothing and behaves exactly as today (full prompt, new session).
- `python3 -m pytest` passes; new tests cover capture, command construction, fallback, family-mismatch skip, and meta/unit persistence. Tests stay hermetic (no real subprocess/GitHub), matching `tests/conftest.py` conventions.

## Test plan
- **runners**: extend `tests/test_runners.py` — `_claude_cc_json` returns the `session_id` from a sample envelope and `None`/garbage when absent; `RunResult.session_id` defaults `None`. Monkeypatch `runners._run` (or `subprocess`) to assert `--resume <id>` is present when `resume` is set and absent otherwise; simulate a stale-session non-zero result and assert exactly one retry without `--resume`, and that an unrelated non-zero failure is not retried.
- **router/_run**: a test where `resume={"family":"claude","id":"abc"}` but the router is forced to pick GLM asserts no resume id reaches the runner; matching family forwards the id.
- **stages**: extend `tests/test_stages.py` — drive `handle_spec` across two ticks asserting `spec_author_session` is persisted after round 0 and a resume dict is passed on round 1, and that `_reset_spec_loop` clears it; drive a unit through implement → request_changes → fix-round implement asserting `implement_session` is stored on the unit and resumed on the fix round. Use the existing fakes/stubs.

## Out of scope / watch-outs for the implementer
- Do **not** touch the budget ledger or reservation logic — resumption is billing-neutral.
- Keep the session-not-found regex conservative so genuine failures still surface as escalations (`_implement_escalation`, `_failure_reason`).
- Don't change worktree create/remove timing; rely on the stable per-slug path.

## Work items
- [ ] Capture session_id in _claude_cc_json and RunResult
- [ ] Add a resume parameter and --resume flag to the Claude Code runners
- [ ] Graceful fallback when a resumed session is stale
- [ ] Thread resume through stages._run with family-matched gating
- [ ] Persist and resume the spec author (and reviewer) sessions
- [ ] Persist and resume the implement session per spec unit on fix rounds
