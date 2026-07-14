"""A2 clean-room components with side-effect-free lazy compatibility exports."""

from __future__ import annotations

import importlib


_LAZY_EXPORTS = {
    "BlueprintTrainer": (".blueprint", "BlueprintTrainer"),
    "CoinTossResolveGame": (".resolving", "CoinTossResolveGame"),
    "CommonA2StrategyRuntime": (".common_native_entry", "CommonA2StrategyRuntime"),
    "HUNLBlueprint": (".hunl_blueprint", "HUNLBlueprint"),
    "HUNLExternalSamplingLCFR": (
        ".hunl_external_sampling",
        "HUNLExternalSamplingLCFR",
    ),
    "HUNLTrainingConfig": (".hunl_external_sampling", "HUNLTrainingConfig"),
    "LeducLinearCFR": (".leduc_linear_cfr", "LeducLinearCFR"),
    "LinearCFR": (".linear_cfr", "LinearCFR"),
    "ResolveCertificate": (".resolving", "ResolveCertificate"),
}

__all__ = sorted(_LAZY_EXPORTS)


def __getattr__(name: str) -> object:
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(name)
    module_name, attribute = target
    value = getattr(importlib.import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted((*globals(), *_LAZY_EXPORTS))
