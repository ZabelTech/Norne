# Implement stage guidance

Your role is to implement the spec(s) for the current issue in the target repository.

## What to do

- Read the spec files in `specs/<issue-number>/`
- Read the relevant code to understand the existing implementation
- Implement every work item in the spec
- Follow the repository's conventions and CLAUDE.md guidance
- Write or update tests for what you change
- Run the test and lint commands and make them pass

## Implementation approach

- Match the existing code style (naming, idiom, comment density)
- Place code where it naturally belongs in the existing structure
- Add tests that verify your changes work correctly
- Make focused, atomic commits with clear messages
- Don't push or open a PR—the orchestrator handles that

## Quality bar

Your changes must:
- Pass all existing tests
- Add or update tests for new functionality
- Follow repo conventions (lint clean, proper structure)
- Meet the acceptance criteria in the spec

## Blockers

If you hit a blocker that needs a human decision:
- Stop and explain it clearly
- Set status=needs_human with a specific reason
- Don't work around it—pause and let the human decide

## Ending

Commit your changes with clear messages. Do not push. End with exactly one json block matching the schema in the prompt.
