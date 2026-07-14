"""Fail-closed import and runtime-input boundary for the independent B route."""

from __future__ import annotations

import ast
import hashlib
import os
import stat
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

from bots.research_native_lab.cfr_neural_search.core.strict_io import read_regular_bytes

from .contracts import M5_ROOT, REPOSITORY_ROOT, load_independence_contract


_DYNAMIC_CALL_NAMES = frozenset({"__import__", "compile", "eval", "exec"})
_DYNAMIC_ATTRIBUTES = frozenset(
    {
        "__import__",
        "exec_module",
        "find_loader",
        "find_spec",
        "import_module",
        "load_module",
        "module_from_spec",
        "spec_from_file_location",
    }
)
_REFLECTION_CALL_NAMES = frozenset({"getattr", "globals", "locals", "vars"})


def _absolute_without_resolve(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _repository_relative_real_path(path: str | Path) -> tuple[Path, str]:
    """Reject lexical escapes and every symlink/special component below repo."""

    candidate = _absolute_without_resolve(path)
    try:
        relative_path = candidate.relative_to(REPOSITORY_ROOT)
    except ValueError as exc:
        raise ValueError("runtime dependency is outside the repository") from exc
    if relative_path == Path(".") or any(
        component in {"", ".", ".."} for component in relative_path.parts
    ):
        raise ValueError("runtime dependency path is not a safe repository child")
    current = REPOSITORY_ROOT
    for index, component in enumerate(relative_path.parts):
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError as exc:
            raise ValueError("runtime dependency does not exist") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError("runtime dependency contains a symlink component")
        if index < len(relative_path.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("runtime dependency parent is not a real directory")
        if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise ValueError("runtime dependency contains a special file")
    resolved = candidate.resolve(strict=True)
    if resolved != candidate:
        raise ValueError("runtime dependency real path differs from lexical path")
    return candidate, relative_path.as_posix()


def validate_runtime_input(path: str | Path) -> Path:
    """Accept only exact Route-B native inputs through a no-symlink path."""

    contract = load_independence_contract()
    candidate, relative = _repository_relative_real_path(path)
    allowed_files = set(contract["allowed_runtime_m4_files"]) | set(
        contract["allowed_runtime_native_sever_files"]
    )
    allowed_roots = tuple(str(root).rstrip("/") for root in contract["allowed_runtime_roots"])
    if relative not in allowed_files and not any(
        relative == root or relative.startswith(root + "/") for root in allowed_roots
    ):
        raise ValueError(f"runtime dependency is outside the Route B allowlist: {relative}")
    lowered_parts = {part.lower() for part in Path(relative).parts}
    if any(token in lowered_parts for token in contract["forbidden_dependency_tokens"]):
        raise ValueError("runtime dependency contains a forbidden route token")
    if relative == "sever/bot_adapter.py" or relative.startswith("sever/bot_adapter/"):
        raise ValueError("legacy Botzone adapter is forbidden in native Route B")
    return candidate


def read_validated_runtime_bytes(
    path: str | Path,
    *,
    expected_sha256: str | None = None,
    max_bytes: int = 1 << 30,
) -> bytes:
    """The sole M5 loader for external runtime bytes.

    Validation establishes the route allowlist; the descriptor-relative M4
    primitive then rejects ancestry substitution, symlinks, non-regular files,
    and changes during the read.  Callers loading a published artifact must
    also supply its frozen digest.
    """

    candidate = validate_runtime_input(path)
    if not candidate.is_file():
        raise ValueError("runtime byte loader requires a regular file")
    if expected_sha256 is not None and (
        type(expected_sha256) is not str
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        raise ValueError("expected runtime digest must be lowercase SHA-256")
    raw = read_regular_bytes(candidate, root=REPOSITORY_ROOT, max_bytes=max_bytes)
    observed = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and observed != expected_sha256:
        raise ValueError("runtime dependency differs from its content binding")
    return raw


def _module_context(path: Path, package_root: Path, prefix: str) -> tuple[str, bool]:
    relative = path.relative_to(package_root)
    if relative.name == "__init__.py":
        suffix = relative.parent.parts
        is_package = True
    else:
        suffix = relative.with_suffix("").parts
        is_package = False
    module = ".".join((prefix, *suffix)) if suffix else prefix
    return module, is_package


def _resolve_relative_module(
    node: ast.ImportFrom,
    *,
    current_module: str,
    is_package: bool,
) -> tuple[str, ...]:
    package_parts = current_module.split(".") if is_package else current_module.split(".")[:-1]
    ascend = node.level - 1
    if ascend >= len(package_parts):
        raise ValueError("relative import escapes the Python package root")
    base_parts = package_parts[: len(package_parts) - ascend]
    if node.module:
        resolved = ".".join((*base_parts, *node.module.split(".")))
        return (resolved,)
    # In ``from .. import name`` the imported names can themselves be modules;
    # retaining them closes token and old-package escapes without guessing.
    return tuple(".".join((*base_parts, *alias.name.split("."))) for alias in node.names)


def audit_import_source(
    source: str,
    *,
    current_module: str,
    is_package: bool = False,
) -> tuple[str, ...]:
    """Parse one source unit, resolve relatives, and reject dynamic imports."""

    tree = ast.parse(source, filename=current_module)
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if any(alias.name == "__import__" for alias in node.names):
                raise ValueError("dynamic __import__ alias is forbidden")
            if node.level:
                imports.extend(
                    _resolve_relative_module(
                        node,
                        current_module=current_module,
                        is_package=is_package,
                    )
                )
            elif node.module:
                imports.append(node.module)
        elif isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Name) and function.id in _DYNAMIC_CALL_NAMES:
                raise ValueError(f"dynamic code/import call is forbidden: {function.id}")
            if isinstance(function, ast.Name) and function.id in _REFLECTION_CALL_NAMES:
                raise ValueError(
                    f"reflection call can bypass import audit and is forbidden: {function.id}"
                )
            if isinstance(function, ast.Attribute) and function.attr in _DYNAMIC_ATTRIBUTES:
                raise ValueError(f"dynamic import attribute is forbidden: {function.attr}")
        elif (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Load)
            and node.id in _DYNAMIC_CALL_NAMES
        ):
            raise ValueError(f"dynamic code/import symbol is forbidden: {node.id}")
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and node.attr in _DYNAMIC_ATTRIBUTES
        ):
            raise ValueError(f"dynamic import attribute is forbidden: {node.attr}")
        elif isinstance(node, ast.Subscript):
            key = node.slice
            if isinstance(key, ast.Constant) and key.value in (
                _DYNAMIC_CALL_NAMES | _DYNAMIC_ATTRIBUTES
            ):
                raise ValueError("subscript-based dynamic import is forbidden")
    return tuple(imports)


