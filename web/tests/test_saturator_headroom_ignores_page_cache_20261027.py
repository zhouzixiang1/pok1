"""Saturator cgroup headroom must not charge reclaimable page cache (2026-10-27).

``_cgroup_memory_headroom_mb`` used ``memory.high - memory.current``. In a
read-heavy run ``memory.current`` is dominated by page cache, which the kernel
evicts before it throttles any allocation, so the gate closed admissions while
the cgroup had plenty of truly-used memory. Live measurement on the evolution
service cgroup (2700 MiB high)::

    anon 622 MB   file 866 MB (inactive_file 836 MB)   slab_unreclaimable 3.6 MB
    memory.current 1512 MB  ->  old headroom 1188 MB
                                new headroom 2700-625 = ~2074 MB

The journal's refusal reasons for the 14 h before the fix were dominated by
these cgroup gates (``low_memory_cgroup_stream_rss`` 227,
``low_memory_cgroup_soft`` 180, ``low_memory_cgroup`` 62) against 242
``soft_cap``, and the observed in-flight packet count flattened at ~5 of 16
configured permits.

The gate now reads ``memory.stat`` and charges only ``anon`` +
``slab_unreclaimable``, falling back to ``memory.current`` when the stat file
is unreadable so a non-cgroup environment keeps the historical behavior.
"""

from __future__ import annotations

import pytest

import llm_concurrency
import llm_saturator
from llm_saturator import saturator_may_launch


MIB = 1024 * 1024


@pytest.fixture(autouse=True)
def _isolate_gates(monkeypatch):
    monkeypatch.setattr(llm_saturator, "_saturator_provider_paused", lambda: False)
    monkeypatch.setattr(llm_saturator, "_mem_available_mb", lambda: 2048)
    monkeypatch.setattr(llm_saturator, "_min_free_mb", lambda: 512)
    monkeypatch.setattr(llm_saturator, "_claude_child_count", lambda: 0)
    monkeypatch.setattr(llm_concurrency, "llm_semaphore_has_capacity", lambda n=1: True)
    monkeypatch.setattr(llm_concurrency, "get_capacity", lambda: 16)
    monkeypatch.delenv("POK_LLM_AIMD_CGROUP_HEADROOM_MB", raising=False)
    monkeypatch.delenv("POK_LLM_SATURATOR_MEMORY_SOFT_FLOOR_MB", raising=False)
    monkeypatch.delenv("POK_LLM_SATURATOR_LOW_MEMORY_INFLIGHT", raising=False)
    monkeypatch.delenv("POK_LLM_SATURATOR_STREAM_RSS_MB", raising=False)
    monkeypatch.delenv("POK_LLM_SATURATOR_BAND_OVERSHOOT_MB", raising=False)


def _cgroup(tmp_path, *, high_mib, current_mib, anon_mib=None,
            file_mib=0, slab_unreclaimable_mib=None, with_stat=True):
    root = tmp_path / "cgroup"
    svc = root / "system.slice" / "pok-evolution.service"
    svc.mkdir(parents=True)
    (svc / "memory.high").write_text(str(high_mib * MIB) + "\n")
    (svc / "memory.current").write_text(str(current_mib * MIB) + "\n")
    if with_stat:
        lines = []
        if anon_mib is not None:
            lines.append(f"anon {anon_mib * MIB}")
        lines.append(f"file {file_mib * MIB}")
        if slab_unreclaimable_mib is not None:
            lines.append(f"slab_unreclaimable {slab_unreclaimable_mib * MIB}")
        (svc / "memory.stat").write_text("\n".join(lines) + "\n")
    cg = tmp_path / "self_cgroup"
    cg.write_text("0::/system.slice/pok-evolution.service\n")
    return {"cgroup_file": str(cg), "cgroup_root": str(root)}


def test_headroom_charges_anon_not_page_cache(tmp_path):
    # The live 2026-10-27 shape: 1512 MiB current of which 866 MiB is cache.
    kw = _cgroup(
        tmp_path,
        high_mib=2700,
        current_mib=1512,
        anon_mib=622,
        file_mib=886,
        slab_unreclaimable_mib=4,
    )
    headroom = llm_saturator._cgroup_memory_headroom_mb(**kw)
    assert headroom == 2700 - (622 + 4)
    # The historical current-based number would have been 1188.
    assert headroom > 2700 - 1512


def test_headroom_survives_a_page_cache_heavy_run(tmp_path):
    # The failure mode that flattened admission to ~1 packet: 1.5 GiB of
    # reclaimable inactive_file against 850 MiB anon.
    kw = _cgroup(
        tmp_path,
        high_mib=2700,
        current_mib=2478,
        anon_mib=856,
        file_mib=1577,
        slab_unreclaimable_mib=4,
    )
    assert llm_saturator._cgroup_memory_headroom_mb(**kw) == 2700 - 860
    # Old accounting: 222 MB -> below the 300 MB hard floor -> refuse.
    assert 2700 - 2478 < 300


def test_headroom_falls_back_to_current_without_memory_stat(tmp_path):
    kw = _cgroup(tmp_path, high_mib=2700, current_mib=1271, with_stat=False)
    assert llm_saturator._cgroup_memory_headroom_mb(**kw) == 2700 - 1271


def test_headroom_falls_back_when_memory_stat_lacks_anon(tmp_path):
    kw = _cgroup(tmp_path, high_mib=2700, current_mib=1271, anon_mib=None, file_mib=800)
    assert llm_saturator._cgroup_memory_headroom_mb(**kw) == 2700 - 1271


def test_unreclaimable_reader_reports_none_on_unparsable_stat(tmp_path):
    root = tmp_path / "cgroup"
    svc = root / "svc"
    svc.mkdir(parents=True)
    (svc / "memory.stat").write_text("anon not-a-number\n")
    assert llm_saturator._cgroup_unreclaimable_bytes(svc) is None


def test_admission_admits_more_packets_when_headroom_is_cache_free(tmp_path, monkeypatch):
    """The gate that refused at 1 in-flight now admits up to the permit cap."""
    kw = _cgroup(
        tmp_path,
        high_mib=2700,
        current_mib=2478,
        anon_mib=856,
        file_mib=1577,
        slab_unreclaimable_mib=4,
    )
    headroom = llm_saturator._cgroup_memory_headroom_mb(**kw)
    monkeypatch.setattr(
        llm_saturator, "_cgroup_memory_headroom_mb", lambda **_kw: headroom
    )
    monkeypatch.setattr(llm_saturator, "_saturator_stream_rss_mb", lambda: 130)
    # 1840 MB of anon headroom, 130 MB per stream, 300 MB floor -> 11 packets.
    for in_flight in range(11):
        ok, reason = saturator_may_launch(in_flight=in_flight, soft_cap=16)
        assert (ok, reason) == (True, "ok"), in_flight


def test_admission_still_refuses_on_real_anon_pressure(tmp_path, monkeypatch):
    """The safety property is unchanged: real anon usage still refuses."""
    kw = _cgroup(tmp_path, high_mib=2700, current_mib=2600, anon_mib=2600, file_mib=0)
    headroom = llm_saturator._cgroup_memory_headroom_mb(**kw)
    assert headroom == 100
    monkeypatch.setattr(
        llm_saturator, "_cgroup_memory_headroom_mb", lambda **_kw: headroom
    )
    ok, reason = saturator_may_launch(in_flight=0, soft_cap=16)
    assert ok is False
    assert reason == "low_memory_cgroup"
