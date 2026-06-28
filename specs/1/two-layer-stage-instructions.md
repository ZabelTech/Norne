# Two-layer stage instructions: general + per-stage, injected into every LLM prompt

# Two-layer stage instructions (general + per-stage), injected into every LLM prompt

## Context

The orchestrator drives an end-to-end LLM code-generation pipeline. Today each model-invoking stage renders a fixed template via `prompts.render` (orchestrator/prompts.py:9-13) and the result is run by a family runner in a per-issue checkout of the TARGET repo. The six model-invoking prompts are SUMMARIZE, SPEC, SPEC_REVIEW, IMPLEMENT, FIX, REVIEW.

Per the approved summary, each LLM stage now receives MERGED agent guidance:
- GENERAL layer: the target repo's own `CLAUDE.md`, read at runtime from the per-issue checkout (`repo_path`). (The approved summary settled this over a fixed baked-in file — 'the target repo's CLAUDE.md as the general layer'. The vitals-mcp CLAUDE.md the human pasted was an example of such a file, not the literal baked-in content.)
- STAGE layer: per-stage `CLAUDE.md` files authored HERE, under `instructions/<folder>/CLAUDE.md`. Folders: summarize, spec, implement, review.

The stage layer is appended AFTER the general layer so it is the more authoritative on conflict. Sub-steps reuse their parent's folder: spec peer-review -> `spec`, fix -> `implement`. Missing files degrade to empty and never block a run.

## Non-goals (per approved scope)
- No per-agent-family (Anthropic/OpenAI/z.ai) variants.
- No target-repo override of the stage layer.
- No instructions for human-gated steps (clarify, approval, merge, done).

## Hard constraints discovered in the code (these drive the design)
1. `runners.parse_structured` (runners.py:46-54) returns the LAST match of `_JSON_BLOCK = r"```json\s*(\{.*?\})\s*```"` (runners.py:42, re.DOTALL). A ```json fence inside injected instructions can hijack the contract: an UNCLOSED ```json in the general layer plus a `{...}` later lets the regex span into the contract and return garbage/`{}` (verified empirically in a prior review round). Mitigation: the loader NEUTRALIZES the json fence tag in BOTH layers — rewrite each three-backtick ```json fence opener to a plain three-backtick ``` fence (matching `json` as a complete info-string so ```json5 / ```jsonc are left untouched) — so instruction fences never satisfy `_JSON_BLOCK`. Content survives; only the syntax-highlight hint is lost. Robust whether the instruction fence is closed or unclosed, and whether it contains braces.
2. `prompts.render` substitutes `<<<TOKEN>>>` in kwarg dict order, so instruction text containing e.g. `<<<SPECS>>>` could be re-expanded. Mitigation: render() pops and applies INSTRUCTIONS LAST, after every other token resolves, and defaults it to `''` when absent (never the literal `'None'`).
3. Two render sites build the prompt before the checkout path is in a local var: handle_summarize renders at stages.py:67 but clones via repo.ensure_repo at :71; handle_review renders at :168 but uses repo.workdir at :172. Both must capture `path` BEFORE rendering so the general layer is read from the real checkout, not a not-yet-cloned/empty path. (ensure_repo is idempotent, so reuse is fine.)
4. handle_spec peer-review renders SPEC_REVIEW (stages.py:112) but routes via `_run("review", ...)`; the instruction FOLDER must be chosen per LOGICAL step at the render site, independent of the routing stage.
5. cwd-aware runners (ClaudeCodeRunner, GlmClaudeCodeRunner) auto-load `<cwd>/CLAUDE.md`, so for those runners the general layer is injected on top of an already-loaded copy. Document this; the stage layer is always the genuine new contribution, and `INSTRUCTIONS_INJECT_GENERAL=false` suppresses the injected general copy.

## Approach

### config (orchestrator/config.py)
Add module attributes (import-time env read, matching existing style; load() references them at call time so tests can monkeypatch the attribute):
- `INSTRUCTIONS_DIR = os.environ.get("INSTRUCTIONS_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "instructions"))` (config.py lives at orchestrator/config.py, so two dirnames = repo root).
- `INSTRUCTIONS_INJECT_GENERAL = _b("INSTRUCTIONS_INJECT_GENERAL", "true")` (default true per approved summary; lets operators suppress the double-loaded general copy).
- `INSTRUCTION_FOLDER_BY_STEP = {"summarize":"summarize", "spec":"spec", "spec_review":"spec", "implement":"implement", "fix":"implement", "review":"review"}`.

