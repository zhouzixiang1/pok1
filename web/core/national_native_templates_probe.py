"""_NATIVE_STREAM_PROBE_SCRIPT companion module.

Holds the stream-decoder probe script injected into candidate policy
workers.  The raw literal was moved here byte-for-byte from
national_native_templates.py.
"""


_NATIVE_STREAM_PROBE_SCRIPT = r'''
import contextlib
import io
import json
import runpy
import sys

entry = sys.argv[1]
captured = io.StringIO()
errors = []
try:
    with contextlib.redirect_stdout(captured):
        namespace = runpy.run_path(entry, run_name="_national_stream_contract_probe")
except BaseException as exc:
    errors.append(f"entry_load:{type(exc).__name__}:{exc}")
    namespace = {}
if captured.getvalue():
    errors.append(f"stdout_pollution:{captured.getvalue()[:160]!r}")

decoder_class = namespace.get("NationalStreamDecoder")
cases = (
    ("raise 200", ["raise 200"], ""),
    ("earnChips -100", ["earnChips -100"], ""),
    ("raise 200call", ["raise 200", "call"], ""),
    ("raise 200earnChips -100", ["raise 200", "earnChips -100"], ""),
    (
        "earnChips -100preflop|SMALLBLIND|<0,3><1,3>",
        ["earnChips -100", "preflop|SMALLBLIND|<0,3><1,3>"],
        "",
    ),
    ("allinriver|<3,12>", ["allin", "river|<3,12>"], ""),
    ("earnChips\t-100", [], "earnChips\t-100"),
    ("earnChips  -100", [], "earnChips  -100"),
    ("earnChips\n-100", [], "earnChips\n-100"),
    ("raise  200", [], "raise  200"),
    ("raise ", [], "raise "),
    ("preflop|SMALLBLIND|<0,3>", [], "preflop|SMALLBLIND|<0,3>"),
    ("call\n", ["call"], "\n"),
)

def decode(chunks):
    decoder = decoder_class()
    emitted = []
    for chunk in chunks:
        emitted.extend(decoder.feed(chunk))
    emitted.extend(decoder.flush_idle())
    return emitted, decoder.buffer

if decoder_class is None:
    errors.append("missing_decoder_class")
else:
    for raw, expected, expected_remainder in cases:
        chunkings = [(raw,)]
        chunkings.extend((raw[:split], raw[split:]) for split in range(1, len(raw)))
        chunkings.append(tuple(raw))
        for chunks in chunkings:
            try:
                actual, remainder = decode(chunks)
            except BaseException as exc:
                errors.append(
                    f"decode_exception:{raw!r}:{type(exc).__name__}:{exc}"
                )
                break
            if actual != expected or remainder != expected_remainder:
                errors.append(
                    f"decode_mismatch:{raw!r}:chunks={chunks!r}:"
                    f"actual={actual!r}:remainder={remainder!r}:"
                    f"expected={expected!r}:expected_remainder={expected_remainder!r}"
                )
                break

print(json.dumps({"errors": errors[:20]}, ensure_ascii=True))
'''
