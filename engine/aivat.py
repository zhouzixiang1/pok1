"""AIVAT (All-In Value Adjustment) variance reduction for heads-up mirror battles.

Post-processes judge display logs to replace the luck-of-the-deal component of
all-in-showdown hands with their skill-equivalent expected value. Composes
cleanly with hole-swap mirroring and requires NO change to judge.py / battle.py
semantics: the feature flag `aivat_enabled` (default False) gates the entire
adjustment, so OFF = bit-identical Phase-0 behavior.

Detection model (heads-up, single effective all-in per hand):
    judge.py enforces that the second consecutive all-in auto-folds
    (`if self.allin_occurred: return player_action(FOLD)`, judge.py:298-300).
    Therefore each hand that reaches showdown-via-allin has exactly ONE all-in
    action, after which remaining community cards are run out deterministically.
    Folded-settled hands (no allin-showdown) have no chance element to remove
    and are left at their realized value.

AIVAT adjustment per all-in-showdown hand (perspective = player p):
    pot       = pot at the all-in display snapshot (both players' contributions)
    equity_p  = aivat_equity(hole_p, hole_opp, public, round)   # tie = 0.5
    contrib_p = 20000 - player_chips_p_at_snapshot              # chips p put in
    expected_p_delta = equity_p * pot - contrib_p                # skill EV delta

    The hand's realized net-chips delta for player p is replaced wholesale by
    `expected_p_delta` (in heads-up single-allin the entire hand settlement IS
    the all-in-runout settlement). Non-allin / fold-settled hands keep their
    realized delta.

Symmetry with hole-swap mirroring:
    adjusted_net_chips_p = sum over all hands (normal + mirror) of adjusted delta.
    The mirror hand swaps hole cards, so mirror equity ≈ (1 - normal equity)
    from p's perspective, preserving the cancellation that makes hole-swap a
    variance-reducer. AIVAT removes the residual runout variance on top.
"""
import os
import sys

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)

from judge import (
    Card,
    find_max_hand_type,
    compare_full_cards,
    HandType,
    compare_cards_for_hand_type,
)

INITIAL_CHIPS = 20000  # judge.py:504 — each hand resets to this


# ────────────────────────────────────────────────────────────────────────────
# Equity computation
# ────────────────────────────────────────────────────────────────────────────

def _river_equity(hole_me, hole_opp, public):
    """Exact equity on the river / full board (5 public cards). tie = 0.5."""
    seven_me = [Card.from_int(c) for c in (list(hole_me) + list(public))]
    seven_opp = [Card.from_int(c) for c in (list(hole_opp) + list(public))]
    cmp = compare_full_cards(seven_me, seven_opp)  # >0 me win, ==0 tie, <0 lose
    if cmp == 0:
        return 0.5
    return 1.0 if cmp > 0 else 0.0


def _mc_equity(hole_me, hole_opp, public, n_sims=2000, seed=None):
    """Monte Carlo equity for non-river all-in. tie = 0.5.

    Both players' hole cards are KNOWN at the all-in moment (both exposed when
    the all-in is called and the hand goes to showdown). So we sample ONLY the
    remaining community cards uniformly from the unseen deck, evaluate both
    fixed 7-card hands with the authoritative judge evaluator, and average.
    Ties are resolved exactly via compare_cards_for_hand_type when hand types
    match (heads-up ties ~3-4%).
    """
    import numpy as np
    rng = np.random.default_rng(seed)
    hole_me = list(hole_me)
    hole_opp = list(hole_opp)
    public = list(public)
    seen = set(hole_me) | set(hole_opp) | set(public)
    deck = [c for c in range(52) if c not in seen]
    arr = np.array(deck, dtype=np.int64)
    need = 5 - len(public)        # community cards left to draw
    if need <= 0:
        return _river_equity(hole_me, hole_opp, public)
    if len(arr) < need:
        return 0.5  # degenerate (shouldn't happen in legal play)

    score = 0.0
    for _ in range(n_sims):
        idx = rng.choice(len(arr), size=need, replace=False)
        sim_public = public + [int(arr[i]) for i in idx]
        seven_me = [Card.from_int(c) for c in hole_me + sim_public]
        seven_opp = [Card.from_int(c) for c in hole_opp + sim_public]
        ht_me, cards_me = find_max_hand_type(seven_me)
        ht_opp, cards_opp = find_max_hand_type(seven_opp)
        if ht_me.value > ht_opp.value:
            score += 1.0
        elif ht_me.value == ht_opp.value:
            # Same hand type: resolve exactly via the judge comparator.
            # cmp>0 me wins, cmp==0 tie (split), cmp<0 me loses.
            cmp = compare_cards_for_hand_type(cards_me, cards_opp, ht_me)
            if cmp > 0:
                score += 1.0
            elif cmp == 0:
                score += 0.5
            # else cmp<0 → opp wins → 0
        # else opp wins → 0
    return score / n_sims


