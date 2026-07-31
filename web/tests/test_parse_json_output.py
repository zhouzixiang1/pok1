"""Regression tests for parse_json_output / parse_json_output_with_mode.

Anchors the inline (unfenced) JSON extraction strategy: GLM with effort=max
often emits a long chain-of-thought followed by the final JSON object INLINE
(no ```json fence). Before the fix, parse_json_output only looked inside
```json fences or tried raw json.loads, so it returned None and Master's scout
proposals were mis-rejected as proposal_json_object_required even though the
model had produced a complete, valid JSON object. The fix adds a
brace-matching strategy that finds the longest parseable {...} object anywhere
in the output.
"""

import json

from llm_query_retry import parse_json_output, parse_json_output_with_mode


def _proposal():
    """A minimal valid scout proposal object."""
    return {
        "targeted_failure": "fold equity is size-invariant",
        "structural_change": "scale fold probability by raise geometry",
        "mechanism_target": "opponent.terminal_response",
        "target_files": ["policy.py"],
        "falsifier": {
            "test_name": "terminal_response_adaptation",
            "intervention_target": "opponent.terminal_response",
            "control": "low fold anchor",
            "intervention": "raise fold anchor",
            "expected_observation": "raise_to increases",
        },
    }


def test_fenced_json_block_still_parses():
    """Existing ```json fenced output keeps working."""
    obj = _proposal()
    output = f"Here is my plan:\n```json\n{json.dumps(obj)}\n```\nDone."
    data, mode = parse_json_output_with_mode(output)
    assert mode == "OK"
    assert data == obj


def test_raw_json_still_parses():
    """Output that IS just a JSON object parses."""
    obj = _proposal()
    data, mode = parse_json_output_with_mode(json.dumps(obj))
    assert mode == "OK"
    assert data == obj


def test_inline_unfenced_json_after_prose_parses():
    """The regression: inline JSON (no fence) after a long preamble extracts the
    TOP-LEVEL object, not an inner sub-object like falsifier."""
    obj = _proposal()
    preamble = (
        "Let me reason through this carefully. " * 50
        + "I have analyzed the EV comparison and the fold-equity term. "
        + "Now I will emit the final JSON object.\n"
    )
    output = preamble + json.dumps(obj)
    data, mode = parse_json_output_with_mode(output)
    assert mode == "OK", f"expected OK, got {mode}"
    assert isinstance(data, dict)
    # Must be the TOP-LEVEL object, not the nested falsifier sub-dict.
    assert data.get("mechanism_target") == "opponent.terminal_response"
    assert "targeted_failure" in data
    assert "falsifier" in data


def test_inline_json_returns_longest_object_not_inner():
    """When multiple {...} objects are present, the LONGEST (outermost) wins."""
    inner = {"a": 1}
    outer = {"top": "level", "nested": inner, "more": [1, 2, 3]}
    output = f"prose {json.dumps(inner)} more prose then final: {json.dumps(outer)}"
    data = parse_json_output(output)
    assert data == outer


def test_empty_and_no_json_classify_correctly():
    assert parse_json_output_with_mode("") == (None, "NO_JSON")
    assert parse_json_output_with_mode("   ") == (None, "NO_JSON")
    data, mode = parse_json_output_with_mode("just prose, no json at all")
    assert mode == "NO_FENCE"
    assert data is None


def test_malformed_json_with_brace_is_parse_error():
    """Output that looks like JSON (has braces) but can't parse → PARSE_ERROR."""
    data, mode = parse_json_output_with_mode("{broken json missing quotes}")
    assert mode == "PARSE_ERROR"
    assert data is None
