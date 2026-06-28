# HANDOFF — design decisions & open items

Context for whoever (human or agent) picks this up in a code session. The
README says *what* the system does and how to run it; this file says *why* it's
built this way, what was deliberately ruled out, and what still needs your call.
Facts below were verified ~June 2026 and some are volatile — re-check the ones
flagged ⚠️.

## The hard constraints this was built under
These came from the project owner and are load-bearing — don't "optimize" them
away without re-deciding:

- **Subscriptions only. No API key, no API credits, no pay-as-you-go.**
- **No local models.** (Considered; off the table.)
- **Runs on a Fly Machine.**
- **Claude Code is the harness for Claude; Pi is the harness for other models.**
- **Models: Claude (Anthropic Max) + GLM (z.ai GLM Coding Plan).** ChatGPT was
  considered and **dropped** (see below).
- **Goals: cost-saving / "subscription maxing" + diversity of thought.**

## Why the architecture is what it is

### Why a Fly box, not GitHub-hosted Actions
Subscription auth (`claude setup-token` OAuth, Pi's login) is designed for a
persistent, logged-in machine — not ephemeral GitHub-hosted runners. Injecting
subscription OAuth tokens into GitHub runners is fragile and the most
ToS-exposed path. So: **agents run on Fly; GitHub is only the state store + the
human UI.** The official `claude-code-action` is therefore *not* used — it's for
GitHub-hosted runners. GitHub Actions remains fine for cheap CI (tests/lint) if
you want it, separate from this.

### Why Claude stays in Claude Code and only GLM goes through Pi
⚠️ Anthropic (since ~April 2026) blocks **Claude subscription OAuth tokens from
third-party harnesses** — using them in tools other than Claude's own is a ToS
violation and is enforced server-side. Claude Code is the *sanctioned* path for
a Claude subscription. GLM via z.ai is fine in **either** Claude Code or Pi
(both are on z.ai's supported-tools list). So Claude→Claude Code, GLM→Pi keeps
everything inside sanctioned harnesses.

### Why ChatGPT was dropped
A ChatGPT *subscription* driven headlessly through Pi (a third-party harness) is
the same ToS category Anthropic bans for its own tokens, and OpenAI's posture is
similar; it was also the fiddliest to auth on a headless box (OAuth credential
transfer vs. z.ai's two env vars). GLM already gives a genuinely different model
family (different lab, different training) — i.e. real diversity of thought —
so ChatGPT's marginal value didn't justify the risk. If a third family is ever
wanted, the sanctioned path is **Codex CLI** (OpenAI's own harness), added as a
new runner — accept it's the at-risk leg.

### Why the token gate is *soft*, not a true preflight
⚠️ Subscriptions expose **no remaining-balance API**. So the gate self-tracks:
- **Claude pool** — token-counted from Claude Code's `--output-format json`
  output, tallied against rolling 5h/weekly **soft** budgets. The denominator is
  fuzzy: Anthropic doesn't publish exact per-window token allowances and they
  vary with server load. The budget numbers in `config.py` are placeholders.
- **GLM pool** — prompt-counted (z.ai is prompt-quota'd), vs the published tier
  limits; your z.ai dashboard is the calibration source of truth.
- **429 backstop** — anything the estimate misses surfaces as a rate-limit error
  and parks the issue (`blocked:budget`), retried when a window resets.
This was explicitly accepted as good-enough; it cannot be exact on subscriptions.

### The real payoff of two pools: maxing + failover
Routing isn't just "block if low." Each stage has a priority list of families
(`config.ROUTING`); the router picks the first with headroom. Bulk **implement**
prefers cheap GLM (spreads load off the Max plan); **spec/review** prefer Claude.
When one pool's window is tapped, work **fails over** to the other; only when
**both** are tapped does the issue park. That's the "subscription maxing."

### Diversity of thought, concretely
Whoever **implements** gets reviewed by the **other** model family (router
enforces the exclusion). Cross-model **disagreement** on a spec is the trigger to
escalate to a human (`human:needed`) rather than guess. So "diversity of thought"
is an actual escalation rule, not a vibe.

### State lives in GitHub labels + a volume-side JSON store
One `flow:*` label = where an issue is. Humans drive gates with `approve` and
`human:resolved`; the bot raises `human:needed` (judgement call) and
`blocked:budget` (all pools tapped). Detail the labels can't hold (branch, PR#,
review round, implementer family, paused guidance, clarify-loop cursor) lives in
`store.issue_meta` on the Fly volume. Single Machine = single writer; the design
assumes that.

### Specs as files, one branch/PR per issue
Specs are written to `specs/<issue>/*.md` **on the work branch** so the reviewer
reads them next to the diff. (Sub-issues were considered; files were simpler.)
All of an issue's work items go in one branch `pipeline/issue-N` → one PR.

## ⚠️ Billing safety (do not skip)
- **Never set `ANTHROPIC_API_KEY`** anywhere (image, `fly secrets`, shell). If
  present it takes precedence and Claude Code **bills per-token, bypassing your
  subscription.** `runners._base_env()` strips it defensively, but don't rely on
  that alone.
- Known edge case: `claude -p` can bill as **API** usage if your account's org
  also has API access, even with OAuth. **After the first runs, confirm zero
  charges on platform.claude.com.**
- The sanctioned headless Claude auth is `claude setup-token` →
  `CLAUDE_CODE_OAUTH_TOKEN`; usage draws from the subscription.

## Open items / your call before relying on it
1. **Pi headless mode.** Pi is TUI-first; confirm your installed Pi's
   non-interactive entry point and fix the one `cmd` in `runners.GlmPiRunner`.
   Configure Pi's z.ai provider in `~/.pi`. If it doesn't fit cleanly, set
   `GLM_RUNNER=claude-code-zai` — same GLM models via Claude Code's z.ai
   endpoint, guaranteed headless. *(This is the least-certain part of the build.)*
2. **Calibrate budgets.** `CLAUDE_5H_TOKEN_BUDGET` / weekly and `GLM_TIER` /
   `GLM_QUOTA_MULTIPLIER` are conservative placeholders — tune against real runs
   and the z.ai dashboard.
3. **Fill `CLAUDE.md`** project commands (test/lint/build) — implement & review
   depend on them.
4. **z.ai data residency.** GLM runs on a Chinese provider (Zhipu). Fine for
   hobby/OSS; reconsider for proprietary or regulated code.
5. **z.ai supported-tools rule.** Only ever call GLM *through* Claude Code or Pi,
   never a raw endpoint call from the orchestrator.

## Deliberately NOT done (so you don't think it's a bug)
- No third model family (ChatGPT dropped — see above).
- **Polling, not webhooks** — handlers are event-shaped, so swapping to webhooks
  later is easy.
- Specs are files, not GitHub sub-issues.
- Single Machine only (the state machine assumes one writer).

## ⚠️ Volatility to re-check periodically
The Anthropic subscription / Agent-SDK billing picture has been moving: a June 15
2026 change that would have split programmatic/Agent-SDK/GitHub-Actions usage
into a separate dollar credit pool was **announced and then paused**, so that
usage currently still draws from subscription limits. If that flips back, the
economics and even the feasibility of subscription-only automation change. Re-
check before assuming the cost model still holds.

## Where things live (for navigation)
- Routing table + budgets + state labels: `orchestrator/config.py`
- Budget ledger (the gate): `orchestrator/store.py` → `Ledger`
- Family selection under the gate: `orchestrator/router.py`
- Model adapters (Claude Code / GLM): `orchestrator/runners.py`
- Stage logic + flow transitions: `orchestrator/stages.py`
- Poll loop + signal handling: `orchestrator/main.py`
- Prompts + structured-output contract: `orchestrator/prompts.py`
