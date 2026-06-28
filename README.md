# GitHub agentic build pipeline (subscription-only, Claude + GLM)

An orchestrator that drives your workflow — issue → summary loop → approval →
spec(s)/work items → implement in a branch → cross-model code review → merge —
entirely on **subscriptions you already pay for**, with a **token-budget gate**
in front of every model call.

- **Claude** (Anthropic Max) via the **Claude Code** CLI, authed with a
  subscription OAuth token. No API key, no API credits.
- **GLM** (z.ai **GLM Coding Plan**) via the **Pi** harness (or Claude Code
  pointed at z.ai's Anthropic-compatible endpoint). Flat subscription; when its
  quota is spent it simply stops — no overage billing.
- Runs as **one always-on Fly Machine**. GitHub is the state store + your UI.

> Why a Fly box and not GitHub Actions? Subscription auth (`claude setup-token`,
> Pi's login) is built for a persistent, logged-in machine — not ephemeral
> GitHub-hosted runners. So the agents run on Fly; GitHub holds the state.

## Your workflow → the state machine

One `flow:*` label marks where each issue is. You drive the gates from the
GitHub UI with two labels.

```
 (add `pipeline` label)
        │
        ▼
 flow:summarize ──► flow:clarify ⇄ (you reply)        # summary loop
        │  └──────────────► flow:approval
        ▼                       │ (you add `approve`)   # approval via status change
 flow:spec  ── needs_human? ──► human:needed ⇄ (you decide + `human:resolved`)
        │   (a different model peer-reviews the spec = diversity of thought)
        ▼
 flow:implement  (work items implemented in one branch `pipeline/issue-N`)
        ▼
 flow:review  ── request_changes ──► (loop back to implement, bounded)
        │     └─ approve
        ▼
 flow:merge ──► (you click merge, or AUTO_MERGE on green) ──► flow:done
```

At any judgement call the bot adds **`human:needed`** and comments the reason;
you reply and add **`human:resolved`** to resume. If every model pool is out of
quota the bot adds **`blocked:budget`** and retries automatically when a window
resets.

## How the budget gate works (and its honest limits)

Subscriptions expose no "remaining balance" API, so the gate is a **soft,
self-tracked ledger** plus a **429 backstop** — exactly as discussed:

- **Claude pool**: token-counted. We read exact input/output tokens from Claude
  Code's `--output-format json` per call and tally them against rolling 5-hour
  and weekly soft budgets (`CLAUDE_5H_TOKEN_BUDGET`, `CLAUDE_WEEK_TOKEN_BUDGET`).
  These numbers are *calibration knobs* — Anthropic doesn't publish exact
  per-window allowances and they vary with server load. Start conservative.
- **GLM pool**: prompt-counted (z.ai is prompt-quota'd). We count one prompt
  per call × a safety multiplier, against the published tier limits
  (`GLM_TIER` = lite/pro/max). Your z.ai dashboard is the source of truth to
  calibrate against.
- **Routing = subscription maxing + failover**: each stage has a priority list
  of model families (`config.ROUTING`); the router picks the first with
  headroom. Bulk *implement* prefers cheap GLM; *spec*/*review* prefer Claude.
  When one pool's window is tapped, work **fails over** to the other. Only when
  **both** are tapped does the issue park as `blocked:budget`.
- **Diversity of thought**: whoever *implements* gets reviewed by the **other**
  family. Cross-model disagreement on a spec is what triggers a `human:needed`
  escalation.

It cannot be exact. The 429 backstop catches anything the estimate misses.

## Setup

### 1. Generate the subscription credentials (on your own logged-in machine)
```bash
# Claude (Max): produces a 1-year OAuth token. Keep ANTHROPIC_API_KEY UNSET.
claude setup-token            # copy the sk-ant-oat01-... value

# z.ai: create a key at the Z.ai Open Platform (GLM Coding Plan subscriber).
```

### 2. Create the Fly app + volume
```bash
fly apps create gh-orchestrator
fly volumes create orchestrator_data --size 1 --region ams   # your region
```

### 3. Set secrets — ⚠️ do NOT set ANTHROPIC_API_KEY anywhere
```bash
fly secrets set \
  GH_TOKEN=ghp_xxx \
  GH_REPO=owner/repo \
  BOT_LOGIN=your-bot-username \
  CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-xxx \
  ZAI_AUTH_TOKEN=xxx
```
If `ANTHROPIC_API_KEY` is present, Claude Code **bills per-token and bypasses
your subscription**. After the first runs, confirm there are **zero charges**
on platform.claude.com (a known `-p` edge case bills API if your org also has
API access — verify once).

### 4. Create the labels and deploy
```bash
./scripts/setup_labels.sh owner/repo     # needs the gh CLI
fly deploy
fly logs                                  # watch it come up
```

### 5. Use it
Open an issue describing what you want, add the **`pipeline`** label, and watch
the flow labels move. Answer clarifications in comments; add **`approve`** to
green-light the spec; add **`human:resolved`** after any judgement call.

## Configuration (env / `fly.toml [env]`)

| Var | Default | Meaning |
|-----|---------|---------|
| `GLM_RUNNER` | `pi` | `pi` (your choice) or `claude-code-zai` (guaranteed-headless fallback) |
| `GLM_TIER` | `lite` | Sizes the GLM budget: lite/pro/max |
| `AUTO_MERGE` | `false` | `true` = merge automatically on green; else you click merge |
| `MAX_REVIEW_ROUNDS` | `3` | Fix-loop cap before escalating to you |
| `POLL_INTERVAL` | `30` | Seconds between GitHub polls |
| `TRIGGER_LABEL` | `pipeline` | Label that admits an issue to the pipeline |
| `CLAUDE_5H_TOKEN_BUDGET` | `3000000` | Soft Claude 5h cap — calibrate |
| `GLM_QUOTA_MULTIPLIER` | `2.0` | Quota units charged per GLM call (peak/off-peak safety) |
| `BUDGET_SAFETY_FRACTION` | `0.85` | Park at this fraction of a window |

## Things to verify / know before relying on it

- **Pi headless mode.** Pi is TUI-first; confirm your installed Pi's
  non-interactive entry point and adjust the one `cmd` in
  `orchestrator/runners.py` (`GlmPiRunner`). If it doesn't fit, set
  `GLM_RUNNER=claude-code-zai` — same GLM models, definitely headless. Configure
  Pi's z.ai provider in `~/.pi` per Pi's provider docs (key = `ZAI_AUTH_TOKEN`,
  base URL = z.ai).
- **z.ai supported-tools rule.** The GLM Coding Plan is limited to officially
  supported tools (Claude Code, Pi, …). This pipeline only ever calls GLM
  *through* those harnesses — keep it that way; don't add raw endpoint calls.
- **z.ai data residency.** GLM runs on a Chinese provider (Zhipu). Fine for
  hobby/OSS; reconsider for proprietary or regulated code.
- **Claude OAuth is for Claude Code only.** Using a Claude subscription token in
  third-party harnesses violates Anthropic's terms and is blocked — that's why
  Claude stays in Claude Code and only GLM goes through Pi.
- **Single writer.** Run exactly one Machine; the state machine assumes one.

## Development / tests

The orchestrator's pure logic — the budget ledger, the router, structured-output
parsing, prompt rendering, and the route→run→meter path — is covered by a
hermetic unit suite under `tests/` (no network, no model calls, no subprocesses;
the environment is stubbed in `tests/conftest.py`).

```bash
pip install -r requirements-dev.txt
python3 -m pytest
```

CI runs the same suite on every push (`.github/workflows/tests.yml`). This is
the cheap GitHub-Actions CI the design explicitly keeps separate from the
subscription-driven agents on Fly.

## Extending

- **Sub-issues instead of `specs/*.md`**: swap `repo.write_specs` + the spec
  prompt to create GitHub sub-issues per work item.
- **Webhooks instead of polling**: add a tiny web service and have GitHub push
  events; the stage handlers are already event-shaped.
- **More models / families**: add a runner in `runners.py`, a family to
  `config.ROUTING`, and the router handles failover automatically.

## Files
```
fly.toml                  one always-on Machine + volume
Dockerfile                node + claude-code + pi + python orchestrator
orchestrator/
  config.py               env, state labels, routing table, budgets
  main.py                 poll loop + signal handling (approve/resolved/budget)
  stages.py               the 4 stages + flow dispatch
  router.py               pick a model family under the budget gate
  runners.py              Claude Code + GLM (Pi / z.ai) adapters
  store.py                budget ledger + per-issue metadata (on the volume)
  repo.py                 git checkout / branch / commit / push helpers
  github_client.py        thin GitHub REST client
  prompts.py              stage prompt templates (structured JSON contract)
scripts/setup_labels.sh   create the labels
CLAUDE.md                 guidance every agent reads
```
