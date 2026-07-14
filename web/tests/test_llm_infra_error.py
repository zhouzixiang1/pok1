"""Unit coverage for provider/infrastructure error classification.

The retired Reviewer/Critic retry-state tests lived here as well.  That state
machine is not part of ``national_tcp_policy_v1``: non-Worker availability is
covered by ``test_nonworker_llm_availability_deferral.py`` and strict role
dispatch/receipts by ``test_strict_authority_workflow.py``.  Keeping the old
``default`` workflow fixture active would silently test a second authority.
"""

import asyncio


class TestIsLlmInfraError:
    def test_claude_sdk_error_is_infra(self):
        from claude_agent_sdk import ClaudeSDKError
        from llm_failure import is_llm_infra_error

        assert is_llm_infra_error(ClaudeSDKError("signature error")) is True

    def test_timeout_error_is_infra(self):
        from llm_failure import is_llm_infra_error

        assert is_llm_infra_error(asyncio.TimeoutError()) is True

    def test_connection_error_is_infra(self):
        from llm_failure import is_llm_infra_error

        assert is_llm_infra_error(ConnectionError("refused")) is True

    def test_os_error_is_infra(self):
        from llm_failure import is_llm_infra_error

        assert is_llm_infra_error(OSError("broken pipe")) is True

    def test_value_error_is_not_infra(self):
        from llm_failure import is_llm_infra_error

        assert is_llm_infra_error(ValueError("bad value")) is False

    def test_json_decode_error_is_not_infra(self):
        from json import JSONDecodeError
        from llm_failure import is_llm_infra_error

        assert is_llm_infra_error(JSONDecodeError("msg", "doc", 0)) is False

    def test_key_error_is_not_infra(self):
        from llm_failure import is_llm_infra_error

        assert is_llm_infra_error(KeyError("missing")) is False

    def test_exit143_is_shutdown_cancel(self):
        from llm_failure import is_shutdown_cancel_error

        assert is_shutdown_cancel_error(
            Exception("Command failed with exit code 143")
        ) is True
        assert is_shutdown_cancel_error(Exception("terminated by signal 15")) is True
        assert is_shutdown_cancel_error(Exception("bad value")) is False

    def test_infra_payload_has_marker_and_fields(self):
        from claude_agent_sdk import ClaudeSDKError
        from llm_failure import infra_payload

        payload = infra_payload(ClaudeSDKError("boom"), approved=False, foo=1)
        assert payload == {
            "llm_failed": True,
            "infra_error": True,
            "error": "boom",
            "approved": False,
            "foo": 1,
        }
