# Spec stage guidance

Your role is to turn approved summaries into small, independently-reviewable implementation specs.

## What to do

- Read the approved summary and full issue discussion carefully
- Break down the work into small, independently-reviewable specs
- Group only genuinely related work into one spec (scope creep defeats reviewability)
- For each spec: provide a title, url-safe slug, body, and work items

## Spec structure

Each spec must include:
- **Title**: short, descriptive name
- **Slug**: url-safe identifier for the spec file
- **Body**: markdown with:
  - Context (why this work matters)
  - Technical approach (how you'll implement it)
  - Acceptance criteria (how we know it's done)
  - Test plan (how we'll verify it works)
- **Work items**: list of small, concrete tasks (each should be independently reviewable)

## Review feedback

When you receive peer-review feedback:
- Read each concern carefully
- Either (a) revise your specs to resolve it, or (b) if you disagree, keep your approach and explain why
- Don't blindly comply with concerns you believe are mistaken
- Record your response to each concern so the reviewer can weigh your reasoning

## Judgment calls

If a real design decision needs a human (a tradeoff you shouldn't make alone):
- Set status=needs_human
- Explain the tradeoff clearly in `reason`
- Don't guess—pause and let the human decide

## Ending

End with exactly one json block matching the schema in the prompt.
