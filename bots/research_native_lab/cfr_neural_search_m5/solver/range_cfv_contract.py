"""Sealed, content-bound provider contract for complete range CFV leaves."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from enum import Enum
from pathlib import Path
from types import (
    BuiltinFunctionType,
    BuiltinMethodType,
    CodeType,
    FunctionType,
    ModuleType,
)
from typing import Any

from bots.research_native_lab.cfr_neural_search.core.identity import payload_sha256

from ..cfv.semantics import RangeCFVQuery, RangeCFVResult
from ..cfv.public_state import PublicHUNLState
from ..core.contracts import (
    CFV_SEMANTICS_PATH,
    M5_ROOT,
    REPOSITORY_ROOT,
    foundation_contract_digest,
    load_cfv_semantics,
    load_oracle_gate_contract,
    verify_m4_dependency,
)
from bots.research_native_lab.common_contracts import NationalGameState
from ..core.independence import (
    read_validated_runtime_bytes,
    verify_import_independence,
)


Evaluator = Callable[[RangeCFVQuery], RangeCFVResult]
_FACTORY_SEAL = object()
_COMMON_PROVIDER_SOURCES = tuple(
    REPOSITORY_ROOT / "bots" / "research_native_lab" / "common_contracts" / name
    for name in ("__init__.py", "actions.py", "cards.py", "constants.py", "national_state.py")
)
_M5_EXACT_PROVIDER_SOURCES = tuple(
    M5_ROOT / "cfv" / name
    for name in (
        "__init__.py",
        "combo_index.py",
        "hunl_micro_oracle.py",
        "pairwise.py",
        "public_state.py",
        "ranges.py",
        "semantics.py",
        "toy_oracle.py",
    )
)


def _code_constant_payload(value: object) -> object:
    if value is None or value is Ellipsis or value is NotImplemented:
        return {"type": repr(value)}
    if type(value) in (bool, int, str):
        return {"type": type(value).__name__, "value": value}
    if type(value) is float:
        return {"type": "float", "hex": value.hex()}
    if type(value) is complex:
        return {
            "type": "complex",
            "real": value.real.hex(),
            "imag": value.imag.hex(),
        }
    if type(value) is bytes:
        return {"type": "bytes", "hex": value.hex()}
    if type(value) is slice:
        return {
            "type": "slice",
            "start": _code_constant_payload(value.start),
            "stop": _code_constant_payload(value.stop),
            "step": _code_constant_payload(value.step),
        }
    if type(value) in (tuple, frozenset):
        items = [_code_constant_payload(item) for item in value]
        if type(value) is frozenset:
            items.sort(
                key=lambda item: json.dumps(
                    item, sort_keys=True, separators=(",", ":")
                )
            )
        return {"type": type(value).__name__, "items": items}
    if type(value) is CodeType:
        return {"type": "code", "value": _code_payload(value)}
    raise TypeError(f"unsupported Python code constant: {type(value).__name__}")


def _code_payload(code: CodeType) -> Mapping[str, Any]:
    """Stable code identity that excludes CPython's mutable quickening cache."""

    return {
        "argcount": code.co_argcount,
        "posonlyargcount": code.co_posonlyargcount,
        "kwonlyargcount": code.co_kwonlyargcount,
        "nlocals": code.co_nlocals,
        "stacksize": code.co_stacksize,
        "flags": code.co_flags,
        "code_hex": code.co_code.hex(),
        "consts": [_code_constant_payload(value) for value in code.co_consts],
        "names": list(code.co_names),
        "varnames": list(code.co_varnames),
        "freevars": list(code.co_freevars),
        "cellvars": list(code.co_cellvars),
        "exceptiontable_hex": code.co_exceptiontable.hex(),
    }


def _callable_code_sha256(evaluator: Evaluator) -> str:
    return payload_sha256(_code_payload(evaluator.__code__))


def _function_source(function: FunctionType) -> str:
    path = Path(function.__code__.co_filename).absolute()
    try:
        return path.relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError:
        return function.__code__.co_filename


