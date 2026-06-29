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
    session_id: str = ""            # Claude Code session id for resumption


_JSON_BLOCK = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_RL_HINT = re.compile(r"rate.?limit|429|overloaded|quota.*exceed|usage limit", re.I)
_SESSION_NOT_FOUND_HINT = re.compile(r"session.*not found|invalid.*session|session.*expired|session.*evicted", re.I)


def _loads(s):
    """json.loads, tolerating trailing commas (a common model slip). None on fail."""
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        fixed = re.sub(r",(\s*[}\]])", r"\1", s)        # drop trailing commas
        if fixed != s:
            try:
                return json.loads(fixed)
            except json.JSONDecodeError:
                pass
        return None


def _balanced_objects(text):
    """Every top-level {...} substring in `text`, in document order, respecting
    string literals and escapes (so braces inside strings don't miscount)."""
    out, depth, start, in_str, esc = [], 0, -1, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                out.append(text[start:i + 1])
    return out


def parse_structured(text):
    """Pull the result object out of a model reply, tolerantly. Prefer the LAST
    ```json fenced block (the contract — and the only fence the two-layer
    instruction loader leaves as ```json, so injected examples can't hijack it);
    fall back to the LAST balanced {...} object anywhere in the reply. The
    fallback recovers a run that emitted the JSON but dropped/broke the closing
    fence, trailed prose after it, or otherwise lost the exact contract."""
    if not text:
        return {}
    for blk in reversed(_JSON_BLOCK.findall(text)):       # preferred: fenced
        obj = _loads(blk)
        if isinstance(obj, dict):
            return obj
    for blk in reversed(_balanced_objects(text)):         # fallback: bare object
        obj = _loads(blk)
        if isinstance(obj, dict) and obj:                 # skip stray {} / non-dicts
            return obj
    return {}


def _run(cmd, cwd, env, pool):
    """Run a model subprocess. Returns (proc, blob); on timeout returns
    (None, "timeout") so callers can tell a wall-clock kill from a clean exit."""
    try:
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True,
                           text=True, timeout=DEFAULT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    blob = (p.stdout or "") + "\n" + (p.stderr or "")
    if p.returncode != 0 and _RL_HINT.search(blob):
        raise RateLimited(pool)
    return p, blob


def _base_env():
    env = dict(os.environ)
    # Never let an API key sneak in — it would bill per-token, bypassing the sub.
    env.pop("ANTHROPIC_API_KEY", None)
    return env


# A code-writing stage (implement/fix) edits many files, writes tests, and runs
# them — it needs far more turns than a read-and-reason stage. The effort knob
# tunes how hard the model THINKS; this floor keeps a write stage from running
# out of turns mid-feature (the failure that stranded issue #1's implement work).
WRITE_TURN_FLOOR = 120


def _apply_effort(prompt, effort, write=False):
    """Prepend the effort directive and return (prompt, max_turns).

    Prepended (not appended) so the prompt's trailing 'end with one json block'
    instruction stays last. Code-writing stages get a turn floor so they don't
    stop before reporting `done`.
    """
    t = config.effort_tuning(effort)
    max_turns = max(t["max_turns"], WRITE_TURN_FLOOR) if write else t["max_turns"]
    if t["directive"]:
        prompt = f"{t['directive']}\n\n{prompt}"
    return prompt, max_turns


