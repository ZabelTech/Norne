# Multi-repo support: auto-discover ZabelTech repos and namespace per-repo state

## Context

Today the orchestrator watches exactly one repo. `config.GH_REPO=owner/repo` is read once at import; `github_client.GitHub` defaults to it; `repo.REMOTE` and `repo.workdir(n)` are hardcoded around it; and `store`/`main`/`stages` key all per-issue state by the bare issue number `n`. Issue #10 (approved summary) asks for automatic multi-repo discovery: set `GH_OWNER=ZabelTech` once via `fly secrets set`, and each poll cycle the orchestrator enumerates the org's repos (`GET /orgs/{owner}/repos`), builds a per-repo `GitHub` client, and fans out polling across all of them — no redeploy to add/remove a repo (just create/delete it in the org). One `GH_TOKEN` covers all repos (confirmed by the human in discussion).

The load-bearing risk is **key collisions**: two repos can both have issue `#5`. Every place that uses `n` as a *storage* key (the JSON issues store) or a *filesystem* key (work-dir / worktree paths / git locks) must be namespaced per repo. Places that use `n` as a *GitHub API argument* must stay a bare number, because the per-repo `GitHub` client already scopes the call to the right repo via `self.repo`.

## Why one spec (not several)

The namespacing key must be applied *consistently and atomically* across `config`, `github_client`, `repo`, `store`, `stages`, and `main`. The pipeline implements each spec on its own branch cut from the base and opens an independent PR; splitting this refactor across specs would (a) create broken intermediate states if PRs merge out of order, and (b) impose a hidden merge-order dependency the pipeline can't express. It is one cohesive change; the work items below give per-module review granularity within the single branch/PR.

## Approach