### loader (new orchestrator/instructions.py)
`load(step, repo_path, inject_general=None)` returns the merged guidance string (possibly `''`):
- Resolve `inject_general` from `config.INSTRUCTIONS_INJECT_GENERAL` when None. All config lookups happen INSIDE load() (reference `config.X`, never an import-time binding) so monkeypatching works.
- General text: read `os.path.join(repo_path, "CLAUDE.md")` only if inject_general and repo_path are truthy.
- Stage folder: `config.INSTRUCTION_FOLDER_BY_STEP.get(step, step)`; stage text: read `os.path.join(config.INSTRUCTIONS_DIR, folder, "CLAUDE.md")`.
- Private `_read(p)`: `open(p, encoding="utf-8")` (strict), return `f.read().strip()`; on `except (OSError, UnicodeDecodeError): return ""`. (UnicodeDecodeError is a ValueError, not an OSError, so the broad tuple is required; strict decode + swallow means a binary/corrupt CLAUDE.md is skipped — returning `''` rather than injecting U+FFFD garbage — and never blocks a run.)
- Neutralize the json fence tag in EACH layer's text (rewrite the three-backtick ```json opener to a plain ``` fence).
- Merge: skip empty layers. When both present, emit a short labelled 'general' section first and a labelled '<step>' section last whose header states it takes precedence on conflict. Return `''` when both are empty.

### prompts.py
- `render`: `instructions = kw.pop("INSTRUCTIONS", "")`, run the existing per-kwarg replace loop, then finally `out = out.replace("<<<INSTRUCTIONS>>>", str(instructions))`. (INSTRUCTIONS applied last -> collision-safe; default `""` -> absent-safe, never the literal 'None'.)
- Add exactly one `<<<INSTRUCTIONS>>>` to each of the six templates, after the opening paragraph and before the task/issue content, above the trailing json contract.

### stages.py
- Add `instructions` to `from . import config, prompts, repo, runners, router` (module import — enables `monkeypatch.setattr(stages.instructions, "load", ...)`).
- Add `_build_prompt(template, step, path, **tokens)` -> `prompts.render(template, INSTRUCTIONS=instructions.load(step, path), **tokens)`.
- Rewire the six render sites via `_build_prompt` with the correct logical step, capturing `path` before rendering where needed:
  - handle_summarize: `path = repo.ensure_repo(n)` first; `_build_prompt(prompts.SUMMARIZE, "summarize", path, NUM=n, TITLE=..., BODY=..., CLARIFICATIONS=...)`; then `_run("summarize", ..., cwd=path)`.
  - handle_spec author: `_build_prompt(prompts.SPEC, "spec", path, NUM=n, TITLE=..., SUMMARY=summary)`.
  - handle_spec peer-review: `_build_prompt(prompts.SPEC_REVIEW, "spec_review", path, SPECS=_specs_text(specs))` (still routed via `_run("review", ..., exclude_family=fam)`).
  - handle_implement: fix branch -> `_build_prompt(prompts.FIX, "fix", path, NUM=n, ROUND=rnd, FEEDBACK=...)`; else `_build_prompt(prompts.IMPLEMENT, "implement", path, NUM=n)`.
  - handle_review: `path = repo.workdir(n)` first; `_build_prompt(prompts.REVIEW, "review", path, ...)`; then `_run("review", ..., cwd=path, ...)`.

### Authored stage content
Create `instructions/<stage>/CLAUDE.md` for summarize, spec, implement, review: concise, stage-appropriate guidance. Sketch — summarize: understand the issue + target repo, restate goal/scope/non-goals/assumptions, ask at most 3 blocking questions, ground answers in the real repo rather than guessing, end with the exact json contract. spec: turn the approved summary into small independently-reviewable specs, group genuinely related work, give design approach + acceptance criteria + test plan + url-safe slugs, return needs_human for genuine tradeoffs. implement: follow repo conventions + CLAUDE.md, read specs/, implement every work item, write/update tests, run test/lint, make focused commits, do not push or open a PR, report done vs needs_human honestly. review: independent perspective, check correctness/acceptance-criteria/test-coverage/bugs/security, approve only if it genuinely satisfies the spec, request_changes for fixable issues, needs_human for real judgement calls. On conflict these override the general CLAUDE.md.

