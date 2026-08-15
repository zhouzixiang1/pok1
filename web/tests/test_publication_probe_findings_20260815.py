"""Regressions for the 2026-08-15 afternoon fix batch.

1. P0 publication: the native-tier tag annotation must carry
   ``staging-candidate-hash:`` (the completion-tag validator collects only
   official-/staging- prefixed keys); legacy ``candidate-hash:`` tags (v173,
   v185) still validate via the collector alias. The bare key failed
   ``completion_tag_metadata_mismatch:staging-candidate-hash`` on EVERY
   publication since bc668676.
2. P1a probe: a watchdog-timed-out probe run is a load artifact — retry it
   (bounded) instead of letting it break the repeat loop into an automatic
   ``runtime_probe_non_repeatable`` hard-gate failure (v183/v184, 18+ versions).
3. P2 master guidance: the snapshot-evidence repair hint must state the real
   scout limit (2), not 3 (v184's 26-minute churn retried into the same
   rejection).
4. Consumption loop: saturator duel sessions persist bounded Phase-4 findings;
   the scheduler renders the latest source-relevant records into the
   master-context ``match_analysis`` slot.
"""

import sys
import types
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parents[1]
CORE_DIR = WEB_DIR / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))


# --- 1. publication tag key -------------------------------------------------


def test_native_tag_message_carries_staging_candidate_hash():
    from publication_transaction import _staging_publication_tag_message

    msg = _staging_publication_tag_message(186, "master", "ab" * 32)
    assert "staging-candidate-hash: " + "ab" * 32 in msg
    # The legacy bare key must not be the only carrier anymore.
    assert "\ncandidate-hash:" not in msg


def test_legacy_bare_candidate_hash_tag_still_validates():
    import bot_artifact

    legacy_body = (
        "National bot v185: master\n"
        "\n"
        "candidate-hash: " + "cd" * 32 + "\n"
        "publication-tier: native"
    )

    def fake_git(*args, **kwargs):
        return types.SimpleNamespace(returncode=0, stdout=legacy_body)

    def fake_identity(_token):
        return {"issues": [], "tag_artifact_hash": "cd" * 32, "tag": "t"}

    orig_git = getattr(bot_artifact, "_git", None)
    orig_ident = bot_artifact.published_bot_identity
    bot_artifact._git = fake_git
    bot_artifact.published_bot_identity = fake_identity
    try:
        result = bot_artifact.validate_completion_tag(
            "national_cloud_v186",
            expected_metadata={"staging-candidate-hash": "cd" * 32},
            certificate_path="",
        )
    finally:
        if orig_git is not None:
            bot_artifact._git = orig_git
        bot_artifact.published_bot_identity = orig_ident

    mismatches = [
        i for i in result.get("issues", [])
        if str(i).startswith("completion_tag_metadata_mismatch")
    ]
    assert mismatches == [], f"legacy tag failed validation: {mismatches}"


# --- 2. probe timeout retry -------------------------------------------------

def _patch_probe_env(nrp, monkeypatch, run_once):
    monkeypatch.setattr(nrp, "_run_once", run_once)
    monkeypatch.setattr(
        nrp, "build_runtime_probe_spec",
        lambda root: {"code_fingerprint": "fp", "spec_digest": "d"},
    )
    monkeypatch.setattr(nrp, "_cache_get", lambda key: None)
    monkeypatch.setattr(nrp, "_cache_put", lambda key, value: None)
    monkeypatch.setattr(nrp, "_bot_code_fingerprint", lambda root: "fp")
    monkeypatch.setattr(nrp, "_repeat_validation_issues", lambda run, spec: [])
    monkeypatch.setattr(nrp, "_repeatability_view", lambda run: {"v": 1})
    monkeypatch.setattr(
        nrp, "_repeatability_evidence",
        lambda views: {"differing_path_count": 0},
    )



