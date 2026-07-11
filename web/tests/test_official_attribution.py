from official_attribution import ATTRIBUTION_POLICY_ID, attribute_round, attribute_suite


def _receipt(*, kind="opponent", passed=False, issues=None, wire_issues=None, observed_exit=None):
    receipt = {
        "round_id": f"{kind}_01",
        "round_kind": kind,
        "round_index": 1,
        "passed": passed,
        "bot_a": {"name": "Candidate", "path": "/bots/national_v10"},
        "bot_b": {"name": "Opponent", "path": "/bots/national_v9"},
        "issues": list(issues or []),
        "wire_replay_summary": {"issues": list(wire_issues or [])},
    }
    if observed_exit is not None:
        receipt["observed_exit"] = observed_exit
    return receipt


def test_candidate_wire_violation_is_blocking():
    result = attribute_round(_receipt(wire_issues=[{
        "kind": "illegal_check",
        "conn": "A",
        "hand": 17,
        "stage": "turn",
    }]))

    assert result["policy_id"] == ATTRIBUTION_POLICY_ID
    assert result["candidate_verdict"] == "fail"
    assert result["candidate_blocking"] is True
    assert result["findings"][0]["subject_domain"] == "candidate"


def test_opponent_wire_violation_invalidates_round_without_failing_candidate():
    result = attribute_round(_receipt(wire_issues=[{
        "kind": "illegal_allin",
        "conn": "B",
        "hand": 8,
        "stage": "preflop",
    }]))

    assert result["candidate_verdict"] == "inconclusive"
    assert result["candidate_blocking"] is False
    assert result["retry_required"] is True
    assert result["findings"][0]["subject_domain"] == "opponent"


def test_self_play_second_connection_is_still_candidate():
    result = attribute_round(_receipt(kind="self_play", wire_issues=[{
        "kind": "unsolicited_client_action",
        "conn": "B",
    }]))

    assert result["candidate_verdict"] == "fail"
    assert result["findings"][0]["subject_domain"] == "candidate"


def test_platform_exit_dominates_normal_bot_socket_shutdown():
    result = attribute_round(_receipt(
        issues=["Candidate_exited_early: rc=0", "platform_exited_early: rc=1"],
        observed_exit={
            "subject_domain": "platform",
            "subject_instance_id": "official_exe",
            "returncode": 1,
            "bot_a_returncode": 0,
            "bot_b_returncode": 0,
        },
    ))

    assert result["candidate_verdict"] == "inconclusive"
    assert result["candidate_blocking"] is False
    assert all(item["candidate_impact"] != "block" for item in result["findings"])


def test_candidate_crash_requires_candidate_attributed_evidence():
    explicit = attribute_round(_receipt(
        issues=["Candidate_exited_early: rc=1"],
        observed_exit={
            "subject_domain": "candidate",
            "subject_instance_id": "candidate_a",
            "connection": "A",
            "returncode": 1,
        },
    ))
    ambiguous = attribute_round(_receipt(issues=["Candidate_exited_early: rc=0"]))

    assert explicit["candidate_verdict"] == "fail"
    assert ambiguous["candidate_verdict"] == "inconclusive"
    assert ambiguous["candidate_blocking"] is False


def test_traceback_terminal_exception_distinguishes_bot_crash_from_peer_reset():
    bot_bug = attribute_round(_receipt(issues=["botA.stderr.log: ValueError: bad state"]))
    peer_reset = attribute_round(_receipt(
        issues=[
            "botA.stderr.log: Traceback (most recent call last):",
            "botA.stderr.log: ConnectionResetError: [Errno 104] Connection reset by peer",
        ],
    ))

    assert bot_bug["candidate_verdict"] == "fail"
    assert bot_bug["candidate_blocking"] is True
    assert peer_reset["candidate_verdict"] == "inconclusive"
    assert peer_reset["candidate_blocking"] is False


def test_suite_only_passes_when_every_round_is_countable():
    passed = _receipt(passed=True)
    opponent_fault = _receipt(wire_issues=[{"kind": "illegal_call", "conn": "B"}])

    assert attribute_suite([passed])["candidate_verdict"] == "pass"
    result = attribute_suite([passed, opponent_fault])
    assert result["candidate_verdict"] == "inconclusive"
    assert result["countable_rounds"] == 1
