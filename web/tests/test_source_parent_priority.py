"""Source-parent priority-eval persistent writer (master-stage H2H deep sampling).

While a generation grinds through the master phase no official writer feeds
``priority_eval.json``: prepare's eval_wait intent has already been consumed
and the post-publication archivist step only runs after commit. The H2H
pairings a Master plan needs to cite then starve below the statistical
evidence floor (``cited_sample_too_small``, matchups commonly < 15 games).

These tests pin the persistent source-parent writer's four arbitration rules
(a missing file / b candidate eval_wait / c already-effective source parent /
d stale foreign intent), its both-wire-sides games accounting, the atomic
write shape, its fail-soft posture, its system events, and the
``run_master_impl`` entry hook.
"""

import json
import sys
from pathlib import Path

WEB_CORE = Path(__file__).resolve().parents[1] / "core"
if str(WEB_CORE) not in sys.path:
    sys.path.insert(0, str(WEB_CORE))

from bot_namespace import bot_name  # noqa: E402
import source_parent_priority as spp  # noqa: E402

SOURCE_V = 41
NEXT_V = 42
FOREIGN_V = 7
OTHER_V = 9
SOURCE_BOT = bot_name(SOURCE_V)
NEXT_BOT = bot_name(NEXT_V)
FOREIGN_BOT = bot_name(FOREIGN_V)
OTHER_BOT = bot_name(OTHER_V)


def _write_stats(results_dir: Path, pairs: dict | None) -> None:
    if pairs is None:
        return
    (results_dir / "elo_daemon_stats.json").write_text(
        json.dumps({"pairs": pairs, "total_games": 0}), encoding="utf-8"
    )


