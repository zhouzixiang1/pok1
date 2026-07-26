"""Rendered-prompt receipt issuing and dispatch-scope validation for llm_query.

Extracted as a cohesive business cluster; llm_query.py retains thin delegate
shells so external ``from llm_query import <name>`` and
``monkeypatch.setattr(llm_query, "<name>", ...)`` keep resolving.

Business responsibility:
* Issue digest-sealed renderer/evidence/MCP receipts.
* Validate rendered prompt integrity.
* Validate complete dispatch scope before provider call.
* Render/bind the system-owned role-contract suffix onto the provider prompt.

The role-contract registry itself (``LLMRoleContract``, ``ACTIVE_LLM_ROLE_CONTRACTS``,
``resolve_llm_role_contract`` ...) stays in ``llm_query.py`` and is reached
through ``_lq.<name>``.  Likewise the receipt-authority singleton
``_LLM_RECEIPT_AUTHORITY``, the schema tag, and the project root path stay in
``llm_query.py`` so identity checks remain centralized.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from bot_namespace import ACTIVE_BOT_PREFIX

import llm_query as _lq  # for cross-refs to registry, receipts authority, emit helpers


# --- Receipt dataclasses ----------------------------------------------------
# These are frozen dataclasses with simple field types only; no callable
# defaults, so defining them at module init does not dereference ``_lq``.

@dataclass(frozen=True)
class LLMRendererReceipt:
    role_id: str
    runtime_role: str
    producer_file: str
    producer_name: str
    producer_file_sha256: str
    producer_function_sha256: str
    template_digests: tuple
    rendered_prompt_sha256: str
    rendered_prompt_chars: int
    receipt_digest: str
    producer: object
    _authority: object


@dataclass(frozen=True)
class LLMEvidenceReceipt:
    role_id: str
    provenance_kind: str
    provenance_json: str
    provenance_sha256: str
    renderer_receipt_digest: str
    receipt_digest: str
    _authority: object


@dataclass(frozen=True)
class LLMMCPReceipt:
    role_id: str
    config_json: str
    config_sha256: str
    receipt_digest: str
    _authority: object


@dataclass(frozen=True)
class LLMDispatchReceipt:
    schema: str
    role_id: str
    runtime_role: str
    model: str
    renderer: LLMRendererReceipt
    evidence: LLMEvidenceReceipt
    mcp: LLMMCPReceipt
    receipt_digest: str
    _authority: object


@dataclass(frozen=True)
class LLMRenderedMaterial:
    """Replay result: provider text and its causally derived provenance."""

    text: str
    evidence_kind: str
    evidence_provenance: dict


class RenderedLLMPrompt(str):
    """String-compatible but sealed output of a replayable renderer."""

    def __new__(
        cls,
        *,
        role_id,
        runtime_role,
        text,
        renderer_inputs_json,
        dispatch_receipt,
        producer,
        _authority,
    ):
        instance = str.__new__(cls, str(text))
        object.__setattr__(instance, "role_id", str(role_id))
        object.__setattr__(instance, "runtime_role", str(runtime_role))
        object.__setattr__(instance, "text", str(text))
        object.__setattr__(instance, "renderer_inputs_json", str(renderer_inputs_json))
        object.__setattr__(instance, "dispatch_receipt", dispatch_receipt)
        object.__setattr__(instance, "producer", producer)
        object.__setattr__(instance, "_authority", _authority)
        object.__setattr__(instance, "_sealed", True)
        return instance

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise AttributeError("RenderedLLMPrompt is immutable")
        object.__setattr__(self, name, value)


@dataclass(frozen=True)
class FrozenLLMCapability:
    role_id: str
    model: str
    selected_tools: tuple
    read_dirs: tuple
    read_files: tuple
    write_dirs: tuple
    write_files: tuple
    evidence_dir: str | None
    context_files: tuple
    exact_bash_commands: tuple
    strict_authority_json: str | None
    strict_authority_sha256: str | None
    _authority: object


def _receipt_digest(payload):
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _normalize_receipt_value(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise _lq.LLMRoleContractError("non-finite evidence provenance number")
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple)):
        return [_normalize_receipt_value(item) for item in value]
    if isinstance(value, dict):
        normalized = {}
        for key in sorted(value, key=lambda item: str(item)):
            if not isinstance(key, str) or not key:
                raise _lq.LLMRoleContractError(
                    "evidence provenance requires non-empty string keys"
                )
            normalized[key] = _normalize_receipt_value(value[key])
        return normalized
    raise _lq.LLMRoleContractError(
        f"unsupported evidence provenance value: {type(value).__name__}"
    )


def _canonical_strict_authority_json(strict_authority):
    if strict_authority is None:
        return None
    if type(strict_authority) is not dict:
        raise _lq.LLMRoleContractError("strict-authority descriptor must be a plain object")
    try:
        normalized = _normalize_receipt_value(strict_authority)
    except _lq.LLMRoleContractError as exc:
        raise _lq.LLMRoleContractError(
            f"strict-authority descriptor is not canonically serializable: {exc}"
        ) from exc
    if not isinstance(normalized, dict):
        raise _lq.LLMRoleContractError("strict-authority descriptor must be an object")
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _assert_strict_authority_unchanged(strict_authority, frozen_capability):
    expected = frozen_capability.strict_authority_json
    current = _canonical_strict_authority_json(strict_authority)
    if current != expected:
        raise _lq.LLMRoleContractError(
            f"{frozen_capability.role_id}: strict-authority descriptor changed "
            "after capability validation"
        )


def _project_strict_authority_state(owner, internal):
    """Publish internal effect results without re-admitting caller authority."""

    if owner is None or internal is None:
        return
    if type(owner) is not dict or type(internal) is not dict:
        raise _lq.LLMRoleContractError("strict-authority projection requires plain objects")
    projected = deepcopy(internal)
    owner.clear()
    owner.update(projected)


def _project_relative_path(path, *, require_file=False):
    raw = Path(str(path))
    absolute = raw if raw.is_absolute() else _lq._LLM_PROJECT_ROOT / raw
    cursor = _lq._LLM_PROJECT_ROOT
    try:
        relative_parts = absolute.absolute().relative_to(
            _lq._LLM_PROJECT_ROOT.absolute()
        ).parts
    except ValueError as exc:
        raise _lq.LLMRoleContractError(f"path outside active project: {path}") from exc
    for part in relative_parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise _lq.LLMRoleContractError(f"symlinked LLM authority path: {path}")
    resolved = absolute.resolve(strict=False)
    try:
        relative = resolved.relative_to(_lq._LLM_PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise _lq.LLMRoleContractError(f"resolved path outside active project: {path}") from exc
    relative_text = relative.as_posix()
    if not relative_text or relative_text == "." or relative_text.startswith("archive/"):
        raise _lq.LLMRoleContractError(f"invalid active LLM authority path: {path}")
    if require_file and not resolved.is_file():
        raise _lq.LLMRoleContractError(f"required renderer source is not a file: {path}")
    return relative_text, resolved


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _producer_binding(contract, producer):
    if not callable(producer):
        raise _lq.LLMRoleContractError(f"{contract.role_id}: renderer producer is not callable")
    source_file = inspect.getsourcefile(producer)
    if not source_file:
        raise _lq.LLMRoleContractError(f"{contract.role_id}: renderer producer has no source file")
    relative, resolved = _project_relative_path(source_file, require_file=True)
    if relative != contract.producer_file:
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: producer file {relative!r} is not "
            f"{contract.producer_file!r}"
        )
    producer_name = str(getattr(producer, "__name__", ""))
    if producer_name != contract.producer_name:
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: producer {producer_name!r} is not "
            f"{contract.producer_name!r}"
        )
    try:
        function_source = inspect.getsource(producer).encode("utf-8")
    except (OSError, TypeError) as exc:
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: renderer producer source unavailable"
        ) from exc
    templates = []
    for template_path in contract.template_paths:
        relative_template, resolved_template = _project_relative_path(
            template_path,
            require_file=True,
        )
        if relative_template != template_path:
            raise _lq.LLMRoleContractError(
                f"{contract.role_id}: non-canonical template path {template_path}"
            )
        templates.append((template_path, _sha256_file(resolved_template)))
    return {
        "producer_file": relative,
        "producer_name": producer_name,
        "producer_file_sha256": _sha256_file(resolved),
        "producer_function_sha256": hashlib.sha256(function_source).hexdigest(),
        "template_digests": tuple(templates),
    }


def _type_identity(value):
    if isinstance(value, type):
        return f"{value.__module__}.{value.__qualname__}"
    return str(value)


def _active_mcp_config_payload(mcp_servers):
    selected = dict(mcp_servers or {})
    if not selected:
        return {}
    if set(selected) != {"evolution"}:
        raise _lq.LLMRoleContractError(
            f"unregistered MCP server objects: {sorted(selected)}"
        )
    from tools import evolution_server, mcp_tools

    if selected["evolution"] is not evolution_server:
        raise _lq.LLMRoleContractError(
            "Orchestrator evolution MCP must be the system-owned server object"
        )
    if not isinstance(evolution_server, dict) or set(evolution_server) != {
        "type", "name", "instance",
    }:
        raise _lq.LLMRoleContractError("system evolution MCP config shape drift")
    instance = evolution_server.get("instance")
    tools_payload = []
    for tool in mcp_tools:
        handler = getattr(tool, "handler", None)
        handler_file = inspect.getsourcefile(handler) if callable(handler) else None
        if not handler_file:
            raise _lq.LLMRoleContractError("evolution MCP tool handler source unavailable")
        handler_relative, handler_resolved = _project_relative_path(
            handler_file,
            require_file=True,
        )
        schema = getattr(tool, "input_schema", {}) or {}
        tools_payload.append({
            "name": str(getattr(tool, "name", "")),
            "description_sha256": hashlib.sha256(
                str(getattr(tool, "description", "")).encode("utf-8")
            ).hexdigest(),
            "input_schema": {
                str(key): _type_identity(value)
                for key, value in sorted(schema.items())
            },
            "handler_file": handler_relative,
            "handler_name": str(getattr(handler, "__name__", "")),
            "handler_file_sha256": _sha256_file(handler_resolved),
        })
    return {
        "type": evolution_server.get("type"),
        "name": evolution_server.get("name"),
        "instance_name": str(getattr(instance, "name", "")),
        "instance_version": str(getattr(instance, "version", "")),
        "tools": tools_payload,
    }


def _issue_llm_dispatch_receipt(
    role_name,
    rendered_prompt,
    *,
    producer,
    evidence_kind,
    evidence_provenance,
    mcp_servers=(),
    model="sonnet",
):
    """Issue one sealed receipt from the real renderer and evidence producer.

    Callers provide the callable and the structured source payload, never a
    renderer-name string.  The issuer selects the expected source/template from
    the active role registry and content-binds their current bytes.
    """

    contract = _lq.resolve_llm_role_contract(role_name)
    if str(evidence_kind) != contract.evidence_provenance_kind:
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: evidence kind {evidence_kind!r} is not "
            f"{contract.evidence_provenance_kind!r}"
        )
    prompt_text = str(rendered_prompt or "")
    producer_binding = _producer_binding(contract, producer)
    renderer_payload = {
        "role_id": contract.role_id,
        "runtime_role": str(role_name),
        **producer_binding,
        "rendered_prompt_sha256": hashlib.sha256(
            prompt_text.encode("utf-8")
        ).hexdigest(),
        "rendered_prompt_chars": len(prompt_text),
    }
    renderer_receipt = LLMRendererReceipt(
        **renderer_payload,
        receipt_digest=_receipt_digest(renderer_payload),
        producer=producer,
        _authority=_lq._LLM_RECEIPT_AUTHORITY,
    )

    normalized_provenance = _normalize_receipt_value(evidence_provenance)
    if not isinstance(normalized_provenance, dict):
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: evidence provenance must be an object"
        )
    missing = [
        field for field in contract.required_evidence_fields
        if field not in normalized_provenance
    ]
    if missing:
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: evidence provenance fields missing: {missing}"
        )
    provenance_json = json.dumps(
        normalized_provenance,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence_payload = {
        "role_id": contract.role_id,
        "provenance_kind": contract.evidence_provenance_kind,
        "provenance_sha256": hashlib.sha256(
            provenance_json.encode("utf-8")
        ).hexdigest(),
        "renderer_receipt_digest": renderer_receipt.receipt_digest,
    }
    evidence_receipt = LLMEvidenceReceipt(
        **evidence_payload,
        provenance_json=provenance_json,
        receipt_digest=_receipt_digest(evidence_payload),
        _authority=_lq._LLM_RECEIPT_AUTHORITY,
    )

    mcp_payload_value = _active_mcp_config_payload(mcp_servers)
    mcp_json = json.dumps(
        mcp_payload_value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    mcp_payload = {
        "role_id": contract.role_id,
        "config_sha256": hashlib.sha256(mcp_json.encode("utf-8")).hexdigest(),
    }
    mcp_receipt = LLMMCPReceipt(
        **mcp_payload,
        config_json=mcp_json,
        receipt_digest=_receipt_digest(mcp_payload),
        _authority=_lq._LLM_RECEIPT_AUTHORITY,
    )
    dispatch_payload = {
        "schema": _lq._LLM_RECEIPT_SCHEMA,
        "role_id": contract.role_id,
        "runtime_role": str(role_name),
        "model": str(model),
        "renderer_receipt_digest": renderer_receipt.receipt_digest,
        "evidence_receipt_digest": evidence_receipt.receipt_digest,
        "mcp_receipt_digest": mcp_receipt.receipt_digest,
    }
    return LLMDispatchReceipt(
        schema=_lq._LLM_RECEIPT_SCHEMA,
        role_id=contract.role_id,
        runtime_role=str(role_name),
        model=str(model),
        renderer=renderer_receipt,
        evidence=evidence_receipt,
        mcp=mcp_receipt,
        receipt_digest=_receipt_digest(dispatch_payload),
        _authority=_lq._LLM_RECEIPT_AUTHORITY,
    )


def render_llm_prompt(
    role_name,
    *,
    producer,
    renderer_inputs,
    mcp_servers=(),
    model="sonnet",
):
    """Replayably render and seal one active provider prompt.

    The provider boundary never accepts a text string plus a claimed renderer.
    This wrapper canonicalizes the renderer inputs, calls the registered
    production renderer itself, then signs the exact output. Validation invokes
    the same callable again from the stored inputs, so replacing or independently
    constructing ``text`` fails even when the correct producer is named.
    """

    contract = _lq.resolve_llm_role_contract(role_name)
    if str(model) not in contract.allowed_models:
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: model {model!r} outside "
            f"{list(contract.allowed_models)!r}"
        )
    normalized_inputs = _normalize_receipt_value(renderer_inputs)
    if not isinstance(normalized_inputs, dict):
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: renderer inputs must be an object"
        )
    renderer_inputs_json = json.dumps(
        normalized_inputs,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    replay_inputs = json.loads(renderer_inputs_json)
    try:
        material = producer(replay_inputs)
    except Exception as exc:
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: production renderer failed: "
            f"{type(exc).__name__}"
        ) from exc
    if (
        not isinstance(material, LLMRenderedMaterial)
        or not isinstance(material.text, str)
        or not material.text
        or not isinstance(material.evidence_provenance, dict)
    ):
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: production renderer returned no typed material"
        )
    dispatch_receipt = _issue_llm_dispatch_receipt(
        role_name,
        material.text,
        producer=producer,
        evidence_kind=material.evidence_kind,
        evidence_provenance=material.evidence_provenance,
        mcp_servers=mcp_servers,
        model=model,
    )
    return RenderedLLMPrompt(
        role_id=contract.role_id,
        runtime_role=str(role_name),
        text=material.text,
        renderer_inputs_json=renderer_inputs_json,
        dispatch_receipt=dispatch_receipt,
        producer=producer,
        _authority=_lq._LLM_RECEIPT_AUTHORITY,
    )


def _validate_rendered_llm_prompt(
    rendered, contract, role_name, mcp_servers, model="sonnet"
):
    if not isinstance(rendered, RenderedLLMPrompt):
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: sealed RenderedLLMPrompt required"
        )
    if (
        rendered._authority is not _lq._LLM_RECEIPT_AUTHORITY
        or rendered.role_id != contract.role_id
        or rendered.runtime_role != str(role_name)
    ):
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: rendered prompt authority/subject mismatch"
        )
    if rendered.producer is not rendered.dispatch_receipt.renderer.producer:
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: rendered prompt producer receipt mismatch"
        )
    try:
        replay_inputs = json.loads(rendered.renderer_inputs_json)
        replayed = rendered.producer(replay_inputs)
    except Exception as exc:
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: renderer replay failed: {type(exc).__name__}"
        ) from exc
    if not isinstance(replayed, LLMRenderedMaterial) or replayed.text != rendered.text:
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: rendered prompt replay mismatch"
        )
    replayed_provenance_json = json.dumps(
        _normalize_receipt_value(replayed.evidence_provenance),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if (
        replayed.evidence_kind
        != rendered.dispatch_receipt.evidence.provenance_kind
        or replayed_provenance_json
        != rendered.dispatch_receipt.evidence.provenance_json
    ):
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: rendered evidence provenance replay mismatch"
        )
    _validate_llm_dispatch_receipt(
        rendered.dispatch_receipt,
        contract,
        role_name,
        rendered.text,
        mcp_servers,
        model,
    )
    return rendered


def _validate_llm_dispatch_receipt(
    receipt,
    contract,
    role_name,
    rendered_prompt,
    mcp_servers,
    model,
):
    if not isinstance(receipt, LLMDispatchReceipt):
        raise _lq.LLMRoleContractError(f"{contract.role_id}: typed dispatch receipt required")
    if receipt._authority is not _lq._LLM_RECEIPT_AUTHORITY:
        raise _lq.LLMRoleContractError(f"{contract.role_id}: dispatch receipt authority invalid")
    if (
        receipt.schema != _lq._LLM_RECEIPT_SCHEMA
        or receipt.role_id != contract.role_id
        or receipt.runtime_role != str(role_name)
        or receipt.model != str(model)
        or receipt.model not in contract.allowed_models
    ):
        raise _lq.LLMRoleContractError(f"{contract.role_id}: dispatch receipt subject mismatch")
    renderer = receipt.renderer
    if renderer._authority is not _lq._LLM_RECEIPT_AUTHORITY:
        raise _lq.LLMRoleContractError(f"{contract.role_id}: renderer receipt authority invalid")
    binding = _producer_binding(contract, renderer.producer)
    prompt_text = str(rendered_prompt or "")
    renderer_payload = {
        "role_id": contract.role_id,
        "runtime_role": str(role_name),
        **binding,
        "rendered_prompt_sha256": hashlib.sha256(
            prompt_text.encode("utf-8")
        ).hexdigest(),
        "rendered_prompt_chars": len(prompt_text),
    }
    if any(
        getattr(renderer, key) != value for key, value in renderer_payload.items()
    ) or renderer.receipt_digest != _receipt_digest(renderer_payload):
        raise _lq.LLMRoleContractError(f"{contract.role_id}: renderer receipt drift")
    evidence = receipt.evidence
    if evidence._authority is not _lq._LLM_RECEIPT_AUTHORITY:
        raise _lq.LLMRoleContractError(f"{contract.role_id}: evidence receipt authority invalid")
    try:
        provenance = json.loads(evidence.provenance_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: evidence provenance receipt invalid"
        ) from exc
    missing = [
        field for field in contract.required_evidence_fields
        if field not in provenance
    ]
    evidence_payload = {
        "role_id": contract.role_id,
        "provenance_kind": contract.evidence_provenance_kind,
        "provenance_sha256": hashlib.sha256(
            evidence.provenance_json.encode("utf-8")
        ).hexdigest(),
        "renderer_receipt_digest": renderer.receipt_digest,
    }
    if (
        missing
        or evidence.provenance_kind != contract.evidence_provenance_kind
        or any(getattr(evidence, key) != value for key, value in evidence_payload.items())
        or evidence.receipt_digest != _receipt_digest(evidence_payload)
    ):
        raise _lq.LLMRoleContractError(f"{contract.role_id}: evidence receipt drift")
    mcp = receipt.mcp
    current_mcp_json = json.dumps(
        _active_mcp_config_payload(mcp_servers),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    mcp_payload = {
        "role_id": contract.role_id,
        "config_sha256": hashlib.sha256(
            current_mcp_json.encode("utf-8")
        ).hexdigest(),
    }
    if (
        mcp._authority is not _lq._LLM_RECEIPT_AUTHORITY
        or mcp.config_json != current_mcp_json
        or any(getattr(mcp, key) != value for key, value in mcp_payload.items())
        or mcp.receipt_digest != _receipt_digest(mcp_payload)
    ):
        raise _lq.LLMRoleContractError(f"{contract.role_id}: MCP config receipt drift")
    dispatch_payload = {
        "schema": _lq._LLM_RECEIPT_SCHEMA,
        "role_id": contract.role_id,
        "runtime_role": str(role_name),
        "model": str(model),
        "renderer_receipt_digest": renderer.receipt_digest,
        "evidence_receipt_digest": evidence.receipt_digest,
        "mcp_receipt_digest": mcp.receipt_digest,
    }
    if receipt.receipt_digest != _receipt_digest(dispatch_payload):
        raise _lq.LLMRoleContractError(f"{contract.role_id}: dispatch receipt digest drift")
    return receipt


def _llm_selected_tools(tools):
    if tools is None:
        return ()
    if not isinstance(tools, (list, tuple, set, frozenset)):
        raise _lq.LLMRoleContractError(
            "active LLM roles require an explicit built-in tool-name sequence"
        )
    names = tuple(str(item) for item in tools)
    if len(names) != len(set(names)):
        raise _lq.LLMRoleContractError("duplicate built-in tool grant")
    if isinstance(tools, (set, frozenset)):
        names = tuple(sorted(names))
    return names


def _llm_selected_tool_set(tools):
    return frozenset(_llm_selected_tools(tools))


def _llm_selected_mcp_servers(mcp_servers):
    if isinstance(mcp_servers, dict):
        return frozenset(str(name) for name in mcp_servers)
    return frozenset(str(name) for name in (mcp_servers or ()))


_BOT_DIR_SCOPE_RE = re.compile(rf"^bots/{re.escape(ACTIVE_BOT_PREFIX)}(?P<version>\d+)$")
_EVIDENCE_SCOPE_RE = re.compile(
    r"^web/core/results/v(?P<version>\d+)/evidence_snapshot$"
)
_BOOTSTRAP_EVIDENCE_SCOPE_RE = re.compile(
    rf"^bots/{re.escape(ACTIVE_BOT_PREFIX)}(?P<version>\d+)/\.protocol_bootstrap_no_strength_evidence$"
)
_WORKER_WORKSPACE_SCOPE_RE = re.compile(
    r"^web/core/results/workflow/artifacts/workspaces/(?P<digest>[0-9a-f]{64})$"
)
_WORKFLOW_ARTIFACT_SCOPE_RE = re.compile(
    r"^web/core/results/workflow/artifacts/(?P<digest>[0-9a-f]{64})$"
)
_CROSSOVER_WORKSPACE_SCOPE_RE = re.compile(
    r"^web/core/results/crossover_workspaces/"
    r"v(?P<version>\d+)-attempt-(?P<attempt>\d+)-[A-Za-z0-9_-]+$"
)


def _raw_scope_entries(raw, *, default_kind):
    dirs = []
    files = []
    if raw is None:
        return dirs, files
    if isinstance(raw, dict):
        allowed_keys = {"dirs", "directories", "files", "paths"}
        unknown = set(raw) - allowed_keys
        if unknown:
            raise _lq.LLMRoleContractError(f"unknown LLM scope keys: {sorted(unknown)}")
        dirs.extend(raw.get("dirs") or raw.get("directories") or ())
        files.extend(raw.get("files") or raw.get("paths") or ())
    elif isinstance(raw, (list, tuple, set, frozenset)):
        (dirs if default_kind == "dirs" else files).extend(raw)
    else:
        (dirs if default_kind == "dirs" else files).append(raw)
    return dirs, files


def _canonical_scope_paths(values):
    result = []
    for value in values:
        relative, _resolved = _project_relative_path(value)
        if relative in result:
            raise _lq.LLMRoleContractError(f"duplicate LLM authority path: {relative}")
        result.append(relative)
    return result


def _scope_evidence_provenance(dispatch_receipt):
    if not isinstance(dispatch_receipt, LLMDispatchReceipt):
        return {}
    try:
        value = json.loads(dispatch_receipt.evidence.provenance_json)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _validate_role_scope(
    contract,
    role_name,
    *,
    allowed_read_dirs,
    allowed_write_dir,
    allowed_evidence_snapshot_dir,
    context_files,
    exact_bash_commands,
    dispatch_receipt,
    selected_tools,
    strict_authority_json,
    model,
):
    read_dirs_raw, read_files_raw = _raw_scope_entries(
        allowed_read_dirs,
        default_kind="dirs",
    )
    write_dirs_raw, write_files_raw = _raw_scope_entries(
        allowed_write_dir,
        default_kind="dirs",
    )
    read_dirs = _canonical_scope_paths(read_dirs_raw)
    read_files = _canonical_scope_paths(read_files_raw)
    write_dirs = _canonical_scope_paths(write_dirs_raw)
    write_files = _canonical_scope_paths(write_files_raw)
    context = _canonical_scope_paths(context_files or ())
    evidence = None
    if allowed_evidence_snapshot_dir is not None:
        evidence, _resolved = _project_relative_path(
            allowed_evidence_snapshot_dir
        )
    selected_bash = tuple(str(item).strip() for item in (exact_bash_commands or ()))
    provenance = _scope_evidence_provenance(dispatch_receipt)

    if context:
        # Active production renderers inject their compiled context directly
        # before signing the prompt. No provider role accepts caller-selected
        # context files.
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: external context files are forbidden"
        )

    policy = contract.scope_policy
    if policy in {"none", "orchestrator_mcp_only"}:
        if any((read_dirs, read_files, write_dirs, write_files, evidence)):
            raise _lq.LLMRoleContractError(
                f"{contract.role_id}: filesystem authority is forbidden"
            )
    elif policy == "canonical_candidates":
        if (
            read_files or write_dirs or write_files
            or not 1 <= len(read_dirs) <= 2
            or any(_BOT_DIR_SCOPE_RE.fullmatch(path) is None for path in read_dirs)
        ):
            raise _lq.LLMRoleContractError(
                f"{contract.role_id}: only one/two canonical "
                f"{ACTIVE_BOT_PREFIX}<N> read dirs are allowed"
            )
        if evidence is not None and not (
            _EVIDENCE_SCOPE_RE.fullmatch(evidence)
            or _BOOTSTRAP_EVIDENCE_SCOPE_RE.fullmatch(evidence)
        ):
            raise _lq.LLMRoleContractError(
                f"{contract.role_id}: evidence path is not a generation snapshot"
            )
        subject_versions = {
            int(value)
            for key, value in provenance.items()
            if key in {"source_v", "next_v"}
            and isinstance(value, int)
        }
        read_versions = {
            int(_BOT_DIR_SCOPE_RE.fullmatch(path).group("version"))
            for path in read_dirs
        }
        if subject_versions and not read_versions.issubset(subject_versions):
            raise _lq.LLMRoleContractError(
                f"{contract.role_id}: candidate read scope is outside receipt subject versions"
            )
        if evidence is not None:
            match = _EVIDENCE_SCOPE_RE.fullmatch(evidence) or (
                _BOOTSTRAP_EVIDENCE_SCOPE_RE.fullmatch(evidence)
            )
            if subject_versions and int(match.group("version")) not in subject_versions:
                raise _lq.LLMRoleContractError(
                    f"{contract.role_id}: evidence snapshot version is outside receipt subject"
                )
    elif policy in {"worker_candidate", "debug_candidate"}:
        if (
            read_files or write_dirs or len(read_dirs) != 1
            or _WORKER_WORKSPACE_SCOPE_RE.fullmatch(read_dirs[0]) is None
            or evidence is not None
        ):
            raise _lq.LLMRoleContractError(
                f"{contract.role_id}: exact lease-isolated Worker workspace required"
            )
        candidate = read_dirs[0]
        candidate_from_receipt = provenance.get("candidate_path")
        if candidate_from_receipt is not None:
            candidate_relative, _ = _project_relative_path(candidate_from_receipt)
            if candidate_relative != candidate:
                raise _lq.LLMRoleContractError(
                    f"{contract.role_id}: candidate scope differs from evidence receipt"
                )
        if policy == "debug_candidate":
            if write_files:
                raise _lq.LLMRoleContractError("debug agent cannot receive write scope")
        else:
            expected_write = f"{candidate}/policy.py"
            if write_files != [expected_write]:
                raise _lq.LLMRoleContractError(
                    "Worker write scope must be the compiled candidate policy.py"
                )
            if provenance.get("allowed_files") != ["policy.py"]:
                raise _lq.LLMRoleContractError(
                    "Worker evidence receipt must bind allowed_files=['policy.py']"
                )
            task = provenance.get("task")
            next_v = provenance.get("next_v")
            if not isinstance(task, dict) or not isinstance(next_v, int):
                raise _lq.LLMRoleContractError("Worker compiled task provenance invalid")
            from worker_boundary import allowed_files_for_task

            if allowed_files_for_task(task, next_v) != ["policy.py"]:
                raise _lq.LLMRoleContractError(
                    "Worker compiled task does not authorize policy.py"
                )
    elif policy == "crossover_workspace":
        artifact_dirs = [
            path for path in read_dirs
            if _WORKFLOW_ARTIFACT_SCOPE_RE.fullmatch(path)
        ]
        workspace_dirs = [
            path for path in read_dirs
            if _CROSSOVER_WORKSPACE_SCOPE_RE.fullmatch(path)
        ]
        if (
            read_files or write_dirs or len(read_dirs) != 3
            or len(artifact_dirs) != 2 or len(workspace_dirs) != 1
        ):
            raise _lq.LLMRoleContractError(
                "Crossover requires two immutable parent artifacts and one target workspace"
            )
        workspace = workspace_dirs[0]
        if write_files != [f"{workspace}/policy.py"]:
            raise _lq.LLMRoleContractError(
                "Crossover write scope must be the exact target policy.py"
            )
        role_match = re.search(r"→v(?P<version>\d+)", str(role_name))
        workspace_match = _CROSSOVER_WORKSPACE_SCOPE_RE.fullmatch(workspace)
        if (
            role_match is None
            or int(role_match.group("version"))
            != int(workspace_match.group("version"))
            or provenance.get("target_v") != int(workspace_match.group("version"))
        ):
            raise _lq.LLMRoleContractError("Crossover target version scope mismatch")
        parent_artifacts = provenance.get("parent_artifacts")
        if sorted(parent_artifacts or ()) != sorted(
            path.rsplit("/", 1)[-1] for path in artifact_dirs
        ):
            raise _lq.LLMRoleContractError(
                "Crossover parent artifact scope differs from evidence receipt"
            )
        if evidence is not None:
            evidence_match = _EVIDENCE_SCOPE_RE.fullmatch(evidence)
            if (
                evidence_match is None
                or int(evidence_match.group("version"))
                != provenance.get("target_v")
            ):
                raise _lq.LLMRoleContractError("Crossover evidence snapshot version mismatch")
    elif policy == "operator_exact_files":
        if (
            read_dirs or write_dirs or write_files or evidence is not None
            or set(read_files) != set(contract.fixed_read_files)
            or len(read_files) != len(contract.fixed_read_files)
            or selected_bash != contract.fixed_bash_commands
        ):
            raise _lq.LLMRoleContractError(
                "Operator probe scope/config differs from the fixed oracle contract"
            )
        repo_root = provenance.get("repo_root")
        if repo_root is None or Path(str(repo_root)).resolve() != _lq._LLM_PROJECT_ROOT.resolve():
            raise _lq.LLMRoleContractError("Operator probe repo root receipt mismatch")
    else:
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: unknown scope policy {policy!r}"
        )

    if policy != "operator_exact_files" and selected_bash:
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: exact Bash allowlist is not registered"
        )
    def absolute_paths(paths):
        return tuple(
            str((_lq._LLM_PROJECT_ROOT / path).resolve(strict=False)) for path in paths
        )

    return FrozenLLMCapability(
        role_id=contract.role_id,
        model=str(model),
        selected_tools=tuple(selected_tools),
        read_dirs=absolute_paths(read_dirs),
        read_files=absolute_paths(read_files),
        write_dirs=absolute_paths(write_dirs),
        write_files=absolute_paths(write_files),
        evidence_dir=(
            str((_lq._LLM_PROJECT_ROOT / evidence).resolve(strict=False))
            if evidence is not None else None
        ),
        context_files=absolute_paths(context),
        exact_bash_commands=selected_bash,
        strict_authority_json=strict_authority_json,
        strict_authority_sha256=(
            hashlib.sha256(strict_authority_json.encode("utf-8")).hexdigest()
            if strict_authority_json is not None else None
        ),
        _authority=_lq._LLM_RECEIPT_AUTHORITY,
    )


def validate_llm_role_dispatch(
    role_name,
    *,
    tools,
    rendered_prompt,
    provider_path="subagent_sdk",
    mcp_servers=(),
    context_files=(),
    allowed_read_dirs=None,
    allowed_write_dir=None,
    allowed_evidence_snapshot_dir=None,
    strict_authority=None,
    exact_bash_commands=None,
    model="sonnet",
):
    """Fail closed if a real provider dispatch exceeds its registered scope."""

    contract = _lq.resolve_llm_role_contract(role_name)
    if str(model) not in contract.allowed_models:
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: model {model!r} outside "
            f"{list(contract.allowed_models)!r}"
        )
    selected_tool_names = _llm_selected_tools(tools)
    selected_tools = frozenset(selected_tool_names)
    selected_mcp = _llm_selected_mcp_servers(mcp_servers)
    rendered_prompt = _validate_rendered_llm_prompt(
        rendered_prompt,
        contract,
        role_name,
        mcp_servers,
        model,
    )
    dispatch_receipt = rendered_prompt.dispatch_receipt
    if str(provider_path) != contract.provider_path:
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: provider path {provider_path!r} is not "
            f"{contract.provider_path!r}"
        )
    if selected_tools not in contract.allowed_tool_sets:
        allowed = [sorted(group) for group in contract.allowed_tool_sets]
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: tools {sorted(selected_tools)!r} outside {allowed!r}"
        )
    if selected_mcp != contract.allowed_mcp_servers:
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: MCP servers {sorted(selected_mcp)!r} outside "
            f"{sorted(contract.allowed_mcp_servers)!r}"
        )
    if contract.requires_read_scope and not any((
        allowed_read_dirs,
        allowed_write_dir,
        context_files,
    )):
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: explicit filesystem read scope is required"
        )
    if contract.requires_write_scope and allowed_write_dir is None:
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: exact write scope is required"
        )
    if not contract.requires_write_scope and allowed_write_dir is not None:
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: filesystem write scope is forbidden"
        )
    if context_files and not contract.allows_context_files:
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: context-file prompt injection is not registered"
        )
    if (
        allowed_evidence_snapshot_dir is not None
        and not contract.allows_evidence_snapshot
    ):
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: filesystem evidence snapshot is not registered"
        )
    if strict_authority is not None and not contract.allows_strict_authority:
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: strict-authority call binding is not registered"
        )
    if (
        exact_bash_commands is not None
        and not contract.allows_exact_bash_commands
    ):
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: exact Bash command grants are not registered"
        )
    if contract.allows_exact_bash_commands and exact_bash_commands is None:
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: exact Bash command allowlist is required"
        )
    strict_authority_json = _canonical_strict_authority_json(strict_authority)
    frozen_capability = _validate_role_scope(
        contract,
        role_name,
        allowed_read_dirs=allowed_read_dirs,
        allowed_write_dir=allowed_write_dir,
        allowed_evidence_snapshot_dir=allowed_evidence_snapshot_dir,
        context_files=context_files,
        exact_bash_commands=exact_bash_commands,
        dispatch_receipt=dispatch_receipt,
        selected_tools=selected_tool_names,
        strict_authority_json=strict_authority_json,
        model=str(model),
    )
    return contract, dispatch_receipt, frozen_capability


def render_llm_role_contract_suffix(
    contract,
    role_name,
    *,
    tools,
    mcp_servers=(),
    rendered_provider_prefix="",
    dispatch_receipt=None,
    frozen_capability=None,
):
    """Render the final, system-owned provider instruction for one dispatch."""

    provider_prefix = str(rendered_provider_prefix or "")
    capability_payload = {
        "model": frozen_capability.model,
        "selected_tools": list(frozen_capability.selected_tools),
        "read_dirs": list(frozen_capability.read_dirs),
        "read_files": list(frozen_capability.read_files),
        "write_dirs": list(frozen_capability.write_dirs),
        "write_files": list(frozen_capability.write_files),
        "evidence_dir": frozen_capability.evidence_dir,
        "context_files": list(frozen_capability.context_files),
        "exact_bash_commands": list(frozen_capability.exact_bash_commands),
        "strict_authority_sha256": frozen_capability.strict_authority_sha256,
    }
    payload = {
        "schema": "national_tcp_llm_role_contract_v2",
        "role_id": contract.role_id,
        "runtime_role": str(role_name),
        "provider_path": contract.provider_path,
        "model": frozen_capability.model,
        "renderer": contract.renderer,
        "renderer_receipt_digest": dispatch_receipt.renderer.receipt_digest,
        "renderer_producer_file": dispatch_receipt.renderer.producer_file,
        "renderer_producer_file_sha256": (
            dispatch_receipt.renderer.producer_file_sha256
        ),
        "renderer_producer_function_sha256": (
            dispatch_receipt.renderer.producer_function_sha256
        ),
        "renderer_template_digests": dict(
            dispatch_receipt.renderer.template_digests
        ),
        "evidence_provenance_kind": (
            dispatch_receipt.evidence.provenance_kind
        ),
        "evidence_provenance_sha256": (
            dispatch_receipt.evidence.provenance_sha256
        ),
        "evidence_receipt_digest": dispatch_receipt.evidence.receipt_digest,
        "mcp_config_sha256": dispatch_receipt.mcp.config_sha256,
        "mcp_receipt_digest": dispatch_receipt.mcp.receipt_digest,
        "dispatch_receipt_digest": dispatch_receipt.receipt_digest,
        "frozen_capability_sha256": _receipt_digest(capability_payload),
        "frozen_capability": capability_payload,
        "selected_builtin_tools": list(frozen_capability.selected_tools),
        "selected_mcp_servers": sorted(_llm_selected_mcp_servers(mcp_servers)),
        "provider_read_scope": contract.provider_read_scope,
        "provider_write_scope": contract.provider_write_scope,
        "evidence_policy": contract.evidence_policy,
        "history_policy": contract.history_policy,
        "rendered_provider_prefix_chars": len(provider_prefix),
        "rendered_provider_prefix_sha256": hashlib.sha256(
            provider_prefix.encode("utf-8")
        ).hexdigest(),
        "strength_authority": "zero",
        "certification_authority": "zero",
        "rating_authority": "zero",
        "historical_memory_authority": "zero",
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    payload["contract_digest"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    proposal_emission_gate = ""
    if contract.role_id == "master_proposal":
        repair_attempt = str(role_name).endswith(
            (" SCHEMA RETRY", " DISTINCTNESS RETRY")
        )
        proposal_emission_gate = (
            "\n\n# SYSTEM-OWNED MASTER PROPOSAL EMISSION GATE (LAST)\n"
            + (
                "This is the sole repair attempt (attempt 2 of 2). "
                if repair_attempt
                else "This is the initial attempt (attempt 1 of at most 2). "
            )
            + "The only admissible Scout completion is one raw JSON object "
            "matching the rendered FINAL SCOUT OUTPUT CONTRACT. Do not return "
            "Markdown fences, analysis, an acknowledgement, a summary, or "
            "trailing commentary. Apply the closed schema and any system-owned "
            "repair instruction in the rendered prefix, then emit the complete "
            "object now. "
            + (
                "A malformed or duplicate repair object is rejected; there is "
                "no third attempt."
                if repair_attempt
                else "If this object is rejected, only the system may authorize "
                "the single bounded repair attempt; do not self-retry or emit a "
                "second object."
            )
        )
    return (
        "\n\n# SYSTEM-OWNED ACTIVE LLM ROLE CONTRACT (FINAL)\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n\nThe rendered template and every attached context block are "
        "subordinate to this final contract. Do not read, infer from, search, "
        "or request archive/legacy content, mutable live result files, unbound "
        "replays, free-standing lessons/experience, generic Git history, or any "
        "path/tool not listed above. Historical input is allowed only when the "
        "history_policy explicitly names its system-bound form. This response "
        "may perform only its registered advisory or scoped implementation "
        "function; it is never strength evidence, a rating/certification result, "
        "persistent memory, or authority to override deterministic gates.\n"
        + proposal_emission_gate
        + "\n"
    )


def bind_llm_role_provider_prompt(
    rendered_prompt,
    role_name,
    *,
    tools,
    provider_path="subagent_sdk",
    mcp_servers=(),
    context_files=(),
    allowed_read_dirs=None,
    allowed_write_dir=None,
    allowed_evidence_snapshot_dir=None,
    strict_authority=None,
    exact_bash_commands=None,
    max_chars=None,
    provider_prefix=None,
    frozen_capability=None,
    model="sonnet",
):
    """Validate a dispatch and place its role contract last in provider input."""

    if frozen_capability is not None:
        contract = _lq.resolve_llm_role_contract(role_name)
        if (
            not isinstance(frozen_capability, FrozenLLMCapability)
            or frozen_capability._authority is not _lq._LLM_RECEIPT_AUTHORITY
            or frozen_capability.role_id != contract.role_id
        ):
            raise _lq.LLMRoleContractError(
                f"{contract.role_id}: frozen capability authority invalid"
            )
        allowed_read_dirs = {
            "dirs": frozen_capability.read_dirs,
            "files": frozen_capability.read_files,
        }
        allowed_write_dir = {
            "dirs": frozen_capability.write_dirs,
            "files": frozen_capability.write_files,
        } if (frozen_capability.write_dirs or frozen_capability.write_files) else None
        allowed_evidence_snapshot_dir = frozen_capability.evidence_dir
        context_files = frozen_capability.context_files
        exact_bash_commands = frozen_capability.exact_bash_commands or None
        tools = frozen_capability.selected_tools
        model = frozen_capability.model
    contract, dispatch_receipt, validated_capability = validate_llm_role_dispatch(
        role_name,
        tools=tools,
        rendered_prompt=rendered_prompt,
        provider_path=provider_path,
        mcp_servers=mcp_servers,
        context_files=context_files,
        allowed_read_dirs=allowed_read_dirs,
        allowed_write_dir=allowed_write_dir,
        allowed_evidence_snapshot_dir=allowed_evidence_snapshot_dir,
        strict_authority=strict_authority,
        exact_bash_commands=exact_bash_commands,
        model=model,
    )
    if frozen_capability is not None and validated_capability != frozen_capability:
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: frozen capability replay mismatch"
        )
    # The sealed renderer bytes are immutable evidence.  Do not even strip
    # trailing whitespace here: doing so would make the provider prefix differ
    # from the receipt while appearing visually identical in logs.
    base = str(
        rendered_prompt.text if provider_prefix is None else provider_prefix
    )
    if rendered_prompt.text not in base:
        raise _lq.LLMRoleContractError(
            f"{contract.role_id}: provider prefix does not contain the sealed "
            "renderer output"
        )
    suffix = render_llm_role_contract_suffix(
        contract,
        role_name,
        tools=tools,
        mcp_servers=mcp_servers,
        rendered_provider_prefix=base,
        dispatch_receipt=dispatch_receipt,
        frozen_capability=validated_capability,
    )
    if max_chars is not None:
        if len(base) + len(suffix) > int(max_chars):
            raise _lq.LLMRoleContractError(
                f"{contract.role_id}: sealed provider prompt exceeds the "
                "provider budget; renderer output cannot be truncated"
            )
    return base + suffix, contract
