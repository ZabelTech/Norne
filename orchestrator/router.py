"""Pick the model family for a stage given budget headroom.

Rules:
  * Walk the stage's priority list (config.ROUTING) and choose the first
    family whose pool has headroom in the ledger. -> "subscription maxing"
    + automatic failover when one pool's window is tapped.
  * For "review", exclude `exclude_family` so the reviewer is a DIFFERENT
    model than the implementer (diversity-of-thought cross-check).
  * Return None when nothing has headroom -> caller parks `blocked:budget`.
"""
from . import config


def choose_family(stage, ledger, exclude_family=None):
    for fam in config.ROUTING[stage]:
        if exclude_family and fam == exclude_family:
            continue
        if ledger.headroom(fam):
            return fam
    return None
