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
