# v142 national_v123 old-pool probe TCP

Parent: `v141_national_v123_profile_veto_tcp`.

This version keeps the v123 rule base plus the v141 neural overlay and disables
one bad late-flop free-action veto found by native TCP trace/force probes.

The disabled guard is:

- `flop_late_weak_highcard_free_raise_check_enabled`

Evidence:

- v3 seed 2026074500 hand 65: the v141 guard changed a rule `raise 4958` into
  `check`; the following line induced a losing flop all-in. Forcing the rule
  raise changed the paired result from `-993` to `+12124`.
- v2 seed 2026074501 hand 64 showed the same Q6 high-card late-flop pattern.

No new positive action is introduced. The version simply lets the existing rule
base's already-sanitized national raise-to-total action stand in that spot.
