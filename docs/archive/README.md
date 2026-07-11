# Archived Documentation

This directory holds historical documentation that has been superseded by the
current national-native + official-certification era of the project. Files are
kept for historical context and were moved here with `git mv` (history is
preserved), not deleted.

Active references remain in `docs/` — including the official-EXE oracles, the
dual-checkout sync policy, the national platform alignment report, the strength
model, and every design document named in `CLAUDE.md` / `AGENTS.md`. Files
here are **not authoritative for current behavior**; read `AGENTS.md` and
`CLAUDE.md` first.

## Layout

- **`version-history/`** — early neural-national bot version reports (v103–v130)
  from the hand-crafted VPIP/PFR profile-gate era, before the GRU opponent-aware
  model (v144+). Superseded by the active neural reports in `docs/`.
- **`2026-06-audits/`** — one-shot system audits, bug reports, and root-cause
  analyses from 2026-06. The issues they describe were fixed, and the
  architecture has since moved to national-native TCP plus official Windows EXE
  certification.
- **`evolution-plans/`** — implemented阶段性 evolution fix / observation plans
  (2026-06 through 2026-07-01). Kept as a record of what was done, not as current
  guidance.
- **`superseded-reports/`** — deep design / reference reports whose proposals
  were overtaken by later implementation decisions.
- **`reference-patches/`** — legacy patch bundles that were never applied.
