"""Saturator bot-set selection: published-tag filtering + focus pool bias.

The saturator must analyze only PUBLISHED bots (a version with an annotated
completion tag) — an in-flight draft's candidate dir has no tag and must not
be served as a reference bot. The FOCUS bot rotates by session id over a
biased pool (newest 4 published bots + the v1 bootstrap), so findings land on
the versions planning actually consumes via focus_v/opponent_v matching.
"""

import sys
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parents[1]
CORE_DIR = WEB_DIR / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

import llm_saturator  # noqa: E402


def test_published_bot_dirs_filters_by_tag_and_sorts_desc(monkeypatch, tmp_path):
    bots_dir = tmp_path / "bots"
    for v in (173, 29, 105):
        d = bots_dir / f"national_cloud_v{v}"
        d.mkdir(parents=True)
        (d / "policy.py").write_text("# policy", encoding="utf-8")
    # v174 is on disk but has NO completion tag (in-flight) -> excluded.
    draft = bots_dir / "national_cloud_v174"
    draft.mkdir(parents=True)
    (draft / "policy.py").write_text("# draft", encoding="utf-8")
    # v83 has a tag but no policy.py -> excluded.
    (bots_dir / "national_cloud_v83").mkdir(parents=True)

    monkeypatch.setattr(
        llm_saturator, "_published_versions", lambda: {173, 29, 105, 83}
    )
    import evolution_infra

    monkeypatch.setattr(evolution_infra, "BOTS_DIR", str(bots_dir))

    dirs = llm_saturator._published_bot_dirs()
    assert [d.name for d in dirs] == [
        "national_cloud_v173",
        "national_cloud_v105",
        "national_cloud_v29",
    ]


def test_saturator_bots_rotates_focus_within_biased_pool(monkeypatch):
    dirs = [Path(f"/bots/national_cloud_v{v}") for v in (173, 105, 88, 83, 79, 29, 27)]
    monkeypatch.setattr(llm_saturator, "_published_bot_dirs", lambda: dirs)

    # Focus pool = newest 4 only (no v1 published here).
    s0 = llm_saturator._saturator_bots(0)
    assert s0[0].name == "national_cloud_v173"  # focus = newest
    assert len(s0) == 2
    assert len({d.name for d in s0}) == 2  # no duplicates

    s1 = llm_saturator._saturator_bots(1)
    assert s1[0].name == "national_cloud_v105"  # focus rotates within pool
    assert s1[0].name not in [d.name for d in s1[1:]]

    # Session 4 wraps to the pool head again — old bots (v79/v29/v27) are
    # never the focus when a v1 bootstrap is absent.
    s4 = llm_saturator._saturator_bots(4)
    assert s4[0].name == "national_cloud_v173"


def test_saturator_bots_includes_v1_bootstrap_in_focus_pool(monkeypatch):
    # Newest-first pool WITH the v1 bootstrap at the tail: the pool becomes
    # newest 4 + v1, so some sessions deep-dive the long-standing rank-1
    # selection parent (planning's most common source_v).
    dirs = [Path(f"/bots/national_cloud_v{v}") for v in (173, 105, 88, 83, 79, 27, 1)]
    monkeypatch.setattr(llm_saturator, "_published_bot_dirs", lambda: dirs)

    pool = dirs[:4] + [dirs[-1]]
    for i in range(len(pool)):
        s = llm_saturator._saturator_bots(i)
        assert s[0] == pool[i % len(pool)]
    # v1 session still pairs it with the newest other bot as opponent.
    s_v1 = llm_saturator._saturator_bots(4)
    assert s_v1[0].name == "national_cloud_v1"
    assert s_v1[1].name == "national_cloud_v173"


def test_saturator_bots_empty_pool(monkeypatch):
    monkeypatch.setattr(llm_saturator, "_published_bot_dirs", lambda: [])
    assert llm_saturator._saturator_bots(7) == []


def test_saturator_job_rotation_splits_work():
    names = [llm_saturator.saturator_job_for(i)["name"] for i in range(6)]
    assert names == [
        "matchup_packet",
        "line_audit",
        "function_trace",
        "matchup_packet",
        "line_audit",
        "function_trace",
    ]
    assert llm_saturator.saturator_job_for(0)["bot_limit"] == 2
    assert llm_saturator.saturator_job_for(1)["bot_limit"] == 1
    for i in range(3):
        prompt = str(llm_saturator.saturator_job_for(i)["prompt"])
        assert "HARD STOP" in prompt
        assert "18 Read" in prompt


def test_pick_preemptable_many_batches_youngest():
    tasks = {"old": 100.0, "mid": 500.0, "young": 900.0}
    assert llm_saturator._pick_preemptable_many(tasks, 5.0, 3) == []
    assert llm_saturator._pick_preemptable_many(tasks, 45.0, 2) == ["young", "mid"]
    assert llm_saturator._pick_preemptable(tasks, 45.0) == "young"


def test_saturator_may_launch_respects_ram_and_soft_cap(monkeypatch):
    import llm_concurrency as lc

    monkeypatch.setattr(llm_saturator, "_mem_available_mb", lambda: 2048)
    monkeypatch.setattr(llm_saturator, "_saturator_provider_paused", lambda: False)
    monkeypatch.setattr(lc, "_pipeline_pending", 0)
    monkeypatch.setattr(lc, "_pipeline_first_pending_ts", None)
    monkeypatch.setattr(lc, "_SHARED_LLM_SEMAPHORE", None)
    monkeypatch.setattr(lc, "_GLOBAL_LLM_SEMAPHORE", None)

    monkeypatch.setattr(llm_saturator, "_claude_child_count", lambda: 4)
    ok, reason = llm_saturator.saturator_may_launch(in_flight=0, soft_cap=4)
    assert ok is False
    assert reason == "claude_children"

    monkeypatch.setattr(llm_saturator, "_claude_child_count", lambda: 1)
    ok, reason = llm_saturator.saturator_may_launch(in_flight=4, soft_cap=4)
    assert ok is False
    assert reason == "soft_cap"

    ok, reason = llm_saturator.saturator_may_launch(in_flight=1, soft_cap=4)
    assert ok is True
    assert reason == "ok"

    monkeypatch.setattr(llm_saturator, "_mem_available_mb", lambda: 64)
    monkeypatch.setattr(llm_saturator, "_min_free_mb", lambda: 512)
    ok, reason = llm_saturator.saturator_may_launch(in_flight=1, soft_cap=4)
    assert ok is False
    assert reason == "low_memory"


def test_usage_tokens_tolerates_dict_and_object():
    assert llm_saturator._usage_tokens(
        {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 1000,
            "cache_creation_input_tokens": 10,
        }
    ) == 1160
    assert llm_saturator._usage_tokens(None) == 0
    assert llm_saturator._usage_tokens("garbage") == 0