def _function_key(function: FunctionType) -> str:
    return (
        f"{function.__module__}:{function.__qualname__}:"
        f"{_callable_code_sha256(function)}"
    )


def _class_key(value: type) -> str:
    return f"{value.__module__}:{value.__qualname__}"


def _recursive_code_names(code: CodeType) -> frozenset[str]:
    names = set(code.co_names)
    for value in code.co_consts:
        if type(value) is CodeType:
            names.update(_recursive_code_names(value))
    return frozenset(names)


def _expand_repository_symbol(module: str) -> bool:
    return module.startswith(
        "bots.research_native_lab.cfr_neural_search_m5"
    ) or module.startswith("bots.research_native_lab.common_contracts")


def _runtime_dependency_manifest(evaluator: Evaluator) -> Mapping[str, Any]:
    """Fingerprint recursively referenced Python helpers and core state classes.

    Source hashes alone do not detect same-process replacement of a module
    global.  This manifest follows every repository Python function named by a
    provider/helper code object and every method of the authoritative query and
    Common state classes.  Builtins, modules, constants, defaults, and closure
    contents receive deterministic descriptors as well.
    """

    pending_functions: list[FunctionType] = [evaluator]
    pending_classes: list[type] = [
        RangeCFVQuery,
        RangeCFVResult,
        PublicHUNLState,
        NationalGameState,
    ]
    functions: dict[str, Mapping[str, Any]] = {}
    classes: dict[str, Mapping[str, Any]] = {}

    def descriptor(value: object) -> Mapping[str, Any]:
        if value is None or type(value) in (bool, int, str):
            return {"kind": "literal", "type": type(value).__name__, "value": value}
        if type(value) is float:
            return {"kind": "float", "hex": value.hex()}
        if type(value) is bytes:
            return {
                "kind": "bytes",
                "length": len(value),
                "sha256": hashlib.sha256(value).hexdigest(),
            }
        if type(value) in (tuple, list):
            items = [descriptor(item) for item in value]
            return {
                "kind": type(value).__name__,
                "length": len(items),
                "sha256": payload_sha256({"items": items}),
            }
        if type(value) in (set, frozenset):
            items = sorted(
                (descriptor(item) for item in value),
                key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
            )
            return {
                "kind": type(value).__name__,
                "length": len(items),
                "sha256": payload_sha256({"items": items}),
            }
        if type(value) is dict:
            items = [
                (descriptor(key), descriptor(item)) for key, item in value.items()
            ]
            items.sort(
                key=lambda item: json.dumps(
                    item[0], sort_keys=True, separators=(",", ":")
                )
            )
            return {
                "kind": "dict",
                "length": len(items),
                "sha256": payload_sha256({"items": items}),
            }
        if isinstance(value, Enum):
            return {
                "kind": "enum_member",
                "class": _class_key(type(value)),
                "name": value.name,
                "value": descriptor(value.value),
            }
        if type(value) is FunctionType:
            if _expand_repository_symbol(value.__module__):
                pending_functions.append(value)
            return {
                "kind": "python_function",
                "key": _function_key(value),
                "code_sha256": _callable_code_sha256(value),
                "source": _function_source(value),
            }
        if type(value) in (BuiltinFunctionType, BuiltinMethodType):
            return {
                "kind": "builtin_callable",
                "module": value.__module__,
                "name": value.__name__,
            }
        if type(value) is type:
            if _expand_repository_symbol(value.__module__):
                pending_classes.append(value)
            return {"kind": "class", "key": _class_key(value)}
        if type(value) is ModuleType:
            return {"kind": "module", "name": value.__name__}
        value_type = type(value)
        return {
            "kind": "typed_object",
            "type_module": value_type.__module__,
            "type_qualified_name": value_type.__qualname__,
        }

    while pending_functions or pending_classes:
        while pending_functions:
            function = pending_functions.pop()
            key = _function_key(function)
            identity = {
                "module": function.__module__,
                "qualified_name": function.__qualname__,
                "source": _function_source(function),
                "code_sha256": _callable_code_sha256(function),
            }
            existing = functions.get(key)
            if existing is not None:
                if existing["identity"] != identity:
                    raise ValueError(
                        f"runtime dependency has an ambiguous function symbol: {key}"
                    )
                continue
            referenced_names = _recursive_code_names(function.__code__)
            global_bindings: dict[str, Mapping[str, Any]] = {}
            for name in sorted(referenced_names):
                if name not in function.__globals__:
                    continue
                value = function.__globals__[name]
                if type(value) is ModuleType:
                    attributes = {
                        attribute: descriptor(value.__dict__[attribute])
                        for attribute in sorted(referenced_names)
                        if attribute != name and attribute in value.__dict__
                    }
                    global_bindings[name] = {
                        "kind": "module",
                        "name": value.__name__,
                        "attributes": attributes,
                    }
                else:
                    global_bindings[name] = descriptor(value)
            closure = []
            if function.__closure__ is not None:
                closure = [descriptor(cell.cell_contents) for cell in function.__closure__]
            functions[key] = {
                "identity": identity,
                "defaults": descriptor(function.__defaults__),
                "kwdefaults": descriptor(function.__kwdefaults__),
                "closure": closure,
                "globals": global_bindings,
            }

        while pending_classes:
            value = pending_classes.pop()
            key = _class_key(value)
            if key in classes:
                continue
            members: dict[str, Mapping[str, Any]] = {}
            for name, member in sorted(value.__dict__.items()):
                functions_to_bind: list[FunctionType] = []
                if type(member) is FunctionType:
                    functions_to_bind.append(member)
                elif type(member) in (classmethod, staticmethod):
                    functions_to_bind.append(member.__func__)
                elif type(member) is property:
                    functions_to_bind.extend(
                        function
                        for function in (member.fget, member.fset, member.fdel)
                        if type(function) is FunctionType
                    )
                if functions_to_bind:
                    for function in functions_to_bind:
                        if _expand_repository_symbol(function.__module__):
                            pending_functions.append(function)
                    members[name] = {
                        "kind": "method_set",
                        "functions": [
                            {
                                "key": _function_key(function),
                                "code_sha256": _callable_code_sha256(function),
                                "source": _function_source(function),
                            }
                            for function in functions_to_bind
                        ],
                    }
            classes[key] = {"members": members}

    return {
        "schema": "route-b-m5-runtime-dependency-manifest-v1",
        "functions": dict(sorted(functions.items())),
        "classes": dict(sorted(classes.items())),
    }


