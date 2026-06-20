"""Tests for parse_json_output_with_mode failure-mode classification (A2).

Validates that Master-output parse failures are classified into a distinguishable
mode (OK / NO_JSON / NO_FENCE / PARSE_ERROR) instead of the undifferentiated
"malformed JSON" that hid three distinct root causes during the v125 retry-storm.
"""

from llm_query import parse_json_output, parse_json_output_with_mode


class TestParseJsonOutputWithMode:
    def test_ok_valid_fenced_json(self):
        output = (
            "Here is the plan:\n"
            '```json\n{"tasks": [{"worker_id": 1, "role": "architect"}]}\n```\n'
            "Done."
        )
        data, mode = parse_json_output_with_mode(output)
        assert mode == "OK"
        assert data == {"tasks": [{"worker_id": 1, "role": "architect"}]}

    def test_ok_raw_json(self):
        data, mode = parse_json_output_with_mode('{"tasks": []}')
        assert mode == "OK"
        assert data == {"tasks": []}

    def test_no_json_empty(self):
        data, mode = parse_json_output_with_mode("")
        assert mode == "NO_JSON"
        assert data is None

    def test_no_json_whitespace_only(self):
        data, mode = parse_json_output_with_mode("   \n  \t ")
        assert mode == "NO_JSON"
        assert data is None

    def test_no_fence_prose_without_any_json(self):
        # The v125 NO_FENCE failure: the model emitted prose (reading files,
        # analyzing) but never produced a ```json block or any JSON structure
        # before being wall-clock terminated.
        output = (
            "I'll start by reading the H2H data and experience pool. "
            "Let me analyze the recent losses against claude_v121..."
        )
        data, mode = parse_json_output_with_mode(output)
        assert mode == "NO_FENCE"
        assert data is None

    def test_parse_error_malformed_fenced_json(self):
        # Had a ```json fence but the content does not parse.
        output = '```json\n{"tasks": [broken json here\n```'
        data, mode = parse_json_output_with_mode(output)
        assert mode == "PARSE_ERROR"
        assert data is None

    def test_parse_error_unterminated_brace(self):
        output = 'Plan:\n{"tasks": ["unterminated'
        data, mode = parse_json_output_with_mode(output)
        assert mode == "PARSE_ERROR"
        assert data is None

    def test_backward_compat_original_function_unchanged(self):
        # A2 must NOT alter the original parse_json_output behavior.
        assert parse_json_output('```json\n{"a": 1}\n```') == {"a": 1}
        assert parse_json_output('{"b": 2}') == {"b": 2}
        assert parse_json_output("not json at all, no braces") is None
        assert parse_json_output("") is None
