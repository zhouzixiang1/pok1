---
name: phase2-sprt-truncation-default-pass
description: Phase 2 AgentAssay SPRT n_max truncation defaults to PASS (not rate-fallback) to keep type-I valid; verified numerically + 30 tests pass.
metadata:
  type: project
---

Phase 2 manual fix in `web/core/decision_tester.py:run_decision_tests_sprt`: when the Wald SPRT reaches n_max (default 12) without crossing an LLR boundary, the decision defaults to **PASS** (final_rule `n_max_default_pass`) rather than a rate<0.7 fallback.

**Why:** A rate-based FAIL at truncation inflates type-I to ~2α. At n_max=12 with p0=0.85, P(rate<0.7 | p0) ≈ 0.085 alone, which alone exceeds α=0.05. Truncation-default-PASS eliminates that contribution; the documented synthetic numbers are p=0.85 type-I=0.011, p=0.30 FAIL rate=0.974, p=0.60 FAIL=0.434, p=0.50=0.679. The Wald SPRT's type-I control rests on LLR *crossing an acceptance boundary*; truncating without a crossing means evidence for H1 is insufficient → presumptive PASS (H0 acceptable).

**How to apply:** Severe regressions (p ≤ p1=0.60) cross ln(A)=ln(18)=2.890 well before n_max and still FAIL via `sprt_h1`; only the ambiguous mid-band is affected. Do NOT reintroduce a rate-based FAIL at truncation — it re-breaks type-I. Verified numerically (test sequences stay inside bounds) and the full `test_phase2_cs_sprt.py` suite (30 tests) passes.

**Note:** The `"decision": "PASS"|"FAIL"|"UNDECIDED"` docstring and the `final_rule = "rate_fallback"` init default are **dead code** — the truncation branch always overrides decision to PASS, so UNDECIDED is never returned; rate_fallback is always overwritten. `run_decision_tests_sprt` is NOT yet wired into tool_gates.py (quality gate still uses the classic run_decision_tests_detail path) — it's self-contained, exercised only by its test suite as of this write. See [[aivat-sidepot-bias-bug]] for the parallel Phase-1 work.