class RangeCFVLeafContract:
    """Immutable provider handle; only content-binding factories can build it."""

    __slots__ = (
        "_provider_id",
        "_provider_kind",
        "_receipt_json",
        "_digest",
        "_evaluator",
        "_runtime_manifest_builder",
        "_frozen",
    )

    def __new__(cls, *arguments: object, **keywords: object) -> "RangeCFVLeafContract":
        if keywords.pop("_seal", None) is not _FACTORY_SEAL or arguments or keywords:
            raise TypeError("RangeCFVLeafContract is sealed; use a content-binding factory")
        return super().__new__(cls)

    def __init__(self, *, _seal: object) -> None:
        # Initialization is completed atomically by ``_make_contract`` before
        # the object is exposed.  The argument is intentionally unusable by
        # callers because the module-private seal is identity-checked above.
        if _seal is not _FACTORY_SEAL:
            raise TypeError("invalid RangeCFVLeafContract factory seal")

    def __setattr__(self, name: str, value: object) -> None:
        if hasattr(self, "_frozen"):
            raise AttributeError("RangeCFVLeafContract is immutable")
        object.__setattr__(self, name, value)

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def provider_kind(self) -> str:
        return self._provider_kind

    @property
    def digest(self) -> str:
        return self._digest

    @property
    def receipt(self) -> Mapping[str, Any]:
        return json.loads(self._receipt_json.decode("utf-8"))

    def _verify_runtime_binding(self) -> Mapping[str, Any]:
        if hashlib.sha256(self._receipt_json).hexdigest() != self._digest:
            raise ValueError("leaf contract receipt differs from its digest")
        receipt = self.receipt
        if (
            receipt.get("provider_id") != self._provider_id
            or receipt.get("provider_kind") != self._provider_kind
        ):
            raise ValueError("leaf contract identity differs from its receipt")
        evaluator = self._evaluator
        if type(evaluator) is not FunctionType:
            raise ValueError("leaf evaluator is no longer an exact Python function")
        callable_receipt = receipt.get("callable")
        if type(callable_receipt) is not dict or callable_receipt != {
            "module": evaluator.__module__,
            "qualified_name": evaluator.__qualname__,
        }:
            raise ValueError("leaf evaluator symbol differs from its receipt")
        if (
            evaluator.__closure__ is not None
            or evaluator.__defaults__ is not None
            or evaluator.__kwdefaults__ is not None
        ):
            raise ValueError("leaf evaluator acquired mutable closure/default state")
        if receipt.get("callable_code_sha256") != _callable_code_sha256(evaluator):
            raise ValueError("leaf evaluator code object differs from its receipt")
        runtime_manifest_builder = self._runtime_manifest_builder
        if type(runtime_manifest_builder) is not FunctionType:
            raise ValueError("leaf verifier manifest builder is no longer an exact function")
        builder_receipt = receipt.get("verifier_runtime_manifest_builder")
        builder_identity = {
            "module": runtime_manifest_builder.__module__,
            "qualified_name": runtime_manifest_builder.__qualname__,
        }
        builder_runtime_digest = payload_sha256(
            runtime_manifest_builder(runtime_manifest_builder)
        )
        if type(builder_receipt) is not dict or builder_receipt != {
            "callable": builder_identity,
            "code_sha256": _callable_code_sha256(runtime_manifest_builder),
            "runtime_dependencies_sha256": builder_runtime_digest,
        }:
            raise ValueError("leaf verifier manifest builder binding changed")
        pinned_builder = load_oracle_gate_contract()["leaf_consumer"][
            "verifier_runtime_manifest_builder"
        ]
        if builder_receipt != pinned_builder:
            raise ValueError("leaf verifier manifest builder differs from the system pin")
        runtime_dependencies = runtime_manifest_builder(evaluator)
        runtime_digest = payload_sha256(runtime_dependencies)
        if receipt.get("runtime_dependencies_sha256") != runtime_digest:
            raise ValueError(
                "leaf provider runtime helper bindings changed: "
                f"expected {receipt.get('runtime_dependencies_sha256')}, "
                f"observed {runtime_digest}"
            )
        if receipt.get("runtime_dependency_function_count") != len(
            runtime_dependencies["functions"]
        ) or receipt.get("runtime_dependency_class_count") != len(
            runtime_dependencies["classes"]
        ):
            raise ValueError("leaf provider runtime dependency closure changed")
        callable_path = Path(evaluator.__code__.co_filename).absolute()
        try:
            callable_relative = callable_path.relative_to(REPOSITORY_ROOT).as_posix()
        except ValueError as exc:
            raise ValueError("leaf evaluator source escaped the repository") from exc
        if receipt.get("callable_source") != callable_relative:
            raise ValueError("leaf evaluator source path differs from its receipt")
        sources = receipt.get("provider_sources")
        if type(sources) is not dict or not sources:
            raise ValueError("leaf provider source receipt is empty")
        for relative, expected in sorted(sources.items()):
            if type(relative) is not str or type(expected) is not str:
                raise ValueError("leaf provider source receipt is malformed")
            observed = hashlib.sha256(
                read_validated_runtime_bytes(REPOSITORY_ROOT / relative)
            ).hexdigest()
            if observed != expected:
                raise ValueError(f"leaf provider source changed: {relative}")
        m4 = verify_m4_dependency()
        if receipt.get("m4_source_snapshot_sha256") != m4["source_snapshot_sha256"]:
            raise ValueError("leaf provider M4 dependency changed")
        imports = verify_import_independence()
        if (
            receipt.get("m5_import_audit_schema") != imports["schema"]
            or receipt.get("m5_import_audited_source_files")
            != imports["source_file_count"]
        ):
            raise ValueError("leaf provider import boundary changed")
        return receipt

    def evaluate(self, query: RangeCFVQuery) -> RangeCFVResult:
        if type(query) is not RangeCFVQuery:
            raise TypeError("leaf contract accepts only exact RangeCFVQuery")
        self._verify_runtime_binding()
        query.assert_authoritative()
        result = self._evaluator(query)
        query.assert_authoritative()
        if type(result) is not RangeCFVResult:
            raise TypeError("leaf provider returned a non-CFV result")
        if result.query_sha256 != query.digest:
            raise ValueError("leaf provider result is bound to a different query")
        if result.provider_id != self.provider_id:
            raise ValueError("leaf provider result id differs from its sealed contract")
        result.validate_against(query)
        if self.provider_kind == "exact_oracle":
            tolerance = float(
                load_oracle_gate_contract()["hunl"][
                    "exact_zero_sum_abs_tolerance_bb"
                ]
            )
            if (
                abs(result.raw_zero_sum_residual_bb) > tolerance
                or abs(result.deployed_zero_sum_residual_bb) > tolerance
            ):
                raise ValueError("exact leaf provider violated the zero-sum threshold")
        elif self.provider_kind == "neural_model":
            tolerance = float(load_cfv_semantics()["zero_sum"]["tolerance_bb"])
            if abs(result.deployed_zero_sum_residual_bb) > tolerance:
                raise ValueError("neural leaf deployment violated zero-sum threshold")
        else:
            raise ValueError("leaf provider kind is unsupported")
        return result


