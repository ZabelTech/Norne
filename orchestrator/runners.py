"""Model-runner adapters.

Each runner takes a prompt + working dir and returns a RunResult. Two families:

  claude  -> Claude Code CLI, native auth (CLAUDE_CODE_OAUTH_TOKEN = Max sub).
  glm     -> z.ai GLM Coding Plan, via EITHER:
               GLM_RUNNER="pi"             -> Pi harness  (what you asked for)
               GLM_RUNNER="claude-code-zai"-> Claude Code pointed at z.ai's
                                              Anthropic-compatible endpoint
                                              (guaranteed-headless fallback)

Billing safety: the Claude runner's subprocess env has ANTHROPIC_API_KEY
*stripped*, so Claude Code always falls back to the subscription OAuth token.
"""
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from . import config

DEFAULT_TIMEOUT = int(os.environ.get("RUN_TIMEOUT", "1800"))  # 30 min/step


class RateLimited(Exception):
    """A pool hit its provider rate limit (the 429 backstop)."""
    def __init__(self, pool):
        self.pool = pool
        super().__init__(f"{pool} rate limited")


@dataclass
class RunResult:
    ok: bool
    text: str                       # the model's final message
    data: dict = field(default_factory=dict)   # parsed trailing ```json block
    input_tokens: int = 0
    output_tokens: int = 0
    raw: str = ""


_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_RL_HINT = re.compile(r"rate.?limit|429|overloaded|quota.*exceed|usage limit", re.I)


def parse_structured(text):
    """Pull the LAST ```json {...} ``` block out of a model reply."""
    blocks = _JSON_BLOCK.findall(text or "")
    if not blocks:
        return {}
    try:
        return json.loads(blocks[-1])
    except json.JSONDecodeError:
        return {}


def _run(cmd, cwd, env, pool):
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True,
                           text=True, timeout=DEFAULT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return RunResult(ok=False, text="", raw="timeout")
    blob = (p.stdout or "") + "\n" + (p.stderr or "")
    if p.returncode != 0 and _RL_HINT.search(blob):
        raise RateLimited(pool)
    return p, blob


def _base_env():
    env = dict(os.environ)
    # Never let an API key sneak in — it would bill per-token, bypassing the sub.
    env.pop("ANTHROPIC_API_KEY", None)
    return env


def _claude_cc_json(stdout):
    """Parse `claude -p --output-format json` -> (final_text, in_tok, out_tok)."""
    text, itok, otok = "", 0, 0
    try:
        obj = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
    except (json.JSONDecodeError, IndexError):
        return stdout, 0, 0
    text = obj.get("result") or obj.get("text") or ""
    usage = obj.get("usage") or {}
    itok = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
    otok = usage.get("output_tokens", 0)
    return text, itok, otok


class ClaudeCodeRunner:
    family = "claude"

    def run(self, prompt, cwd, write=False, model=None):
        env = _base_env()
        env["CLAUDE_CODE_OAUTH_TOKEN"] = config.CLAUDE_CODE_OAUTH_TOKEN
        env.pop("ANTHROPIC_BASE_URL", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        cmd = ["claude", "-p", prompt, "--output-format", "json",
               "--permission-mode", "bypassPermissions", "--max-turns", "40"]
        if model:
            cmd += ["--model", model]
        res, blob = _run(cmd, cwd, env, "claude")
        text, itok, otok = _claude_cc_json(res.stdout)
        return RunResult(ok=res.returncode == 0, text=text, data=parse_structured(text),
                         input_tokens=itok, output_tokens=otok, raw=blob)


class GlmClaudeCodeRunner:
    """GLM via Claude Code pointed at z.ai (Anthropic-compatible endpoint)."""
    family = "glm"

    def run(self, prompt, cwd, write=False, model="glm-4.7"):
        env = _base_env()
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)          # use z.ai, not Anthropic
        env["ANTHROPIC_BASE_URL"] = config.ZAI_BASE_URL
        env["ANTHROPIC_AUTH_TOKEN"] = config.ZAI_AUTH_TOKEN
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        cmd = ["claude", "-p", prompt, "--output-format", "json",
               "--permission-mode", "bypassPermissions", "--max-turns", "40",
               "--model", model]
        res, blob = _run(cmd, cwd, env, "glm")
        text, itok, otok = _claude_cc_json(res.stdout)
        return RunResult(ok=res.returncode == 0, text=text, data=parse_structured(text),
                         input_tokens=itok, output_tokens=otok, raw=blob)


class GlmPiRunner:
    """GLM via the Pi harness.

    NOTE: Pi is TUI-first. Confirm your installed Pi's non-interactive entry
    point and adjust the `cmd` below if needed (e.g. a --print/-p flag or
    stdin). Provider config for z.ai lives in ~/.pi (see README). If your Pi
    build has no clean headless mode, set GLM_RUNNER=claude-code-zai — same
    models, definitely headless.
    """
    family = "glm"

    def run(self, prompt, cwd, write=False, model="glm-4.7"):
        env = _base_env()
        env["PI_MODEL"] = model
        # Best-effort non-interactive invocation; verify against your Pi version.
        cmd = ["pi", "--print", "--model", model, prompt]
        res, blob = _run(cmd, cwd, env, "glm")
        text = res.stdout if isinstance(res, subprocess.CompletedProcess) else ""
        return RunResult(ok=getattr(res, "returncode", 1) == 0, text=text,
                         data=parse_structured(text), raw=blob)


_CLAUDE = ClaudeCodeRunner()
_GLM = GlmPiRunner() if config.GLM_RUNNER == "pi" else GlmClaudeCodeRunner()


def get_runner(family):
    return _CLAUDE if family == "claude" else _GLM
