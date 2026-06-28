"""Pick the model family for a stage given credentials + budget headroom.

Rules:
  * A family is *eligible* only if its credential is configured
    (config.family_available) AND its pool has headroom in the ledger.
  * Walk the stage's priority list (config.ROUTING) and choose the first
    eligible family. -> "subscription maxing" + automatic failover when one
    pool's window is tapped (or one family isn't configured at all).
  * For "review", exclude `exclude_family` so the reviewer is a DIFFERENT
    model than the implementer (diversity-of-thought cross-check). If NO other
    family is even configured -- a single-family deployment, e.g. a Claude-less
    GLM-only box -- that cross-check is impossible, so degrade to a same-family
    review rather than parking the issue forever. When the other family is
    merely out of *budget*, we still return None so the caller parks
    `blocked:budget` and resumes after a window reset (preserving cross-family
    review when two families really are configured).
  * Return None when nothing is eligible -> caller parks `blocked:budget`.
"""
from . import config


def _eligible(family, ledger):
    return config.family_available(family) and ledger.headroom(family)


def choose_family(stage, ledger, exclude_family=None):
    families = config.ROUTING[stage]
    # Prefer a family OTHER than the excluded (implementer) one — the
    # diversity-of-thought cross-check.
    for fam in families:
        if exclude_family and fam == exclude_family:
            continue
        if _eligible(fam, ledger):
            return fam
    # No *different* family is eligible — whether because no other family is
    # configured (single-family deploy) OR the other family is out of budget.
    # Rather than park the issue, allow a same-family review with the excluded
    # family if it can still run. The review stage always uses that family's
    # BEST model + high effort, so a self-review is still a solid check; a true
    # cross-family review resumes automatically once the other window resets.
    if exclude_family and _eligible(exclude_family, ledger):
        return exclude_family
    return None