def _claude_cc_json(stdout):
    """Parse `claude -p --output-format json` -> (final_text, in_tok, out_tok,
    structured, session_id). `structured` is the envelope's `structured_output`
    field — the parsed object the CLI returns when run with `--json-schema`
    (None otherwise); callers prefer it over re-parsing the text. `session_id`
    is the envelope's `session_id` field for resuming conversations.

    `in_tok` is an *effective* input count for the budget gate: fresh input +
    cache-creation at full weight, plus cache-READS weighted down
    (config.CLAUDE_CACHE_READ_WEIGHT). Cache reads are re-counted every turn and
    priced ~0.1x, so charging them at 1x over-counts a long agentic run ~10x vs
    the real subscription — the bug that wrongly parked Claude."""
    text, itok, otok, structured, session_id = "", 0, 0, None, ""
    try:
        obj = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
    except (json.JSONDecodeError, IndexError):
        return stdout, 0, 0, None, ""
    text = obj.get("result") or obj.get("text") or ""
    usage = obj.get("usage") or {}
    itok = (usage.get("input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
            + int(config.CLAUDE_CACHE_READ_WEIGHT * usage.get("cache_read_input_tokens", 0)))
    otok = usage.get("output_tokens", 0)
    structured = obj.get("structured_output")
    session_id = obj.get("session_id") or ""
    return text, itok, otok, structured, session_id


def _data_from(text, structured):
    """The parsed result object for a run: prefer the CLI's `structured_output`
    (constrained by `--json-schema`), else the tolerant parse of the final text."""
    if isinstance(structured, dict) and structured:
        return structured
    return parse_structured(text)


class ClaudeCodeRunner:
    family = "claude"

    def run(self, prompt, cwd, write=False, model=None, effort="medium", schema=None, resume=None):
        env = _base_env()
        env["CLAUDE_CODE_OAUTH_TOKEN"] = config.CLAUDE_CODE_OAUTH_TOKEN
        env.pop("ANTHROPIC_BASE_URL", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        prompt, max_turns = _apply_effort(prompt, effort, write=write)
        cmd = ["claude", "-p", prompt, "--output-format", "json",
               "--permission-mode", "bypassPermissions", "--max-turns", str(max_turns)]
        if resume:
            cmd += ["--resume", resume]
        # Constrain the result to schema-valid JSON (and get a parsed
        # `structured_output` back). The flag takes the schema INLINE as a string.
        if schema:
            cmd += ["--json-schema",
                    schema if isinstance(schema, str) else json.dumps(schema)]
        if model:
            cmd += ["--model", model]
        res, blob = _run(cmd, cwd, env, "claude")
        # Graceful stale-session fallback: if resume was passed and the run failed
        # with a session-not-found error, retry once without --resume
        if (resume and res is not None and res.returncode != 0 and
                _SESSION_NOT_FOUND_HINT.search(blob)):
            # Retry without --resume for a fresh session
            retry_cmd = [c for i, c in enumerate(cmd) if c not in ("--resume", resume) or
                         (i > 0 and cmd[i-1] != "--resume")]
            res, blob = _run(retry_cmd, cwd, env, "claude")
        if res is None:                                   # timed out
            return RunResult(ok=False, text="", raw=blob)
        text, itok, otok, structured, session_id = _claude_cc_json(res.stdout)
        return RunResult(ok=res.returncode == 0, text=text,
                         data=_data_from(text, structured),
                         input_tokens=itok, output_tokens=otok, raw=blob, session_id=session_id)


class GlmClaudeCodeRunner:
    """GLM via Claude Code pointed at z.ai (Anthropic-compatible endpoint)."""
    family = "glm"

    def run(self, prompt, cwd, write=False, model="glm-4.7", effort="medium", schema=None, resume=None):
        env = _base_env()
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)          # use z.ai, not Anthropic
        env["ANTHROPIC_BASE_URL"] = config.ZAI_BASE_URL
        env["ANTHROPIC_AUTH_TOKEN"] = config.ZAI_AUTH_TOKEN
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        prompt, max_turns = _apply_effort(prompt, effort, write=write)
        # NB: `schema` is intentionally NOT forwarded as `--json-schema` here —
        # z.ai's Anthropic-compatible endpoint may not honor the output_config
        # the flag adds, so we rely on the tolerant parser instead (the schema is
        # accepted for a uniform runner signature).
        cmd = ["claude", "-p", prompt, "--output-format", "json",
               "--permission-mode", "bypassPermissions", "--max-turns", str(max_turns),
               "--model", model]
        if resume:
            cmd += ["--resume", resume]
        res, blob = _run(cmd, cwd, env, "glm")
        # Graceful stale-session fallback: if resume was passed and the run failed
        # with a session-not-found error, retry once without --resume
        if (resume and res is not None and res.returncode != 0 and
                _SESSION_NOT_FOUND_HINT.search(blob)):
            # Retry without --resume for a fresh session
            retry_cmd = [c for i, c in enumerate(cmd) if c not in ("--resume", resume) or
                         (i > 0 and cmd[i-1] != "--resume")]
            res, blob = _run(retry_cmd, cwd, env, "glm")
        if res is None:                                   # timed out
            return RunResult(ok=False, text="", raw=blob)
        text, itok, otok, structured, session_id = _claude_cc_json(res.stdout)
        return RunResult(ok=res.returncode == 0, text=text,
                         data=_data_from(text, structured),
                         input_tokens=itok, output_tokens=otok, raw=blob, session_id=session_id)


class GlmPiRunner:
    """GLM via the Pi harness.

    NOTE: Pi is TUI-first. Confirm your installed Pi's non-interactive entry
    point and adjust the `cmd` below if needed (e.g. a --print/-p flag or
    stdin). Provider config for z.ai lives in ~/.pi (see README). If your Pi
    build has no clean headless mode, set GLM_RUNNER=claude-code-zai — same
    models, definitely headless.
    """
    family = "glm"

    def run(self, prompt, cwd, write=False, model="glm-4.7", effort="medium", schema=None, resume=None):
        # resume is accepted for signature uniformity but ignored (Pi has no resume)
        env = _base_env()
        env["PI_MODEL"] = model
        prompt, _ = _apply_effort(prompt, effort)   # Pi has no max-turns flag here
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