def _write_priority(results_dir: Path, payload: dict) -> None:
    (results_dir / "priority_eval.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _read_priority(results_dir: Path) -> dict:
    return json.loads(
        (results_dir / "priority_eval.json").read_text(encoding="utf-8")
    )


def _sample_pairs() -> dict:
    """12 + 8 = 20 games involving SOURCE_BOT on both wire sides.

    The third pair does not involve the source parent and must not count.
    """
    return {
        f"{SOURCE_BOT} vs {FOREIGN_BOT}": 12,
        f"{FOREIGN_BOT} vs {SOURCE_BOT}": 8,
        f"{FOREIGN_BOT} vs {OTHER_BOT}": 99,
    }


class _EventSpy:
    def __init__(self, monkeypatch):
        self.calls = []
        monkeypatch.setattr(spp, "log_system_event", self._record)

    def _record(self, event_type, severity, message, data=None):
        self.calls.append({
            "event": event_type,
            "severity": severity,
            "message": message,
            "data": data or {},
        })

    @property
    def names(self):
        return [call["event"] for call in self.calls]


# ── rule (a): missing file → write source parent ────────────────────────────


def test_rule_a_missing_file_writes_source_parent(tmp_path):
    _write_stats(tmp_path, _sample_pairs())
    result = spp.assert_source_parent_priority(
        source_v=SOURCE_V, next_v=NEXT_V, results_dir=tmp_path
    )
    assert isinstance(result, dict)
    assert result["bot"] == SOURCE_BOT
    assert result["min_games"] == 320  # 20 current + 300 increment
    on_disk = _read_priority(tmp_path)
    assert on_disk["bot"] == SOURCE_BOT
    assert on_disk["min_games"] == 320


def test_rule_a_missing_stats_file_counts_zero_games(tmp_path):
    result = spp.assert_source_parent_priority(
        source_v=SOURCE_V, next_v=NEXT_V, results_dir=tmp_path
    )
    assert isinstance(result, dict)
    assert result["bot"] == SOURCE_BOT
    assert result["min_games"] == 300  # 0 + 300


def test_rule_a_uses_namespace_bot_name_helper(tmp_path):
    """Bot names must come from the canonical bot_name helper (no second
    hand-rolled prefix concatenation)."""
    result = spp.assert_source_parent_priority(
        source_v=SOURCE_V, next_v=NEXT_V, results_dir=tmp_path
    )
    assert result["bot"] == bot_name(SOURCE_V)
    assert result["bot"] != bot_name(NEXT_V)


# ── rule (b): incumbent bot is the current candidate → defer ────────────────


def test_rule_b_candidate_eval_wait_takes_precedence(tmp_path):
    _write_stats(tmp_path, _sample_pairs())
    original = {"bot": NEXT_BOT, "min_games": 12, "source": "prepare_eval_wait"}
    _write_priority(tmp_path, original)
    before = (tmp_path / "priority_eval.json").read_text(encoding="utf-8")

    result = spp.assert_source_parent_priority(
        source_v=SOURCE_V, next_v=NEXT_V, results_dir=tmp_path
    )
    assert result is None
    assert (tmp_path / "priority_eval.json").read_text(encoding="utf-8") == before


# ── rule (c): incumbent bot is the source parent → defer (expiry semantics) ─


def test_rule_c_source_parent_already_effective_not_rewritten(tmp_path):
    _write_stats(tmp_path, _sample_pairs())
    original = {"bot": SOURCE_BOT, "min_games": 320, "since": 111.0}
    _write_priority(tmp_path, original)
    before = (tmp_path / "priority_eval.json").read_text(encoding="utf-8")

    result = spp.assert_source_parent_priority(
        source_v=SOURCE_V, next_v=NEXT_V, results_dir=tmp_path
    )
    assert result is None
    # Not rewritten: a rewrite would reset min_games and disturb the daemon's
    # games-reached expiry semantics for an already-effective intent.
    assert (tmp_path / "priority_eval.json").read_text(encoding="utf-8") == before


# ── rule (d): stale foreign intent → overwrite with the source parent ───────


def test_rule_d_stale_foreign_intent_is_overwritten(tmp_path):
    _write_stats(tmp_path, _sample_pairs())
    _write_priority(tmp_path, {"bot": FOREIGN_BOT, "min_games": 500})

    result = spp.assert_source_parent_priority(
        source_v=SOURCE_V, next_v=NEXT_V, results_dir=tmp_path
    )
    assert isinstance(result, dict)
    assert result["bot"] == SOURCE_BOT
    on_disk = _read_priority(tmp_path)
    assert on_disk["bot"] == SOURCE_BOT
    assert on_disk["min_games"] == 320


def test_rule_d_unreadable_incumbent_is_replaced(tmp_path):
    """A corrupt/unreadable incumbent is an unusable intent, not an
    eval_wait/candidate or source-parent deferral — overwrite it."""
    _write_stats(tmp_path, _sample_pairs())
    (tmp_path / "priority_eval.json").write_text(
        "not json at all {{{", encoding="utf-8"
    )
    result = spp.assert_source_parent_priority(
        source_v=SOURCE_V, next_v=NEXT_V, results_dir=tmp_path
    )
    assert isinstance(result, dict)
    assert _read_priority(tmp_path)["bot"] == SOURCE_BOT


# ── games accounting: both wire sides, no substring hazards ──────────────────


def test_games_count_both_sides_without_substring_confusion(tmp_path):
    # bot_name(410) contains bot_name(41) as a strict substring; the writer
    # must split on " vs " like the daemon does, never substring-match.
    similar = bot_name(410)
    pairs = {
        f"{similar} vs {OTHER_BOT}": 500,
        f"{SOURCE_BOT} vs {FOREIGN_BOT}": 6,
    }
    _write_stats(tmp_path, pairs)
    result = spp.assert_source_parent_priority(
        source_v=SOURCE_V, next_v=NEXT_V, results_dir=tmp_path
    )
    assert result["min_games"] == 306  # only 6 real games + 300


# ── atomicity / on-disk shape ────────────────────────────────────────────────


def test_write_is_atomic_valid_json_no_temp_litter(tmp_path):
    for _ in range(3):
        spp.assert_source_parent_priority(
            source_v=SOURCE_V, next_v=NEXT_V, results_dir=tmp_path
        )
        # Repeated re-runs converge (rule c defers after the first write).
    on_disk_bytes = (tmp_path / "priority_eval.json").read_bytes()
    json.loads(on_disk_bytes.decode("utf-8"))  # must be valid JSON
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


# ── fail-soft ────────────────────────────────────────────────────────────────


def test_fail_soft_when_results_dir_is_not_a_directory(tmp_path):
    blocker = tmp_path / "occupied"
    blocker.write_text("regular file", encoding="utf-8")
    unusable_results_dir = blocker / "results"
    # Must not raise: parent path is a regular file so no state file can be
    # created under it.
    result = spp.assert_source_parent_priority(
        source_v=SOURCE_V, next_v=NEXT_V, results_dir=unusable_results_dir
    )
    assert result is None


def test_fail_soft_never_raises_on_garbage_versions(tmp_path):
    result = spp.assert_source_parent_priority(
        source_v="not-a-version", next_v=NEXT_V, results_dir=tmp_path
    )
    assert result is None


# ── system events ────────────────────────────────────────────────────────────


def test_event_asserted_emitted_on_write(tmp_path, monkeypatch):
    spy = _EventSpy(monkeypatch)
    spp.assert_source_parent_priority(
        source_v=SOURCE_V, next_v=NEXT_V, results_dir=tmp_path
    )
    assert "pipeline.source_parent_priority_asserted" in spy.names
    payload = spy.calls[0]["data"]
    assert payload["bot"] == SOURCE_BOT
    assert payload["decision"]


def test_event_deferred_emitted_on_candidate_deferral(tmp_path, monkeypatch):
    spy = _EventSpy(monkeypatch)
    _write_priority(tmp_path, {"bot": NEXT_BOT, "min_games": 12})
    spp.assert_source_parent_priority(
        source_v=SOURCE_V, next_v=NEXT_V, results_dir=tmp_path
    )
    assert "pipeline.source_parent_priority_deferred" in spy.names
    deferred = [c for c in spy.calls
                if c["event"] == "pipeline.source_parent_priority_deferred"][0]
    assert deferred["data"]["bot"] == SOURCE_BOT
    assert deferred["data"]["decision"]
    assert deferred["data"]["incumbent_bot"] == NEXT_BOT


def test_event_failed_emitted_on_unwritable_results_dir(tmp_path, monkeypatch):
    spy = _EventSpy(monkeypatch)
    blocker = tmp_path / "occupied"
    blocker.write_text("regular file", encoding="utf-8")
    spp.assert_source_parent_priority(
        source_v=SOURCE_V, next_v=NEXT_V, results_dir=blocker / "results"
    )
    assert "pipeline.source_parent_priority_failed" in spy.names


# ── run_master_impl hook existence ───────────────────────────────────────────


def test_run_master_impl_entry_hook_is_wired():
    dispatch_path = WEB_CORE / "tool_planning_master_dispatch.py"
    source = dispatch_path.read_text(encoding="utf-8")
    assert "source_parent_priority" in source
    impl_at = source.index("async def run_master_impl")
    call_at = source.rindex("assert_source_parent_priority(")
    assert call_at > impl_at, (
        "the source-parent priority assertion must be wired inside "
        "run_master_impl (master entry path)"
    )
    # The hook must be fail-soft: the call site sits inside a try block.
    try_at = source.rindex("try:", 0, call_at)
    except_at = source.index("except", call_at)
    assert try_at < call_at < except_at
