# Archived Documentation

This directory holds historical documentation that has been superseded by the
raw national-TCP policy era. Files are kept for provenance and incident review;
they are not inputs to the active evolution system.

Active references remain directly in `docs/`, including the immutable
official-EXE oracles, certification policy, raw-stream runtime contract, and
dual-checkout policy. Files below this directory are **legacy-untrusted**:

- active Python code must not import or execute them;
- prompt assembly and retrieval must not read or summarize them;
- ratings, source selection, capability discovery, and gates must not consume
  their results;
- their old Botzone/JSON, adapter, strategy-wrapper, RL, or experimental claims
  must not be field-upgraded into current evidence.

Historical files may be consulted manually only when investigating provenance.
Current behavior is defined by active code, `AGENTS.md`, and the documents that
remain at the top level of `docs/`.

## Layout

- **`version-history/`** — old bot-version reports. All of their strategy and
  evaluation observations predate the strict typed-policy ABI.
- **`2026-06-audits/`** — one-shot system audits, bug reports, and root-cause
  analyses from 2026-06. The issues they describe were fixed, and the
  architecture has since moved to the strict `national_tcp_policy_v1` typed
  policy ABI plus official Windows EXE certification.
- **`evolution-plans/`** — implemented阶段性 evolution fix / observation plans
  (2026-06 through 2026-07-01). Kept as a record of what was done, not as current
  guidance.
- **`superseded-reports/`** — old handoffs, plans, RL designs, neural experiment
  reports, evidence analyses, and architecture proposals overtaken by the
  strict raw-TCP policy design.
- **`reference-patches/`** — legacy patch bundles that were never applied.