def aivat_equity(hole_me, hole_opp, public_cards, round_num, n_sims=2000, seed=None):
    """Heads-up showdown equity ∈ [0,1] for `hole_me` vs `hole_opp` on `public_cards`.

    - River / full 5-card board (len(public_cards)==5): exact via compare_full_cards.
      NOTE: this equals the deterministic river all-in outcome, so the AIVAT
      adjustment is a near no-op on river all-ins (no variance to remove) — benign.
    - Non-river (0..4 public cards): numpy Monte Carlo reusing find_max_hand_type.

    Args:
        hole_me: list[int] (len 2) — my hole cards (int 0..51).
        hole_opp: list[int] (len 2) — opponent hole cards.
        public_cards: list[int] (len 0..5) — current community cards.
        round_num: int (0=preflop..3=river); informational, dispatch is by len(public).
        n_sims: int — MC sample count for non-river (default 2000 → SE ~1.1%).
        seed: optional RNG seed for deterministic MC.

    Returns:
        float in [0,1]: P(I win) + 0.5 * P(tie).
    """
    public_cards = list(public_cards)
    if len(public_cards) >= 5:
        return _river_equity(hole_me, hole_opp, public_cards)
    return _mc_equity(hole_me, hole_opp, public_cards, n_sims=n_sims, seed=seed)


# ────────────────────────────────────────────────────────────────────────────
# Judge log → per-hand all-in adjustment
# ────────────────────────────────────────────────────────────────────────────

def _extract_allin_snapshot(judge_log, consumed_history_len_ref=None):
    """Scan a single hand's judge log (list of {"output":...}) for the all-in
    showdown moment, and return the equity-relevant snapshot.

    Detection (robust for this engine where every hand resets both players to
    INITIAL_CHIPS=20000, so a heads-up all-in-showdown ALWAYS commits both
    stacks to 0):

      A hand is an AIVAT-eligible all-in-showdown iff BOTH players reach 0 chips
      by the hand's final request display (mutual all-in → runout to showdown).
      - allin-vs-call   → final chips [0,0], pot=40000   → ELIGIBLE (adjust).
      - allin-vs-fold   → final chips [0, >0]            → NOT eligible (no
                           chance event; folder forfeits, keep realized).
      - fold-settled    → no player at 0 chips           → NOT eligible.

    We capture the all-in decision point as the FIRST display where any player
    hits 0 chips (that's where the all-in was committed and the equity question
    is decided: remaining community cards are run out afterward with no further
    betting). The hole cards / public / round at that snapshot are the AIVAT
    equity inputs.

    Returns dict {round, public, hole_me, hole_opp, pot, contrib_me, contrib_opp}
    or None if the hand is not an all-in-showdown.
    """
    # Walk request-displays. Track first allin-touch and the final display.
    allin_snapshot = None
    last_display = None
    for entry in judge_log:
        out = entry.get("output") if isinstance(entry, dict) else None
        if out is None or not isinstance(out, dict):
            continue
        display = out.get("display")
        if not isinstance(display, dict):
            continue
        last_display = display
        chips = display.get("player_chips")
        if (allin_snapshot is None and isinstance(chips, list) and len(chips) == 2
                and (chips[0] == 0 or chips[1] == 0)):
            allin_snapshot = display

    if allin_snapshot is None or last_display is None:
        return None

    # Eligibility: BOTH players must be at 0 chips in the final display (mutual
    # all-in → showdown). If only one is at 0, the hand settled by fold (the
    # non-allin player folded rather than calling), so no chance event → skip.
    final_chips = last_display.get("player_chips")
    if not (isinstance(final_chips, list) and len(final_chips) == 2
            and final_chips[0] == 0 and final_chips[1] == 0):
        return None

    # Also exclude hands that ended by explicit fold in round_player_bet (defensive).
    rpb = last_display.get("round_player_bet")
    if isinstance(rpb, list) and any(b == -1 for b in rpb):
        return None

    hole = last_display.get("player_cards")
    if not hole or len(hole) < 2:
        return None
    pot = int(last_display.get("pot", 0))
    chips = last_display.get("player_chips", [INITIAL_CHIPS, INITIAL_CHIPS])

    # Equity decision point = the first all-in snapshot (where betting stopped).
    snap_public = allin_snapshot.get("public_cards") or []
    snap_round = allin_snapshot.get("round", 0)
    snap_hole = allin_snapshot.get("player_cards")
    if not snap_hole or len(snap_hole) < 2:
        snap_hole = hole
        snap_public = last_display.get("public_cards") or []
        snap_round = last_display.get("round", 0)

    contrib_me = INITIAL_CHIPS - int(chips[0])
    contrib_opp = INITIAL_CHIPS - int(chips[1])

    return {
        "round": snap_round,
        "public": list(snap_public),
        "hole_me": list(snap_hole[0]),
        "hole_opp": list(snap_hole[1]),
        "pot": pot,
        "contrib_me": contrib_me,
        "contrib_opp": contrib_opp,
    }


