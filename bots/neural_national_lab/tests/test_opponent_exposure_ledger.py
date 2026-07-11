from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import opponent_exposure_ledger as ledger  # noqa: E402


CANDIDATE_SHA = "a" * 64
ARTIFACT_SHA = "b" * 64


def test_prior_development_exposure_blocks_final_blind(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger.open_exposure(
        path,
        role="train",
        opponents=["national_v57"],
        run_id="training-1",
    )

    with pytest.raises(ValueError, match="already exposed"):
        ledger.reserve_final_blind(
            path,
            opponents=["national_v57"],
            run_id="final-1",
            candidate_sha256=CANDIDATE_SHA,
        )


def test_final_reservation_blocks_every_other_reader(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    reserved = ledger.reserve_final_blind(
        path,
        opponents=["national_v200", "national_v201"],
        run_id="final-1",
        candidate_sha256=CANDIDATE_SHA,
    )
    assert reserved["changed"] is True

    with pytest.raises(ValueError, match="reserved for final blind"):
        ledger.open_exposure(
            path,
            role="policy_gate",
            opponents=["national_v200"],
            run_id="other",
        )
    with pytest.raises(ValueError, match="not reserved by this run"):
        ledger.open_exposure(
            path,
            role="final_blind",
            opponents=["national_v200"],
            run_id="wrong-final",
            candidate_sha256=CANDIDATE_SHA,
            artifact_sha256=ARTIFACT_SHA,
        )

    opened = ledger.open_exposure(
        path,
        role="final_blind",
        opponents=["national_v200", "national_v201"],
        run_id="final-1",
        candidate_sha256=CANDIDATE_SHA,
        artifact_sha256=ARTIFACT_SHA,
    )
    assert opened["changed"] is True
    with pytest.raises(ValueError, match="not reserved by this run"):
        ledger.open_exposure(
            path,
            role="final_blind",
            opponents=["national_v200", "national_v201"],
            run_id="final-1",
            candidate_sha256=CANDIDATE_SHA,
            artifact_sha256=ARTIFACT_SHA,
        )


def test_released_unopened_reservation_can_be_reused(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger.reserve_final_blind(
        path,
        opponents=["national_v200"],
        run_id="abandoned",
        candidate_sha256=CANDIDATE_SHA,
    )
    ledger.release_final_blind(
        path, opponents=["national_v200"], run_id="abandoned"
    )

    opened = ledger.open_exposure(
        path,
        role="early_stop",
        opponents=["national_v200"],
        run_id="training-2",
    )
    assert opened["changed"] is True


def test_opened_final_blind_is_one_time_and_then_regression_only(
    tmp_path: Path,
) -> None:
    path = tmp_path / "ledger.json"
    ledger.reserve_final_blind(
        path,
        opponents=["national_v200"],
        run_id="final-1",
        candidate_sha256=CANDIDATE_SHA,
    )
    ledger.open_exposure(
        path,
        role="final_blind",
        opponents=["national_v200"],
        run_id="final-1",
        candidate_sha256=CANDIDATE_SHA,
        artifact_sha256=ARTIFACT_SHA,
    )

    with pytest.raises(ValueError, match="already exposed"):
        ledger.reserve_final_blind(
            path,
            opponents=["national_v200"],
            run_id="final-2",
            candidate_sha256=CANDIDATE_SHA,
        )
    with pytest.raises(ValueError, match="only become regression"):
        ledger.open_exposure(
            path,
            role="policy_selection",
            opponents=["national_v200"],
            run_id="later",
        )
    assert ledger.open_exposure(
        path,
        role="regression",
        opponents=["national_v200"],
        run_id="later",
    )["changed"] is True


def test_exact_open_event_is_idempotent_and_persistent(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    kwargs = {
        "role": "model_calibration",
        "opponents": ["national_v142"],
        "run_id": "calibration-1",
        "artifact_sha256": "c" * 64,
    }

    assert ledger.open_exposure(path, **kwargs)["changed"] is True
    assert ledger.open_exposure(path, **kwargs)["changed"] is False

    payload = json.loads(path.read_text(encoding="utf-8"))
    report = ledger.status(path)
    assert len(payload["events"]) == 1
    assert report["events"] == 1
    assert report["opponents"]["national_v142"]["exposures"][0][
        "role"
    ] == "model_calibration"


def test_final_blind_is_bound_to_frozen_candidate_and_report(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    with pytest.raises(ValueError, match="candidate_sha256 is required"):
        ledger.reserve_final_blind(
            path, opponents=["national_v200"], run_id="final-1"
        )

    ledger.reserve_final_blind(
        path,
        opponents=["national_v200"],
        run_id="final-1",
        candidate_sha256=CANDIDATE_SHA,
    )
    with pytest.raises(ValueError, match="candidate mismatch"):
        ledger.open_exposure(
            path,
            role="final_blind",
            opponents=["national_v200"],
            run_id="final-1",
            candidate_sha256="c" * 64,
            artifact_sha256=ARTIFACT_SHA,
        )
    with pytest.raises(ValueError, match="artifact_sha256 is required"):
        ledger.open_exposure(
            path,
            role="final_blind",
            opponents=["national_v200"],
            run_id="final-1",
            candidate_sha256=CANDIDATE_SHA,
        )


def test_hashes_must_be_sha256_hex(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="64-character"):
        ledger.open_exposure(
            tmp_path / "ledger.json",
            role="train",
            opponents=["national_v119"],
            run_id="train-1",
            artifact_sha256="not-a-digest",
        )
