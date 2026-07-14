"""Physical-combo CFV schemas and exact semantics."""
"""Physical-combination range CFV contracts and exact M5 oracles."""

from .combo_index import COMBOS, COMBO_COUNT, COMBO_REGISTRY_SHA256
from .public_state import ACTION_SLOTS, PublicActionRecord, PublicHUNLState
from .semantics import RangeCFVQuery, RangeCFVResult

__all__ = [
    "ACTION_SLOTS",
    "COMBOS",
    "COMBO_COUNT",
    "COMBO_REGISTRY_SHA256",
    "PublicActionRecord",
    "PublicHUNLState",
    "RangeCFVQuery",
    "RangeCFVResult",
]