def aivat_adjust_hand(judge_log_hand, perspective=0, n_sims=2000, seed=None):
    """AIVAT-adjusted net-chips delta for ONE hand, for `perspective` player.

    Args:
        judge_log_hand: list of judge log entries for a single hand (the segment
            of the full game log belonging to one hand; see split_log_into_hands).
        perspective: 0 or 1 — which player's delta to return.

    Returns:
        float: the adjusted net-chips delta for `perspective` on this hand.
        - All-in-showdown hand: equity_p * pot - contrib_p  (skill EV).
        - Fold/non-allin hand: the realized net-chips delta for `perspective`
          (taken from the hand's finish display final_result if present, else 0).
    """
    snap = _extract_allin_snapshot(judge_log_hand, None)

    # Find the hand's realized result (from the finish/last display final_result)
    realized = _hand_realized_delta(judge_log_hand, perspective)

    if snap is None:
        return realized

    # All-in-showdown: replace realized with skill EV.
    if perspective == 0:
        hole_me, hole_opp = snap["hole_me"], snap["hole_opp"]
        contrib_p = snap["contrib_me"]
    else:
        hole_me, hole_opp = snap["hole_opp"], snap["hole_me"]
        contrib_p = snap["contrib_opp"]

    equity = aivat_equity(hole_me, hole_opp, snap["public"], snap["round"],
                          n_sims=n_sims, seed=seed)
    expected_delta = equity * snap["pot"] - contrib_p
    return expected_delta


def _hand_realized_delta(judge_log_hand, perspective):
    """Extract the realized net-chips delta for `perspective` from a hand's log.

    The finish display's final_result[i].win_chips is already mean-centered
    (judge.py:570: win_chips = chips - mean_chips). For a hand, that is exactly
    the per-hand delta. Falls back to 0 if no final_result found (e.g. the log
    segment is incomplete — shouldn't happen for finished games).
    """
    for entry in reversed(judge_log_hand):
        out = entry.get("output") if isinstance(entry, dict) else None
        if out is None or not isinstance(out, dict):
            continue
        display = out.get("display")
        if isinstance(display, dict) and "final_result" in display:
            fr = display["final_result"]
            if isinstance(fr, list) and len(fr) > perspective:
                wc = fr[perspective].get("win_chips", 0)
                try:
                    return float(wc)
                except (TypeError, ValueError):
                    return 0.0
    return 0.0


def split_log_into_hands(full_log):
    """Split a full game judge log (one mirror game = up to 70 hands) into
    per-hand segments.

    A hand boundary occurs when matchdata.hand increments (judge.py:520-525) or
    when the game finishes. We segment by tracking the `hand` field across
    request-displays. Each segment is the list of log entries belonging to one
    hand (including its finish display).

    full_log: list alternating [{"output":...}, {pid:{...},"output":None}, ...].
    Returns: list of per-hand log segments (each a list of entries).
    """
    hands = []
    current = []
    current_hand_idx = None
    finished = False

    for entry in full_log:
        current.append(entry)
        out = entry.get("output") if isinstance(entry, dict) else None
        if out is None or not isinstance(out, dict):
            continue
        display = out.get("display")
        if not isinstance(display, dict):
            continue
        md = display.get("matchdata")
        hand_idx = md.get("hand") if isinstance(md, dict) else None
        if hand_idx is None:
            continue
        if current_hand_idx is None:
            current_hand_idx = hand_idx
        if hand_idx != current_hand_idx:
            # Hand advanced: the current segment (up to but not including the
            # first entry of the new hand) is one hand. Re-attach this entry to
            # the next segment.
            new_current = [current.pop()]
            hands.append(current)
            current = new_current
            current_hand_idx = hand_idx
        # Detect finish (last hand)
        if out.get("command") == "finish":
            finished = True

    if current:
        hands.append(current)
    return hands


def aivat_adjust_game(full_log, perspective=0, n_sims=2000, seed=None):
    """Sum of AIVAT-adjusted per-hand deltas for one full game log, for
    `perspective` player. This replaces summing raw win_chips across hands."""
    total = 0.0
    for hand_seg in split_log_into_hands(full_log):
        total += aivat_adjust_hand(hand_seg, perspective=perspective,
                                   n_sims=n_sims, seed=seed)
    return total


def aivat_net_chips_pair(normal_log, mirror_log, n_sims=2000, seed=None):
    """AIVAT-adjusted net_chips_0 for one mirror pair.

    Mirrors the Phase-0 definition:
        net_chips_0 = chips_normal[0] + chips_mirror[0]
    but with each game's bot-0 sum replaced by the AIVAT-adjusted sum over hands.

    normal_log / mirror_log: full judge logs for the normal and mirror games.

    Determinism: pass `seed` for reproducible MC. If None, MC uses fresh entropy
    (acceptable for the variance-reduction purpose; CI consumers treat the
    adjusted stream statistically).
    """
    normal_adj = aivat_adjust_game(normal_log, perspective=0, n_sims=n_sims, seed=seed)
    mirror_adj = aivat_adjust_game(mirror_log, perspective=0, n_sims=n_sims, seed=seed)
    return normal_adj + mirror_adj
