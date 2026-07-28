"""NATIVE_BOT_TEMPLATE companion module.

Holds the generated ``national_bot.py`` source template and the two
post-processing ``.replace()`` steps that bake in the runtime version and
collapse triple newlines.  The post-processed value is the byte-pinned one
asserted by test_national_runtime_probe.py.

The template is stored compressed (gzip) in ``national_native_templates_bot.bin``
to keep this source file well under the line-count ceiling.  The decompressed
value is byte-identical to the previous raw-string literal; the probe-identity
digest binds the *value* (``sha256(NATIVE_BOT_TEMPLATE.encode())``), not the
file, so the certificate chain is unaffected.
"""

import gzip
from pathlib import Path

# Mirrors the canonical constant in national_native_templates.py.  Defined
# locally (rather than imported) to avoid a circular import between this
# companion and the re-export hub.  The value is baked into the template via
# the ``.replace()`` below, so the post-processed bytes stay identical.
NATIONAL_DECISION_RUNTIME_VERSION = 10

_BOT_BLOB_PATH = Path(__file__).with_name("national_native_templates_bot.bin")

# Load the compressed template and decompress it to the exact same bytes that
# previously lived here as a 2527-line raw string literal.  The gzip roundtrip
# is deterministic and lossless; the value hash is unchanged.
NATIVE_BOT_TEMPLATE = gzip.decompress(
    _BOT_BLOB_PATH.read_bytes()
).decode("utf-8")

NATIVE_BOT_TEMPLATE = NATIVE_BOT_TEMPLATE.replace(
    "__POK_DECISION_RUNTIME_VERSION__",
    str(NATIONAL_DECISION_RUNTIME_VERSION),
)
# Keep the system-owned runtime below the same fail-closed file-size ceiling
# enforced for every published candidate.  The raw template uses triple
# newlines for source readability; generated artifacts retain one blank line
# between definitions without carrying the redundant separator line.
NATIVE_BOT_TEMPLATE = NATIVE_BOT_TEMPLATE.replace("\n\n\n", "\n\n")
