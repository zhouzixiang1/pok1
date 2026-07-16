from __future__ import annotations

from types import SimpleNamespace

import pytest

import worker_mcp.compatibility as compatibility
import worker_mcp.healthcheck as healthcheck
from worker_mcp.healthcheck import HealthChecker
from worker_mcp.compatibility import RuntimeCompatibilityError


class _Persistence:
    def ping(self) -> None:
        return None


class _ForbiddenExecutor:
    async def run(self, *args, **kwargs):
        raise AssertionError("shallow healthcheck must not invoke the model executor")


@pytest.mark.asyncio
async def test_shallow_health_skips_unverified_version_and_model_canaries(
    worker_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = SimpleNamespace(
        persistence=_Persistence(),
        queue=SimpleNamespace(running=True),
        executor=_ForbiddenExecutor(),
    )
    monkeypatch.setattr(
        healthcheck.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        healthcheck,
        "runtime_inventory",
        lambda config: {
            "claude_agent_sdk": (True, "version verified"),
            "claude_code": (True, "version verified"),
            "cc_switch_contract": (
                None,
                "unverified: expected=3.17.0; endpoint exposes no version evidence",
            ),
        },
    )

    async def gateway_ok(config):
        return SimpleNamespace(healthy=True)

    monkeypatch.setattr(healthcheck, "check_gateway", gateway_ok)
    monkeypatch.setattr(healthcheck, "require_worker_credential", lambda config: None)

    result = await HealthChecker(worker_config, service).check(deep=False)

    assert result.status == "healthy"
    assert result.components["cc_switch_contract"].status == "skipped"
    assert "unverified" in result.components["cc_switch_contract"].detail
    for component in ("text_canary", "tool_canary", "structured_output_canary"):
        assert result.components[component].status == "skipped"
        assert "diagnose.py --deep" in result.components[component].detail


def test_cc_switch_expected_version_is_not_reported_as_verified(
    worker_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        compatibility.importlib.metadata,
        "version",
        lambda package: worker_config.runtime.expected_claude_agent_sdk,
    )
    monkeypatch.setattr(
        compatibility.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=f"Claude Code {worker_config.runtime.expected_claude_code}",
            stderr="",
        ),
    )

    inventory = compatibility.runtime_inventory(worker_config)

    verified, detail = inventory["cc_switch_contract"]
    assert verified is None
    assert "unverified" in detail
    assert worker_config.runtime.expected_cc_switch in detail


def test_runtime_compatibility_gate_enforces_sdk_cli_and_sandbox(
    worker_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        compatibility,
        "runtime_inventory",
        lambda config, **kwargs: {
            "claude_agent_sdk": (False, "installed=old"),
            "claude_code": (True, "installed=2.1.205"),
            "linux_sandbox": (True, "available"),
            "cc_switch_contract": (None, "unverified"),
        },
    )
    with pytest.raises(RuntimeCompatibilityError, match="claude_agent_sdk"):
        compatibility.require_compatible_runtime(
            worker_config, path="/usr/bin", home="/tmp/worker-home"
        )

    monkeypatch.setattr(
        compatibility,
        "runtime_inventory",
        lambda config, **kwargs: {
            "claude_agent_sdk": (True, "installed=0.2.91"),
            "claude_code": (True, "installed=2.1.205"),
            "linux_sandbox": (True, "available"),
            "cc_switch_contract": (None, "unverified"),
        },
    )
    compatibility.require_compatible_runtime(
        worker_config, path="/usr/bin", home="/tmp/worker-home"
    )


def test_claude_cli_version_token_must_match_exactly(
    worker_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        compatibility.importlib.metadata,
        "version",
        lambda package: worker_config.runtime.expected_claude_agent_sdk,
    )
    monkeypatch.setattr(
        compatibility.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="Claude Code 2.1.2050",
            stderr="",
        ),
    )
    inventory = compatibility.runtime_inventory(worker_config)
    assert inventory["claude_code"][0] is False
