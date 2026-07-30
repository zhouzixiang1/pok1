"""Internal-match admission cluster, extracted from ``elo_daemon``.

Holds ``admit_internal_match_result``, which serializes the sole rating-mutation
boundary for native 70-hand strength samples.  The parent module
(``elo_daemon``) keeps a thin delegate shell for the moved symbol so that:

* intra-module callers (``main``, ``run_single_match``) continue to resolve
  through the parent namespace;
* test-suite direct calls such as
  ``elo_daemon.admit_internal_match_result(...)`` keep working; and
* any future ``monkeypatch.setattr(elo_daemon, "<name>", fake)`` is still
  observed because the delegate forwards to this companion at call time.

Implementation contract
-----------------------
The companion imports the parent module as ``_ed`` and reads every
module-level constant / mutable global (``RESULTS_DIR``, ``REPLAY_DIR``,
``BOTS_DIR``, ``MATCH_HISTORY_FILE``, ``daemon_evaluation_identity_digest``)
live through ``_ed.<name>``.  This is required because those globals are
populated by ``main()`` long after this module is first imported; reading
them at import time would freeze a stale snapshot.

Script-launch alias (load-bearing)
----------------------------------
Production starts the daemon as ``python …/elo_daemon.py`` (see
``daemon_management.start_daemon``), so the parent file is ``__main__``.
``elo_daemon.py`` must register ``sys.modules["elo_daemon"]`` to that same
object **before** importing this companion; otherwise ``import elo_daemon``
here dual-loads a twin whose ``daemon_evaluation_identity_digest`` stays
``None`` and every completed match raises
``staged match identity no longer matches the daemon evaluation epoch``.

Intra-cluster helpers that ``admit_internal_match_result`` calls
(``_discard_staged_match``, ``process_result``, ``_ensure_safe_replay_directory``)
remain in the parent module and are reached through ``_ed.<name>`` so existing
monkeypatches and delegates keep resolving.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import elo_daemon as _ed

# Stable library helpers re-imported here (these are not daemon globals).
from evolution_infra import append_locked_jsonl


def admit_internal_match_result(result, ratings, h2h, bot_stats, *, verbose=False):
    """Serialize rating mutation and authoritative history/replay publication."""
    if result[6] is not None or int(result[5] or 0) <= 0:
        _ed._discard_staged_match(result)
        return _ed.process_result(result, ratings, h2h, bot_stats, verbose=verbose)
    admission = result[8] if len(result) > 8 else None
    if not isinstance(admission, dict):
        raise RuntimeError("successful internal match has no staged admission receipt")

    from evaluation_bundle import evaluation_cycle_lock
    from evaluation_data_identity import current_evaluation_digest

    with evaluation_cycle_lock(_ed.RESULTS_DIR, exclusive=False):
        expected_identity = str(admission.get("evaluation_identity_digest") or "")
        current_identity = current_evaluation_digest(_ed.RESULTS_DIR)
        if (
            not expected_identity
            or expected_identity != _ed.daemon_evaluation_identity_digest
            or current_identity != expected_identity
        ):
            _ed._discard_staged_match(result)
            raise RuntimeError(
                "staged match identity no longer matches the daemon evaluation epoch"
            )
        replay_root = _ed._ensure_safe_replay_directory(_ed.REPLAY_DIR)
        pending_root = _ed._ensure_safe_replay_directory(replay_root / ".pending")
        if pending_root.parent != replay_root:
            raise RuntimeError("staged replay directory escapes replay root")
        pending = Path(str(admission.get("pending_path") or ""))
        if (
            pending.is_symlink()
            or not pending.is_file()
            or pending.resolve().parent != pending_root
        ):
            raise RuntimeError("staged match replay path is missing or unsafe")
        payload = pending.read_bytes()
        if len(payload) != int(admission.get("replay_bytes", -1)):
            raise RuntimeError("staged match replay size mismatch")
        if hashlib.sha256(payload).hexdigest() != admission.get("replay_sha256"):
            raise RuntimeError("staged match replay digest mismatch")
        parsed = json.loads(payload.decode("utf-8"))
        summary = admission.get("summary")
        if not isinstance(parsed, dict) or not isinstance(summary, dict):
            raise RuntimeError("staged match admission payload is invalid")
        if parsed.get("evaluation_identity_digest") != expected_identity:
            raise RuntimeError("staged replay identity mismatch")
        if parsed.get("evaluation_epoch") != "national_tcp_policy_v1":
            raise RuntimeError("staged replay evaluation epoch mismatch")
        if parsed.get("execution_mode") != "native_tcp":
            raise RuntimeError("staged replay execution mode mismatch")
        # This is the sole mutation boundary for native rating/H2H.  A
        # successful worker result is insufficient: only a complete strict
        # 70-hand envelope with raw replay, artifact and timing proof may be
        # admitted.  Diagnostic/non-strength staged receipts stay outside this
        # API rather than becoming a back door into Glicko.
        if (
            parsed.get("strength_sample_unit") != "70_hand_match"
            or int(parsed.get("hands_per_strength_sample", 0) or 0) != 70
            or parsed.get("strength_admitted") is not True
            or parsed.get("strength_complete") is not True
            or parsed.get("strength_compliance_passed") is not True
        ):
            raise RuntimeError("staged match is not an admitted 70-hand strength sample")
        try:
            from bot_artifact import hash_path
            from national_native import (
                _artifact_execution_is_valid,
                require_native_match_timing_plan,
                validate_native_match_timing_evidence,
            )
            from replay_analysis import validate_native_replay

            replay_validation = validate_native_replay(
                parsed,
                expected_evaluation_identity_digest=expected_identity,
                expected_replay_id=str(admission.get("filename") or ""),
            )
            if not replay_validation.accepted:
                raise RuntimeError(
                    "staged replay strict validation failed:"
                    + str(replay_validation.reason)
                )
            staged_timing_plan = require_native_match_timing_plan(
                parsed.get("native_match_timing_plan"),
                hands=70,
                requested_timeout_sec=None,
            )
            if parsed.get("native_match_timing_plan_digest") != (
                staged_timing_plan.digest()
            ):
                raise RuntimeError("staged replay timing plan digest mismatch")
            expected_artifacts = {
                str(parsed["bot0"]): hash_path(_ed.BOTS_DIR / str(parsed["bot0"])),
                str(parsed["bot1"]): hash_path(_ed.BOTS_DIR / str(parsed["bot1"])),
            }
            if dict(replay_validation.artifact_hashes) != expected_artifacts:
                raise RuntimeError(
                    "staged replay artifact identity does not match current bot bytes"
                )
            for index, replay in enumerate(parsed.get("games") or []):
                timing_issues = validate_native_match_timing_evidence(
                    replay,
                    timing_plan=staged_timing_plan,
                )
                if timing_issues:
                    raise RuntimeError(
                        f"staged replay {index} timing evidence invalid:"
                        + ";".join(timing_issues)
                    )
                if not _artifact_execution_is_valid(
                    replay.get("artifact_execution"),
                    expected_artifacts,
                ):
                    raise RuntimeError(
                        f"staged replay {index} artifact identity invalid"
                    )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                "staged replay strength evidence invalid:"
                f"{type(exc).__name__}"
            ) from exc
        summary_fields = (
            "id", "timestamp", "execution_mode", "evaluation_epoch", "bot0",
            "bot1", "bot0_wins", "bot1_wins", "draws",
            "evaluation_identity_digest", "strength_sample_unit",
            "hands_per_strength_sample", "strength_admitted", "strength_complete",
            "strength_compliance_passed", "strength_sample_count",
            "net_chips_bot0", "strength_order",
            "native_match_timing_plan", "native_match_timing_plan_digest",
        )
        derived_summary = {field: parsed.get(field) for field in summary_fields}
        derived_summary["replay_sha256"] = hashlib.sha256(payload).hexdigest()
        if summary != derived_summary:
            raise RuntimeError("staged match summary is not canonical replay projection")
        if (
            str(derived_summary.get("bot0")) != str(result[0])
            or str(derived_summary.get("bot1")) != str(result[1])
            or int(derived_summary.get("bot0_wins", -1)) != int(result[2])
            or int(derived_summary.get("bot1_wins", -1)) != int(result[3])
            or int(derived_summary.get("draws", -1)) != int(result[4])
        ):
            raise RuntimeError("staged match receipt disagrees with worker result")

        # Main-thread order is the transaction: mutate in-memory ratings/H2H,
        # append the matching history row, then expose the replay. Any failure
        # is fatal to this daemon run; restart hydrates the previous pointer and
        # discards/truncates this uncommitted work.
        admitted = _ed.process_result(
            result,
            ratings,
            h2h,
            bot_stats,
            verbose=verbose,
        )
        if admitted <= 0:
            raise RuntimeError("successful staged match produced no rating admission")
        append_locked_jsonl(_ed.MATCH_HISTORY_FILE, summary)
        final_path = replay_root / str(admission.get("filename") or "")
        if (
            final_path.parent != replay_root
            or final_path.exists()
            or final_path.is_symlink()
        ):
            raise RuntimeError("staged match final replay path collision")
        os.replace(pending, final_path)
        return admitted
