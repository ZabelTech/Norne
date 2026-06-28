"""Tiny structured logger -> stdout (Fly captures and timestamps each line).

Convention:
  - `[#N] ...`  : an issue-scoped event (stage transition, skip reason, outcome)
  - `  stage: ...` (2-space indent): a model interaction within the current
    issue (single worker => these lines belong to the [#N] line just above)
  - other lines : poll-loop / lifecycle events
"""


def log(msg):
    print(msg, flush=True)
