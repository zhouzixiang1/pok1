"""Guosai socket runner: play PokerSkill agent on local competition platform."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import pathlib
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ._core import generate_prompt
from .schema import validate_game_state

logger = logging.getLogger(__name__)

HERO = 0
VILLAIN = 1
SMALL_BLIND = 50
BIG_BLIND = 100
INITIAL_CHIPS = 20000

_RANKS = "23456789TJQKA"
_SUITS = "shdc"  # 0=spade,1=heart,2=diamond,3=club


def _load_create_llm_client():
    """Load llm_client.py directly to avoid importing _battle.__init__ on local builds."""
    llm_path = pathlib.Path(__file__).parent / "_battle" / "llm_client.py"
    spec = importlib.util.spec_from_file_location(
        "pokerskill_agent._battle.llm_client_standalone",
        llm_path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load llm client module from: {llm_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.create_llm_client


create_llm_client = _load_create_llm_client()


def _chips_to_bb(chips: int) -> float:
    return chips / BIG_BLIND


def _bb_to_chips(bb: float) -> int:
    return int(round(bb * BIG_BLIND))


def _fmt_bb_from_chips(chips: int) -> str:
    v = chips / BIG_BLIND
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _parse_cards(msg: str) -> List[str]:
    cards: List[str] = []
    for suit_str, rank_str in re.findall(r"<\s*(\d+)\s*,\s*(\d+)\s*>", msg):
        suit = int(suit_str)
        rank = int(rank_str)
        if suit < 0 or suit > 3 or rank < 0 or rank > 12:
            continue
        cards.append(_RANKS[rank] + _SUITS[suit])
    return cards


def _extract_json_object(text: str) -> Optional[dict]:
    try:
        return json.loads(text)
    except Exception:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except Exception:
            pass

    # Best-effort extraction for noisy outputs.
    for m in re.finditer(r"\{", text):
        start = m.start()
        for end in range(len(text) - 1, start, -1):
            if text[end] != "}":
                continue
            chunk = text[start : end + 1]
            try:
                return json.loads(chunk)
            except Exception:
                continue
    return None


def _normalize_action_token(token: str) -> str:
    t = (token or "").strip().lower()
    mapping = {
        "f": "f",
        "fold": "f",
        "k": "k",
        "check": "k",
        "c": "c",
        "call": "c",
        "b": "b",
        "bet": "b",
        "raise": "b",
        "allin": "b",
    }
    return mapping.get(t, "")


def _parse_llm_decision(text: str) -> Tuple[str, Optional[float]]:
    obj = _extract_json_object(text)
    if not obj:
        return "", None

    action = _normalize_action_token(str(obj.get("action", "")))
    amount = obj.get("amount")
    if amount is None:
        return action, None
    try:
        amount_f = float(amount)
    except Exception:
        return action, None
    return action, amount_f


_MSG_PATTERN = re.compile(
    r"(preflop\|(?:SMALLBLIND|BIGBLIND)\|(?:<\d+,\d+>){2})|"
    r"(flop\|(?:<\d+,\d+>){3})|"
    r"(turn\|<\d+,\d+>)|"
    r"(river\|<\d+,\d+>)|"
    r"(oppo_hands\|(?:<\d+,\d+>){2})|"
    r"(earnChips\s+-?\d+)|"
    r"(raise\s+-?\d+)|"
    r"(bet\s+-?\d+)|"
    r"(allin)|"
    r"(check)|"
    r"(call)|"
    r"(fold)|"
    r"(name)",
    re.IGNORECASE,
)


_MSG_START_RE = re.compile(
    r"(?i)name|preflop\||flop\||turn\||river\||oppo_hands\||"
    r"earnchips\s|raise\s|bet\s|allin|fold|call|check"
)
_MSG_START_TOKENS = (
    "name",
    "preflop|",
    "flop|",
    "turn|",
    "river|",
    "oppo_hands|",
    "earnchips ",
    "raise ",
    "bet ",
    "allin",
    "fold",
    "call",
    "check",
)


def _trailing_message_start_prefix(text: str) -> str:
    lower = text.lower()
    max_len = max(len(t) for t in _MSG_START_TOKENS)
    for length in range(min(len(lower), max_len), 0, -1):
        suffix = lower[-length:]
        if any(token.startswith(suffix) for token in _MSG_START_TOKENS):
            return text[-length:]
    return ""


def _is_complete_message(message: str) -> bool:
    lower = message.lower()
    if lower in {"name", "allin", "fold", "call", "check"}:
        return True
    if re.fullmatch(r"(?:raise|bet)\s+[+-]?\d+", lower):
        return True
    if re.fullmatch(r"earnchips\s+[+-]?\d+", lower):
        return True
    card_count = len(re.findall(r"<\s*\d+\s*,\s*\d+\s*>", message))
    if lower.startswith("preflop|"):
        parts = message.split("|", 2)
        return (
            len(parts) == 3
            and parts[1].strip().upper() in {"SMALLBLIND", "BIGBLIND"}
            and card_count == 2
        )
    if lower.startswith("flop|"):
        return card_count == 3
    if lower.startswith(("turn|", "river|")):
        return card_count == 1
    if lower.startswith("oppo_hands|"):
        return card_count == 2
    return False


def _split_messages(buffer: str) -> Tuple[List[str], str]:
    """
    Split stream buffer into complete protocol messages and remainder.
    """
    text = buffer.replace("\x00", "").replace("\r", "").replace("\n", "").strip()
    if not text:
        return [], ""

    starts = [m.start() for m in _MSG_START_RE.finditer(text)]
    if not starts:
        return [], _trailing_message_start_prefix(text)

    messages: List[str] = []
    remainder = ""
    for idx, start in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(text)
        candidate = text[start:end].strip()
        if not candidate:
            continue
        if _is_complete_message(candidate):
            messages.append(candidate)
        elif idx + 1 == len(starts):
            remainder += candidate
    return messages, remainder


@dataclass
class GuosaiConfig:
    host: str
    port: int
    team_name: str
    num_hands: int
    model: str
    backend: str
    llm_base_url: str
    llm_api_key: str
    temperature: float
    max_tokens: int
    thinking_budget: int
    use_skills: bool
    step_timeout_s: float
    socket_timeout_s: float
    max_retries: int


@dataclass
class _LLMConfig:
    model: str
    backend: str
    llm_base_url: str
    llm_api_key: str
    temperature: float
    max_tokens: int
    thinking_budget: int
    num_concurrent: int = 1


@dataclass
class GuosaiResult:
    started: int
    completed: int
    failed: int
    total_earn_chips: int

    def summary(self) -> str:
        avg = (self.total_earn_chips / self.completed) if self.completed else 0.0
        return (
            f"Started: {self.started} | Completed: {self.completed} | Failed: {self.failed}\n"
            f"Total earnChips: {self.total_earn_chips}\n"
            f"Avg earnChips/hand: {avg:.2f}"
        )


class _HandState:
    def __init__(self, hand_id: int, role: str, hole_cards: List[str]):
        self.hand_id = hand_id
        self.role = role
        self.hero_is_sb = role == "SMALLBLIND"
        self.street = "preflop"
        self.hero_cards = hole_cards[:]  # ["As","Kh"]
        self.board_cards: List[str] = []

        self.stacks = [INITIAL_CHIPS, INITIAL_CHIPS]  # [hero, villain]
        self.street_bets = [0, 0]  # chips invested on this street
        self.pot = 0

        # Post blinds.
        if self.hero_is_sb:
            self._post_blind(HERO, SMALL_BLIND)
            self._post_blind(VILLAIN, BIG_BLIND)
            self.current_actor = HERO
        else:
            self._post_blind(HERO, BIG_BLIND)
            self._post_blind(VILLAIN, SMALL_BLIND)
            self.current_actor = VILLAIN

        self.action_history: List[str] = []
        self.actions_in_street = 0
        self.previous_check = False
        self.last_raise_to = BIG_BLIND  # preflop first legal raise-to is 200
        self.allin_actor: Optional[int] = None
        self.waiting_next_street = False
        self.hand_over = False

    def _post_blind(self, actor: int, amount: int) -> None:
        amount = min(amount, self.stacks[actor])
        self.stacks[actor] -= amount
        self.street_bets[actor] += amount
        self.pot += amount

    def advance_street(self, street: str, cards: List[str]) -> None:
        if self.action_history and self.action_history[-1] != "_":
            self.action_history.append("_")

        self.street = street
        self.board_cards.extend(cards)
        self.street_bets = [0, 0]
        self.actions_in_street = 0
        self.previous_check = False
        self.last_raise_to = None
        self.waiting_next_street = False
        self.allin_actor = None
        self.current_actor = HERO if not self.hero_is_sb else VILLAIN  # postflop BB first

    def _open_raise_min(self) -> int:
        if self.street == "preflop":
            return 200
        return 100

    def _raise_bounds(self, actor: int) -> Optional[Tuple[int, int]]:
        stack = self.stacks[actor]
        if stack <= 0:
            return None
        self_bet = self.street_bets[actor]
        max_to = self_bet + stack
        base_min = self._open_raise_min()
        if self.last_raise_to is not None:
            min_to = max(base_min, self.last_raise_to * 2)
        else:
            min_to = base_min
        if max_to < min_to:
            return None
        return min_to, max_to

    def legal_context_for_hero(self) -> Dict[str, object]:
        if self.hand_over or self.current_actor != HERO:
            return {"legal_actions": [], "raise_min": None, "raise_max": None}

        hero_bet = self.street_bets[HERO]
        vil_bet = self.street_bets[VILLAIN]
        to_call = max(0, vil_bet - hero_bet)
        stack = self.stacks[HERO]

        actions: List[str] = []
        raise_min_bb: Optional[float] = None
        raise_max_bb: Optional[float] = None

        if to_call > 0:
            actions.append("f")
            if stack >= to_call:
                actions.append("c")
        else:
            # Match guosai protocol behavior used by your working bots:
            # - preflop: only BB's first response after SB completes can "check"
            # - postflop: first actor may "check"; non-first should use "call" as pass
            check_allowed = False
            if self.street == "preflop":
                check_allowed = (
                    self.actions_in_street == 1
                    and self.street_bets[HERO] == BIG_BLIND
                    and self.street_bets[VILLAIN] == BIG_BLIND
                )
            else:
                check_allowed = self.actions_in_street == 0

            if check_allowed:
                actions.append("k")
            else:
                actions.append("c")

        can_raise = self.allin_actor != VILLAIN and stack > to_call
        if can_raise:
            bounds = self._raise_bounds(HERO)
            if bounds:
                min_to, max_to = bounds
                actions.append("b")
                raise_min_bb = _chips_to_bb(min_to)
                raise_max_bb = _chips_to_bb(max_to)

        return {
            "legal_actions": actions,
            "raise_min": raise_min_bb,
            "raise_max": raise_max_bb,
        }

    def to_pokerskill_state(self, use_skills: bool) -> dict:
        legal = self.legal_context_for_hero()
        raw = {
            "hand_id": self.hand_id,
            "street": self.street,
            "hero_hole_cards": "".join(self.hero_cards),
            "board_cards": "".join(self.board_cards),
            "pot": _chips_to_bb(self.pot),
            "total_pot": _chips_to_bb(self.pot),
            "hero_stack": _chips_to_bb(self.stacks[HERO]),
            "villain_stack": _chips_to_bb(self.stacks[VILLAIN]),
            "hero_position": "BTN" if self.hero_is_sb else "BB",
            "legal_actions": legal["legal_actions"],
            "raise_min": legal["raise_min"],
            "raise_max": legal["raise_max"],
            "action_history": self.action_history[:],
            "use_skills": use_skills,
        }
        return validate_game_state(raw)

    def _apply_fold(self, actor: int) -> None:
        self.action_history.append("f")
        self.hand_over = True
        self.current_actor = None
        self.waiting_next_street = False

    def _apply_check(self, actor: int) -> None:
        self.action_history.append("k")
        self.actions_in_street += 1
        if self.previous_check and self.last_raise_to is None:
            self.waiting_next_street = True
            self.current_actor = None
            self.previous_check = False
        else:
            self.previous_check = True
            self.current_actor = 1 - actor

    def _apply_call(self, actor: int) -> None:
        self_bet = self.street_bets[actor]
        opp_bet = self.street_bets[1 - actor]
        to_call = max(0, opp_bet - self_bet)
        pay = min(self.stacks[actor], to_call)
        self.stacks[actor] -= pay
        self.street_bets[actor] += pay
        self.pot += pay
        self.action_history.append("c")
        self.actions_in_street += 1
        self.previous_check = False
        # Special preflop rule:
        # when SB only calls up to BB (no raise yet), BB still has one action (check/raise).
        is_blind_completion = (
            self.street == "preflop"
            and self.last_raise_to == BIG_BLIND
            and self.street_bets[HERO] == BIG_BLIND
            and self.street_bets[VILLAIN] == BIG_BLIND
        )
        if is_blind_completion:
            self.waiting_next_street = False
            self.current_actor = 1 - actor
        else:
            self.waiting_next_street = True
            self.current_actor = None

    def _apply_raise_to(self, actor: int, target_to: int, forced_allin: bool = False) -> None:
        self_bet = self.street_bets[actor]
        target_to = max(target_to, self_bet + 1)
        max_to = self_bet + self.stacks[actor]
        target_to = min(target_to, max_to)
        delta = target_to - self_bet
        self.stacks[actor] -= delta
        self.street_bets[actor] = target_to
        self.pot += delta
        self.last_raise_to = target_to
        self.action_history.append(f"b{_fmt_bb_from_chips(target_to)}")
        self.actions_in_street += 1
        self.previous_check = False
        if forced_allin or self.stacks[actor] == 0:
            self.allin_actor = actor
        self.current_actor = 1 - actor

    def apply_platform_action(self, actor: int, action: str, amount: Optional[int] = None) -> None:
        if self.hand_over:
            return
        a = action.lower().strip()
        if a == "fold":
            self._apply_fold(actor)
            return
        if a == "check":
            self._apply_check(actor)
            return
        if a == "call":
            self._apply_call(actor)
            return
        if a == "allin":
            self._apply_raise_to(actor, self.street_bets[actor] + self.stacks[actor], forced_allin=True)
            return
        if a == "raise":
            self._apply_raise_to(actor, int(amount or 0), forced_allin=False)
            return
        # Unknown action: fail safe as fold.
        self._apply_fold(actor)

    def _fallback_command(self) -> str:
        legal = self.legal_context_for_hero()["legal_actions"]
        if "k" in legal:
            return "check"
        if "c" in legal:
            return "call"
        if "f" in legal:
            return "fold"
        if "b" in legal:
            bounds = self._raise_bounds(HERO)
            if bounds:
                min_to, max_to = bounds
                if max_to == self.street_bets[HERO] + self.stacks[HERO]:
                    return "allin"
                return f"raise {min_to}"
        return "fold"

    def decide_command_from_llm(self, action: str, amount_bb: Optional[float]) -> str:
        legal_ctx = self.legal_context_for_hero()
        legal = set(legal_ctx["legal_actions"])
        if not legal:
            return "fold"

        a = _normalize_action_token(action)
        if a not in {"f", "k", "c", "b"}:
            return self._fallback_command()

        if a == "f":
            return "fold" if "f" in legal else self._fallback_command()
        if a == "k":
            return "check" if "k" in legal else self._fallback_command()
        if a == "c":
            return "call" if "c" in legal else self._fallback_command()

        # action == "b"
        if "b" not in legal:
            return self._fallback_command()

        bounds = self._raise_bounds(HERO)
        if not bounds:
            return self._fallback_command()
        min_to, max_to = bounds
        target_to = _bb_to_chips(amount_bb) if amount_bb is not None else min_to
        target_to = max(min_to, min(max_to, target_to))

        hero_bet = self.street_bets[HERO]
        hero_stack = self.stacks[HERO]
        if target_to - hero_bet >= hero_stack:
            return "allin"
        return f"raise {target_to}"

    def apply_hero_command(self, command: str) -> None:
        cmd = command.strip().lower()
        if cmd.startswith("raise "):
            m = re.match(r"raise\s+(-?\d+)", cmd)
            if not m:
                self.apply_platform_action(HERO, "fold")
                return
            self.apply_platform_action(HERO, "raise", int(m.group(1)))
            return
        if cmd in {"fold", "check", "call", "allin"}:
            self.apply_platform_action(HERO, cmd)
            return
        self.apply_platform_action(HERO, "fold")


class GuosaiRunner:
    def __init__(self, config: GuosaiConfig):
        self.config = config
        self._hand: Optional[_HandState] = None
        self._hand_id = 0
        self._started = 0
        self._completed = 0
        self._failed = 0
        self._total_earn = 0
        self._writer: Optional[asyncio.StreamWriter] = None
        self._recv_buffer = ""

    async def run(self) -> GuosaiResult:
        llm_cfg = _LLMConfig(
            model=self.config.model,
            backend=self.config.backend,
            llm_base_url=self.config.llm_base_url,
            llm_api_key=self.config.llm_api_key,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            thinking_budget=self.config.thinking_budget,
            num_concurrent=1,
        )
        llm_client = create_llm_client(llm_cfg)
        logger.info("LLM client ready: backend=%s model=%s", self.config.backend, self.config.model)

        try:
            reader, writer = await asyncio.open_connection(self.config.host, self.config.port)
            self._writer = writer
            logger.info("Connected to guosai platform %s:%s", self.config.host, self.config.port)

            while self._completed < self.config.num_hands:
                raw_line = await asyncio.wait_for(
                    reader.read(256),
                    timeout=self.config.socket_timeout_s,
                )
                if not raw_line:
                    logger.warning("Socket closed by server")
                    break
                chunk = raw_line.decode("utf-8", errors="ignore")
                self._recv_buffer += chunk
                lines, self._recv_buffer = _split_messages(self._recv_buffer)
                for line in lines:
                    await self._handle_line(line, llm_client)

            return GuosaiResult(
                started=self._started,
                completed=self._completed,
                failed=self._failed,
                total_earn_chips=self._total_earn,
            )
        finally:
            try:
                if self._hand is not None:
                    llm_client.end_hand(self._hand.hand_id)
            except Exception:
                pass
            try:
                await llm_client.close()
            except Exception:
                pass
            if self._writer is not None:
                self._writer.close()
                try:
                    await self._writer.wait_closed()
                except Exception:
                    pass

    async def _send_line(self, line: str) -> None:
        if self._writer is None:
            return
        # Keep wire format consistent with legacy bots (raw command without newline).
        data = line.encode("utf-8")
        self._writer.write(data)
        await self._writer.drain()
        # Keep a tiny send gap to reduce command sticky-packet risk.
        await asyncio.sleep(0.05)
        logger.info("-> %s", line)

    def _parse_action_line(self, line: str) -> Optional[Tuple[str, Optional[int]]]:
        t = line.strip().lower()
        if t in {"fold", "call", "check", "allin"}:
            return t, None
        m = re.match(r"^(?:raise|bet)\s+(-?\d+)$", t)
        if m:
            return "raise", int(m.group(1))
        return None

    async def _maybe_act(self, llm_client) -> None:
        if self._hand is None or self._hand.hand_over:
            return
        if self._hand.current_actor != HERO or self._hand.waiting_next_street:
            return

        try:
            state = self._hand.to_pokerskill_state(use_skills=self.config.use_skills)
            prompt = generate_prompt(state)
            llm_text = await asyncio.wait_for(
                llm_client.chat(
                    hand_id=self._hand.hand_id,
                    system_prompt=prompt["system_prompt"],
                    user_prompt=prompt["user_prompt"],
                ),
                timeout=self.config.step_timeout_s,
            )
            action, amount = _parse_llm_decision(llm_text)
            command = self._hand.decide_command_from_llm(action, amount)
        except Exception as e:
            logger.warning("Decision failure on hand %s: %s", self._hand.hand_id, e)
            command = self._hand._fallback_command()

        await self._send_line(command)
        self._hand.apply_hero_command(command)

    async def _handle_line(self, line: str, llm_client) -> None:
        logger.info("<- %s", line)
        lower = line.lower()

        if lower == "name":
            await self._send_line(self.config.team_name)
            return

        if lower.startswith("preflop|"):
            # preflop|SMALLBLIND|<0,3><1,3>
            parts = line.split("|")
            if len(parts) < 3:
                return
            role = parts[1].strip().upper()
            cards = _parse_cards(parts[2])
            if role not in {"SMALLBLIND", "BIGBLIND"} or len(cards) != 2:
                logger.warning("Invalid preflop line: %s", line)
                return
            self._hand_id += 1
            self._started += 1
            self._hand = _HandState(hand_id=self._hand_id, role=role, hole_cards=cards)
            await self._maybe_act(llm_client)
            return

        if lower.startswith("flop|"):
            if self._hand is None:
                return
            cards = _parse_cards(line)
            if len(cards) >= 3:
                self._hand.advance_street("flop", cards[:3])
            await self._maybe_act(llm_client)
            return

        if lower.startswith("turn|"):
            if self._hand is None:
                return
            cards = _parse_cards(line)
            if cards:
                self._hand.advance_street("turn", cards[:1])
            await self._maybe_act(llm_client)
            return

        if lower.startswith("river|"):
            if self._hand is None:
                return
            cards = _parse_cards(line)
            if cards:
                self._hand.advance_street("river", cards[:1])
            await self._maybe_act(llm_client)
            return

        if lower.startswith("earnchips"):
            m = re.match(r"earnchips\s+(-?\d+)", lower)
            if m:
                earn = int(m.group(1))
                self._total_earn += earn
            else:
                earn = 0
            self._completed += 1
            logger.info("Hand %s done, earnChips=%s", self._hand_id, earn)
            if self._hand is not None:
                llm_client.end_hand(self._hand.hand_id)
                self._hand.hand_over = True
            return

        if lower.startswith("oppo_hands|"):
            return

        act = self._parse_action_line(line)
        if act and self._hand is not None:
            # Platform emits opponent action to us.
            action, amount = act
            self._hand.apply_platform_action(VILLAIN, action, amount)
            await self._maybe_act(llm_client)
            return

        # Unknown line: ignore.
        logger.debug("Ignored line: %s", line)

