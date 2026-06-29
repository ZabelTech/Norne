"""Central config, state-machine constants, and the model routing table."""
import math
import os


def _b(name, default="false"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


# ── Connection ────────────────────────────────────────────────────────────
GH_TOKEN = os.environ["GH_TOKEN"]
GH_REPO = os.environ["GH_REPO"]                       # "owner/repo"
BOT_LOGIN = os.environ.get("BOT_LOGIN", "").lower()    # informational; NOT the identity
# signal anymore — bot comments are tagged with a marker (see github_client), so the
# pipeline works even when it posts under a human's own token (shared identity).

CLAUDE_CODE_OAUTH_TOKEN = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
ZAI_AUTH_TOKEN = os.environ.get("ZAI_AUTH_TOKEN", "")
ZAI_BASE_URL = os.environ.get("ZAI_BASE_URL", "https://api.z.ai/api/anthropic")

GLM_RUNNER = os.environ.get("GLM_RUNNER", "pi")        # "pi" | "claude-code-zai"
GLM_TIER = os.environ.get("GLM_TIER", "lite")          # lite | pro | max

# ── Behaviour ─────────────────────────────────────────────────────────────
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
AUTO_MERGE = _b("AUTO_MERGE")
MAX_REVIEW_ROUNDS = int(os.environ.get("MAX_REVIEW_ROUNDS", "3"))
MAX_SPEC_ROUNDS = int(os.environ.get("MAX_SPEC_ROUNDS", "3"))   # author<->reviewer spec loop
TRIGGER_LABEL = os.environ.get("TRIGGER_LABEL", "pipeline")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
WORKDIR_ROOT = os.environ.get("WORKDIR_ROOT", os.path.join(DATA_DIR, "work"))

# ── Concurrency (Level 1 + Level 2) ─────────────────────────────────────────
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "1"))          # pooled workers across issues
MAX_SPEC_WORKERS = int(os.environ.get("MAX_SPEC_WORKERS", str(MAX_WORKERS)))  # per-issue spec fan-out

# ── Instructions (two-layer: general + per-stage) ────────────────────────
# Instructions directory lives at the repo root (config.py is at orchestrator/config.py)
_INSTRUCTIONS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTRUCTIONS_DIR = os.environ.get("INSTRUCTIONS_DIR", os.path.join(_INSTRUCTIONS_ROOT, "instructions"))
INSTRUCTIONS_INJECT_GENERAL = _b("INSTRUCTIONS_INJECT_GENERAL", "true")
INSTRUCTION_FOLDER_BY_STEP = {
    "summarize": "summarize",
    "spec": "spec",
    "spec_review": "spec",
    "implement": "implement",
    "fix": "implement",
    "review": "review",
}

# ── Budget gate (soft) ────────────────────────────────────────────────────
# Anthropic does not publish exact per-window token allowances and they vary
# with server load, so these are *soft* calibration knobs. Start conservative,
# watch real runs, and adjust. The 429 backstop in main.py catches overruns.
CLAUDE_5H_TOKEN_BUDGET = int(os.environ.get("CLAUDE_5H_TOKEN_BUDGET", "3000000"))
CLAUDE_WEEK_TOKEN_BUDGET = int(os.environ.get("CLAUDE_WEEK_TOKEN_BUDGET", "40000000"))

# z.ai is *prompt*-quota'd. These mirror published GLM Coding Plan tiers.
# GLM-5.2/5-Turbo consume quota at a multiplier (3x peak / 2x off-peak, 1x
# off-peak promo through Sep 2026); we apply a flat safety multiplier per call.
GLM_TIER_LIMITS = {
    "lite": {"per5h": 80,   "perweek": 400},
    "pro":  {"per5h": 400,  "perweek": 2000},
    "max":  {"per5h": 1600, "perweek": 8000},
}
GLM_QUOTA_MULTIPLIER = float(os.environ.get("GLM_QUOTA_MULTIPLIER", "2.0"))
# leave headroom so we park before slamming the wall (GLM + the default)
BUDGET_SAFETY_FRACTION = float(os.environ.get("BUDGET_SAFETY_FRACTION", "0.85"))
# Claude gets its own per-window ceilings: a higher 5h fraction (short window
# refills fast, so we can run it hotter) and a lower weekly one (the weekly cap
# is the real ceiling — protect it harder).
CLAUDE_5H_SAFETY_FRACTION = float(os.environ.get("CLAUDE_5H_SAFETY_FRACTION", "0.90"))
CLAUDE_WEEK_SAFETY_FRACTION = float(os.environ.get("CLAUDE_WEEK_SAFETY_FRACTION", "0.80"))
# Cache-READ tokens are re-counted every agentic turn and priced ~0.1x; counting
# them at full weight is what made the local ledger over-count ~10x vs the real
# subscription. Weight them down so the gate tracks effective usage, not cache
# churn. (cache-CREATION is a real write at ~1.25x, counted at full weight.)
CLAUDE_CACHE_READ_WEIGHT = float(os.environ.get("CLAUDE_CACHE_READ_WEIGHT", "0.1"))
# When a provider actually rate-limits a family (z.ai 429 / overloaded), back
# that family off for this long. The LOCAL ledger can't see the provider's real
# quota, so without a cooldown the blocked:budget gate un-parks on stale local
# headroom and retries the same dead family every poll (a tight thrash loop).
RATE_LIMIT_COOLDOWN = int(os.environ.get("RATE_LIMIT_COOLDOWN", "600"))  # 10 min

