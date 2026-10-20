"""Cgroup-aware memory heartbeat (2026-10-20 memory investigation follow-up).

The 2026-10-20 diagnosis found the heartbeat blind in exactly three ways:
it read only ``/proc/self`` (RSS/threads), so the ~1.4-1.5G reclaimable
file cache, the per-stream ~115MB one-shot codex children, and the
memory.pressure throttling signal were all invisible, and the
investigation had to be launched from governor ratios instead.  The
review downgraded the fix to observability only (evidence chain for a
capacity change not closed; config was replaced mid-window 12/250 -> 14/130).

These tests pin the review's four landing constraints for that downgrade:

a) cgroup path resolved from ``/proc/self/cgroup`` (same pattern as
   ``elo_daemon_governor._resolve_cgroup_dir``) with an env override, and
   NO hard-coded system.slice fallback;
b) the direct-child census tolerates pid races (vanished / malformed
   entries are skipped, never fatal) and the formatter never raises;
c) the extended fields cover ``memory.current``, ``memory.stat``
   anon/file, and ``memory.pressure`` (PSI some/full avg10/avg60/avg300);
d) the historical ``memory heartbeat: rss_mb=%d threads=%d`` prefix stays
   byte-stable (``journalctl -t pok-evolution | grep pok.memory`` keeps
   working; fields are extended, not restructured).
"""

from __future__ import annotations

import os
from pathlib import Path

from server import app as server_app


def _fake_stat(pid: int, comm: str, ppid: int, rss_pages: int) -> str:
    """One /proc/<pid>/stat line with rss (field 24) at after-paren index 21."""

    rest = " ".join(["S", str(ppid)] + ["0"] * 19 + [str(rss_pages)])
    return f"{pid} ({comm}) {rest}"


# ── constraint a: cgroup dir resolution ────────────────────────────────────


def test_resolve_cgroup_dir_from_proc_self_cgroup_20261020(tmp_path: Path):
    svc = tmp_path / "system.slice" / "pok-evolution.service"
    svc.mkdir(parents=True)
    (svc / "memory.current").write_text("123\n")
    resolved = server_app._resolve_memory_cgroup_dir(
        proc_self_cgroup_text="0::/system.slice/pok-evolution.service\n",
        fs_root=tmp_path,
    )
    assert resolved == svc
    # Root-cgroup spelling "0::/" resolves onto the fs root itself.
    (tmp_path / "memory.current").write_text("1\n")
    assert (
        server_app._resolve_memory_cgroup_dir(
            proc_self_cgroup_text="0::/\n", fs_root=tmp_path
        )
        == tmp_path
    )


def test_resolve_cgroup_dir_rejects_v1_and_missing_candidate_20261020(
    tmp_path: Path,
):
    (tmp_path / "memory.current").write_text("1\n")
    # cgroup v1 / hybrid lines carry a non-"0" controller id -> ignored.
    assert (
        server_app._resolve_memory_cgroup_dir(
            proc_self_cgroup_text="2:memory:/system.slice/x\n12:blkio:/y\n",
            fs_root=tmp_path,
        )
        is None
    )
    # A v2 line whose candidate lacks memory.current resolves to None —
    # no hard-coded system.slice fallback may silently read another
    # service's counters (review constraint a).
    assert (
        server_app._resolve_memory_cgroup_dir(
            proc_self_cgroup_text="0::/system.slice/other.service\n",
            fs_root=tmp_path,
        )
        is None
    )


