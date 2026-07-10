# Official EXE Certification Policy

The Windows national platform is a compliance oracle. It never contributes to
Glicko, H2H, source-selection strength, or strategy win-rate scores.

## Full Certificate

`official-full-v2` is immutable:

- five self-play rounds;
- three rounds against an eligible native opponent;
- exactly 70 hands per round;
- THP, raw wire events, replay summary, bot logs, stdout/stderr, platform log,
  and screenshots retained for every round;
- deterministic evidence passes with no protocol, communication, timeout,
  state-machine, or obvious decision-state violation;
- the bounded LLM compliance analysis completes with a pass verdict and at
  least 0.5 confidence.

A full certificate binds all of the following:

- candidate artifact hash;
- opponent artifact hash;
- exact full profile and policy ID;
- official EXE, harness, certification service, Wine profile, timeouts, and UI
  fingerprint;
- deterministic evidence hash;
- LLM analysis hash.

The candidate and opponent hashes are captured before the EXE starts and
recomputed after it exits. A change makes the run inconclusive and prevents
caching. Before Git commit, the candidate hash is checked again. The annotated
`national-bot-vN` tag records `official-certificate`,
`official-candidate-hash`, and `official-policy` fields. A published bot is
formally certified only when the mutable status, immutable certificate record,
current artifact, and tag annotation all agree.

## Eligibility Roles

Eligibility is role-specific:

- `official_opponent`: may be used in the EXE full suite;
- `parent_source`: may seed a new generation or crossover;
- `rating_pool`: may participate in the active native rating inventory.

New candidates must use a content-bound full certificate. They cannot be
grandfathered.

Historical transition grants live in the tracked
`web/core/official_grandfathering.json` policy. A grant binds an annotated
completion tag, the currently published bot artifact hash, allowed roles, and
a sunset generation. It never changes or replaces official evidence status.
Known blocking official evidence overrides every grant.

Automatic bootstrap grandfathering is forbidden. The temporary v142 official
opponent grant exists only to bootstrap the first `official-full-v2`
certificate and expires by policy. Once a formally certified opponent exists,
it has higher selection priority.

## Migration Safety

The tracked transition allowlist preserves the current 30-bot inventory while
historical certification proceeds. This avoids deleting ratings and H2H state
all at once. It also prevents loss of the runtime reaped ledger from reviving
every historical tag: a legacy bot outside the tracked allowlist remains
ineligible.

Historical bots should be recertified in batches. A failed bot moves out of
eligible roles but its source, old ratings, and evidence remain available for
audit. The validator is never relaxed to keep a historical bot active.
