# Intra-process concurrency: pooled workers across issues + worktree fan-out within an issue

## Context

Norne is fully serial today. Three mechanisms enforce it, and all three are in scope:

1. **One worker thread** — `main.py` runs a single `_worker` draining a `queue.Queue`; `process_issue` runs to completion before the next. The `inflight` set + queue only prevent re-dispatch, they add no concurrency. Each issue gets a *fresh* `Ledger()` view (`main._worker` line ~120).
2. **One checkout per issue** — `repo.workdir(n)` is a single tree `/data/work/issue-N` shared by all of that issue's per-spec branches. `handle_implement`/`handle_review` pick `next(u for u in units if u['stage']==X)` (exactly one unit/tick) and `repo.checkout_branch(path, u['branch'], base)` into that shared tree.
3. **Lock-free store + non-thread-safe ledger** — `store._load`/`_save` are lock-free read-modify-write; `update_issue_meta` is a load→merge→save RMW. `Ledger` has no lock and charges budget only *after* a run (`stages._record`). The single writer is the only thing keeping these correct.

Goal (per the approved summary): add thread concurrency at two levels and ship both together. **Level 1** = multiple issues at once. **Level 2** = multiple spec branches of one issue at once. `MAX_WORKERS=1` must reproduce today's exact serial behavior.

**Why one spec, not several:** Level 2 calls Level 1's new `Ledger.reserve`/`reconcile` API and the store lock, and both levels add constants to `config.py`. Splitting them sends each to its own branch+worktree off `main` (that's exactly what this feature builds), where they'd collide in `config.py`/`store.py`/`stages.py` and Level 2 would import an API that doesn't exist yet on its base. They must land together on one branch.

## Approach

### Level 1 — across issues (`config.py`, `store.py`, `main.py`)

**Thread-safe store.** A per-call lock on `_load`/`_save` individually does **not** prevent lost updates — two threads can interleave load,load,save,save. The lock must span the whole RMW. Add a process-wide per-path lock registry in `store.py` (`_lock_for(path) -> threading.Lock`, itself guarded by a module lock) and hold the path lock across the *entire* read-modify-write in `update_issue_meta`, and across the read in `issue_meta`.

**Thread-safe ledger with reservation.** Today `main` builds a fresh `Ledger()` per issue, so under concurrency several in-memory `self.d` copies would race their `_save` onto `ledger.json`. Build **one shared `Ledger`** in `main()` and pass it to every `process_issue` (replacing the per-issue `Ledger()`); give `Ledger` an internal `threading.RLock` guarding `_roll`/`_save`/`record`/`headroom`/`next_reset`/`snapshot` and the new API. Split `headroom` into the public method (acquires the lock) and an internal `_has_headroom(pool)` (assumes lock held) so reservation can check-and-charge atomically. New API:
- `reserve(pool, tokens=0, prompts=0) -> bool`: under the lock, `_roll()`; if `_has_headroom(pool)` add the estimate to BOTH windows (w5+wk) and return `True`; else return `False` and charge nothing. This is the atomic gate that closes the window where N runners all pass `headroom()` and then collectively bust.
- `reconcile(pool, reserved_tokens=0, reserved_prompts=0, actual_tokens=0, actual_prompts=0)`: under the lock, adjust both windows by `(actual - reserved)` so the pool lands at true usage.
- Keep `record(...)` (still used elsewhere/tested) as the simple post-charge path.

**Reserve estimates** live in `config.py` as deliberately generous constants (N concurrent model subprocesses multiply spend — see issue Notes), env-overridable: `CLAUDE_RESERVE_TOKENS` (claude pool, tokens) and `GLM_RESERVE_PROMPTS` (glm pool, prompts; default `ceil(GLM_QUOTA_MULTIPLIER)`). Wire reservation into `stages._run`: after the router picks `fam`, `reserve` that pool's estimate; if `reserve` returns `False`, treat the pool as out of headroom — exclude it and re-route (fail over to the other family) or `raise BudgetParked()` exactly as today; after the run, replace the bare `_record` with `reconcile(pool, reserved=…, actual=…)`. `_record`'s actual-usage computation (claude=input+output tokens, glm=`ceil(GLM_QUOTA_MULTIPLIER)` prompts) becomes the `actual_*` args.

