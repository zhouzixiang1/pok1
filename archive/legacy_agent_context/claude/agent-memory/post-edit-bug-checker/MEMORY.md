---
name: memory-index
description: Index of agent memories
---

- [Engine Judge Re-raise Bug](engine-judge-reraise-architectural-bug.md) — engine/judge.py single-check architecture vs sever/validator.py separate rules
- [rc2b Classify Bug](rc2b-classify-target-change-empty-dst-bug.md) — _classify_target_change misclassifies empty-new-file as unchanged; 1 test fails
- [AIVAT Phase1 Broken Detection](aivat-phase1-broken-detection-real-judge-log.md) — d2fad78: realized-delta reads final_result (running-total, not per-hand) + allin eligibility misses stack-mismatch allins; latent (flag OFF) but ON-path biased
