# Agent guidance (read by every pipeline stage)

You are running non-interactively inside an automated pipeline on a Fly Machine.
There is no human watching this session in real time.

## Hard rules
- **Finish every response with exactly one ```json block** matching the schema
  in your prompt. The orchestrator parses only that block. No prose after it.
- **Never touch secrets, tokens, `.env`, CI credentials, or `fly.toml`.**
- Stay within the working directory. Do not run destructive git commands
  (`push --force`, `reset --hard` on shared branches, branch deletion).
- During *implement*: make focused commits but **do not push and do not open a
  PR** — the orchestrator handles that. During *review*: read only, don't edit.

## Quality bar
- Follow existing code conventions, file layout, and naming in this repo.
- Add or update tests for what you change; run the test and lint commands and
  make them pass before reporting `done`.
- Implement against the spec in `specs/<issue>/`. Meet its acceptance criteria.
- If a real design decision needs a human (a tradeoff you shouldn't make alone),
  return `needs_human` with a clear `reason` rather than guessing.

## Project specifics
This repo is the orchestrator itself (Python). When changing it:
- Test:  `python3 -m pytest`  (unit tests live in `tests/`; no network/subprocess)
- Lint:  *(none configured yet — keep imports clean and match surrounding style)*
- Build: *(no build step — pure Python; `pip install -r requirements-dev.txt` for dev deps)*
- Conventions: stdlib-first, small modules under `orchestrator/`, one concern per
  file (see the "Where things live" map in `HANDOFF.md`). Add/extend tests in
  `tests/` for any logic you touch; keep them hermetic (env is stubbed in
  `tests/conftest.py`, no real GitHub/model calls).
