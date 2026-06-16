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
    matched_contrib = min(contrib_0, contrib_1)
    main_pot        = 2 * matched_contrib                       # side pot excluded
    equity_p        = aivat_equity(hole_p, hole_opp, public, round)  # tie = 0.5
    expected_p_delta = equity_p * main_pot - matched_contrib     # skill EV delta

    Stack-mismatch all-ins can leave an unmatched side pot that judge.py returns
    to the covering player at settlement; that side pot has no runout chance
    component and must not enter the equity adjustment. Nonallin/fold-settled
    hands keep their realized delta.

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

    Detection (real-log grounded — see 200-game replay scan):

      A hand is an AIVAT-eligible all-in-showdown iff:
        1. At least one player hit 0 chips during the hand (an all-in was
           committed). This covers BOTH mutual all-in (final chips [0,0]) and
           the far more common stack-mismatch all-in (final chips [0, X>0] or
           [X>0, 0], where one player covered the other's shove). The old code
           required BOTH at 0, which real logs show matches only ~5% of all-in
           showdowns (stack-mismatch preflop shoves dominate).
        2. The hand reached showdown with NO fold: the FINAL request display has
           no -1 in `round_player_bet` (a -1 means someone folded → no chance
           event → keep realized). All-in-then-fold hands are excluded here.
        3. The board was run out to the river (5 public cards in the final
           display). This is the real "reached showdown via all-in" criterion
           (postflop round==3 == river with 5 cards; round==4 is the game-finish
           terminal frame only). Preflop all-ins whose board never completed
           (shouldn't happen for legal mirror games, which always run out) are
           excluded defensively.

      - allin-vs-call (mutual)    → final chips [0,0], pot=2*min_contrib → ELIGIBLE
      - allin-vs-call (covered)   → final chips [0, X>0] or [X>0, 0]    → ELIGIBLE
      - allin-vs-fold             → only P0 at 0, P1 folded → no chance event → SKIP
      - fold-settled              → no player at 0 chips                 → SKIP

    We capture the all-in decision point as the FIRST display where any player
    hits 0 chips (that's where the all-in was committed and the equity question
    is decided: remaining community cards are run out afterward with no further
    betting). The hole cards / public / round at that snapshot are the AIVAT
    equity inputs.

    Returns dict {round, public, hole_me, hole_opp, pot, main_pot,
    matched_contrib, contrib_me, contrib_opp} or None if the hand is not an
    all-in-showdown.
    """
    # Walk request-displays. Track first allin-touch and the final display.
    # Skip settlement frames: a display carrying `temp_result` (or a finish
    # frame) is the NEXT hand's first display rendering THIS hand's settlement
    # (judge.py:577-578 increments `hand` before make_request_json attaches the
    # result). Its player_chips/public/pot describe the next hand's reset state,
    # NOT this hand's runout — including it would corrupt eligibility & snapshot.
    allin_snapshot = None
    last_display = None
    for entry in judge_log:
        out = entry.get("output") if isinstance(entry, dict) else None
        if out is None or not isinstance(out, dict):
            continue
        display = out.get("display")
        if not isinstance(display, dict):
            continue
        if isinstance(display.get("temp_result"), list) or out.get("command") == "finish":
            continue  # settlement/terminal frame — not a betting display of this hand
        last_display = display
        chips = display.get("player_chips")
        if (allin_snapshot is None and isinstance(chips, list) and len(chips) == 2
                and (chips[0] == 0 or chips[1] == 0)):
            allin_snapshot = display

    if allin_snapshot is None or last_display is None:
        return None

    # Eligibility #1: an all-in was committed (≥1 player reached 0 chips). The
    # walk above guarantees allin_snapshot is set iff this holds; re-check
    # defensively against the final display too.
    final_chips = last_display.get("player_chips")
    if not (isinstance(final_chips, list) and len(final_chips) == 2
            and (final_chips[0] == 0 or final_chips[1] == 0)):
        # The all-in snapshot may have been at 0 but chips changed afterwards
        # (shouldn't happen post-allin). Require ≥1 player at 0 in the FINAL
        # display for a genuine all-in-runout-to-showdown.
        return None

    # Eligibility #2: no fold in the final display's round_player_bet
    # (-1 == folded). A fold means the hand settled without a runout → no
    # chance event to AIVAT-remove.
    rpb = last_display.get("round_player_bet")
    if isinstance(rpb, list) and any(b == -1 for b in rpb):
        return None

    # Eligibility #3: board run out to the river (5 public cards) → genuine
    # showdown with a chance event on the runout. round==4 is the game-finish
    # terminal frame only, not a playable river.
    pub = last_display.get("public_cards") or []
    if len(pub) != 5:
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
    matched_contrib = min(contrib_me, contrib_opp)
    main_pot = 2 * matched_contrib

    return {
        "round": snap_round,
        "public": list(snap_public),
        "hole_me": list(snap_hole[0]),
        "hole_opp": list(snap_hole[1]),
        "pot": pot,
        "main_pot": main_pot,
        "matched_contrib": matched_contrib,
        "contrib_me": contrib_me,
        "contrib_opp": contrib_opp,
    }


def aivat_adjust_hand(judge_log_hand, perspective=0, n_sims=2000, seed=None,
                      realized_delta=None):
    """AIVAT-adjusted net-chips delta for ONE hand, for `perspective` player.

    Args:
        judge_log_hand: list of judge log entries for a single hand (the segment
            of the full game log belonging to one hand; see split_log_into_hands).
        perspective: 0 or 1 — which player's delta to return.
        realized_delta: optional caller-supplied per-hand realized delta. Used by
            aivat_adjust_game to repair the terminal hand, whose final_result is
            cumulative rather than per-hand.

    Returns:
        float: the adjusted net-chips delta for `perspective` on this hand.
        - All-in-showdown hand: equity_p * main_pot - matched_contrib.
          Only the matched main pot is adjusted; unmatched side-pot chips are
          returned deterministically and have no runout chance component.
        - Fold/non-allin hand: the realized net-chips delta for `perspective`
          from per-hand temp_result. Full-game callers repair the terminal
          cumulative final_result in aivat_adjust_game.
    """
    snap = _extract_allin_snapshot(judge_log_hand, None)

    # Find the hand's realized result (from the finish/last display final_result)
    realized = _hand_realized_delta(judge_log_hand, perspective) if realized_delta is None else float(realized_delta)

    if snap is None:
        return realized

    # All-in-showdown: replace realized with skill EV for the matched main pot.
    # In stack-mismatch all-ins, the unmatched side pot is returned to the
    # covering player by judge.py settlement and has no runout chance component.
    if perspective == 0:
        hole_me, hole_opp = snap["hole_me"], snap["hole_opp"]
    else:
        hole_me, hole_opp = snap["hole_opp"], snap["hole_me"]

    equity = aivat_equity(hole_me, hole_opp, snap["public"], snap["round"],
                          n_sims=n_sims, seed=seed)
    expected_delta = equity * snap["main_pot"] - snap["matched_contrib"]
    return expected_delta


def _hand_realized_delta(judge_log_hand, perspective):
    """Extract the realized net-chips delta for `perspective` from a hand's log.

    Real-log grounded: the per-hand mean-centered delta lives in the
    `temp_result[i].win_chips` field that judge.py attaches to the FIRST display
    of the NEXT hand (judge.py:559-575): at hand settlement it computes
        player_final_chips = game.get_player_final_chips(result)   # side-pot adjusted
        mean_chips = sum(player_final_chips) / N
        temp_result[i].win_chips = player_final_chips[i] - mean_chips
    and stores that on matchdata, which the next make_request_json renders as
    `display.temp_result`. So THIS hand's realized delta is found by scanning
    the segment for a display carrying `temp_result`.

    IMPORTANT: do NOT use `player_chips[i] - INITIAL_CHIPS`. `player_chips` is
    the STAGE-END chips (pre-settlement, e.g. [0, 1] at the allin-runout's last
    betting display), NOT the side-pot-adjusted settlement chips. Using it gives
    the wrong delta for every stack-mismatch all-in (e.g. real [0,1]/pot=39999
    hand → player_chips[0]-20000 = -20000, but the true delta is -19999).

    `final_result[i].win_chips` (judge.py:484-491) is the GAME's cumulative
    running total (total_win_chips), available only on the game's final
    finish frame — it is NOT per-hand. Kept only as a last-resort fallback for
    the terminal hand where temp_result may be absent.

    Falls back to 0 if neither is found (incomplete segment — shouldn't happen
    for finished games).
    """
    for entry in judge_log_hand:
        out = entry.get("output") if isinstance(entry, dict) else None
        if out is None or not isinstance(out, dict):
            continue
        display = out.get("display")
        if not isinstance(display, dict):
            continue
        tr = display.get("temp_result")
        if isinstance(tr, list) and len(tr) > perspective:
            wc = tr[perspective].get("win_chips", 0) if isinstance(tr[perspective], dict) else 0
            try:
                return float(wc)
            except (TypeError, ValueError):
                return 0.0
    # Fallback: game-terminal frame's final_result (cumulative running total).
    for entry in reversed(judge_log_hand):
        out = entry.get("output") if isinstance(entry, dict) else None
        if out is None or not isinstance(out, dict):
            continue
        display = out.get("display")
        if isinstance(display, dict) and "final_result" in display:
            fr = display["final_result"]
            if isinstance(fr, list) and len(fr) > perspective:
                wc = fr[perspective].get("win_chips", 0) if isinstance(fr[perspective], dict) else 0
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
        out = entry.get("output") if isinstance(entry, dict) else None
        is_settlement_frame = (
            isinstance(out, dict)
            and isinstance(out.get("display"), dict)
            and (isinstance(out["display"].get("temp_result"), list)
                 or out.get("command") == "finish")
        )

        # If we haven't started any segment yet, this entry opens hand 0.
        if current_hand_idx is None:
            current.append(entry)
            if isinstance(out, dict) and isinstance(out.get("display"), dict):
                md = out["display"].get("matchdata")
                current_hand_idx = md.get("hand") if isinstance(md, dict) else None
            continue

        # Determine this entry's hand index, if it carries one.
        hand_idx = None
        if isinstance(out, dict) and isinstance(out.get("display"), dict):
            md = out["display"].get("matchdata")
            hand_idx = md.get("hand") if isinstance(md, dict) else None

        if hand_idx is not None and hand_idx != current_hand_idx and not is_settlement_frame:
            # Genuine first action frame of a new hand. Flush the current
            # segment as a complete hand, then start the new segment here.
            hands.append(current)
            current = []
            current_hand_idx = hand_idx

        if is_settlement_frame and hand_idx is not None and hand_idx != current_hand_idx:
            # Settlement frame for the CURRENT hand, but judge.py already
            # incremented `hand` to the next number before rendering it
            # (judge.py:577-578 → make_request_json attaches temp_result to the
            # next hand's first display). Attach it to the current segment
            # WITHOUT advancing current_hand_idx, so the real next-hand action
            # frame still triggers the boundary above.
            current.append(entry)
            continue

        current.append(entry)
        if out is not None and isinstance(out, dict) and isinstance(out.get("display"), dict):
            md = out["display"].get("matchdata")
            if isinstance(md, dict) and md.get("hand") is not None and not is_settlement_frame:
                current_hand_idx = md["hand"]
        # Detect finish (last hand)
        if out is not None and isinstance(out, dict) and out.get("command") == "finish":
            finished = True

    if current:
        hands.append(current)
    return hands


def aivat_adjust_game(full_log, perspective=0, n_sims=2000, seed=None):
    """Sum of AIVAT-adjusted per-hand deltas for one full game log, for
    `perspective` player. This replaces summing raw win_chips across hands."""
    hands = split_log_into_hands(full_log)
    if not hands:
        return 0.0

    # temp_result gives per-hand deltas for all settled nonterminal hands. The
    # game-finish final_result is cumulative total_win_chips, so derive the
    # terminal hand's realized delta by subtracting prior per-hand deltas before
    # handing it to aivat_adjust_hand. This prevents double-counting the whole
    # game's running total on the last nonallin hand.
    realized = [_hand_realized_delta(hand_seg, perspective) for hand_seg in hands]
    final_total = None
    for entry in reversed(hands[-1]):
        out = entry.get("output") if isinstance(entry, dict) else None
        display = out.get("display") if isinstance(out, dict) else None
        if isinstance(display, dict):
            fr = display.get("final_result")
            if isinstance(fr, list) and len(fr) > perspective:
                wc = fr[perspective].get("win_chips", 0) if isinstance(fr[perspective], dict) else 0
                try:
                    final_total = float(wc)
                    break
                except (TypeError, ValueError):
                    pass
    if final_total is not None:
        realized[-1] = final_total - sum(realized[:-1])

    total = 0.0
    for hand_seg, realized_delta in zip(hands, realized):
        total += aivat_adjust_hand(hand_seg, perspective=perspective,
                                   n_sims=n_sims, seed=seed,
                                   realized_delta=realized_delta)
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
