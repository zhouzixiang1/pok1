# Official EXE Re-raise Boundary Oracle (2026-07-11)

## Result

The official Windows EXE accepts an exact 2x consecutive raise-to total.
After `raise 200`, `raise 400` is legal. Values below 400 are illegal under
this boundary. `raise 401` remains legal but is not the minimum.

Generated native bots and the legacy adapter may retain `2x + 1` as
conservative sizing headroom. That implementation policy must not be described
as an official legality requirement.

## Controlled Evidence

The scripted two-seat probe alternated roles across hands:

1. Small blind sent `raise 200`.
2. Big blind sent exact `raise 400`.
3. The official EXE relayed `raise 400` to the small blind.
4. Small blind sent `fold`.
5. Both seats received zero-sum `earnChips -200` / `earnChips 200`.

The sequence succeeded with both connections acting as the re-raiser. A
separate `raise 401` control also succeeded.

Evidence identity:

- observed locally: `2026-07-11T16:38:01+08:00`;
- official EXE SHA-256:
  `9d01b443d4920a7e06a487d87ea1b050ea2ca5359023602f98c3c236c734e81a`;
- exact-2x raw `wire_events.jsonl` SHA-256:
  `dc9dffa1121bee77bab1478842b7f336e1d4a72686e2ad7cbf322ed077bf85f3`;
- 2x-plus-one control raw `wire_events.jsonl` SHA-256:
  `ab2828becfc470749a33da92434c6fd812a30e7ec80683613b7a40c605ff28be`.

The checked-in regression fixture is a minimal event projection with no host
paths or screenshots:
`sever/tests/fixtures/official_raise_boundary_oracle_20260711.json`. The raw
runtime captures remain outside Git; their hashes bind this conclusion to the
original evidence.

## System Consequence

- `sever/engine/validator.py` and the official wire replay reject only values
  below `2 * previous_raise_to`.
- Active national-generation prompts state the inclusive boundary.
- `2x + 1` sanitizers and compatibility fixers are labeled as conservative
  headroom, not as proof that exact 2x is illegal.
- Official EXE evidence remains compliance-only and never contributes to
  ratings, H2H, source selection, or strength scores.
