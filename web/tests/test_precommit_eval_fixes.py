"""Integration tests for P0, P1, and P3 fixes.

P0: Reap signal + priority eval written BEFORE archive in commit_bot (tool_commit.py)
P1: Time-based bot list refresh every 30s in daemon (elo_daemon.py)
P3: Stage-aware timeout skip for verified/critic_checked stages (orchestrator.py)
"""

import inspect
import json
import logging
from pathlib import Path

import pytest

from bot_namespace import bot_name, bot_tag, parse_bot_version
from conftest import STRICT_TARGET_V

# Branch-portable strict-bot fixtures.  These tests materialize a single (or
# paired) strict policy bot at the first strict version; hardcoding
# ``national_v143``/``national-bot-v143`` fails on the tencent-cloud-runtime
# branch where the active namespace is ``national_cloud_v*``.
BOT_V = STRICT_TARGET_V
BOT_NAME = bot_name(BOT_V)
BOT_TAG = bot_tag(BOT_V)
# A second distinct strict version for paired-bot (reap) tests.
BOT_V2 = STRICT_TARGET_V + 1
BOT_NAME_2 = bot_name(BOT_V2)
BOT_TAG_2 = bot_tag(BOT_V2)


def _write_native_bot_contract(bot_dir: Path) -> None:
    """Create a minimal strict national TCP policy artifact."""

    from bot_namespace import (
        FIRST_STRICT_POLICY_VERSION,
        NATIONAL_RUNTIME_MANIFEST,
        POLICY_EPOCH_RECEIPT,
        build_policy_epoch_receipt,
        build_runtime_manifest,
        parse_bot_version,
    )
    from national_native import ensure_native_entry

    version = parse_bot_version(bot_dir.name)
    assert version is not None and version >= FIRST_STRICT_POLICY_VERSION
    (bot_dir / "policy.py").write_text(
        "def get_baseline_decision(context):\n"
        "    return {'kind': 'pass'}\n\n"
        "def iter_decisions(context, baseline, deadline):\n"
        "    return ()\n",
        encoding="utf-8",
    )
    ensure_native_entry(bot_dir, overwrite=True)
    manifest = build_runtime_manifest(bot_dir)
    (bot_dir / NATIONAL_RUNTIME_MANIFEST).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    receipt = build_policy_epoch_receipt(
        bot_dir,
        version,
        parent_versions=() if version == FIRST_STRICT_POLICY_VERSION else (FIRST_STRICT_POLICY_VERSION,),
    )
    (bot_dir / POLICY_EPOCH_RECEIPT).write_text(
        json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
    )


def test_national_precommit_rejects_one_sample():
    import tool_eval

    blockers = tool_eval._national_sample_contract_blockers(
        {
            "hands_per_match": 70,
            "net_chips_samples": 1,
            "aggregate_ci_lower": None,
            "aggregate_ci_upper": None,
            "gate_degraded": True,
        },
        expected_samples=8,
    )

    assert {item["reason"] for item in blockers} == {"national_sample_shortfall"}


def test_national_precommit_does_not_make_secondary_chip_ci_a_hard_gate():
    import tool_eval

    blockers = tool_eval._national_sample_contract_blockers(
        {
            "hands_per_match": 70,
            "net_chips_samples": 8,
            "aggregate_ci_lower": None,
            "aggregate_ci_upper": None,
            "gate_degraded": True,
        },
        expected_samples=8,
    )

    assert blockers == []


# ── P0: Reap Signal Ordering ─────────────────────────────────────────

