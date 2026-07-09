# v141 national_v123 profile veto TCP

Parent: `v140_national_v123_overlay_no_large_commit_veto_tcp`.

This version keeps the v123 rule base plus the v140 neural overlay and adds
three narrow profile-gated vetoes from native TCP trace analysis:

- Late KJo 3bet/call-off spot versus rare preflop all-in profiles now folds
  instead of calling a 190bb jam.
- Medium-pair limp/reraise spots now call instead of making a large 4bet when
  the opponent profile matches the v2 hard-negative trace.
- Weak unpaired high-card flops avoid all-in over small leads and avoid large
  free-action raises late in the match against high postflop-pressure profiles.

All new overrides return only `fold`, `call`, or `check`; no new positive
raise action is introduced.
