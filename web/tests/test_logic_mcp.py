"""Logic-level tests for MCP tools — verifies data transformations and invariants."""

import json
from pathlib import Path

import pytest


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
    @pytest.mark.requires_active_bot
    def test_returns_dict_for_known_bots(self):
        from tool_helpers import load_h2h_avg_winrates
        result = load_h2h_avg_winrates()
        assert isinstance(result, dict)
        # Should have entries for active bots
        assert len(result) > 0

    def test_values_in_valid_range(self):
        from tool_helpers import load_h2h_avg_winrates
        result = load_h2h_avg_winrates()
        for name, wr in result.items():
            assert 0.0 <= wr <= 1.0, f"{name} has invalid winrate: {wr}"


# ── tool_helpers.py: _select_precommit_opponents() ──

@pytest.mark.requires_active_bot
class TestSelectPrecommitOpponents:
    def test_returns_list_of_dicts(self, active_bot_version):
        from tool_helpers import _select_precommit_opponents
        result = _select_precommit_opponents(active_bot_version + 1, active_bot_version)
        assert isinstance(result, list)
        for opp in result:
            assert "name" in opp
            assert "reason" in opp

    def test_no_duplicates(self, active_bot_version):
        from tool_helpers import _select_precommit_opponents
        result = _select_precommit_opponents(active_bot_version + 1, active_bot_version)
        names = [o["name"] for o in result]
        assert len(names) == len(set(names))

    def test_parent_included(self, active_bot_version):
        from tool_helpers import _select_precommit_opponents
        result = _select_precommit_opponents(active_bot_version + 1, active_bot_version)
        names = [o["name"] for o in result]
        assert f"claude_v{active_bot_version}" in names


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


# ── MCP tool: get_status via API ──

class TestMCPGetStatusLogic:
    def test_has_required_fields(self, client):
        resp = client.post("/api/control/tool/get_status", json={"args": {}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert "current_v" in result
        assert "next_v" in result
        assert "active_bots_count" in result

    def test_current_v_positive(self, client):
        resp = client.post("/api/control/tool/get_status", json={"args": {}})
        result = json.loads(resp.json()["result"])
        assert result["current_v"] > 0

    def test_active_bots_count_non_negative(self, client):
        resp = client.post("/api/control/tool/get_status", json={"args": {}})
        result = json.loads(resp.json()["result"])
        assert result["active_bots_count"] >= 0


# ── MCP tool: get_bot_info via API ──

@pytest.mark.requires_active_bot
class TestMCPGetBotInfoLogic:
    def test_existing_bot_has_files(self, client, active_bot_version):
        resp = client.post("/api/control/tool/get_bot_info",
                           json={"args": {"version": active_bot_version}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        assert result.get("exists") is not False
        assert "files" in result
        assert len(result["files"]) > 0

    def test_version_matches_request(self, client, active_bot_version):
        resp = client.post("/api/control/tool/get_bot_info",
                           json={"args": {"version": active_bot_version}})
        result = json.loads(resp.json()["result"])
        assert result.get("version") == active_bot_version


# ── MCP tool: get_match_history via API ──

@pytest.mark.requires_active_bot
class TestMCPGetMatchHistoryLogic:
    def test_respects_n_limit(self, client, active_bot_version):
        resp = client.post("/api/control/tool/get_match_history",
                           json={"args": {"version": active_bot_version, "n": 2}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        if "matches" in result:
            assert len(result["matches"]) <= 2

    def test_matches_are_dicts(self, client, active_bot_version):
        resp = client.post("/api/control/tool/get_match_history",
                           json={"args": {"version": active_bot_version, "n": 3}})
        result = json.loads(resp.json()["result"])
        if "matches" in result:
            for m in result["matches"]:
                assert isinstance(m, dict)


# ── MCP tool: get_h2h via API ──

class TestMCPGetH2HLogic:
    @pytest.mark.requires_active_bot
    def test_opponents_have_win_rate(self, client, active_bot_version):
        resp = client.post("/api/control/tool/get_h2h",
                           json={"args": {"bot_name": f"claude_v{active_bot_version}"}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        if "opponents" in result and result["opponents"]:
            # opponents is a dict: {opp_name: {wins, losses, games, win_rate, tag}}
            for opp_name, opp_data in result["opponents"].items():
                assert "win_rate" in opp_data
                assert "tag" in opp_data
                assert opp_data["tag"] in ("STRENGTH", "WEAKNESS", "neutral")


# ── MCP tool: get_bot_stats via API ──

class TestMCPGetBotStatsLogic:
    @pytest.mark.requires_active_bot
    def test_has_games_and_win_rate(self, client, active_bot_version):
        resp = client.post("/api/control/tool/get_bot_stats",
                           json={"args": {"bot_name": f"claude_v{active_bot_version}"}})
        assert resp.status_code == 200
        result = json.loads(resp.json()["result"])
        if "error" not in result:
            assert "games" in result or "win_rate" in result
