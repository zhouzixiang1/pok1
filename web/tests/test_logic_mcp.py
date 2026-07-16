"""Logic-level tests for MCP tools — verifies data transformations and invariants."""

# ── tool_helpers.py: compute_h2h_avg_winrate() via pure import ──

class TestComputeH2HLogic:
    def test_bot_as_b_side_uses_b_wins(self):
        from tool_helpers import compute_h2h_avg_winrate
        h2h = {
            "opponent vs target": {"games": 10, "a_wins": 3, "b_wins": 7},
        }
        wr = compute_h2h_avg_winrate("target", h2h)
        assert wr is not None
        assert abs(wr - 0.7) < 0.01

    def test_games_zero_skipped(self):
        from tool_helpers import compute_h2h_avg_winrate
        h2h = {"a vs b": {"games": 0, "a_wins": 5, "b_wins": 5}}
        assert compute_h2h_avg_winrate("a", h2h) is None

    def test_missing_wins_default_zero(self):
        from tool_helpers import compute_h2h_avg_winrate
        h2h = {"a vs b": {"games": 10}}
        wr = compute_h2h_avg_winrate("a", h2h)
        assert wr == 0.0

    def test_equal_weight_average(self):
        from tool_helpers import compute_h2h_avg_winrate
        h2h = {
            "a vs b": {"games": 100, "a_wins": 90, "b_wins": 10},  # 0.9
            "a vs c": {"games": 10, "a_wins": 1, "b_wins": 9},     # 0.1
        }
        wr = compute_h2h_avg_winrate("a", h2h)
        # Equal weight: (0.9 + 0.1) / 2 = 0.5, NOT weighted by games
        assert abs(wr - 0.5) < 0.01


# ── tool_helpers.py: load_h2h_avg_winrates() fallback logic ──

class TestLoadH2HAvgWinratesFallback:
    def test_uses_canonical_strength_rows(self, monkeypatch):
        import tool_helpers

        monkeypatch.setattr(
            tool_helpers,
            "_rating_rows_for_active",
            lambda: [
                {"name": "national_v143", "h2h_avg_wr": 0.625},
                {"name": "national_v144", "h2h_avg_wr": None, "win_rate": 0.375},
            ],
        )

        assert tool_helpers.load_h2h_avg_winrates() == {
            "national_v143": 0.625,
            "national_v144": 0.375,
        }


# ── tool_helpers.py: _select_precommit_opponents() ──