class TestP0ReapSignalOrder:
    """Publication and post-publication effects have one durable boundary."""

    def _get_commit_bot_source(self):
        """Return the source text of tool_commit.py."""
        p = Path(__file__).resolve().parent.parent / "core" / "tool_commit.py"
        return p.read_text()

    def _get_commit_bot_body(self):
        """Return only the commit_bot function body from source file."""
        source = self._get_commit_bot_source()
        # Find the async def commit_bot block — @tool wraps it, so inspect won't work.
        # Read the function body by finding the def and tracking indentation.
        start = source.find("async def commit_bot(")
        assert start >= 0, "async def commit_bot not found in tool_commit.py"
        # Extract from function start to the next top-level function/class def
        lines = source[start:].splitlines()
        body_lines = []
        for i, line in enumerate(lines):
            if i > 0 and line and not line[0].isspace() and line.strip():
                break
            body_lines.append(line)
        return "\n".join(body_lines)

    def _get_archivist_body(self):
        # _run_durable_post_publication_archivist was extracted into the
        # tool_commit_archivist_orchestrator companion (parent keeps a thin
        # async delegate). The signal-before-effects ordering invariant still
        # holds; this helper now reads the body from its real home.
        p = Path(__file__).resolve().parent.parent / "core" / "tool_commit_archivist_orchestrator.py"
        source = p.read_text()
        start = source.find("async def _run_durable_post_publication_archivist(")
        assert start >= 0, "async def _run_durable_post_publication_archivist not found in tool_commit_archivist_orchestrator.py"
        end = source.find("\nasync def ", start + 1)
        if end < 0:
            end = source.find("\ndef ", start + 1)
        if end < 0:
            end = len(source)  # last top-level def in the companion
        assert end > start
        return source[start:end]

    def _get_publication_resume_body(self):
        # _resume_publication_transaction lives in the tool_commit_publication
        # companion. inspect.getsource(tool_commit_publication._resume_publication_transaction)
        # intermittently fails under full-suite load (linecache pollution);
        # read the body directly, mirroring _get_archivist_body.
        p = Path(__file__).resolve().parent.parent / "core" / "tool_commit_publication.py"
        source = p.read_text()
        start = source.find("def _resume_publication_transaction(")
        assert start >= 0, "def _resume_publication_transaction not found in tool_commit_publication.py"
        end = source.find("\ndef ", start + 1)
        if end < 0:
            end = source.find("\nasync def ", start + 1)
        if end < 0:
            end = len(source)  # last top-level def in the companion
        assert end > start
        return source[start:end]

    def test_commit_bot_does_not_start_post_publication_effects(self):
        source = self._get_commit_bot_body()
        assert ".reap_signal" not in source
        assert "priority_eval.json" not in source
        assert "archive_rotate_files" not in source
        assert "_execute_strict_log_cleanup" not in source
        assert '"next_tool": "run_archivist"' in source

    def test_archivist_plans_signals_before_archive_effects(self):
        source = self._get_archivist_body()
        signal_pos = source.find('plan_handoff_step(\n                    v, source_v, claim_id, "reap_signal"')
        priority_pos = source.find('"priority_eval",\n                    {')
        rotation_effect_pos = source.find(
            'rotations = _tc.archive_rotate_files(v, row["plan"])'
        )
        cleanup_effect_pos = source.find(
            "log_archives = _tc._execute_strict_log_cleanup"
        )
        assert -1 not in {
            signal_pos, priority_pos, rotation_effect_pos, cleanup_effect_pos,
        }
        assert signal_pos < rotation_effect_pos
        assert priority_pos < rotation_effect_pos < cleanup_effect_pos

    def test_publication_completes_before_durable_handoff(self):
        import tool_commit
        import tool_commit_publication

        source = self._get_commit_bot_body()
        publication_pos = source.find("_resume_publication_transaction")
        handoff_pos = source.find('"next_tool": "run_archivist"')
        assert publication_pos >= 0, "publication transaction not found in commit_bot source"
        assert handoff_pos >= 0, "durable Archivist handoff not found"
        assert publication_pos < handoff_pos
        # _resume_publication_transaction was extracted into the
        # tool_commit_publication companion (parent keeps a thin delegate).
        # The invariant ("publication writes the durable sentinel") still
        # holds; inspect the real body in its new home. Read the body
        # directly from the companion .py instead of inspect.getsource --
        # the latter intermittently fails under full-suite load (linecache
        # pollution for the companion module).
        resume_source = self._get_publication_resume_body()
        assert "_write_completed_sentinel_durable" in resume_source


# ── P1: Time-Based Bot Refresh ───────────────────────────────────────

