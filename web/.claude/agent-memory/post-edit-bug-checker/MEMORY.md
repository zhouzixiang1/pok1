# Memory Index

- [Exploitability probe fix](exploitability-probe-never-ran-fix.md) — 8-gen probe blackout root causes (silent shutdown + nested-fork deadlock) and the safe workers=1+wait_for calling convention for run_exploitability_probes_async.
- [orchestrator cost<0 handler conflation](orchestrator-cost-negative-handler-conflation.md) — cycle_failed -1.0 shares the "API auth error (401/403)" loop branch; diagnostic noise not bug.