class TestSelectPrecommitOpponents:
    @staticmethod
    def _snapshot():
        active = ["national_v143", "national_v144", "national_v145", "national_v146"]
        scores = {
            "national_v143": 0.40,
            "national_v144": 0.90,
            "national_v145": 0.80,
            "national_v146": 0.10,
        }
        return {
            "available": True,
            "selection": {
                "active_bots": active,
                "rows": [
                    {
                        "name": name,
                        "selection_score": score,
                        "leaderboard_score": score,
                    }
                    for name, score in scores.items()
                ],
            },
            "h2h": {
                "national_v143 vs national_v144": {
                    "games": 10,
                    "a_wins": 5,
                    "b_wins": 5,
                    "draws": 0,
                },
                "national_v143 vs national_v146": {
                    "games": 10,
                    "a_wins": 2,
                    "b_wins": 8,
                    "draws": 0,
                }
            },
            "manifest": {"manifest_digest": "frozen-manifest"},
        }

    @staticmethod
    def _singleton_checkpoint():
        from bot_artifact import canonical_digest

        stable_publication = {
            "schema_version": 1,
            "published": True,
            "version": 143,
            "tag": "national-bot-v143",
            "tag_type": "tag",
            "tag_object": "1" * 40,
            "commit_oid": "2" * 40,
            "completion_tree_oid": "3" * 40,
            "tag_artifact_hash": "4" * 64,
        }
        parent_identity = {
            "version": 143,
            "bot": "national_v143",
            "role": "parent_source",
            "epoch": "national_tcp_policy_v1",
            "runtime_manifest_digest": "5" * 64,
            "epoch_receipt_digest": "6" * 64,
            "publication_identity_digest": canonical_digest(
                stable_publication
            ),
            "certificate_digest": "7" * 64,
            "completion_tag": "national-bot-v143",
            "completion_tag_object_oid": "1" * 40,
            "high_water_tag": "national-high-water-v143",
            "high_water_tag_object_oid": "8" * 40,
            "publication_commit_oid": "2" * 40,
            "completion_tree_oid": "3" * 40,
            "tag_artifact_hash": "4" * 64,
        }
        receipt_subject = {
            "schema_version": 1,
            "kind": "national-tcp-policy-singleton-bootstrap-v1",
            "mode": "singleton_strict_bootstrap",
            "epoch": "national_tcp_policy_v1",
            "source_v": 143,
            "next_v": 144,
            "source_artifact_inherited": True,
            "active_bots": ["national_v143"],
            "source_runtime_manifest_digest": "5" * 64,
            "source_epoch_receipt_digest": "6" * 64,
            "source_publication_identity": dict(stable_publication),
            "source_certificate_digest": "7" * 64,
        }
        receipt = {
            **receipt_subject,
            "receipt_digest": canonical_digest(receipt_subject),
        }
        binding_subject = {
            "schema_version": 2,
            "epoch": "national_tcp_policy_v1",
            "mode": "published_strict_parent",
            "next_v": 144,
            "source_v": 143,
            "parent2_v": None,
            "parent_versions": [143],
            "source_artifact_inherited": True,
            "parent_authority": "strict_published_parent_resolution",
            "published_parent_identities": [parent_identity],
            "protocol_bootstrap_receipt_digest": receipt["receipt_digest"],
            "policy_epoch_reset_receipt_digest": None,
            "published_high_water": 143,
            "abandoned_receipt_floor": 0,
            "abandoned_receipt_head_digest": None,
            "allocation_floor": 143,
        }
        return {
            "checkpoint_schema_version": 2,
            "evaluation_epoch": "national_tcp_policy_v1",
            "next_v": 144,
            "source_v": 143,
            "parent2_v": None,
            "stage": "critic_checked",
            "workflow_run_id": "generation:144:singleton-test",
            "checkpoint_revision": 9,
            "epoch_binding": {
                **binding_subject,
                "binding_digest": canonical_digest(binding_subject),
            },
            "audit_context": {
                "protocol_bootstrap": receipt,
                "selection": {
                    "strategy": "singleton_strict_bootstrap",
                    "parent_a": 143,
                    "parent_b": None,
                    "bootstrap_without_strength_evidence": True,
                    "protocol_bootstrap_receipt_digest": receipt[
                        "receipt_digest"
                    ],
                    "evaluation_evidence": {
                        "games": 0,
                        "cutoffs": {},
                        "readiness_reason": "singleton_strict_bootstrap",
                    },
                },
            },
        }

    def test_missing_frozen_snapshot_fails_closed(self, monkeypatch):
        import evidence_snapshot
        import tool_helpers

        monkeypatch.setattr(
            evidence_snapshot,
            "load_generation_evaluation_snapshot",
            lambda _version: {"available": False, "reason": "missing"},
        )
        monkeypatch.setattr(
            tool_helpers,
            "_bot_entry",
            lambda _name: (_ for _ in ()).throw(AssertionError("live bot lookup")),
        )

        assert tool_helpers._select_precommit_opponents(147, 143) == []

    def test_invalid_frozen_pool_fails_closed(self, monkeypatch):
        import evidence_snapshot
        import tool_helpers

        snapshot = self._snapshot()
        snapshot["selection"]["rows"] = snapshot["selection"]["rows"][:-1]
        monkeypatch.setattr(
            evidence_snapshot,
            "load_generation_evaluation_snapshot",
            lambda _version: snapshot,
        )

        assert tool_helpers._select_precommit_opponents(147, 143) == []

    def test_selection_comes_only_from_frozen_snapshot(self, tmp_path, monkeypatch):
        import evidence_snapshot
        import tool_helpers

        snapshot = self._snapshot()
        entries = {}
        for name in snapshot["selection"]["active_bots"]:
            entry = tmp_path / name / "national_bot.py"
            entry.parent.mkdir()
            entry.write_text("# immutable fixture\n", encoding="utf-8")
            entries[name] = entry
        monkeypatch.setattr(
            evidence_snapshot,
            "load_generation_evaluation_snapshot",
            lambda _version: snapshot,
        )
        monkeypatch.setattr(tool_helpers, "_bot_entry", lambda name: entries[name])
        monkeypatch.setattr(
            tool_helpers,
            "get_active_bots",
            lambda: (_ for _ in ()).throw(AssertionError("live active pool reopened")),
        )

        assert tool_helpers._select_precommit_opponents(147, 143) == [
            {"name": "national_v143", "reason": "parent"},
            {"name": "national_v144", "reason": "top_strength"},
            {"name": "national_v145", "reason": "top_strength"},
            {"name": "national_v146", "reason": "source_h2h_weakness"},
        ]

    def test_nonexecutable_frozen_parent_fails_closed(self, tmp_path, monkeypatch):
        import evidence_snapshot
        import tool_helpers

        snapshot = self._snapshot()
        monkeypatch.setattr(
            evidence_snapshot,
            "load_generation_evaluation_snapshot",
            lambda _version: snapshot,
        )
        monkeypatch.setattr(
            tool_helpers,
            "_bot_entry",
            lambda name: tmp_path / name / "national_bot.py",
        )

        assert tool_helpers._select_precommit_opponents(147, 143) == []

    def test_v144_singleton_freezes_exact_published_parent_without_snapshot(
        self,
        tmp_path,
        monkeypatch,
    ):
        import checkpoint_schema
        import evidence_snapshot
        import tool_helpers

        checkpoint = self._singleton_checkpoint()
        parent_entry = tmp_path / "national_v143" / "national_bot.py"
        parent_entry.parent.mkdir()
        parent_entry.write_text("# strict published parent\n", encoding="utf-8")
        monkeypatch.setattr(
            evidence_snapshot,
            "load_generation_evaluation_snapshot",
            lambda _version: {"available": False, "reason": "not_created"},
        )
        monkeypatch.setattr(
            checkpoint_schema,
            "live_checkpoint_parent_authority_errors",
            lambda *_args, **_kwargs: [],
        )
        monkeypatch.setattr(
            tool_helpers,
            "get_active_bots",
            lambda: ["national_v143"],
        )
        monkeypatch.setattr(tool_helpers, "_bot_entry", lambda _name: parent_entry)

        assert tool_helpers._select_precommit_opponents(
            144,
            143,
            checkpoint=checkpoint,
        ) == [{
            "name": "national_v143",
            "reason": "singleton_strict_bootstrap_parent",
        }]

    def test_v144_singleton_fails_closed_on_receipt_binding_drift(
        self,
        tmp_path,
        monkeypatch,
    ):
        from bot_artifact import canonical_digest
        import checkpoint_schema
        import evidence_snapshot
        import tool_helpers

        checkpoint = self._singleton_checkpoint()
        receipt = checkpoint["audit_context"]["protocol_bootstrap"]
        receipt["source_certificate_digest"] = "9" * 64
        receipt["receipt_digest"] = canonical_digest({
            key: value
            for key, value in receipt.items()
            if key != "receipt_digest"
        })
        checkpoint["audit_context"]["selection"][
            "protocol_bootstrap_receipt_digest"
        ] = receipt["receipt_digest"]
        binding = checkpoint["epoch_binding"]
        binding["protocol_bootstrap_receipt_digest"] = receipt[
            "receipt_digest"
        ]
        binding["binding_digest"] = canonical_digest({
            key: value
            for key, value in binding.items()
            if key != "binding_digest"
        })
        parent_entry = tmp_path / "national_v143" / "national_bot.py"
        parent_entry.parent.mkdir()
        parent_entry.write_text("# strict published parent\n", encoding="utf-8")
        monkeypatch.setattr(
            evidence_snapshot,
            "load_generation_evaluation_snapshot",
            lambda _version: {"available": False, "reason": "not_created"},
        )
        monkeypatch.setattr(
            checkpoint_schema,
            "live_checkpoint_parent_authority_errors",
            lambda *_args, **_kwargs: [],
        )
        monkeypatch.setattr(
            tool_helpers,
            "get_active_bots",
            lambda: ["national_v143"],
        )
        monkeypatch.setattr(tool_helpers, "_bot_entry", lambda _name: parent_entry)

        assert tool_helpers._select_precommit_opponents(
            144,
            143,
            checkpoint=checkpoint,
        ) == []

    def test_singleton_exception_never_applies_to_fresh_v143_or_two_bot_pool(
        self,
        tmp_path,
        monkeypatch,
    ):
        import checkpoint_schema
        import evidence_snapshot
        import tool_helpers

        checkpoint = self._singleton_checkpoint()
        parent_entry = tmp_path / "national_v143" / "national_bot.py"
        parent_entry.parent.mkdir()
        parent_entry.write_text("# strict published parent\n", encoding="utf-8")
        monkeypatch.setattr(
            evidence_snapshot,
            "load_generation_evaluation_snapshot",
            lambda _version: {"available": False, "reason": "not_created"},
        )
        monkeypatch.setattr(
            checkpoint_schema,
            "live_checkpoint_parent_authority_errors",
            lambda *_args, **_kwargs: [],
        )
        monkeypatch.setattr(tool_helpers, "_bot_entry", lambda _name: parent_entry)
        monkeypatch.setattr(
            tool_helpers,
            "get_active_bots",
            lambda: ["national_v143", "national_v145"],
        )

        assert tool_helpers._select_precommit_opponents(
            144,
            143,
            checkpoint=checkpoint,
        ) == []
        assert tool_helpers._select_precommit_opponents(
            143,
            142,
            checkpoint={
                **checkpoint,
                "next_v": 143,
                "source_v": 142,
            },
        ) == []