def _make_contract(
    *,
    provider_id: str,
    provider_kind: str,
    receipt: Mapping[str, Any],
    evaluator: Evaluator,
    runtime_manifest_builder: FunctionType,
) -> RangeCFVLeafContract:
    raw = json.dumps(
        receipt,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    contract = RangeCFVLeafContract(_seal=_FACTORY_SEAL)
    object.__setattr__(contract, "_provider_id", provider_id)
    object.__setattr__(contract, "_provider_kind", provider_kind)
    object.__setattr__(contract, "_receipt_json", raw)
    object.__setattr__(contract, "_digest", hashlib.sha256(raw).hexdigest())
    object.__setattr__(contract, "_evaluator", evaluator)
    object.__setattr__(
        contract,
        "_runtime_manifest_builder",
        runtime_manifest_builder,
    )
    object.__setattr__(contract, "_frozen", True)
    return contract


def make_exact_oracle_contract(
    *,
    provider_id: str,
    evaluator: Evaluator,
    provider_source_paths: Iterable[str | Path],
) -> RangeCFVLeafContract:
    """Bind an exact M5 oracle to every implementation source byte."""

    if type(provider_id) is not str or not provider_id.startswith("route-b-m5-"):
        raise ValueError("exact provider id must use the route-b-m5 namespace")
    if type(evaluator) is not FunctionType:
        raise TypeError("exact oracle evaluator must be an exact Python function")
    if (
        evaluator.__closure__ is not None
        or evaluator.__defaults__ is not None
        or evaluator.__kwdefaults__ is not None
    ):
        raise ValueError("exact oracle evaluator cannot carry closure/default state")
    module = evaluator.__module__
    qualified_name = evaluator.__qualname__
    if not (
        module.startswith("bots.research_native_lab.cfr_neural_search_m5.")
        or module.startswith("research_native_lab.cfr_neural_search_m5.")
    ):
        raise ValueError("exact oracle callable must be implemented inside M5")
    callable_path = Path(evaluator.__code__.co_filename).absolute()
    requested_paths = tuple(Path(raw_path).absolute() for raw_path in provider_source_paths)
    source_paths = tuple(
        sorted(
            set(
                (
                    *requested_paths,
                    callable_path,
                    Path(__file__).absolute(),
                    *_M5_EXACT_PROVIDER_SOURCES,
                    *_COMMON_PROVIDER_SOURCES,
                )
            )
        )
    )
    sources: dict[str, str] = {}
    for path in source_paths:
        content = read_validated_runtime_bytes(path)
        relative = path.absolute().relative_to(REPOSITORY_ROOT).as_posix()
        if relative in sources:
            raise ValueError("exact oracle source path is duplicated")
        sources[relative] = hashlib.sha256(content).hexdigest()
    if not sources:
        raise ValueError("exact oracle source closure cannot be empty")
    sources = dict(sorted(sources.items()))
    semantics_raw = read_validated_runtime_bytes(CFV_SEMANTICS_PATH)
    m4_verification = verify_m4_dependency()
    import_verification = verify_import_independence()
    runtime_manifest_builder = _runtime_dependency_manifest
    builder_identity = {
        "module": runtime_manifest_builder.__module__,
        "qualified_name": runtime_manifest_builder.__qualname__,
    }
    builder_binding = {
        "callable": builder_identity,
        "code_sha256": _callable_code_sha256(runtime_manifest_builder),
        "runtime_dependencies_sha256": payload_sha256(
            runtime_manifest_builder(runtime_manifest_builder)
        ),
    }
    runtime_dependencies = runtime_manifest_builder(evaluator)
    runtime_dependencies_sha256 = payload_sha256(runtime_dependencies)
    gate_contract = load_oracle_gate_contract()
    if builder_binding != gate_contract["leaf_consumer"][
        "verifier_runtime_manifest_builder"
    ]:
        raise ValueError("exact-provider verifier builder differs from the system pin")
    known_provider = gate_contract["leaf_consumer"]["known_exact_providers"].get(
        provider_id
    )
    callable_identity = {"module": module, "qualified_name": qualified_name}
    if known_provider is not None and (
        known_provider["callable"] != callable_identity
        or known_provider["runtime_dependencies_sha256"]
        != runtime_dependencies_sha256
    ):
        raise ValueError(
            "known exact provider differs from its pinned callable/runtime closure"
        )
    receipt = {
        "schema": "route-b-m5-range-cfv-leaf-contract-v1",
        "provider_id": provider_id,
        "provider_kind": "exact_oracle",
        "callable": callable_identity,
        "callable_source": callable_path.relative_to(REPOSITORY_ROOT).as_posix(),
        "callable_code_sha256": _callable_code_sha256(evaluator),
        "runtime_dependencies_sha256": runtime_dependencies_sha256,
        "runtime_dependency_function_count": len(runtime_dependencies["functions"]),
        "runtime_dependency_class_count": len(runtime_dependencies["classes"]),
        "known_provider_runtime_pinned": known_provider is not None,
        "verifier_runtime_manifest_builder": builder_binding,
        "provider_sources": sources,
        "provider_sources_sha256": payload_sha256({"files": sources}),
        "cfv_semantics_raw_sha256": hashlib.sha256(semantics_raw).hexdigest(),
        "foundation_contract_sha256": foundation_contract_digest(),
        "m4_source_snapshot_sha256": m4_verification["source_snapshot_sha256"],
        "m5_import_audit_schema": import_verification["schema"],
        "m5_import_audited_source_files": import_verification["source_file_count"],
        "input": "public_state_plus_two_complete_reach_ranges",
        "output": "masked_two_by_1326_counterfactual_values_bb",
    }
    return _make_contract(
        provider_id=provider_id,
        provider_kind="exact_oracle",
        receipt=receipt,
        evaluator=evaluator,
        runtime_manifest_builder=runtime_manifest_builder,
    )
