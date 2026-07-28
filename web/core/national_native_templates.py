"""System-owned national runtime template artifacts.

Re-export hub for the three immutable source-template string literals that
become the generated ``national_bot.py``, ``precompute.py``, and the
stream-decoder probe script, plus the runtime-version constant.

The byte-pinned template literals now live in dedicated companion modules so
this file stays well under the line-count ceiling (the bot template alone is
~2500 lines).  Re-exporting them here preserves every existing
``from national_native_templates import ...`` site (e.g. national_native.py and
national_native_analysis.py) without modification, and the imported values are
byte-identical to the pre-split definitions, so the hash-pinning tests in
test_national_runtime_probe.py continue to pass.

Companions:
* national_native_templates_bot       -> NATIVE_BOT_TEMPLATE (incl. the two
  ``.replace()`` post-processing steps that bake in the runtime version and
  collapse triple newlines; the post-processed value is the byte-pinned one).
* national_native_templates_precompute -> NATIVE_PRECOMPUTE_TEMPLATE.
* national_native_templates_probe      -> _NATIVE_STREAM_PROBE_SCRIPT.
"""

from national_native_templates_bot import NATIVE_BOT_TEMPLATE  # noqa: F401
from national_native_templates_precompute import (  # noqa: F401
    NATIVE_PRECOMPUTE_TEMPLATE,
)
from national_native_templates_probe import _NATIVE_STREAM_PROBE_SCRIPT  # noqa: F401

# Version constant for the national decision runtime.  The bot companion has
# its own local copy (avoids a circular import), but this remains the
# canonical public definition that national_native.py re-exports.
NATIONAL_DECISION_RUNTIME_VERSION = 10
