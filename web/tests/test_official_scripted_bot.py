from scripts.official_scripted_bot import (
    PREFLOP_RERAISE_2X,
    PREFLOP_RERAISE_2X_PLUS_ONE,
    ScriptedClient,
)


class _Socket:
    def __init__(self):
        self.messages = []

    def sendall(self, payload):
        self.messages.append(payload.decode())


def _client(tmp_path, scenario, *, small_blind):
    client = ScriptedClient(
        scenario=scenario,
        name="Probe",
        log_path=tmp_path / f"{scenario}.log",
        action_delay=0,
    )
    client.stage = "preflop"
    client.is_small_blind = small_blind
    client.hand = 1
    return client


def test_exact_and_strict_reraise_probes_emit_controlled_boundaries(tmp_path):
    exact = _client(tmp_path, PREFLOP_RERAISE_2X, small_blind=False)
    control = _client(tmp_path, PREFLOP_RERAISE_2X_PLUS_ONE, small_blind=False)
    exact_socket = _Socket()
    control_socket = _Socket()
    try:
        exact.dispatch(exact_socket, "raise 200")
        control.dispatch(control_socket, "raise 200")
    finally:
        exact.close()
        control.close()

    assert exact_socket.messages == ["raise 400"]
    assert control_socket.messages == ["raise 401"]


def test_probe_small_blind_folds_only_after_relayed_reraise(tmp_path):
    client = _client(tmp_path, PREFLOP_RERAISE_2X, small_blind=True)
    sock = _Socket()
    try:
        client.dispatch(sock, "raise 400")
    finally:
        client.close()
    assert sock.messages == ["fold"]