## Acceptance criteria
1. Every model-invoking render goes through `_build_prompt`, injecting merged general+stage guidance via a single `<<<INSTRUCTIONS>>>` placeholder per template.
2. Each of the six templates contains exactly one `<<<INSTRUCTIONS>>>` (`tpl.count("<<<INSTRUCTIONS>>>") == 1`), positioned before its trailing json contract.
3. With no instruction files present, missing dirs, or non-UTF-8 bytes, `load()` returns `''` and never raises; the pipeline runs unchanged.
4. A ```json fence (even unclosed) in the GENERAL layer does NOT break or replace the model's contract: for merged-instructions followed by a valid trailing ```json contract, `runners.parse_structured` returns the contract dict.
5. Instruction text containing a colliding token (e.g. `<<<SPECS>>>`) survives verbatim in the rendered prompt.
6. Stage layer is appended after the general layer; spec_review resolves to the `spec` folder and fix to the `implement` folder.
7. `INSTRUCTIONS_INJECT_GENERAL=false` suppresses the general layer while keeping the stage layer; default is true.
8. summarize and review capture the checkout path before rendering.
9. The four `instructions/<stage>/CLAUDE.md` files exist with non-empty, stage-appropriate content.
10. `python3 -m pytest` is green; tests stay hermetic (no network/subprocess).

## Test plan

### tests/test_prompts.py (extend; existing tests stay unchanged and green because render defaults INSTRUCTIONS to "")
- `tpl.count("<<<INSTRUCTIONS>>>") == 1` for all six templates; placeholder index < index of the trailing json contract.
- Rendering a real template WITHOUT INSTRUCTIONS leaves no `<<<` residue (placeholder consumed to "").
- Rendering with `INSTRUCTIONS="GUIDE"` places GUIDE in the output and keeps the contract intact.
- Collision: render REVIEW with `SPECS="real"` and `INSTRUCTIONS="see <<<SPECS>>> above"`; assert the literal `<<<SPECS>>> above` substring survives.

### tests/test_instructions.py (new; tmp_path + monkeypatch config.INSTRUCTIONS_DIR / config.INSTRUCTIONS_INJECT_GENERAL)
- general + stage merged, with stage text positioned AFTER general text.
- missing general and missing stage -> `""`.
- spec_review maps to the spec folder; fix maps to the implement folder.
- non-UTF-8 bytes in general AND stage CLAUDE.md -> `load()` returns `""` (a str), no exception.
- `INSTRUCTIONS_INJECT_GENERAL=false` -> general text absent, stage text present.
- fence neutralization: a general CLAUDE.md containing a ```json fence -> the loaded output contains no ```json tag.
- contract protection: `combined = load(general-with-UNCLOSED-```json-fence, stage-after) + valid trailing ```json contract`; assert `runners.parse_structured(combined) == contract_dict`.

### tests/test_stages.py (extend)
- `_build_prompt` forwards (step, path) to a recording stub set via `monkeypatch.setattr(stages.instructions, "load", ...)` and injects its return into the rendered prompt; assert the stub's text appears and load was called with the expected step/path.
- Assert stages imports the instructions module (so the monkeypatch above is possible).

## Risks / notes
- General-layer double-load for cwd-aware runners is intentional/documented; `INSTRUCTIONS_INJECT_GENERAL=false` is the escape hatch. The real value is harness-agnostic injection (Pi's CLAUDE.md handling is uncertain per HANDOFF).
- The neutralized fence loses only the json syntax-highlight hint, not content.
- The 'target-repo CLAUDE.md at runtime' read path follows the approved summary; if the human actually wanted one fixed baked-in general file, only the loader's general path changes — not blocking.

## Work items
- [ ] Add instruction config knobs to orchestrator/config.py
- [ ] Add orchestrator/instructions.py loader
- [ ] Make prompts.render INSTRUCTIONS-aware and add the placeholder
- [ ] Wire _build_prompt into stages.py
- [ ] Author instructions/<stage>/CLAUDE.md
- [ ] Tests: new test_instructions.py + extend test_prompts.py and test_stages.py
- [ ] Docs: HANDOFF.md, README, .env.example