def test_probe_timeout_run_is_retried_not_counted(monkeypatch):
    import national_runtime_probe as nrp

    calls = {"n": 0}

    def fake_run_once(root, spec, timeout_sec):
        calls["n"] += 1
        if calls["n"] <= 2:
            # Two load-induced slow runs, then clean runs.
            return {
                "ok": False,
                "failure_class": "candidate_contract",
                "issues": ["runtime_probe_candidate_timeout:phase=budget"],
            }
        return {"ok": True, "failure_class": "candidate_contract", "issues": []}

    _patch_probe_env(nrp, monkeypatch, fake_run_once)
    result = nrp.run_national_runtime_probe(Path("/tmp/nonexistent-root"))

    issues = result.get("issues") or []
    assert calls["n"] == 4, f"expected 2 timeout + 2 clean runs, got {calls['n']}"
    assert "runtime_probe_non_repeatable" not in issues
    assert not any("runtime_probe_candidate_timeout" in str(i) for i in issues)


def test_probe_persistent_timeout_still_fails_honestly(monkeypatch):
    import national_runtime_probe as nrp

    calls = {"n": 0}

    def fake_run_once(root, spec, timeout_sec):
        calls["n"] += 1
        return {
            "ok": False,
            "failure_class": "candidate_contract",
            "issues": ["runtime_probe_candidate_timeout:phase=budget"],
        }

    _patch_probe_env(nrp, monkeypatch, fake_run_once)
    result = nrp.run_national_runtime_probe(Path("/tmp/nonexistent-root"))

    issues = [str(i) for i in (result.get("issues") or [])]
    # 1 counted run + the bounded extra attempts, then the timeout surfaces.
    assert calls["n"] == 1 + nrp.RUNTIME_PROBE_TIMEOUT_EXTRA_ATTEMPTS
    # The aggregate issues replace the run's own list; the bounded retries
    # were spent and the run count (1 < 2) still fails repeatability.
    assert "runtime_probe_non_repeatable" in issues
    assert result.get("ok") is False


# --- 3. master guidance -----------------------------------------------------


def test_snapshot_too_many_hint_states_real_limit():
    from agent_master_validation import _proposal_schema_repair_guidance

    text = _proposal_schema_repair_guidance(
        ["proposal_snapshot_evidence_too_many"], require_snapshot_evidence=True
    )
    assert "maximum is 2" in text
    assert "maximum is 3" not in text


# --- 4. consumption loop ----------------------------------------------------


def test_findings_extraction_takes_phase4():
    from llm_saturator import _extract_findings_text

    out = (
        "Phase 1 stuff\nPhase 2 stuff\nPhase 3 stuff\n\n"
        "## Phase 4 — Synthesis\nR1: fix X (policy.py:100)\nR2: fix Y"
    )
    text = _extract_findings_text(out)
    assert text.startswith("Phase 4")
    assert "R1" in text


def test_adversarial_findings_block_selects_source_records(tmp_path, monkeypatch):
    import json
    import generation_scheduler as gs
    import evolution_infra

    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", str(tmp_path))
    sat = tmp_path / "saturator"
    sat.mkdir()
    recs = [
        {"focus_v": 11, "opponent_v": 173, "focus_bot": "national_cloud_v11",
         "opponent_bot": "national_cloud_v173", "ts": 1, "report_sha256": "a" * 64,
         "findings_text": "unrelated"},
        {"focus_v": 79, "opponent_v": 173, "focus_bot": "national_cloud_v79",
         "opponent_bot": "national_cloud_v173", "ts": 2, "report_sha256": "b" * 64,
         "findings_text": "FOCUS_FINDING about v79"},
        {"focus_v": 105, "opponent_v": 79, "focus_bot": "national_cloud_v105",
         "opponent_bot": "national_cloud_v79", "ts": 3, "report_sha256": "c" * 64,
         "findings_text": "OPP_FINDING targeting v79"},
    ]
    with open(sat / "findings.jsonl", "w") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")

    block = gs._adversarial_findings_block(79)
    assert block.startswith("ADVISORY")
    assert "FOCUS_FINDING about v79" in block
    assert "OPP_FINDING targeting v79" in block
    assert "unrelated" not in block
    # newest focus record first
    assert block.find("FOCUS_FINDING") < block.find("OPP_FINDING")


def test_adversarial_findings_block_empty_when_nothing_relevant(tmp_path, monkeypatch):
    import generation_scheduler as gs
    import evolution_infra

    monkeypatch.setattr(evolution_infra, "RESULTS_DIR", str(tmp_path))
    assert gs._adversarial_findings_block(173) == ""
    assert gs._adversarial_findings_block(None) == ""
