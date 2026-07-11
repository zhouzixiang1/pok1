# Official hand-70 settlement oracle (2026-07-11)

This controlled formal-sandbox run records the natural end of a 70-hand match
on the official 2021 Windows EXE. It exists to define completion evidence, not
to measure bot strength.

## Reproduction

- Candidate: `bots/national_v141`, self-play round 1.
- Official job: `dca6bdd0e10a5cdb73b6195913369f8ddb19a3aaf9dcd9c62f3d97e761e7deb4`.
- EXE SHA-256: `9d01b443d4920a7e06a487d87ea1b050ea2ca5359023602f98c3c236c734e81a`.
- Formal profile: Bubblewrap isolated network namespace with one inherited,
  preconnected proxy socket per bot.
- Raw wire SHA-256: `ca6e29cee830740ab511f06a3231df39edde26229529fc91bcc8a1c4a482d234`.
- Official THP SHA-256: `c70b60ac80375a2bf41fa72825bd91358cc48c5369eaee778aec0dd10226ca50`.

Both seats received preflop starts for hands 1 through 70. Both received signed,
zero-sum `earnChips` for exactly hands 1 through 69. On hand 70, BotA sent
`fold` and BotB received it at 17:22:22, but neither client received another
`earnChips`. The EXE wrote the stable THP at 17:22:25.

The THP is strict and complete:

- states are exactly `STATE:0` through `STATE:69`, without gaps or duplicates;
- the named earnings in states 0..68 match every named wire settlement for
  hands 1..69;
- `STATE:69:f:Jc9s|3cTs:50|-50:BotB|BotA;` supplies the terminal result;
- BotA's 69 wire settlements sum to `+19721`; state 69 contributes `-50`;
- all 70 states therefore total BotA `+19671`, BotB `-19671`;
- the footer is `BotA赢得19671个筹码`, exactly matching those totals.

The same behavior appears in all eight archived 2026-07-10 rounds: 560 starts,
552 paired TCP settlements (69 per round), and eight complete 70-state THPs.

## Resulting invariant

Formal policy `official-full-v5` does not weaken the network target to 69. A
terminal round passes only when the exact 1..70/1..69 wire boundary has no
pending action or wire issue and a newly written official THP independently
proves state 69. The proof cross-binds every first-69 named earning, final
zero-sum earnings, cumulative totals, footer result, raw-wire hash, and THP
hash. Missing or mismatched evidence remains a failure/inconclusive result.

Official chip totals and winners remain compliance evidence only. They are not
fed into Glicko, H2H, selection score, precommit strength, or experience.
