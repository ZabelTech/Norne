# Summarize stage guidance

Your role is to understand the GitHub issue and restate it clearly before the spec stage.

## What to do

- Read the issue description and all comments thoroughly
- Investigate the target repository to understand its structure, conventions, and context
- Identify what's being asked: the goal, concrete scope, explicit non-goals, and assumptions
- If anything material is ambiguous or missing, ask for it (no more than 3 blocking questions)

## How to investigate

- Look at the repository's code structure, layout, and documentation
- Check for existing patterns and conventions in similar areas of the codebase
- Understand the actual pipeline stages from the code rather than guessing

## Your output

Your summary must be:
- Crisp and comprehensive
- Grounded in the real repository context (not assumptions)
- Include explicit non-goals when scope is bounded
- Call out assumptions that affect the implementation

Only ask questions that truly block progress. If you understand enough to write a spec, proceed with status=ready.

## Ending

End with exactly one json block matching the schema in the prompt.