**Identity lives on the `GitHub` client.** Each client already carries `self.repo` (`"owner/name"`). Add derived identity so every consumer can namespace from the one client it already holds:
- `self.owner`, `self.name` = `repo.split("/", 1)`
- `self.remote` = `f"https://x-access-token:{token}@github.com/{repo}.git"` (the authenticated clone URL, moved off `repo.py`'s module-level `REMOTE`)
- `key(n)` → `f"{self.repo}#{n}"` (e.g. `ZabelTech/foo#5`) — the **store** key
- `slug_n(n)` (or equivalent) → `f"{self.owner}-{self.name}-{n}"` (e.g. `ZabelTech-foo-5`) — the **filesystem** key

**Two key roles, kept distinct:**
- GitHub API calls (`gh.comment(n)`, `gh.get_issue(n)`, `gh.add_labels(n)`, …) keep the **bare `n`** — the client is already repo-scoped.
- Store calls (`issue_meta`, `update_issue_meta`, `_update_unit`) take the **namespaced store key** `gh.key(n)`.
- `repo.py` work-dir/worktree/lock operations derive the **filesystem key** and the **runtime remote** from the passed `gh` client.
- Strictly *in-repo* paths stay bare: `specs/{n}/<slug>.md` and branch `pipeline/issue-{n}/<slug>` live inside one repo's checkout and cannot collide across repos — leave them unchanged to minimise blast radius.

**One-time migration of existing `issues.json` (REQUIRED — prevents data loss on deploy).** Changing the store key format from bare `n` to `{owner}/{name}#n` would orphan all existing in-flight per-issue metadata (`spec_units`, `pr_number`, branch, `review_round`, paused `human_guidance`, clarify `last_comment_seen`). Flow state itself lives in GitHub labels and survives, but with orphaned meta an issue at `flow:implement`/`flow:review` reads empty meta, `_units()` returns `[]`, and `_advance` falls through to its zero-units `else` branch — posting “All 0 spec PR(s) merged”, calling `close_issue`, and parking at `flow:done`, i.e. **silently closing issues that still have open PRs**. To prevent this, add `store.migrate_legacy_keys(repo_slug)` run once at startup (before any polling): for every top-level key in `issues.json` that is a bare integer string (`^\d+$`), move its value to `f"{repo_slug}#{key}"`. It is idempotent (keys already containing `#` are skipped) and lock-protected. `main()` calls it with `repo_slug = config.GH_REPO` when `GH_REPO` is set. The single legacy single-repo deployment must keep `GH_REPO` set on the upgrade deploy so legacy bare keys can be attributed; if bare keys exist while `GH_REPO` is unset, migration logs a clear warning listing the affected numbers and leaves them untouched rather than guessing the repo.

Note on filesystem state: per-issue checkout dirs also move (`issue-N/` → `owner-repo-N/`), but these are **non-authoritative caches** re-cloned on demand by `ensure_repo`/`ensure_worktree` (all branch state is pushed to GitHub), so their path change is not data loss and needs no migration — only `issues.json` holds authoritative non-label state.

**Discovery + fan-out in `main`.** Each poll cycle compute the repo-slug list: if `GH_OWNER` is set, call `github_client.discover_repos(owner, token)` (paginated `GET /orgs/{owner}/repos`); else fall back to `[GH_REPO]`. Build (and cache, keyed by slug, to reuse the `requests.Session`) one `GitHub` client per slug, and run the existing per-repo dispatch for each, sharing the single `MAX_WORKERS` executor, the single shared `Ledger` (budget stays global per the summary), and one `inflight` set whose membership is keyed by `gh.key(n)` so the same issue number in two repos doesn't block.

**`GH_REPO` becomes optional**, retained as the single-repo fallback. Require that at least one of `GH_OWNER` / `GH_REPO` is set (clear error otherwise).

**Robustness:** a discovery API failure must not kill the poll loop — log it, and reuse the last successfully-discovered list if one exists, otherwise skip the cycle. Skip archived repos (read-only; can't host PRs).

## Out of scope (per approved summary)

Per-repo tokens (one token covers all); webhooks (polling stays); per-repo pipeline config / labels / routing; GitHub-App installation flow; changes to any pipeline stage, trigger label, or runner. The budget ledger stays a single global pool.

## Acceptance criteria

- Setting `GH_OWNER=ZabelTech` (with `GH_REPO` unset) makes the orchestrator poll every non-archived repo returned by `GET /orgs/ZabelTech/repos` each cycle, with one `GitHub` client per repo; adding/removing a repo in the org changes what is polled on the next cycle with no restart.
- With only `GH_REPO` set (no `GH_OWNER`), behaviour is identical to today for a single repo — and this is achieved *because* the startup migration rewrites existing bare-`n` `issues.json` keys to `{GH_REPO}#n`, so in-flight issues keep their `spec_units`/`pr_number`/review-round/paused-guidance/clarify-cursor (no orphaned meta, no spurious zero-units close).
- The startup migration is idempotent: a second run finds no bare keys and makes no changes; keys already namespaced are never clobbered; an issue mid-`flow:implement`/`flow:review` before deploy continues from the same unit state after deploy (no `close_issue`, no `flow:done`).
- With neither `GH_OWNER` nor `GH_REPO` set, startup fails fast with a clear message.
- Two repos that both contain issue `#5` keep entirely separate store entries (`owner-a/repo#5` vs `owner-b/repo#5`), separate work-dirs (`owner-a-repo-5/` vs `owner-b-repo-5/`), separate worktree containers, and separate git locks — no collision, no cross-talk.
- `repo.py` has no module-level hardcoded `REMOTE`/`GH_REPO`; clone URL and work-dir name are derived per call from the repo identity.
- A discovery API error logs and is survived (loop keeps running); a per-repo error in one repo doesn't stop polling the others.
- `python3 -m pytest` passes; new tests cover discovery, namespacing, cross-repo isolation, and the legacy-key migration; all tests stay hermetic (no real network/subprocess, per `tests/conftest.py`).

## Test plan

- **config**: `GH_OWNER` parsed; `GH_REPO` optional; missing-both raises; fallback list is `[GH_REPO]` when owner unset.
- **github_client**: `owner`/`name`/`remote`/`key(n)`/filesystem-key derivations; `discover_repos` paginates and filters archived (stub the session `get` to return two pages, assert full_name list and that archived repos are dropped). Existing marker/comment tests still pass.
- **repo**: `workdir`/`workdir_spec`/`ensure_repo`/`ensure_worktree`/`remove_worktree` produce namespaced paths from the repo identity; clone uses the per-repo remote; two different repos with the same `n` get distinct paths and distinct locks. Update the existing `test_repo.py` cases (which call these with a bare int today) to the new signatures.
- **store**: `issue_meta`/`update_issue_meta`/`_update_unit` round-trip under a namespaced string key; two namespaced keys sharing a trailing `#5` don't clobber each other. **Migration**: seed an `issues.json` with bare-`n` keys carrying realistic in-flight meta (`spec_units` with an open `pr_number`, a `review_round`, paused `human_guidance`); run `migrate_legacy_keys("owner/repo")`; assert keys become `owner/repo#n` with values intact, that a second run is a no-op, that pre-namespaced keys are untouched, and that bare keys with no `repo_slug` are left in place with a warning.
- **stages (regression for the bug this migration prevents)**: an issue whose meta was migrated reads non-empty `_units()` and does NOT hit the zero-units `_advance` close path — i.e. a `flow:review` unit with an open PR stays in review/merge rather than being closed.
- **main**: `process_issue` and `dispatch_once` namespace `inflight` by `gh.key(n)`; add a test that the same issue number in two different repo clients is dispatched independently (no false in-flight skip); migration runs once at startup before the first dispatch; discovery-failure path is survived. Update `FakeGH` in `test_main.py` to provide `key()` (and identity attrs as needed).

## Notes / decisions made (not escalating)

- Use `/orgs/{owner}/repos` exactly as approved (ZabelTech is an org). Not falling back to `/users/...`.
- Forks are included; only archived repos are skipped (they can't take PRs). Easy to tighten later if the human wants.
- Discovery runs every cycle (cheap, paginated); this is what makes the repo set runtime-configurable without redeploy.
- Migration attributes legacy bare keys to `GH_REPO`; operators upgrading from the single-repo deployment keep `GH_REPO` set for that deploy. Fresh multi-repo-only deployments have an empty `issues.json`, so migration is a no-op for them.

## Work items
- [ ] config: add GH_OWNER, make GH_REPO optional, validate at least one is set
- [ ] github_client: per-repo identity (owner/name/remote/key) + discover_repos()
- [ ] repo.py: derive work-dir + clone remote per repo and namespace git locks
- [ ] store.py: namespace per-issue metadata by a string key
- [ ] store.py: one-time startup migration of legacy bare-`n` keys to namespaced keys
- [ ] stages.py: thread the namespaced store key + repo client through handlers
- [ ] main.py: discover repos each cycle, run startup migration, and fan out the poll loop
- [ ] tests: discovery, namespacing, migration, and cross-repo isolation coverage
