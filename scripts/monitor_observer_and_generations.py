#!/usr/bin/env python3
"""Monitor observer-cache availability and strict-generation pipeline logic.

Polls /api/control/health every 30s and records, for each poll:
  - HTTP status (200 vs 503) — the observer-503 availability fix
  - epoch authority coherence (epoch_state, epoch_initialized, high_water,
    stream_authority_digest present)
  - active generation stage transitions (v17 master_planned -> ... -> published)
  - published high-water tag movement (national-cloud-bot-v*)
  - any control issues (daemon_dead, stability_observation_unavailable, etc.)

Run: python3 scripts/monitor_observer_and_generations.py [--interval 30] [--max-gen 10]
Stops after observing <max-gen> new published generations, or on Ctrl-C.
Outputs a line per poll to stdout and appends to monitor_observer.log.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HEALTH_URL = "http://127.0.0.1:8000/api/control/health"
LOG = Path(__file__).resolve().parents[1] / "monitor_observer.log"


def git_tags() -> list[str]:
    try:
        out = subprocess.check_output(
            ["git", "tag", "-l", "national-cloud-bot-v*"],
            cwd=str(Path(__file__).resolve().parents[1]),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        ).split()
        return sorted(out, key=lambda t: int(t.rsplit("v", 1)[1]))
    except Exception:
        return []


def poll(timeout: float = 100.0) -> dict:
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            return {
                "http": r.status,
                "elapsed": round(time.monotonic() - t0, 1),
                "body": json.loads(body) if body else {},
                "err": None,
            }
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            detail = {}
        return {"http": e.code, "elapsed": round(time.monotonic() - t0, 1),
                "body": {}, "err": f"HTTP {e.code}: {detail.get('detail', {}).get('reason', str(e))}"}
    except Exception as e:
        return {"http": 0, "elapsed": round(time.monotonic() - t0, 1), "body": {},
                "err": f"{type(e).__name__}: {e}"}


def summarize(p: dict) -> str:
    if p["http"] != 200:
        return f"HTTP {p['http']} ({p['elapsed']}s) ERR={p['err']}"
    b = p["body"]
    s = b.get("status", {}) or {}
    ag = s.get("active_generation") or {}
    return (
        f"HTTP 200 ({p['elapsed']}s) overall={b.get('overall')} "
        f"epoch={s.get('epoch_state')} init={s.get('epoch_initialized')} "
        f"hw={s.get('version_authority_high_water')} "
        f"sad={'yes' if s.get('stream_authority_digest') else 'NO'} "
        f"gen v{ag.get('next_v')} stage={ag.get('stage')} "
        f"issues={b.get('issues')}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--max-gen", type=int, default=10, help="stop after N new published gens")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    start_tags = set(git_tags())
    start_hw = _hw_from_tags(start_tags)
    print(f"[monitor] start high-water tag: v{start_hw}; target: +{args.max_gen} gens -> v{start_hw + args.max_gen}")
    LOG.write_text("")  # reset per run

    last_stage = None
    last_hw = start_hw
    gens_seen = 0
    n = 0
    while True:
        n += 1
        p = poll()
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] #{n} {summarize(p)}"
        print(line, flush=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")

        if p["http"] == 200:
            ag = (p["body"].get("status") or {}).get("active_generation") or {}
            stage = ag.get("stage")
            if stage != last_stage:
                if last_stage is not None:
                    print(f"[monitor] stage transition: {last_stage} -> {stage} (v{ag.get('next_v')})", flush=True)
                last_stage = stage

        tags = git_tags()
        hw = _hw_from_tags(set(tags))
        if hw > last_hw:
            print(f"[monitor] *** NEW published bot: national-cloud-bot-v{hw} (was v{last_hw}) ***", flush=True)
            gens_seen += (hw - last_hw)
            last_hw = hw
            if gens_seen >= args.max_gen:
                print(f"[monitor] observed {gens_seen} new generations. Done.", flush=True)
                return 0

        if args.once:
            return 0
        time.sleep(args.interval)


def _hw_from_tags(tags: set[str]) -> int:
    nums = []
    for t in tags:
        if t.startswith("national-cloud-bot-v"):
            try:
                nums.append(int(t.rsplit("v", 1)[1]))
            except ValueError:
                pass
    return max(nums) if nums else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[monitor] stopped by user", flush=True)
        sys.exit(130)
