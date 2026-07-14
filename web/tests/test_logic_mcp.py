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