class TestP1TimeBasedRefresh:
    """P1: Daemon has time-based bot list refresh every 30s as a safety net."""

    def test_get_active_bots_finds_tagged_completed(self, tmp_path, monkeypatch):
        """get_active_bots returns directories with a tag-backed .completed sentinel."""
        from elo_daemon import get_active_bots
        import evolution_infra

        # Create a fake bot dir with .completed
        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        bot_dir = bots_dir / BOT_NAME
        bot_dir.mkdir()
        (bot_dir / ".completed").touch()
        _write_native_bot_contract(bot_dir)

        monkeypatch.setattr(evolution_infra, "BOTS_DIR", bots_dir)
        monkeypatch.setattr(evolution_infra, "_git", lambda *args, **kwargs: BOT_TAG + "\n")
        monkeypatch.setattr(evolution_infra, "_official_parent_eligible", lambda _bot_dir: True)
        monkeypatch.setattr(evolution_infra, "load_reaped_bot_versions", lambda: set())

        result = get_active_bots()
        assert BOT_NAME in result

    def test_get_active_bots_skips_legacy_newline_native_contract(self, tmp_path, monkeypatch):
        """A tagged newline/readline artifact is not active in the policy epoch."""
        from elo_daemon import get_active_bots
        import evolution_infra

        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        bot_dir = bots_dir / BOT_NAME
        bot_dir.mkdir()
        (bot_dir / ".completed").touch()
        (bot_dir / "national_bot.py").write_text(
            "sock.makefile('r')\nreader.readline()\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(evolution_infra, "BOTS_DIR", bots_dir)
        monkeypatch.setattr(evolution_infra, "_git", lambda *args, **kwargs: BOT_TAG + "\n")
        monkeypatch.setattr(evolution_infra, "_official_parent_eligible", lambda _bot_dir: True)
        monkeypatch.setattr(evolution_infra, "load_reaped_bot_versions", lambda: set())

        result = get_active_bots()
        assert BOT_NAME not in result

    def test_get_active_bots_skips_old_position_semantics(self, tmp_path, monkeypatch):
        """Tagged bots with Botzone-era dealer/SB math are not active parents."""
        from elo_daemon import get_active_bots
        import evolution_infra

        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        bot_dir = bots_dir / BOT_NAME
        bot_dir.mkdir()
        (bot_dir / ".completed").touch()
        _write_native_bot_contract(bot_dir)
        (bot_dir / "state.py").write_text(
            "def infer_remaining_hands_from_requests(requests):\n"
            "    return 70\n\n"
            "def reconstruct_state(req):\n"
            "    dealer_id = req['dealer_id']\n"
            "    sb = next_player(dealer_id, 1)\n"
            "    bb = next_player(dealer_id, 2)\n"
            "    return {'sb': sb, 'bb': bb}\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(evolution_infra, "BOTS_DIR", bots_dir)
        monkeypatch.setattr(evolution_infra, "_git", lambda *args, **kwargs: BOT_TAG + "\n")

        result = get_active_bots()
        assert BOT_NAME not in result

    def test_get_active_bots_skips_untagged_completed(self, tmp_path, monkeypatch):
        """get_active_bots does NOT trust .completed without a national-bot-vN tag."""
        from elo_daemon import get_active_bots
        import evolution_infra

        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        bot_dir = bots_dir / BOT_NAME
        bot_dir.mkdir()
        (bot_dir / ".completed").touch()

        monkeypatch.setattr(evolution_infra, "BOTS_DIR", bots_dir)
        monkeypatch.setattr(evolution_infra, "_git", lambda *args, **kwargs: "")

        result = get_active_bots()
        assert BOT_NAME not in result

    def test_get_active_bots_restores_missing_completed_for_tagged_bot(self, tmp_path, monkeypatch):
        """A tagged bot dir missing gitignored .completed is restored and treated active."""
        from elo_daemon import get_active_bots
        import evolution_infra

        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        bot_dir = bots_dir / BOT_NAME
        bot_dir.mkdir()
        _write_native_bot_contract(bot_dir)

        monkeypatch.setattr(evolution_infra, "BOTS_DIR", bots_dir)
        monkeypatch.setattr(evolution_infra, "_git", lambda *args, **kwargs: BOT_TAG + "\n")
        monkeypatch.setattr(evolution_infra, "_official_parent_eligible", lambda _bot_dir: True)
        monkeypatch.setattr(evolution_infra, "load_reaped_bot_versions", lambda: set())

        result = get_active_bots()
        assert result == [BOT_NAME]
        assert (bot_dir / ".completed").exists()

    def test_read_only_active_bot_catalog_never_repairs_missing_sentinel(
        self, tmp_path, monkeypatch
    ):
        import evolution_infra

        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        bot_dir = bots_dir / BOT_NAME
        bot_dir.mkdir()
        _write_native_bot_contract(bot_dir)

        monkeypatch.setattr(evolution_infra, "BOTS_DIR", bots_dir)
        monkeypatch.setattr(
            evolution_infra,
            "_git",
            lambda *args, **kwargs: BOT_TAG + "\n",
        )
        monkeypatch.setattr(
            evolution_infra,
            "_official_parent_eligible",
            lambda _bot_dir: True,
        )
        monkeypatch.setattr(evolution_infra, "load_reaped_bot_versions", lambda: set())

        assert evolution_infra.get_active_bots_read_only() == []
        assert not (bot_dir / ".completed").exists()

    def test_published_read_only_catalog_works_without_local_sentinel(
        self, tmp_path, monkeypatch
    ):
        import evolution_infra

        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        bot_dir = bots_dir / BOT_NAME
        bot_dir.mkdir()
        _write_native_bot_contract(bot_dir)

        monkeypatch.setattr(evolution_infra, "BOTS_DIR", bots_dir)
        monkeypatch.setattr(
            evolution_infra,
            "_git",
            lambda *args, **kwargs: BOT_TAG + "\n",
        )
        monkeypatch.setattr(
            evolution_infra,
            "_official_parent_eligible",
            lambda _bot_dir: True,
        )
        monkeypatch.setattr(evolution_infra, "load_reaped_bot_versions", lambda: set())

        assert evolution_infra.get_published_active_bots_read_only() == [BOT_NAME]
        assert not (bot_dir / ".completed").exists()

    def test_get_active_bots_does_not_rescan_existing_completed_in_restore_pass(self, tmp_path, monkeypatch):
        """Existing sentinels should not be protocol-scanned during restore preflight."""
        from elo_daemon import get_active_bots
        import evolution_infra

        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        bot_dir = bots_dir / BOT_NAME
        bot_dir.mkdir()
        (bot_dir / ".completed").touch()

        calls = []

        def _eligible(version):
            calls.append(version)
            return True

        monkeypatch.setattr(evolution_infra, "BOTS_DIR", bots_dir)
        monkeypatch.setattr(evolution_infra, "_git", lambda *args, **kwargs: BOT_TAG + "\n")
        monkeypatch.setattr(evolution_infra, "is_active_bot_protocol_eligible", _eligible)
        monkeypatch.setattr(evolution_infra, "_official_parent_eligible", lambda _bot_dir: True)
        monkeypatch.setattr(evolution_infra, "load_reaped_bot_versions", lambda: set())

        result = get_active_bots()

        assert result == [BOT_NAME]
        assert calls == [BOT_V]

    def test_get_active_bots_does_not_restore_reaped_tagged_bot(self, tmp_path, monkeypatch):
        """A deliberately reaped tagged bot remains inactive across discovery calls."""
        from elo_daemon import get_active_bots
        import evolution_infra

        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        bot_dir = bots_dir / BOT_NAME
        bot_dir.mkdir()
        results_dir = tmp_path / "results"
        results_dir.mkdir()
        (results_dir / "reaped_bots.jsonl").write_text(
            f'{{"bot":"{BOT_NAME}","version":{BOT_V},"reason":"test"}}\n',
            encoding="utf-8",
        )

        monkeypatch.setattr(evolution_infra, "BOTS_DIR", bots_dir)
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
        monkeypatch.setattr(evolution_infra, "REAPED_BOTS_FILE", results_dir / "reaped_bots.jsonl")
        monkeypatch.setattr(evolution_infra, "_git", lambda *args, **kwargs: BOT_TAG + "\n")

        result = get_active_bots()
        assert result == []
        assert not (bot_dir / ".completed").exists()

    @pytest.mark.asyncio
    async def test_reap_weakest_deactivates_without_moving_source(self, tmp_path, monkeypatch):
        """Reaping must not move tracked bot dirs and dirty the evolution worktree."""
        import evolution_infra
        import tool_bot_management as tbm

        bots_dir = tmp_path / "bots"
        results_dir = tmp_path / "web" / "core" / "results"
        replay_dir = results_dir / "match_replay"
        bots_dir.mkdir()
        results_dir.mkdir(parents=True)
        replay_dir.mkdir()
        retained_replay = replay_dir / f"match_{BOT_NAME}_{BOT_NAME_2}.json"
        retained_replay.write_text('{"immutable":"evidence"}\n', encoding="utf-8")
        for version in (BOT_V, BOT_V2):
            bot_dir = bots_dir / bot_name(version)
            bot_dir.mkdir()
            _write_native_bot_contract(bot_dir)
            (bot_dir / ".completed").touch()
        (results_dir / "bot_stats.json").write_text(
            json.dumps({BOT_NAME: {"games": 1000}, BOT_NAME_2: {"games": 1000}}) + "\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(evolution_infra, "BOTS_DIR", bots_dir)
        monkeypatch.setattr(evolution_infra, "RESULTS_DIR", results_dir)
        monkeypatch.setattr(evolution_infra, "REAPED_BOTS_FILE", results_dir / "reaped_bots.jsonl")
        monkeypatch.setattr(evolution_infra, "_git", lambda *args, **kwargs: BOT_TAG + "\n" + BOT_TAG_2 + "\n")
        monkeypatch.setattr(evolution_infra, "_official_parent_eligible", lambda _bot_dir: True)
        fsynced_directories = []
        monkeypatch.setattr(
            evolution_infra,
            "_fsync_directory",
            lambda path: fsynced_directories.append(Path(path)),
        )
        reaped_versions = set()
        monkeypatch.setattr(evolution_infra, "load_reaped_bot_versions", lambda: set(reaped_versions))
        monkeypatch.setattr(
            tbm,
            "record_reaped_bot",
            lambda name, **_kwargs: reaped_versions.add(parse_bot_version(name)) or {"bot": name},
        )
        monkeypatch.setattr(tbm, "PROJECT_ROOT", tmp_path)
        monkeypatch.setattr(tbm, "RESULTS_DIR", results_dir)
        monkeypatch.setattr(tbm, "MAX_ACTIVE_BOTS", 1)
        monkeypatch.setattr(
            tbm,
            "load_ratings",
            lambda: {
                BOT_NAME: tbm.Glicko2Player(r=1200, rd=50),
                BOT_NAME_2: tbm.Glicko2Player(r=1600, rd=50),
            },
        )
        monkeypatch.setattr(tbm, "load_h2h_avg_winrates", lambda: {BOT_NAME: 0.4})
        monkeypatch.setattr(tbm, "load_strength_scores", lambda: {BOT_NAME: 0.4})

        result = await tbm._do_reap_weakest(quiet=True)

        assert result["reaped"] is True
        assert result["culled"] == BOT_NAME
        assert result["reap_mode"] == "deactivate_completed_sentinel"
        assert (bots_dir / BOT_NAME / "national_bot.py").exists()
        assert not (bots_dir / BOT_NAME / ".completed").exists()
        assert bots_dir / BOT_NAME in fsynced_directories
        assert retained_replay.read_text(encoding="utf-8") == '{"immutable":"evidence"}\n'
        assert {path.name for path in bots_dir.iterdir()} == {BOT_NAME, BOT_NAME_2}
        assert evolution_infra.get_active_bots() == [BOT_NAME_2]

    def test_refresh_timer_variable_exists(self):
        """Daemon source contains the last_bot_refresh_time variable."""
        source = Path(__file__).resolve().parent.parent / "core" / "elo_daemon.py"
        text = source.read_text()
        assert "last_bot_refresh_time" in text

    def test_refresh_interval_is_30(self):
        """Time-based refresh check uses 30-second interval."""
        source = Path(__file__).resolve().parent.parent / "core" / "elo_daemon.py"
        text = source.read_text()
        # Find the time-based refresh block and verify it uses 30
        assert "last_bot_refresh_time >= 30" in text


class TestDaemonSignalNoise:
    """Daemon status logs should be usable during long-running evolution."""

    def test_pick_match_info_log_is_throttled(self, monkeypatch, caplog):
        import elo_daemon

        clock = [1000.0]
        monkeypatch.setattr(elo_daemon.time, "time", lambda: clock[0])
        monkeypatch.setattr(elo_daemon, "PICK_MATCH_LOG_INTERVAL_SEC", 30.0)
        elo_daemon._pick_match_log_state.clear()

        caplog.set_level(logging.INFO, logger="pok.daemon")
        elo_daemon._log_pick_matches(1, 1, None, 2)
        clock[0] += 1.0
        elo_daemon._log_pick_matches(1, 1, None, 2)
        clock[0] += 31.0
        elo_daemon._log_pick_matches(1, 1, None, 2)

        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "pok.daemon" and "pick_matches:" in record.getMessage()
        ]
        assert len(messages) == 2


# ── P3: Stage-Aware Timeout ──────────────────────────────────────────

class TestP3StageAwareTimeout:
    """P3: Timeout handler skips kill when pipeline is at verified/critic_checked stage."""

    def _read_orchestrator_source(self):
        return Path(__file__).resolve().parent.parent / "core" / "orchestrator.py"

    def test_timeout_skip_stages(self):
        """Timeout handler checks for verified and critic_checked stages."""
        source = self._read_orchestrator_source().read_text()
        # Find the stage-aware timeout block
        assert '"verified"' in source, 'Stage "verified" not found in timeout handler'
        assert '"critic_checked"' in source, 'Stage "critic_checked" not found in timeout handler'
        # Verify they appear in the same conditional block as the timeout handling
        timeout_block_start = source.find("asyncio.TimeoutError")
        assert timeout_block_start >= 0
        verified_in_timeout = source.find('"verified"', timeout_block_start)
        critic_in_timeout = source.find('"critic_checked"', timeout_block_start)
        assert verified_in_timeout > timeout_block_start, (
            '"verified" must appear after the TimeoutError handler'
        )
        assert critic_in_timeout > timeout_block_start, (
            '"critic_checked" must appear after the TimeoutError handler'
        )

    def test_watchdog_still_active(self):
        """P3 does not disable the watchdog — WATCHDOG_TIMEOUT still referenced."""
        source = self._read_orchestrator_source().read_text()
        assert "WATCHDOG_TIMEOUT" in source, "WATCHDOG_TIMEOUT reference removed — watchdog disabled"
        assert "_watchdog_coroutine" in source, "_watchdog_coroutine function removed"

    def test_no_blanket_extension(self):
        """CYCLE_TIMEOUT does not add a blanket +300 or +360 extension."""
        source = self._read_orchestrator_source().read_text()
        # CYCLE_TIMEOUT should be a fixed value, not CYCLE_TIMEOUT + 300 or similar
        assert "CYCLE_TIMEOUT + 300" not in source, (
            "Blanket +300 extension found — P3 should use stage-aware skip, not blanket extension"
        )
        assert "CYCLE_TIMEOUT + 360" not in source, (
            "Blanket +360 extension found — P3 should use stage-aware skip, not blanket extension"
        )
        # Verify CYCLE_TIMEOUT is a simple constant assignment (not dynamically inflated)
        for line in source.splitlines():
            stripped = line.strip()
            if stripped.startswith("CYCLE_TIMEOUT ="):
                # Should be a plain integer, not a sum/expression
                assert "+ 3" not in stripped and "+ 3" not in stripped, (
                    f"CYCLE_TIMEOUT should not be inflated: {stripped}"
                )
                break
