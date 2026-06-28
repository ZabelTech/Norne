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
<!-- Fill these in for your repo: -->
- Test:  `# e.g. npm test`
- Lint:  `# e.g. npm run lint`
- Build: `# e.g. npm run build`
- Conventions: `# e.g. conventional commits, src/ layout, etc.`