def test_resolve_cgroup_dir_env_override_wins_20261020(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("POK_MEMORY_HEARTBEAT_CGROUP_DIR", str(tmp_path))
    assert (
        server_app._resolve_memory_cgroup_dir(
            proc_self_cgroup_text="0::/unresolvable\n", fs_root=tmp_path
        )
        == tmp_path
    )


# ── constraint c: cgroup field reads ───────────────────────────────────────


def test_cgroup_memory_fields_and_pressure_parse_20261020(tmp_path: Path):
    # Byte values are the reviewer's live 2026-10-20 23:2x sample.
    (tmp_path / "memory.current").write_text("2017646592\n")
    (tmp_path / "memory.stat").write_text(
        "anon 714891264\n"
        "file 1377042432\n"
        "kernel 16384\n"
        "sock 4096\n"
    )
    (tmp_path / "memory.pressure").write_text(
        "some avg10=0.00 avg60=0.12 avg300=1.34 total=12345\n"
        "full avg10=0.00 avg60=0.00 avg300=0.05 total=678\n"
    )
    assert server_app._read_cgroup_memory_fields(tmp_path) == {
        "current": 2017646592,
        "anon": 714891264,
        "file": 1377042432,
    }
    assert server_app._read_cgroup_memory_pressure(tmp_path) == {
        "some": (0.00, 0.12, 1.34),
        "full": (0.00, 0.00, 0.05),
    }


def test_cgroup_reads_missing_files_degrade_empty_20261020(tmp_path: Path):
    assert server_app._read_cgroup_memory_fields(tmp_path) == {}
    assert server_app._read_cgroup_memory_pressure(tmp_path) == {}


# ── constraint b: race-tolerant direct-child census ────────────────────────


def test_direct_child_census_aggregates_and_skips_races_20261020(
    tmp_path: Path,
):
    parent = 4242
    page_bytes = os.sysconf("SC_PAGE_SIZE")

    def add(pid: int, comm: str, ppid: int, rss_pages: int) -> None:
        d = tmp_path / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "stat").write_text(_fake_stat(pid, comm, ppid, rss_pages))

    add(4243, "codex", parent, 30000)  # ~117mb each, two streams
    add(4244, "codex", parent, 29867)
    add(4245, "python", parent, 15360)  # ~60mb daemon child
    add(4246, "python", 9999, 50000)  # different parent: must be ignored
    add(4247, "python", parent, 256)  # tiny direct child: still counted
    (tmp_path / "5000").mkdir()  # vanished between listdir and read
    (tmp_path / "notapid").mkdir()  # non-numeric proc entry
    add(6000, "garbage", parent, 1)
    (tmp_path / "6000" / "stat").write_text("no closing paren at all")
    d = tmp_path / "6001"
    d.mkdir()
    (d / "stat").write_text("6001 (short) R 4242")  # truncated stat line

    census = server_app._census_direct_children(
        parent_pid=parent, proc_root=tmp_path
    )
    assert census == {
        "codex": (2, (30000 + 29867) * page_bytes // 1024),
        "python": (2, (15360 + 256) * page_bytes // 1024),
    }


def test_direct_child_census_defaults_to_self_and_real_proc_20261020():
    # Default parent is os.getpid(): this pytest process has no children,
    # so the real-proc walk must return {} without raising.
    assert server_app._census_direct_children() == {}


# ── constraints b + c + d: the heartbeat log line ──────────────────────────


def test_format_line_extends_prefix_with_cgroup_and_children_20261020(
    tmp_path: Path, monkeypatch
):
    (tmp_path / "memory.current").write_text("2017646592\n")
    (tmp_path / "memory.stat").write_text(
        "anon 714891264\nfile 1377042432\n"
    )
    (tmp_path / "memory.pressure").write_text(
        "some avg10=0.00 avg60=0.12 avg300=1.34 total=1\n"
        "full avg10=0.00 avg60=0.00 avg300=0.05 total=1\n"
    )
    monkeypatch.setattr(
        server_app,
        "_census_direct_children",
        lambda parent_pid=None, proc_root=None: {
            "codex": (4, 478000),  # 466mb total
            "python": (2, 218000),  # 212mb total
        },
    )
    line = server_app._format_memory_heartbeat_line(523, 17, tmp_path)
    # Constraint d: byte-stable historical prefix.
    assert line.startswith("memory heartbeat: rss_mb=523 threads=17")
    # Constraint c: current + anon/file split + PSI (avg10/avg60/avg300).
    assert "cgroup_current_mb=1924" in line
    assert "cgroup_anon_mb=681" in line
    assert "cgroup_file_mb=1313" in line
    assert "psi_some=0.00/0.12/1.34" in line
    assert "psi_full=0.00/0.00/0.05" in line
    # Children sorted by total RSS desc.
    assert "children=codex(4,466mb),python(2,212mb)" in line


def test_format_line_without_cgroup_or_children_is_unchanged_20261020(
    monkeypatch,
):
    monkeypatch.setattr(
        server_app,
        "_census_direct_children",
        lambda parent_pid=None, proc_root=None: {},
    )
    assert (
        server_app._format_memory_heartbeat_line(255, 11, None)
        == "memory heartbeat: rss_mb=255 threads=11"
    )


def test_format_line_never_raises_on_broken_cgroup_dir_20261020(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(
        server_app,
        "_census_direct_children",
        lambda parent_pid=None, proc_root=None: {},
    )
    line = server_app._format_memory_heartbeat_line(
        300, 4, tmp_path / "does-not-exist"
    )
    assert line == "memory heartbeat: rss_mb=300 threads=4"


def test_format_line_children_capped_and_sorted_desc_20261020(monkeypatch):
    fake = {f"p{i}": (1, (i + 1) * 1024) for i in range(10)}
    monkeypatch.setattr(
        server_app,
        "_census_direct_children",
        lambda parent_pid=None, proc_root=None: fake,
    )
    line = server_app._format_memory_heartbeat_line(1, 1, None)
    assert "children=p9(1,10mb)" in line
    assert "p2(1,3mb)" in line
    assert "+2more" in line
    # Below the cap cutoff: dropped entirely.
    assert "p1(" not in line
    assert "p0(" not in line
