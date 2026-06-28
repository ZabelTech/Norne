"""Stage prompt templates.

Each prompt instructs the model to finish with a single ```json ...``` block
matching a schema the orchestrator parses (runners.parse_structured). We use
<<<TOKENS>>> for substitution so JSON braces in the templates don't collide.
"""


def render(tpl, **kw):
    out = tpl
    for k, v in kw.items():
        out = out.replace(f"<<<{k}>>>", str(v))
    return out


SUMMARIZE = """You are triaging a GitHub issue for an automated, end-to-end
code-generation pipeline. You are running inside a checkout of the TARGET
repository — investigate it (its code, layout, and docs) to ground your
understanding before answering, e.g. to learn the pipeline's actual stages
rather than guessing.

ISSUE #<<<NUM>>>: <<<TITLE>>>
---
<<<BODY>>>
---
CLARIFICATIONS SO FAR (human replies, newest last):
<<<CLARIFICATIONS>>>
---
Write a crisp restatement of what is being asked: the goal, the concrete
scope, explicit non-goals, and any assumptions. If anything material is
ambiguous or missing such that you could not safely write a spec, ask for it.
Ask only what truly blocks progress (no more than 3 questions).

End with exactly one json block:
```json
{"status": "clarify" | "ready",
 "summary": "<the restatement, markdown ok>",
 "questions": ["<only if status=clarify>"]}
```"""

SPEC = """Turn this approved request into one or more implementation specs.

APPROVED SUMMARY for issue #<<<NUM>>> (<<<TITLE>>>):
<<<SUMMARY>>>

FULL DISCUSSION — the issue description and every comment ((bot) lines are
prior pipeline output; (human) lines are the source of truth):
<<<DISCUSSION>>>

Repo context is in the current directory (read what you need; do NOT edit code).
For each spec: a short title, a url-safe slug, a markdown body (context,
approach, acceptance criteria, test plan), and a list of small, independently
reviewable work items. Group only genuinely related work into one spec.

If a real design decision needs a human (a tradeoff you should not make
unilaterally), set status=needs_human and explain in `reason` instead of guessing.

End with exactly one json block:
```json
{"status": "ready" | "needs_human",
 "reason": "<only if needs_human>",
 "specs": [{"title": "...", "slug": "...", "body": "...",
            "work_items": [{"title": "...", "body": "..."}]}]}
```"""

SPEC_REVIEW = """Critically review these specs for a different perspective.
You did NOT write them. Look for: missing acceptance criteria, infeasible or
risky steps, scope creep, untested edge cases, wrong assumptions.

SPECS:
<<<SPECS>>>

ORIGINAL DISCUSSION — the issue description and every comment (judge the specs
against what was actually asked):
<<<DISCUSSION>>>

End with exactly one json block:
```json
{"verdict": "ok" | "concerns",
 "concerns": ["<specific, actionable>"]}
```"""

IMPLEMENT = """Implement the spec(s) for issue #<<<NUM>>> in the current branch.

The spec files are in `specs/<<<NUM>>>/`. Read them and the relevant code.
Implement every work item. Follow the repo's conventions and CLAUDE.md. Write
or update tests, and run the test/lint commands. Make focused git commits with
clear messages. DO NOT push and DO NOT open a PR — the orchestrator does that.

CONTEXT — the issue and its full discussion (intent; (bot) lines are prior
pipeline output, (human) lines are the source of truth):
<<<DISCUSSION>>>

If you hit a blocker that needs a human decision, stop and explain it.

End with exactly one json block:
```json
{"status": "done" | "needs_human",
 "summary": "<what you changed, for the PR body>",
 "reason": "<only if needs_human>"}
```"""

FIX = """Address this review feedback on issue #<<<NUM>>> in the current branch.

REVIEW FEEDBACK (round <<<ROUND>>>):
<<<FEEDBACK>>>

CONTEXT — the issue, PR, and full discussion:
<<<DISCUSSION>>>

Make the changes, update tests, run tests/lint, and commit. Do not push.

End with exactly one json block:
```json
{"status": "done" | "needs_human",
 "summary": "<what you changed>",
 "reason": "<only if needs_human>"}
```"""

REVIEW = """Review this pull request against its spec and the original issue.
You are a SEPARATE model from the one that wrote the code — be independent.

ORIGINAL ISSUE #<<<NUM>>>: <<<TITLE>>>
<<<SUMMARY>>>

SPEC(S):
<<<SPECS>>>

FULL DISCUSSION — the issue and PR descriptions and every comment (check the
change against what was actually asked for and discussed):
<<<DISCUSSION>>>

The PR branch `<<<BRANCH>>>` is checked out in the current directory and the
target branch is `<<<BASE>>>`. Inspect the change YOURSELF with git: run
`git diff <<<BASE>>>...<<<BRANCH>>>` to see the complete set of changes, then
open whatever files you need for context. Review ALL changed files end to end —
every hunk, not a sample — and run the tests if it helps you judge correctness.

Check correctness, that acceptance criteria are met, test coverage, and obvious
bugs/security issues. Approve only if it genuinely satisfies the spec. Use
request_changes for fixable issues. Use needs_human only for a judgement call a
human must make (e.g. the spec itself looks wrong, or a real tradeoff).

End with exactly one json block:
```json
{"status": "approve" | "request_changes" | "needs_human",
 "summary": "<overall assessment>",
 "comments": ["<specific change requests, if any>"],
 "reason": "<only if needs_human>"}
```"""
