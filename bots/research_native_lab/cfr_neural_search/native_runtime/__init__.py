"""Narrow Common-contract boundary for the future native runtime.

M3 deliberately contains no socket loop or HUNL policy.  It does contain the
fail-closed adapter that a later runtime must use instead of inventing a
route-local state or action model.
"""

from .common_adapter import (
    BoundNationalAction,
    COMMON_CONTRACT_COMMIT,
    COMMON_CONTRACT_GIT_TREE,
    COMMON_RUNTIME_FILE_SHA256,
    CommonContractAdapterError,
    NationalPolicy,
    NationalDecisionSnapshot,
    adapt_national_decision,
    invoke_route_policy,
)

__all__ = [
    "BoundNationalAction",
    "COMMON_CONTRACT_COMMIT",
    "COMMON_CONTRACT_GIT_TREE",
    "COMMON_RUNTIME_FILE_SHA256",
    "CommonContractAdapterError",
    "NationalPolicy",
    "NationalDecisionSnapshot",
    "adapt_national_decision",
    "invoke_route_policy",
]