# Budget reservation estimates — deliberately generous to handle concurrent runs.
# N concurrent model subprocesses multiply token/prompt spend, so we pre-charge
# conservatively to prevent window overrun.
CLAUDE_RESERVE_TOKENS = int(os.environ.get("CLAUDE_RESERVE_TOKENS", "200000"))
GLM_RESERVE_PROMPTS = int(os.environ.get("GLM_RESERVE_PROMPTS",
                                         str(math.ceil(GLM_QUOTA_MULTIPLIER))))

# ── State machine: one flow:* label = where an issue is right now ──────────
FLOW_SUMMARIZE = "flow:summarize"
FLOW_CLARIFY = "flow:clarify"
FLOW_APPROVAL = "flow:approval"
FLOW_SPEC = "flow:spec"
FLOW_IMPLEMENT = "flow:implement"
FLOW_REVIEW = "flow:review"
FLOW_MERGE = "flow:merge"
FLOW_DONE = "flow:done"
ALL_FLOW = {FLOW_SUMMARIZE, FLOW_CLARIFY, FLOW_APPROVAL, FLOW_SPEC,
            FLOW_IMPLEMENT, FLOW_REVIEW, FLOW_MERGE, FLOW_DONE}

# Human-applied signals (you control these from the GitHub UI):
SIG_APPROVE = "approve"            # approve the summary -> spec stage ("approval via status change")
SIG_RESOLVED = "human:resolved"    # you answered a judgement call -> resume the paused stage

# Bot-applied flags:
FLAG_NEEDS_HUMAN = "human:needed"  # paused on a judgement call, waiting for you
FLAG_BLOCKED_BUDGET = "blocked:budget"  # all pools tapped, parked until a window resets

# ── Routing table ─────────────────────────────────────────────────────────
# Per stage, the runners allowed in priority order. The router picks the first
# whose pool has budget headroom. "review" additionally must use a DIFFERENT
# model family than the one that implemented (that's the diversity-of-thought
# cross-check); see router.py.
ROUTING = {
    "summarize": ["claude", "glm"],   # reasoning-light; Claude preferred, GLM keeps it moving
    "spec":      ["claude", "glm"],   # reasoning-heavy; strongly prefer Claude
    "implement": ["glm", "claude"],   # bulk work -> cheap GLM quota first ("subscription maxing")
    "review":    ["claude", "glm"],   # opposite-family enforced by router
}

# Which GLM model to use per stage (cheap vs flagship) to stretch z.ai quota.
GLM_MODEL_BY_STAGE = {
    "summarize": "glm-5.2",   # front-door understanding gates everything -> flagship
    "spec":      "glm-5.2",
    "implement": "glm-4.7",
    "review":    "glm-5.2",
}

# Which Claude model per stage (Opus 4.8 = best, Sonnet 4.6 = balanced). Mirrors
# the GLM split: the best model for the heavy reasoning stages — spec and review
# — and the balanced one for the front-door loop (summarize) and bulk/failover
# implement. review uses the BEST model on purpose (see _model_for / router).
CLAUDE_MODEL_BY_STAGE = {
    "summarize": "claude-sonnet-4-6",
    "spec":      "claude-opus-4-8",
    "implement": "claude-sonnet-4-6",
    "review":    "claude-opus-4-8",
}

# Reasoning effort per MODEL (not per stage): a run uses the effort of whichever
# model the router actually picked, so you can dial a specific (big/expensive)
# model independently. Carried in the bot-comment marker [norne-{model}-{effort}].
# Unknown models default to medium.
EFFORT_BY_MODEL = {
    "claude-opus-4-8":   "medium",
    "claude-sonnet-4-6": "medium",
    "glm-5.2":           "medium",
    "glm-4.7":           "medium",
}


def effort_for(model):
    """The reasoning-effort level for a model (defaults to medium)."""
    return EFFORT_BY_MODEL.get(model, "medium")

# What an effort label actually DOES to a run: a reasoning directive prepended
# to the prompt (the "think"/"think hard" keywords trigger Claude Code's
# extended thinking, and read as plain instructions for GLM via z.ai) plus a
# max-turns budget (more turns = more room to investigate before answering).
EFFORT_TUNING = {
    "low":    {"directive": "",
               "max_turns": 25},
    "medium": {"directive": "Think it through before you answer.",
               "max_turns": 40},
    "high":   {"directive": "Think hard. Investigate thoroughly and reason "
                            "step by step before you answer.",
               "max_turns": 60},
}


def effort_tuning(effort):
    """Resolve an effort label to its {directive, max_turns}, defaulting to medium."""
    return EFFORT_TUNING.get(effort, EFFORT_TUNING["medium"])


def family_available(family):
    """True if `family` has the credential it needs to run at all.

    The router consults this so a *single-family* deployment routes correctly
    instead of selecting a family whose token is absent and then failing at
    call time. Leaving CLAUDE_CODE_OAUTH_TOKEN unset therefore yields a clean
    GLM-only ("no-Claude") deployment with no ROUTING changes required.
    """
    if family == "claude":
        return bool(CLAUDE_CODE_OAUTH_TOKEN)
    if family == "glm":
        return bool(ZAI_AUTH_TOKEN)
    return False
