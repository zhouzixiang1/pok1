"""Red-team hardening regressions (2026-10-12 fixes, _20261011 series).

One focused test per red-team verdict that required a code change:
* seed predictability — the ``posix`` environ read channel is statically
  forbidden in the candidate capability contract;
* gate/reap position mismatch — an idle-but-strong member (top half by
  PLAIN rating) is never the ratchet reap victim on uncertainty alone;
* leak-surface guard vs first-strict bootstrap — the commit ledger no
  longer demands a result-side plan copy that the guard removed;
* mid-flight reap of the frozen pool-weakest — ``pool_gate_label_missing``
  classifies as infrastructure, not a candidate regression.
"""

import ast
import copy

import pytest

from bot_namespace import bot_name


def _policy_tree(text: str) -> ast.Module:
    return ast.parse(text)


class TestPosixForbiddenInCapabilityContract:
    def _evidence(self, text: str) -> dict:
        import national_capability_contract as ncc

        return ncc._policy_static_evidence(_policy_tree(text))

    def test_posix_import_is_forbidden(self):
        # The exact red-team PoC shape: reach posix.environ without the
        # forbidden ``os`` root.
        evidence = self._evidence(
            "import posix as _posix\n"
            "SEED = int(_posix.environ[b'POK_NATIVE_BOT_SEED'])\n"
        )
        assert any(item.endswith(":posix") for item in evidence["forbidden_imports"])

    def test_from_posix_import_is_forbidden(self):
        evidence = self._evidence("from posix import environ\n")
        assert any(item.endswith(":posix") for item in evidence["forbidden_imports"])

    def test_pristine_policy_still_io_free(self):
        evidence = self._evidence(
            "import random\n"
            "import precompute\n"
            "random.seed(0)\n"
        )
        assert evidence["forbidden_imports"] == []

    def test_posix_in_frozen_root_set(self):
        import national_capability_contract as ncc

        assert "posix" in ncc._FORBIDDEN_IMPORT_ROOTS


class TestRatchetVictimBoundedToPlainRatingBottomHalf:
    def _setup(self, monkeypatch, tmp_path, versions, ratings, games=None):
        import test_pool_reap_target_size_20261011 as helper

        return helper._setup_pool(
            monkeypatch,
            tmp_path,
            versions,
            rating_by_version=ratings,
            games_by_version=games or {},
            priority_version=max(versions),
        )

    def test_idle_strong_member_is_protected_from_uncertainty_removal(
        self, monkeypatch, tmp_path
    ):
        """Live-pool v11 shape: top-third plain rating, cons score lowest.

        The conservative key (r - 2rd) treats inactivity-inflated rd as
        weakness; before the fix this member was the reap victim despite
        being plainly stronger than half the pool.
        """
        import tool_commit

        monkeypatch.setenv("POK_PUBLISH_MODE", "pool_ratchet")
        monkeypatch.setenv("POK_POOL_RATCHET_TARGET_SIZE", "12")
        versions = list(range(1, 14))
        ratings = {}
        for version in versions:
            ratings[version] = (1300.0 + (version - 1) * 50.0, 50.0)
        # v9: plain rating 4th highest, but rd=400 drags its conservative
        # score to the pool bottom.
        ratings[9] = (1700.0, 400.0)
        self._setup(monkeypatch, tmp_path, versions, ratings)

        plan = tool_commit._build_pool_reap_plan(
            {
                "identity": {
                    "publication_id": "a" * 64,
                    "commit_oid": "b" * 40,
                    "remote_publication": {"remote_main_oid": "c" * 40},
                    "version": 13,
                }
            }
        )

        assert plan["required_reaps"] == 1
        victim = plan["targets"][0]["candidate"]
        assert victim != bot_name(9)
        # The victim stays the conservative-weakest member of the
        # plain-rating BOTTOM half.
        assert victim == bot_name(1)
        tool_commit._validate_pool_reap_plan(
            plan,
            {
                "identity": {
                    "publication_id": "a" * 64,
                    "commit_oid": "b" * 40,
                    "remote_publication": {"remote_main_oid": "c" * 40},
                    "version": 13,
                }
            },
        )

    def test_bottom_half_victim_existence_is_preserved(self, monkeypatch, tmp_path):
        """A pool above capacity still always yields a bottom-half victim."""
        import tool_commit

        monkeypatch.setenv("POK_PUBLISH_MODE", "pool_ratchet")
        monkeypatch.setenv("POK_POOL_RATCHET_TARGET_SIZE", "12")
        versions = list(range(1, 14))
        # Everyone except current/priority inflated to rd=400 (all nominally
        # weak); the victim must still exist inside the plain bottom half.
        ratings = {
            v: (1300.0 + (v - 1) * 50.0, 400.0) for v in versions
        }
        self._setup(monkeypatch, tmp_path, versions, ratings)

        plan = tool_commit._build_pool_reap_plan(
            {
                "identity": {
                    "publication_id": "a" * 64,
                    "commit_oid": "b" * 40,
                    "remote_publication": {"remote_main_oid": "c" * 40},
                    "version": 13,
                }
            }
        )
        assert plan["required_reaps"] == 1
        assert plan["targets"][0]["candidate"] == bot_name(1)