**Pooled dispatcher.** Replace the single `_worker`+`queue` with a bounded `concurrent.futures.ThreadPoolExecutor(max_workers=config.MAX_WORKERS)` (new `MAX_WORKERS` env, default `1`). Keep the `inflight` set + `threading.Lock` as the at-most-one-worker-per-issue guard: per poll, under the lock skip any `n in inflight`, else add `n` and `executor.submit(process_issue, gh, ledger, issue)`; attach a `future.add_done_callback` that removes `n` from `inflight` (under the lock) and logs timing/exceptions (preserving today's `worker error`/`worker done` logs). Extract the per-poll dispatch body into a small testable unit — `dispatch_once(gh, executor, inflight, lock, ledger)` — so a test drives ONE iteration without the infinite loop. With `MAX_WORKERS=1` the pool runs strictly one task at a time → behavior matches today.

### Level 2 — within an issue, across spec branches (`config.py`, `repo.py`, `stages.py`)

**Git worktrees (with the primary kept off spec branches — resolves the round-1 concern).** Add to `repo.py`:
- `workdir_spec(n, slug) -> os.path.join(config.WORKDIR_ROOT, 'worktrees', f'issue-{n}', slug)`. **Path choice:** the approved summary wrote `/data/work/issue-N/<slug>`, but `/data/work/issue-N` *is* the primary checkout; nesting a worktree inside it makes the primary's own `git add -A`/`git status` (used by `commit_all`/`dirty_files`) treat the worktree as untracked junk. A sibling container (`…/worktrees/issue-N/<slug>`) preserves the approved intent (one isolated tree per spec branch) while keeping every spec tree cleanly OUTSIDE the primary. (Accepted by review round 1.)
- A **per-issue git lock** registry (`_repo_lock_for(n) -> threading.Lock`) guarding primary-mutating ops, so concurrent fan-out workers don't collide on `.git/index.lock` or the shared worktrees ref.
- `ensure_worktree(n, slug, branch, base) -> path`: under the per-issue git lock — `ensure_repo(n)` (the primary clone is the worktree host), `git fetch origin`, then **`git checkout --detach origin/<base>` on the PRIMARY** so NO spec branch is checked out there (this is the load-bearing fix: `git worktree add` refuses a branch checked out in another worktree, and after `_publish_specs` the primary sits on the last spec branch — for a single-spec issue that IS the branch we're about to add). Then `git worktree prune` + `git worktree add -B <branch> <workdir_spec> origin/<branch>` (`-B` create-or-resets the local branch from origin — authoritative since every commit is pushed immediately — and the branch is free because the primary is detached). Idempotent across crashes: `prune` clears stale registrations and `-B`/`add` re-establish a clean tree; if the path already exists, `remove_worktree` it first. Return the worktree path. The slow model run happens in the returned worktree, OUTSIDE the lock.
- `remove_worktree(n, path)`: under the per-issue git lock, `git worktree remove --force <path>` (ignore-if-absent) to bound disk after a unit's stage completes.
- Defensive: have `_publish_specs` end by leaving the primary detached on base (it currently loops `checkout_branch` and lingers on the last spec branch), so the primary never sits on a spec branch between stages.

**Fan-out.** `handle_implement` and `handle_review` currently process one unit/tick. Change each to gather ALL units at the target stage and process them concurrently in a `ThreadPoolExecutor(max_workers=config.MAX_SPEC_WORKERS)` (new env, default `= MAX_WORKERS`), one thread + one worktree per unit. Each worker runs the existing implement/review body against `ensure_worktree(n, u['slug'], u['branch'], base)` instead of the shared `checkout_branch(path, …)`, removing its worktree when done; budget is gated per run by the Level-1 `reserve`/`reconcile` in `_run`.

**Concurrent unit persistence.** Multiple workers on one issue all write `spec_units`; today's last-writer-wins `_save_units(n, units)` would clobber. Add `_update_unit(n, slug, **changes)` that, under the store path lock, reloads `spec_units`, updates ONLY the unit matching `slug`, and saves — each worker persists its own unit through this. Run the aggregate `_advance(...)` and any human escalation **once, after the fan-out barrier**, from the dispatching thread: collect per-unit outcomes; if any unit needs a human, escalate once with the combined reasons (avoids racing `human:needed` label writes + duplicate comments). A failing unit must not lose successful siblings' persisted state.

## Non-goals
No flow/prompt/GitHub-API changes; no spec-authoring fan-out (one author→review loop as today); no multi-machine concurrency. `MAX_WORKERS=1` (hence `MAX_SPEC_WORKERS=1`) reproduces today's serial behavior exactly.

## Acceptance criteria
- Configurable `MAX_WORKERS` (default 1) and `MAX_SPEC_WORKERS` (default `MAX_WORKERS`); the default preserves serial behavior.
- Two issues dispatched together run concurrently (interleaved stage logs) with no `issues.json`/`ledger.json` corruption.
- The budget gate holds under concurrency: no pool exceeds its window ceilings due to races — `reserve` pre-charges atomically, `reconcile` corrects to actual.
- An issue is never processed by two workers at once (the `inflight` guard).
- Within one issue, multiple implement/review units progress concurrently, each in its own worktree; `ensure_worktree` leaves the primary on a NON-spec ref so `git worktree add` never hits 'branch already checked out'; no lost unit updates.
- Hermetic tests only (no real GitHub/model/network/subprocess), per CLAUDE.md; `python3 -m pytest` stays green including existing tests.

## Test plan (hermetic)
Reuse `clock`/`ledger_paths`/`fresh_ledger` fixtures in `tests/conftest.py`; monkeypatch `repo._git`/`subprocess.run`, `stages._run`, `repo`, `gh`.
- **store**: many threads calling `update_issue_meta` with distinct fields all persist (no lost updates); concurrent `Ledger.record`/`reserve`/`reconcile` produce exact tallies; `reserve` returns `False`/charges nothing when a pool lacks headroom, `True`/pre-charges when it has room; `reconcile` lands both windows at actual usage (over- and under-estimate cases).
- **main**: `dispatch_once` honors the `MAX_WORKERS` cap (stub `process_issue` records peak concurrency via a barrier/semaphore; assert ≤ cap) and the per-issue `inflight` guard (the same issue isn't submitted twice while in flight; removed by the done-callback after completion).
- **repo**: `workdir_spec` returns distinct, isolated sibling paths per slug; **`ensure_worktree` leaves the primary on a non-spec ref BEFORE adding the worktree** — capture the full `_git` argv sequence and assert a `checkout --detach` (non-spec ref) on the primary cwd is recorded *before* `['worktree','add', …]`, that `git fetch origin` precedes both, and that the branch passed to worktree-add is never left checked out in the primary; `remove_worktree` issues `worktree remove --force`. (No real git.)
- **stages**: `_run` reserves then reconciles against a fake ledger (assert reserve-before-run, reconcile-after-with-actual, and BudgetParked/failover when `reserve` returns `False`); implement/review fan-out processes ALL eligible units and respects `MAX_SPEC_WORKERS` (mock `_run`/`repo`/`gh`); `_update_unit` keeps concurrent unit writes from clobbering; a failing unit doesn't lose siblings' persisted state and `_advance`/escalation run once after the barrier.

## Work items
- [ ] config: add MAX_WORKERS, MAX_SPEC_WORKERS, and conservative reserve-estimate constants
- [ ] store: per-path lock making issue_meta/update_issue_meta a thread-safe whole-RMW
- [ ] store: Ledger internal lock + reserve/reconcile API
- [ ] main: shared Ledger + ThreadPoolExecutor dispatcher with the inflight guard
- [ ] stages: wire reserve/reconcile into _run
- [ ] repo: per-issue git lock + workdir_spec/ensure_worktree/remove_worktree keeping the primary off spec branches
- [ ] stages: thread-safe per-unit persistence (_update_unit)
- [ ] stages: fan out handle_implement across all implement units via worktrees
- [ ] stages: fan out handle_review across all review units with aggregated escalation
- [ ] tests: store concurrency + ledger reserve/reconcile accounting
- [ ] tests: dispatcher concurrency cap + per-issue inflight guard
- [ ] tests: repo worktree isolation + primary-detach-before-add + stages fan-out
