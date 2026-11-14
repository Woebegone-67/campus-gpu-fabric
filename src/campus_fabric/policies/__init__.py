"""B-side pure scheduling plans; never an authoritative resource ledger."""

from .baselines import BaselinePolicy, make_policy

__all__ = ["BaselinePolicy", "make_policy"]
