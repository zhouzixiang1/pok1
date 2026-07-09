from national_native import NATIVE_BOT_TEMPLATE


def test_native_template_treats_connection_reset_as_server_close():
    assert "except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError) as exc:" in NATIVE_BOT_TEMPLATE
    assert "RECV closed_by_server" in NATIVE_BOT_TEMPLATE
    assert "return 0" in NATIVE_BOT_TEMPLATE
