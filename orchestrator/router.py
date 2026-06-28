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
    for fam in families:
        if exclude_family and fam == exclude_family:
            continue
        if _eligible(fam, ledger):
            return fam
    # No *different* family was eligible. If that's only because no other family
    # is configured at all (single-family deploy), the diversity cross-check
    # cannot be satisfied -- degrade to a same-family review instead of
    # deadlocking. A different family that is merely out of budget does NOT
    # trigger this: we return None so the issue parks until a window resets.
    if exclude_family:
        other_configured = any(
            config.family_available(fam) for fam in families if fam != exclude_family
        )
        if not other_configured and _eligible(exclude_family, ledger):
            return exclude_family
    return None