# ── evolution_infra.py: parse_json_output() ──

class TestParseJsonOutput:
    def test_markdown_json_block(self):
        from evolution_infra import parse_json_output
        output = '```json\n{"key": "value"}\n```'
        result = parse_json_output(output)
        assert result == {"key": "value"}

    def test_bare_json(self):
        from evolution_infra import parse_json_output
        output = '{"key": "value"}'
        result = parse_json_output(output)
        assert result == {"key": "value"}

    def test_invalid_returns_none(self):
        from evolution_infra import parse_json_output
        result = parse_json_output("not json at all")
        assert result is None

    def test_markdown_with_extra_text(self):
        from evolution_infra import parse_json_output
        output = 'Here is the result:\n```json\n{"a": 1}\n```\nDone.'
        result = parse_json_output(output)
        assert result == {"a": 1}


# ── MCP tools are Orchestrator-only, never an HTTP capability ──

def test_no_mcp_or_live_analysis_side_door_is_registered_over_http(client):
    forbidden = {
        "get_status",
        "get_bot_info",
        "get_match_history",
        "get_h2h",
        "get_bot_stats",
        "run_match_analysis",
        "run_performance_verification",
        "analyze_stagnation",
        "diagnose_environment",
        "prepare_next_gen",
        "execute_workers",
        "run_quality_gates",
        "run_precommit_eval",
        "commit_bot",
    }

    response = client.get("/api/control/tools")

    assert response.status_code == 200
    assert forbidden.isdisjoint(response.json()["tools"])


def test_former_read_only_mcp_http_calls_are_also_retired(client):
    for name in (
        "get_status", "get_bot_info", "get_match_history", "get_h2h",
        "get_bot_stats",
    ):
        response = client.post(f"/api/control/tool/{name}", json={"args": {}})
        assert response.status_code == 410
        assert response.json()["detail"]["code"] == "control_tool_executor_retired"