def _source_imports(path: Path, package_root: Path, prefix: str) -> Iterable[str]:
    module, is_package = _module_context(path, package_root, prefix)
    return audit_import_source(
        path.read_text(encoding="utf-8"),
        current_module=module,
        is_package=is_package,
    )


def _validate_import_modules(
    modules: Iterable[str],
    *,
    contract: Mapping[str, Any],
) -> tuple[str, ...]:
    # ``load_independence_contract`` returns a strict Mapping.  Keeping this
    # helper separate lets mutation tests exercise exactly the production
    # policy without creating transient source inside the audited package.
    allowed_external = set(contract["allowed_external_import_roots"])
    allowed_m4 = set(contract["allowed_m4_import_modules"])
    allowed_sever = set(contract["allowed_native_sever_import_modules"])
    forbidden_modules = set(contract["forbidden_import_modules"])
    forbidden_dynamic = set(contract["forbidden_dynamic_import_modules"])
    forbidden_tokens = tuple(contract["forbidden_dependency_tokens"])
    m5_prefix = str(contract["m5_absolute_package"])
    result = tuple(modules)
    for module in result:
        if any(module == name or module.startswith(name + ".") for name in forbidden_modules):
            raise ValueError(f"M5 source imports a forbidden dependency: {module}")
        components = tuple(part.lower() for part in module.split("."))
        if any(token in components for token in forbidden_tokens):
            raise ValueError(f"M5 import names a forbidden dependency: {module}")
        root = module.split(".", 1)[0]
        if root in forbidden_dynamic:
            raise ValueError(f"M5 source imports a dynamic loader: {module}")
        if module == m5_prefix or module.startswith(m5_prefix + "."):
            continue
        if module == "bots.research_native_lab.common_contracts" or module.startswith(
            "bots.research_native_lab.common_contracts."
        ):
            continue
        if module in allowed_sever or root in allowed_external or module in allowed_m4:
            continue
        if root in sys.stdlib_module_names:
            continue
        raise ValueError(f"M5 source imports a non-allowlisted dependency: {module}")
    return result


def audit_route_b_source(
    source: str,
    *,
    current_module: str,
    is_package: bool = False,
) -> tuple[str, ...]:
    imports = audit_import_source(
        source,
        current_module=current_module,
        is_package=is_package,
    )
    return _validate_import_modules(imports, contract=load_independence_contract())


def verify_import_independence(package_root: Path = M5_ROOT) -> dict[str, object]:
    """Reject static, relative, dynamic, legacy, or shadow Route-A imports."""

    contract = load_independence_contract()
    package_root, relative = _repository_relative_real_path(package_root)
    if package_root != M5_ROOT or relative != M5_ROOT.relative_to(REPOSITORY_ROOT).as_posix():
        raise ValueError("independence audit accepts only the real M5 package root")
    m5_prefix = str(contract["m5_absolute_package"])
    entries = sorted(package_root.rglob("*"))
    if any(path.is_symlink() for path in entries):
        raise ValueError("M5 source tree contains a symlink")
    source_files = [
        path for path in entries if path.is_file() and path.suffix == ".py" and "__pycache__" not in path.parts
    ]
    audited_imports: set[str] = set()
    for path in source_files:
        imports = _source_imports(path, package_root, m5_prefix)
        audited_imports.update(_validate_import_modules(imports, contract=contract))
    return {
        "schema": "route-b-m5-import-independence-verification-v2",
        "source_file_count": len(source_files),
        "resolved_imports": sorted(audited_imports),
        "dynamic_import_calls": 0,
        "legacy_adapter_imports": 0,
    }
