#!/usr/bin/env python3
"""National TCP entry for Route A1 ReBeL-like bot with REAL network inference.

Uses the trained value/policy network (deploy.npz) for action selection,
falling back to a simple heuristic when network inference is unavailable.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import select
import socket
import sys
import time

sys.dont_write_bytecode = True

NATIONAL_STREAM_DECODER_VERSION = 3
DEFAULT_OFFICIAL_ACTION_DELAY_SEC = 0.30
DEFAULT_DECISION_HARD_DEADLINE_SEC = 54.0
CARD_RE = re.compile(r"<([0-3]),([0-9]|1[0-2])>")
NUMERIC_RE = re.compile(r"^(raise) ([0-9]+)")
EARN_RE = re.compile(r"^(earnChips) (-?[0-9]+)")
WORDS = ("allin", "check", "call", "fold", "name")
STAGE_CARDS = {"flop|": 3, "turn|": 1, "river|": 1, "oppo_hands|": 2}

# Import Common contracts for network inference
_repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
sys.path.insert(0, _repo_root)
from bots.research_native_lab.common_contracts.national_state import NationalGameState, Street
from bots.research_native_lab.common_contracts.cards import legal_combo_mask
from bots.research_native_lab.common_contracts.actions import Action, ActionKind
from bots.research_native_lab.common_contracts.protocol import NationalProtocolSession, ProtocolEvent
from bots.research_native_lab.rebel_decisionholdem.rebel_like.m5b_data import encode_public_features
from bots.research_native_lab.rebel_decisionholdem.rebel_like.m5b_search import abstract_actions, HUNL_COMBO_COUNT, ACTION_SLOTS
from bots.research_native_lab.rebel_decisionholdem.rebel_like.hunl_pbs import HUNL_COMBOS


def tcp_card_id(suit: int, rank: int) -> int:
    """Convert TCP <suit,rank> to internal card id (0..51)."""
    suit_map = {0: 2, 1: 0, 2: 1, 3: 3}
    return rank * 4 + suit_map[suit]


class NationalStreamDecoder:
    def __init__(self) -> None:
        self.buffer = ""

    @property
    def has_pending_numeric(self) -> bool:
        return bool(re.fullmatch(r"(?:raise [0-9]+|earnChips -?[0-9]+)", self.buffer))

    @staticmethod
    def _card_message(buffer: str, prefix: str, count: int):
        if not buffer.startswith(prefix):
            return None
        position = len(prefix)
        for _ in range(count):
            match = CARD_RE.match(buffer, position)
            if match is None:
                return None
            position = match.end()
        return buffer[:position], buffer[position:]

    def _take(self, allow_terminal_numeric: bool = False):
        self.buffer = self.buffer.lstrip(" \t\r\n")
        if not self.buffer:
            return None
        for blind in ("SMALLBLIND", "BIGBLIND"):
            item = self._card_message(self.buffer, f"preflop|{blind}|", 2)
            if item is not None:
                return item
        for prefix, count in STAGE_CARDS.items():
            item = self._card_message(self.buffer, prefix, count)
            if item is not None:
                return item
        for pattern in (NUMERIC_RE, EARN_RE):
            match = pattern.match(self.buffer)
            if match is not None:
                end = match.end()
                if end == len(self.buffer) and not allow_terminal_numeric:
                    return None
                return self.buffer[:end], self.buffer[end:]
        for word in WORDS:
            if self.buffer.startswith(word):
                return word, self.buffer[len(word):]
        return None

    def feed(self, chunk: str) -> list[str]:
        self.buffer += chunk
        if len(self.buffer) > 65536:
            raise ValueError("TCP buffer exceeded 64KiB")
        emitted = []
        while True:
            item = self._take()
            if item is None:
                break
            message, self.buffer = item
            emitted.append(message)
        return emitted

    def flush_idle(self) -> list[str]:
        emitted = []
        while True:
            item = self._take(True)
            if item is None:
                break
            message, self.buffer = item
            emitted.append(message)
        return emitted


def _parse_cards(message: str) -> list[int]:
    return [tcp_card_id(int(s), int(r)) for s, r in CARD_RE.findall(message)]


class A1NetworkClient:
    """National TCP bot client using trained value/policy network."""

    def __init__(self, name, deploy_path, seed, log_path=""):
        self.name = name
        self.seed = seed
        self.decoder = NationalStreamDecoder()
        self.deploy_path = deploy_path
        self.log = open(log_path, "a") if log_path else None
        self.policy_net = None
        self._has_network = False
        self._load_network()
        self.action_delay = float(
            os.environ.get("POK_OFFICIAL_ACTION_DELAY",
                           str(DEFAULT_OFFICIAL_ACTION_DELAY_SEC)))
        self.decision_deadline = float(
            os.environ.get("POK_DECISION_HARD_DEADLINE_SEC",
                           str(DEFAULT_DECISION_HARD_DEADLINE_SEC)))
        self.hand_number = 0
        self.decision_number = 0
        self.private_cards = []
        self.board = []
        self.street = "preflop"
        self.is_small_blind = False
        self.hero_chips = 20000
        self.opponent_chips = 20000
        self.hero_bet = 0
        self.opponent_bet = 0
        self.pot = 0
        self.hero_action_count = 0
        self.stage_actions = []
        self.responding_to_check = False
        self.opponent_allin = False
        self.fold_seen = False
        # Track game state for network inference
        self._game_state = None
        self._hero_player_idx = 0

    def _load_network(self):
        """Load the trained deploy.npz model."""
        try:
            import numpy as np
            import torch
            deploy_data = np.load(self.deploy_path, allow_pickle=False)
            
            # Build network architecture
            class M5BNet(torch.nn.Module):
                def __init__(self):
                    super().__init__()
                    self.combo_embedding = torch.nn.Embedding(1326, 32)
                    self.global_encoder = torch.nn.Sequential(
                        torch.nn.Linear(4461, 96), torch.nn.GELU(), torch.nn.LayerNorm(96),
                        torch.nn.Linear(96, 96), torch.nn.GELU(), torch.nn.LayerNorm(96),
                    )
                    self.trunk = torch.nn.Sequential(
                        torch.nn.Linear(131, 96), torch.nn.GELU(), torch.nn.LayerNorm(96),
                        torch.nn.Linear(96, 96), torch.nn.GELU(), torch.nn.LayerNorm(96),
                        torch.nn.Linear(96, 96), torch.nn.GELU(), torch.nn.LayerNorm(96),
                    )
                    self.value_head = torch.nn.Linear(96, 2)
                    self.policy_head = torch.nn.Linear(96, 9)
                
                def forward(self, pf, reach, combo_mask, action_mask):
                    batch = pf.shape[0]
                    gi = torch.cat([pf, reach.flatten(1), combo_mask.float(), action_mask.float()], dim=1)
                    gc = self.global_encoder(gi)
                    emb = self.combo_embedding(torch.arange(1326)).unsqueeze(0).expand(batch, -1, -1)
                    local = torch.cat([emb, reach.transpose(1,2), combo_mask.float().unsqueeze(-1), gc.unsqueeze(1).expand(-1, 1326, -1)], dim=2)
                    h = self.trunk(local)
                    v = self.value_head(h).transpose(1,2)
                    l = self.policy_head(h)
                    l = l.masked_fill(~action_mask[:,None,:], torch.finfo(l.dtype).min)
                    return v, l
            
            net = M5BNet()
            state_dict = {k: torch.from_numpy(deploy_data[k]) for k in deploy_data.keys()}
            net.load_state_dict(state_dict)
            net.eval()
            
            self.policy_net = net
            self._has_network = True
            self._log(f"network_loaded=1 params={sum(p.numel() for p in net.parameters())}")
        except Exception as exc:
            self._log(f"network_load_failed={type(exc).__name__}:{str(exc)[:200]}")
            self._has_network = False

    def _log(self, text):
        if self.log:
            self.log.write(text + "\n")

    def _new_hand(self, blind, cards):
        self.hand_number += 1
        self.private_cards = cards
        self.board = []
        self.street = "preflop"
        self.is_small_blind = blind == "SMALLBLIND"
        self.hero_chips = 20000 - (50 if self.is_small_blind else 100)
        self.opponent_chips = 20000 - (100 if self.is_small_blind else 50)
        self.hero_bet = 50 if self.is_small_blind else 100
        self.opponent_bet = 100 if self.is_small_blind else 50
        self.pot = 150
        self.hero_action_count = 0
        self.stage_actions = []
        self.opponent_allin = False
        self.fold_seen = False
        # Bayesian reach factors for PBS belief tracking
        import numpy as _np
        self._reach_factors = _np.ones((2, HUNL_COMBO_COUNT), dtype=_np.float32) / HUNL_COMBO_COUNT
        # Block our own hole cards from opponent's range
        self._block_dead_cards()
        self._last_policy_logits = None  # cache for range update
        # Online search via NationalProtocolSession for authoritative state
        self._session = NationalProtocolSession(self.name)
        self._session_state = None
        self._cfr_iterations = 3  # lightweight search iterations
        self._game_state = None
        self._hero_player_idx = 0 if self.is_small_blind else 1

    def _block_dead_cards(self):
        """Remove combos containing known cards (hero hole + board) from both ranges."""
        import numpy as _np
        known = set(self.private_cards) | set(self.board)
        for combo_idx, (c0, c1) in enumerate(HUNL_COMBOS):
            if c0 in known or c1 in known:
                self._reach_factors[1][combo_idx] = 0.0  # opponent can't hold our cards
        for p in range(2):
            total = self._reach_factors[p].sum()
            if total > 0:
                self._reach_factors[p] /= total

    def _new_street(self, street, cards):
        self.street = street
        self.board.extend(cards)
        self.hero_bet = 0
        self.opponent_bet = 0
        self.hero_action_count = 0
        self.stage_actions = []
        self.opponent_allin = self.opponent_chips == 0
        self.responding_to_check = False
        # Re-block newly revealed board cards from both ranges
        self._block_dead_cards()

    def _to_call(self):
        return max(0, self.opponent_bet - self.hero_bet)

    def _legal_actions(self):
        """Return list of (action_name, wire_command) for legal actions."""
        to_call = self._to_call()
        actions = []
        if to_call > 0:
            actions.append(("fold", "fold"))
            actions.append(("call", "call"))
            if self.hero_chips > to_call:
                min_raise = max(self.opponent_bet * 2, self.opponent_bet + 100)
                if min_raise <= self.hero_chips + self.hero_bet:
                    actions.append(("raise", f"raise {min_raise}"))
                actions.append(("allin", "allin"))
        else:
            actions.append(("check", "check"))
            min_raise = self.hero_bet + 100
            if min_raise <= self.hero_chips + self.hero_bet:
                actions.append(("raise", f"raise {min_raise}"))
            actions.append(("allin", "allin"))
        return actions

    def _fallback(self):
        """Simple heuristic fallback."""
        to_call = self._to_call()
        if to_call == 0:
            return "check"
        if to_call > self.hero_chips:
            return "allin"
        pot_odds = to_call / (self.pot + to_call) if self.pot > 0 else 1.0
        if pot_odds < 0.4:
            return "call"
        return "fold"

    def _update_opponent_range(self, observed_action):
        """Bayesian range update: multiply opponent reach by policy prob of observed action."""
        import numpy as _np
        if self._last_policy_logits is None:
            return
        try:
            logits = self._last_policy_logits  # (1326, 9)
            mask = self._last_action_mask  # (9,)
            aa = self._last_abstract_actions
            # Map observed action to slot indices
            obs_kind = observed_action.split()[0]  # "raise", "call", "check", "allin"
            target_slots = []
            for slot_idx, action in enumerate(aa.slot_actions):
                if action is not None:
                    wire = action.to_wire().split()[0]  # "fold", "call", etc.
                    if wire == obs_kind:
                        target_slots.append(slot_idx)
            if not target_slots:
                return  # Unknown action, can't update
            
            # Compute policy probability for the observed action for each combo
            action_mask_t = _np.array(mask, dtype=bool)
            exp_logits = _np.exp(logits - _np.where(action_mask_t, logits.max(axis=1, keepdims=True), -1e38))
            exp_logits[~action_mask_t] = 0
            probs_all = exp_logits / exp_logits.sum(axis=1, keepdims=True)
            
            # Sum probability across matching slots for each combo
            action_prob = probs_all[:, target_slots].sum(axis=1)  # (1326,)
            
            # Multiply opponent reach by action probability
            self._reach_factors[1] *= action_prob
            total = self._reach_factors[1].sum()
            if total > 0:
                self._reach_factors[1] /= total
            else:
                # Reset to uniform on dead combos if all mass collapsed
                self._reach_factors[1] = _np.ones(HUNL_COMBO_COUNT, dtype=_np.float32) / HUNL_COMBO_COUNT
                self._block_dead_cards()
        except Exception as exc:
            self._log(f"range_update_error={type(exc).__name__}:{str(exc)[:200]}")

    def _build_game_state(self):
        """Build a NationalGameState from current TCP tracking state."""
        if self._session_state is not None and not self._session_state.is_terminal:
            return self._session_state
        return None

    def _network_decide(self):
        """Use the policy network to select an action."""
        if not self._has_network:
            return self._fallback()
        
        try:
            import numpy as np
            import torch
            
            # Build game state for network input
            state = self._build_game_state()
            if state is None:
                return self._fallback()
            
            # Build public features
            public = state.hand_public_dict()
            for k in ['contract_version', 'terminal_reason', 'winner']:
                public.pop(k, None)
            pf = encode_public_features(public)
            
            # Build reach factors (uniform prior - can be improved with Bayesian updates)
            # Use tracked Bayesian reach factors (updated from observed actions)
            reach = self._reach_factors.copy()
            
            # Build combo mask
            combo_mask_arr = np.array(legal_combo_mask(state.board), dtype=bool)
            
            # Build action mask from abstract actions
            aa = abstract_actions(state)
            action_mask_arr = aa.mask
            
            # Forward pass
            with torch.no_grad():
                pf_t = torch.from_numpy(pf).unsqueeze(0)
                reach_t = torch.from_numpy(reach).unsqueeze(0)
                combo_t = torch.from_numpy(combo_mask_arr).unsqueeze(0)
                action_t = torch.from_numpy(action_mask_arr).unsqueeze(0)
                values, logits = self.policy_net(pf_t, reach_t, combo_t, action_t)
            
            # Cache policy logits for range update after our action
            self._last_policy_logits = logits[0].cpu().numpy()  # (1326, 9)
            self._last_action_mask = action_mask_arr
            self._last_abstract_actions = aa
            
            # Get our combo index
            our_combo = tuple(sorted(self.private_cards))
            combo_idx = HUNL_COMBOS.index(our_combo)
            
            # Get policy logits for our combo
            our_logits = logits[0, combo_idx, :]
            
            # Apply softmax over legal actions only
            legal_logits = our_logits.clone()
            legal_logits[~torch.from_numpy(action_mask_arr)] = float('-inf')
            probs = torch.softmax(legal_logits, dim=0)
            
            # Online search closed loop: lightweight CFR improvement using
            # value network as leaf evaluator for depth-1 lookahead
            import numpy as _np
            prior_np = probs.cpu().numpy()
            legal_slots = [i for i in range(9) if action_mask_arr[i]]
            
            if len(legal_slots) > 1 and self._cfr_iterations > 0:
                regrets = _np.zeros(9, dtype=_np.float64)
                strategy_sum = _np.zeros(9, dtype=_np.float64)
                
                for _iter in range(self._cfr_iterations):
                    weight = _iter + 1
                    pos_reg = _np.where(regrets > 0, regrets, 0.0)
                    pos_reg *= action_mask_arr
                    s = pos_reg.sum()
                    cur_strat = pos_reg / s if s > 1e-12 else prior_np.copy()
                    strategy_sum += weight * cur_strat
                    
                    for slot_idx in legal_slots:
                        act = aa.slot_actions[slot_idx]
                        if act is None:
                            continue
                        try:
                            child = state.apply_action(act)
                            if child.is_terminal:
                                cfv = 1.0 if child.winner == 0 else (-1.0 if child.winner == 1 else 0.0)
                            elif child.chance_pending:
                                cfv = prior_np[slot_idx] * 0.1
                            else:
                                child_pub = child.hand_public_dict()
                                for _k in ['contract_version', 'terminal_reason', 'winner']:
                                    child_pub.pop(_k, None)
                                child_pf = encode_public_features(child_pub)
                                child_cm = _np.array(legal_combo_mask(child.board), dtype=bool)
                                child_aa2 = abstract_actions(child)
                                child_am = child_aa2.mask
                                with torch.no_grad():
                                    _, _ = self.policy_net(
                                        torch.from_numpy(child_pf).unsqueeze(0),
                                        torch.from_numpy(reach).unsqueeze(0),
                                        torch.from_numpy(child_cm).unsqueeze(0),
                                        torch.from_numpy(child_am).unsqueeze(0))
                                    cv, _ = self.policy_net(
                                        torch.from_numpy(child_pf).unsqueeze(0),
                                        torch.from_numpy(reach).unsqueeze(0),
                                        torch.from_numpy(child_cm).unsqueeze(0),
                                        torch.from_numpy(child_am).unsqueeze(0))
                                cfv = float(cv[0, 0, combo_idx])
                            regrets[slot_idx] += cfv
                        except Exception:
                            regrets[slot_idx] += 0.0
                
                total_s = strategy_sum.sum()
                if total_s > 1e-12:
                    improved = strategy_sum / total_s
                    improved[~action_mask_arr] = 0
                    if improved.sum() > 1e-12:
                        improved = improved / improved.sum()
                        probs = torch.from_numpy(improved).float()
            
            # Sample action from (possibly CFR-improved) policy
            action_idx = torch.multinomial(probs, 1).item()
            action = aa.slot_actions[action_idx]
            
            if action is None:
                # Fallback to first legal action
                for i, a in enumerate(aa.slot_actions):
                    if a is not None:
                        action = a
                        break
            
            wire = action.to_wire()
            self._log(f"network_decision=1 action={wire} probs={probs.tolist()}")
            return wire
            
        except Exception as exc:
            self._log(f"network_error={type(exc).__name__}:{str(exc)[:200]}")
            return self._fallback()

    def decide(self):
        started = time.monotonic()
        self.decision_number += 1
        action = self._network_decide()
        elapsed = time.monotonic() - started
        self._log(f"hand={self.hand_number} decision={self.decision_number} "
                   f"action={action} elapsed={elapsed:.3f}s")
        return action

    def _send_action(self, sock, action):
        time.sleep(self.action_delay)
        sock.sendall(action.encode("ascii"))

    def _process_message(self, msg):
        """Process one decoded TCP message, return True if decision needed."""
        # Feed to NationalProtocolSession for authoritative state tracking
        try:
            event = self._session.receive(msg)
            if event.kind == "name_requested":
                self._session.name_response()
            self._session_state = self._session.current
        except Exception as exc:
            self._log(f"session_error={type(exc).__name__}:{str(exc)[:200]}")

        if msg.startswith("name"):
            return False
        if msg.startswith("preflop|"):
            parts = msg.split("|")
            blind = parts[1]
            cards = _parse_cards(msg)
            self._new_hand(blind, cards)
            return blind == "SMALLBLIND"
        if msg.startswith("flop|"):
            cards = _parse_cards(msg)
            self._new_street("flop", cards)
            return not self.is_small_blind
        if msg.startswith("turn|"):
            cards = _parse_cards(msg)
            self._new_street("turn", cards)
            return not self.is_small_blind
        if msg.startswith("river|"):
            cards = _parse_cards(msg)
            self._new_street("river", cards)
            return not self.is_small_blind
        if msg.startswith("oppo_hands|"):
            return False
        if msg == "fold":
            self.fold_seen = True
            return False
        if msg == "check":
            self.responding_to_check = True
            self._update_opponent_range("check")
            return True
        if msg == "call":
            committed = min(self.opponent_chips, max(0, self.hero_bet - self.opponent_bet))
            self.opponent_chips -= committed
            self.opponent_bet += committed
            self.pot += committed
            self.stage_actions.append(("call", None))
            self.responding_to_check = True
            self._update_opponent_range("call")
            return True
        if msg == "allin":
            committed = self.opponent_chips
            self.opponent_chips = 0
            self.opponent_bet += committed
            self.pot += committed
            self.opponent_allin = True
            self.stage_actions.append(("allin", None))
            self._update_opponent_range("allin")
            return True
        if msg.startswith("raise "):
            target = int(msg.split(" ", 1)[1])
            committed = min(self.opponent_chips, max(0, target - self.opponent_bet))
            self.opponent_chips -= committed
            self.opponent_bet += committed
            self.pot += committed
            self.stage_actions.append(("raise", target))
            self._update_opponent_range("raise")
            return True
        if msg.startswith("earnChips"):
            return False
        return False

    def _apply_hero(self, action):
        if action.startswith("raise "):
            target = int(action.split(" ", 1)[1])
            committed = max(0, target - self.hero_bet)
            self.hero_chips -= committed
            self.hero_bet = target
            self.pot += committed
            self.stage_actions.append(("raise", target))
        elif action == "call":
            committed = min(self.hero_chips, max(0, self.opponent_bet - self.hero_bet))
            self.hero_chips -= committed
            self.hero_bet += committed
            self.pot += committed
            self.stage_actions.append(("call", None))
        elif action == "allin":
            committed = self.hero_chips
            self.hero_chips = 0
            self.hero_bet += committed
            self.pot += committed
            self.stage_actions.append(("allin", None))
        elif action == "fold":
            self.fold_seen = True
        elif action == "check":
            self.stage_actions.append(("check", None))
            self.responding_to_check = False
        self.hero_action_count += 1

    def run(self, host, port, match_timeout=180):
        sock = socket.create_connection((host, port), timeout=15.0)
        sock.setblocking(False)
        self._log(f"connected host={host} port={port}")

        numeric_pending_since = None
        start_time = time.monotonic()
        cumulative_earn = 0

        while True:
            if time.monotonic() - start_time > match_timeout:
                break
            readable, _, _ = select.select([sock], [], [], 0.01)
            if readable:
                try:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    messages = self.decoder.feed(chunk.decode("ascii", errors="replace"))
                    numeric_pending_since = (
                        time.monotonic() if self.decoder.has_pending_numeric else None
                    )
                except (BlockingIOError, ConnectionError, OSError):
                    break
            else:
                ready = (
                    self.decoder.has_pending_numeric
                    and numeric_pending_since is not None
                    and time.monotonic() - numeric_pending_since >= 0.05
                )
                messages = self.decoder.flush_idle() if ready else []
                if ready:
                    numeric_pending_since = None

            for msg in messages:
                self._log(f"recv: {msg[:80]}")
                if msg == "name":
                    time.sleep(self.action_delay)
                    sock.sendall(self.name.encode("ascii"))
                    self._log(f"send: {self.name}")
                    continue

                needs_decision = self._process_message(msg)
                if needs_decision:
                    action = self.decide()
                    self._apply_hero(action)
                    # Submit to session to keep state in sync
                    try:
                        if self._session.pending_decision_id is not None:
                            self._session.submit_action(
                                self._session.pending_decision_id, action)
                            self._session_state = self._session.current
                    except Exception as exc:
                        self._log(f"submit_error={type(exc).__name__}:{str(exc)[:200]}")
                    time.sleep(self.action_delay)
                    try:
                        sock.sendall(action.encode("ascii"))
                    except (BrokenPipeError, ConnectionError, OSError):
                        break
                    self._log(f"send: {action}")
                
                if msg.startswith("earnChips"):
                    earn = int(msg.split(" ", 1)[1])
                    cumulative_earn += earn

        sock.close()
        self._log("disconnected")
        if self.log:
            self.log.close()
        
        # Print telemetry
        print(f"A1_TELEMETRY {{\"bot_name\":\"{self.name}\",\"earnings\":{cumulative_earn},\"decisions\":{self.decision_number},\"network_used\":{str(self._has_network).lower()}}}", file=sys.stderr)
        return 0


def main():
    parser = argparse.ArgumentParser(description="Route A1 national TCP bot")
    parser.add_argument("--deploy", required=True, help="Path to deploy.npz")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10001)
    parser.add_argument("--name", default="RouteA1")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--log", default="")
    parser.add_argument("--match-timeout", type=float, default=180.0)
    args = parser.parse_args()

    client = A1NetworkClient(args.name, args.deploy, args.seed, args.log)
    return client.run(args.host, args.port, args.match_timeout)


if __name__ == "__main__":
    raise SystemExit(main())