def _ledger_errors(result):
    errors = []
    for gate in result.get("failed_gates") or []:
        errors.extend(gate.get("errors") or [])
    return errors


class TestFirstStrictLedgerToleratesMissingResultSidePlan:
    @staticmethod
    def _bootstrap_checkpoint(plan):
        from tests.test_native_runtime_quality_identity import _commit_checkpoint

        ckpt = _commit_checkpoint()
        ckpt["audit_context"] = {"precommit_eval_plan": plan}
        precommit = ckpt["gate_results"]["precommit_eval"]
        precommit.update({
            "precommit_gate_admitted": True,
            "strength_admitted": False,
            "rating_eligible": False,
            "official_opponent_eligible": False,
            "strength_order": {"samples": 0},
        })
        return ckpt

    @staticmethod
    def _run_ledger(monkeypatch, tmp_path, ckpt):
        import national_native
        import national_position_contract
        import precommit_eval_contract
        import runtime_architecture_policy
        import system_strict_bootstrap
        import tool_commit
        import tool_gates

        monkeypatch.setattr(
            tool_gates, "_bot_code_fingerprint", lambda _path: "b" * 64
        )
        monkeypatch.setattr(
            runtime_architecture_policy,
            "runtime_contract_ledger_digest",
            lambda _ledger: "c" * 64,
        )
        monkeypatch.setattr(
            runtime_architecture_policy,
            "validate_runtime_contract_ledger",
            lambda _ledger: [],
        )
        monkeypatch.setattr(
            precommit_eval_contract,
            "validate_precommit_plan",
            lambda *_args, **_kwargs: [],
        )
        monkeypatch.setattr(
            precommit_eval_contract,
            "validate_evaluation_contract",
            lambda *_args, **_kwargs: [],
        )
        monkeypatch.setattr(
            national_native, "check_native_contract", lambda *_a, **_k: []
        )
        monkeypatch.setattr(
            national_position_contract,
            "detect_position_semantics_errors",
            lambda *_args, **_kwargs: [],
        )
        monkeypatch.setattr(
            system_strict_bootstrap,
            "is_declared_native_bootstrap",
            lambda _ckpt: True,
        )
        return tool_commit.validate_commit_gate_ledger(
            144,
            143,
            ckpt,
            bot_dir=tmp_path,
        )

    @pytest.fixture
    def control_plan(self):
        return {
            "plan_digest": "e" * 64,
            "opponents": [
                {"name": "first_strict_control_v1",
                 "authority": "system_first_strict_control"}
            ],
            "sample_plan": [],
        }

    def test_new_shape_record_without_result_side_plan_passes_compare(
        self, monkeypatch, tmp_path, control_plan
    ):
        ckpt = self._bootstrap_checkpoint(control_plan)
        result = self._run_ledger(monkeypatch, tmp_path, ckpt)
        assert "first_strict_control_result_plan_mismatch" not in (
            _ledger_errors(result)
        )

    def test_legacy_matching_result_side_plan_still_passes_compare(
        self, monkeypatch, tmp_path, control_plan
    ):
        ckpt = self._bootstrap_checkpoint(control_plan)
        ckpt["gate_results"]["precommit_eval"]["precommit_eval_plan"] = (
            control_plan
        )
        result = self._run_ledger(monkeypatch, tmp_path, ckpt)
        assert "first_strict_control_result_plan_mismatch" not in (
            _ledger_errors(result)
        )

    def test_legacy_tampered_result_side_plan_is_still_caught(
        self, monkeypatch, tmp_path, control_plan
    ):
        ckpt = self._bootstrap_checkpoint(control_plan)
        tampered = copy.deepcopy(control_plan)
        tampered["plan_digest"] = "0" * 64
        ckpt["gate_results"]["precommit_eval"]["precommit_eval_plan"] = tampered
        result = self._run_ledger(monkeypatch, tmp_path, ckpt)
        assert "first_strict_control_result_plan_mismatch" in (
            _ledger_errors(result)
        )


class TestPoolGateLabelMissingClassifiesAsInfrastructure:
    def test_mixed_infra_and_label_missing_is_infra_timeout(self):
        from failure_classification import classify_precommit_gate, is_infra_blocker

        assert is_infra_blocker("pool_gate_label_missing") is True
        # The red-team executed delta: the pure-runner-exception set was
        # already infra; adding the frozen-gate-opponent-vanished blocker
        # must not flip the verdict to a candidate regression.
        verdict = classify_precommit_gate({
            "passed": False,
            "blockers": [
                {"reason": "native_precommit_exception",
                 "details": "FileNotFoundError: bots/national_cloud_v11"},
                {"reason": "pool_gate_label_missing",
                 "details": "no matchup row for the frozen pool-weakest label"},
            ],
        })
        assert verdict == "infra_timeout"

    def test_genuine_regression_blockers_still_classify_as_regression(self):
        from failure_classification import classify_precommit_gate

        verdict = classify_precommit_gate({
            "passed": False,
            "blockers": [{"reason": "did_not_beat_pool_weakest"}],
        })
        assert verdict == "regression"
