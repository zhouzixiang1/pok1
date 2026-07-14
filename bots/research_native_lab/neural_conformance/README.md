# Neural conformance substrate

This package contains only route-neutral correctness helpers used to compare
independent neural poker experiments.  It owns no labels, strategy, model,
normalizer, checkpoint, route seed, solver or training artifact.

`public_family.py` canonicalizes a nonterminal Common `NationalGameState` under
global suit permutations while retaining the complete single-hand betting
state and exact legal-action support.  Flop cards are unordered; turn and river
positions remain ordered.  Hole cards, hand number and match context never
enter the family identity.

`split.py` freezes a complete pre-label dataset into leakage-closed components.
Public family, trajectory, rollout, augmentation, duplicate-PBS and source-copy
relations are unioned transitively.  A generator checkpoint is provenance, not
a global edge.  Every ID is a content digest and every family payload is
replayed through Common before its registry digest is accepted.

The generated manifest is never its own authority.  Its verifier requires an
externally supplied `SplitAuthority` and separately pinned authority digest.
That authority commits the route, route salt, split thresholds, complete
pre-label record set, relation graph, Common-derived family registry and
minimum split sizes.  Editing a record or omitting an edge and then rebuilding
a self-consistent manifest does not verify against the old authority.

Each public family receives a stable route-salted base split.  A transitive
component uses the most restrictive member assignment (`test > validation >
train`), so union cannot downgrade heldout data.  The monotonic-extension gate
allows a later round only when every previously frozen record, family payload
and sample split is unchanged.  A cross-split bridge therefore invalidates the
extension instead of moving an old test/validation sample into training.

Routes may share these algorithms and schemas, but each route must use its own
registered domain, salt, provenance graph, record set and manifest.  Cross-route
reuse is explicitly rejected.  A route must not read another route's dataset,
labels, test results, normalizer or model.

This code can prove content relationships only after the route gate pins the
authority digest before labeling.  It is not a clock, signature service or
formal blind-data authority; those remain outside the current host.
