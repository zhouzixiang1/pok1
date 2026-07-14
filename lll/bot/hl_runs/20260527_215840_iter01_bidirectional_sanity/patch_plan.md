# Patch Plan Draft

This file is intentionally advisory. It does not modify `1.py`.

## Priority

- Candidate looks promising. Prefer adding regression cases before merging strategy edits.

## Suggested Focus

- Review `fold_gives_opponent_lock` gates and `choose_anti_lock_pressure_action` sizing on extracted anti-lock hands.
- Inspect check-probe hands; tighten wetness/fold-to-raise conditions only if extracted losses cluster there.
- Start with the highest-loss hands in `interesting_hands.jsonl`; classify whether loss came from preflop defense, postflop stackoff, or missed value.

## Regression Checklist

- `sanitize_action` keeps every returned action legal.
- Ordinary non-anti-lock trash-hand and non-3bet discipline remains conservative.
- Anti-lock remains a scoped exception when folding gives the opponent a lock.
- Consecutive-check `100` probe / steal-pot behavior remains available.
- Replay cases use full Botzone request payloads.
