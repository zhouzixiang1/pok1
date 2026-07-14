---
name: rc2b-classify-target-change-empty-dst-bug
description: rc2b _classify_target_change misclassifies (src missing, dst empty) as unchanged; 1 test fails
metadata:
  type: project
---

rc2b fix in web/core/agent_workers.py added `_classify_target_change(src_exists, dst_exists, src_text, dst_text)` (lines 136-149) and rewrote the zero-changes verification block (lines 242-285) to bucket failures into `invalid_target`/`deleted` vs genuine `unchanged`.

**Bug found 2026-06-15:** branch ordering causes `_classify_target_change(False, True, "", "")` (src missing, dst EXISTS but EMPTY) to return `"unchanged"` instead of `"invalid_target"`. The new_file guard requires `dst_text` truthy, the invalid_target guard requires `not dst_exists`, so empty-dst-new-file falls through to `src_text == dst_text` → `"unchanged"`. Test `tests/test_rc2b_zero_changes.py::test_new_file_requires_nonempty_dst` fails.

**Why:** spec pseudocode and implementation both have this ordering; the test contradicts the pseudocode. Real-world impact is limited — empty new-file still triggers a `zero_changes` retry (failure caught), just with wrong `_last_failure_type` and a slightly misleading "use Edit" hint.

**How to apply:** if touching this helper, reorder so the empty-new-file case returns `invalid_target` (move `not src_exists` general guard before the equal-text check), or change the test expectation. Verify against the rc2b test file.

Verified safe: rollback block (227-235) NOT touched; NEW-file preservation intact (only overwrites when src_file.exists()); `_last_failure_type` is JSONL-only string, no downstream programmatic branch on `"invalid_target"`; 814 existing tests pass (no regression); source_v=None path skips the whole block unchanged.

Related: [[rc2a-target-rel-annotation-strip]] (rc2a test_annotation_with_backslash_path also fails — separate fix scope, regex ordering: backslash normalize happens AFTER annotation strip so `(NEW)/` doesn't match).
