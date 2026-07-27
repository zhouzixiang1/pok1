"""THP wire-parsing cluster for the official platform harness.

Moved from ``official_platform_harness`` to keep the parsing/binding logic in a
cohesive unit. Every intra-companion call to a moved symbol routes through
``_oph.<name>(...)``; main-side constants and helpers are also accessed via
``_oph.`` to avoid duplicating state.
"""

from __future__ import annotations

import hashlib
import shutil
import time
from pathlib import Path
from typing import Any

import official_platform_harness as _oph


def _platform_thp_dirs(exe_path: Path) -> list[Path]:
    dirs: list[Path] = []
    for path in (exe_path.parent, exe_path.parent.parent):
        resolved = path.resolve()
        if resolved not in dirs:
            dirs.append(resolved)
    return dirs


def _coerce_platform_dirs(platform_dirs: Path | list[Path] | tuple[Path, ...]) -> list[Path]:
    if isinstance(platform_dirs, Path):
        return [platform_dirs]
    return [Path(path) for path in platform_dirs]


def _snapshot_platform_thp_files(platform_dirs: Path | list[Path] | tuple[Path, ...]) -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    for platform_dir in _oph._coerce_platform_dirs(platform_dirs):
        for path in platform_dir.glob("THP-*.txt"):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            snapshot[str(path.resolve())] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def _collect_new_thp_files(
    platform_dirs: Path | list[Path] | tuple[Path, ...],
    *,
    before: dict[str, tuple[int, int]],
    artifact_dir: Path,
    wait_sec: float = 0.0,
    stable_sec: float = 0.5,
) -> tuple[list[str], list[str]]:
    artifacts: list[str] = []
    issues: list[str] = []
    deadline = time.time() + max(0.0, wait_sec)
    last_signature: tuple[tuple[str, int, int], ...] = ()
    stable_since: float | None = None

    while True:
        signature: list[tuple[str, int, int]] = []
        for platform_dir in _oph._coerce_platform_dirs(platform_dirs):
            for path in sorted(platform_dir.glob("THP-*.txt")):
                if not path.is_file():
                    continue
                try:
                    stat = path.stat()
                    key = str(path.resolve())
                except OSError:
                    continue
                current = (stat.st_size, stat.st_mtime_ns)
                if before.get(key) != current:
                    signature.append((str(path), stat.st_size, stat.st_mtime_ns))
        current_signature = tuple(signature)
        if current_signature:
            if current_signature == last_signature:
                if stable_since is not None and time.time() - stable_since >= stable_sec:
                    break
            else:
                last_signature = current_signature
                stable_since = time.time()
        if time.time() >= deadline:
            break
        time.sleep(0.2)

    for path_name, _, _ in last_signature:
        path = Path(path_name)
        try:
            if not path.exists():
                continue
            artifact_dir.mkdir(parents=True, exist_ok=True)
            destination = _oph._unique_destination(artifact_dir / path.name)
            shutil.move(str(path), str(destination))
            artifacts.append(str(destination))
        except OSError as exc:
            issues.append(f"thp_collect_error: {path.name}: {type(exc).__name__}: {exc}")
    return artifacts, issues


def _summarize_thp_files(paths: list[str]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for name in paths:
        path = Path(name)
        summary: dict[str, Any] = {"path": str(path), "exists": path.exists(), "hand_records": 0, "bytes": 0}
        if path.exists():
            try:
                raw = path.read_bytes()
                summary["bytes"] = len(raw)
                summary["sha256"] = hashlib.sha256(raw).hexdigest()
                text = raw.decode("gb2312", errors="replace")
                hand_indices = [int(value) for value in _oph.THP_HAND_RE.findall(text)]
                summary["hand_records"] = len(hand_indices)
                summary["hand_indices"] = hand_indices
            except OSError as exc:
                summary["issue"] = f"thp_read_error: {type(exc).__name__}: {exc}"
            except Exception as exc:
                summary["issue"] = f"thp_parse_error: {type(exc).__name__}: {exc}"
        summaries.append(summary)
    return summaries


def _canonical_thp_evidence(
    summaries: list[dict[str, Any]],
    *,
    expected_hands: int,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Select one content identity and require an exact official match length."""
    issues: list[str] = []
    readable = [
        item
        for item in summaries
        if isinstance(item, dict)
        and item.get("exists") is True
        and not item.get("issue")
    ]
    if not readable:
        return None, ["thp_missing_for_full_70_hand_round"]
    if any(not item.get("sha256") for item in readable):
        issues.append("thp_digest_missing")
    content_digests = {
        str(item.get("sha256"))
        for item in readable
        if item.get("sha256")
    }
    if len(content_digests) != 1:
        issues.append(
            "thp_ambiguous_multiple_outputs: "
            f"files={len(readable)} unique_contents={len(content_digests)}"
        )
        return None, issues
    selected = min(readable, key=lambda item: str(item.get("path") or ""))
    try:
        hand_records = int(selected.get("hand_records", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        hand_records = 0
    if hand_records != expected_hands:
        issues.append(
            "thp_hand_count_mismatch: "
            f"hands={hand_records} expected={expected_hands}"
        )
    expected_indices = list(range(expected_hands))
    if selected.get("hand_indices") != expected_indices:
        issues.append(
            "thp_hand_index_sequence_mismatch: "
            f"expected=0..{max(0, expected_hands - 1)}"
        )
    canonical = {
        "path": str(selected.get("path") or ""),
        "sha256": str(selected.get("sha256") or ""),
        "bytes": int(selected.get("bytes", 0) or 0),
        "hand_records": hand_records,
        "duplicate_paths": sorted(
            str(item.get("path") or "")
            for item in readable
            if item is not selected
        ),
    }
    return canonical, issues


def _changed_thp_paths(
    platform_dirs: Path | list[Path] | tuple[Path, ...],
    *,
    before: dict[str, tuple[int, int]],
) -> list[Path]:
    paths: dict[str, Path] = {}
    for platform_dir in _oph._coerce_platform_dirs(platform_dirs):
        for path in platform_dir.glob("THP-*.txt"):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                resolved = path.resolve()
                stat = path.stat()
            except OSError:
                continue
            if before.get(str(resolved)) != (stat.st_size, stat.st_mtime_ns):
                paths[str(resolved)] = path
    return [paths[key] for key in sorted(paths)]


def _strict_thp_match(
    text: str,
    *,
    expected_hands: int,
    expected_names: tuple[str, str],
) -> tuple[dict[str, Any] | None, list[str]]:
    issues: list[str] = []
    if len(set(expected_names)) != 2 or any(not name for name in expected_names):
        return None, ["thp_expected_players_invalid"]
    markers = [int(value) for value in _oph.THP_HAND_RE.findall(text)]
    if markers != list(range(expected_hands)):
        issues.append("thp_hand_index_sequence_mismatch")
    matches = list(_oph.THP_RECORD_RE.finditer(text))
    if len(matches) != expected_hands:
        issues.append(
            "thp_strict_record_count_mismatch: "
            f"records={len(matches)} expected={expected_hands}"
        )
    records: list[dict[str, Any]] = []
    totals = {name: 0 for name in expected_names}
    for match in matches:
        index = int(match.group(1))
        earnings = [int(match.group(4)), int(match.group(5))]
        players = [match.group(6), match.group(7)]
        if not match.group(2) or not match.group(3):
            issues.append(f"thp_record_payload_empty:{index}")
        _parsed_actions, action_issue = _oph._parse_thp_action_payload(match.group(2))
        if action_issue:
            issues.append(f"thp_record_actions_invalid:{index}:{action_issue}")
        _parsed_cards, card_issue = _oph._parse_thp_card_payload(match.group(3))
        if card_issue:
            issues.append(f"thp_record_cards_invalid:{index}:{card_issue}")
        if sum(earnings) != 0:
            issues.append(f"thp_record_earnings_not_zero_sum:{index}")
        if set(players) != set(expected_names) or len(set(players)) != 2:
            issues.append(f"thp_record_players_mismatch:{index}")
            continue
        earnings_by_player = {
            players[0]: earnings[0],
            players[1]: earnings[1],
        }
        for name, amount in earnings_by_player.items():
            totals[name] += amount
        records.append({
            "index": index,
            "actions": match.group(2),
            "cards": match.group(3),
            "earnings": earnings,
            "players": players,
            "earnings_by_player": earnings_by_player,
        })
    if [item["index"] for item in records] != list(range(expected_hands)):
        issues.append("thp_strict_record_index_sequence_mismatch")
    footers = list(_oph.THP_FOOTER_RE.finditer(text))
    if len(footers) != 1:
        issues.append(f"thp_footer_count_mismatch:{len(footers)}")
        footer = None
    else:
        footer = footers[0]
    footer_result = ""
    if footer is not None:
        footer_players = [footer.group(1), footer.group(2)]
        footer_result = footer.group(3)
        if set(footer_players) != set(expected_names) or len(set(footer_players)) != 2:
            issues.append("thp_footer_players_mismatch")
        values = list(totals.values())
        if sum(values) != 0:
            issues.append("thp_match_totals_not_zero_sum")
        elif all(value == 0 for value in values):
            if footer_result != "平局":
                issues.append("thp_footer_draw_result_mismatch")
        else:
            winner = max(totals, key=totals.get)
            amount = totals[winner]
            expected_result = f"{winner}赢得{amount}个筹码"
            if amount <= 0 or footer_result != expected_result:
                issues.append(
                    "thp_footer_result_mismatch: "
                    f"result={footer_result!r} expected={expected_result!r}"
                )
    if issues:
        return None, list(dict.fromkeys(issues))
    return {
        "records": records,
        "match_totals": totals,
        "footer_result": footer_result,
        "footer_timestamp": footer.group(4) if footer is not None else "",
        "footer_event": footer.group(5) if footer is not None else "",
    }, []


def _parse_thp_card_group(
    payload: str,
    *,
    expected_count: int,
) -> tuple[list[list[int]] | None, str]:
    tokens = _oph.THP_CARD_RE.findall(payload)
    if len(tokens) != expected_count or "".join(tokens) != payload:
        return None, f"expected_{expected_count}_cards"
    cards = [
        [_oph.THP_SUIT_TO_TCP[token[1]], _oph.THP_RANK_TO_TCP[token[0]]]
        for token in tokens
    ]
    if len({tuple(card) for card in cards}) != len(cards):
        return None, "duplicate_cards"
    return cards, ""


def _parse_thp_action_payload(
    payload: str,
) -> tuple[list[list[str]] | None, str]:
    """Parse exact per-street THP actions without accepting garbage suffixes."""

    streets = payload.split("/")
    if not payload or not 1 <= len(streets) <= 4:
        return None, "street_shape"
    parsed: list[list[str]] = []
    for index, street in enumerate(streets):
        if not street:
            return None, f"street_{index}_empty"
        tokens = _oph.THP_ACTION_TOKEN_RE.findall(street)
        if not tokens or "".join(tokens) != street:
            return None, f"street_{index}_token_shape"
        if "f" in tokens[:-1]:
            return None, f"street_{index}_fold_not_terminal"
        if tokens[-1] not in {"c", "f"}:
            return None, f"street_{index}_terminal_missing"
        if index < len(streets) - 1 and tokens[-1] != "c":
            return None, f"street_{index}_not_closed"
        parsed.append(tokens)
    return parsed, ""


def _parse_thp_card_payload(
    payload: str,
) -> tuple[dict[str, Any] | None, str]:
    parts = payload.split("/")
    if not 1 <= len(parts) <= 4:
        return None, "street_shape"
    hole_parts = parts[0].split("|")
    if len(hole_parts) != 2:
        return None, "hole_shape"
    big_blind, issue = _oph._parse_thp_card_group(
        hole_parts[0],
        expected_count=2,
    )
    if issue:
        return None, f"big_blind_{issue}"
    small_blind, issue = _oph._parse_thp_card_group(
        hole_parts[1],
        expected_count=2,
    )
    if issue:
        return None, f"small_blind_{issue}"
    public_by_stage: dict[str, list[list[int]]] = {}
    for stage, expected_count, stage_payload in zip(
        ("flop", "turn", "river"),
        (3, 1, 1),
        parts[1:],
    ):
        cards, issue = _oph._parse_thp_card_group(
            stage_payload,
            expected_count=expected_count,
        )
        if issue:
            return None, f"{stage}_{issue}"
        assert cards is not None
        public_by_stage[stage] = cards
    assert big_blind is not None and small_blind is not None
    all_cards = [
        *big_blind,
        *small_blind,
        *(card for cards in public_by_stage.values() for card in cards),
    ]
    if len({tuple(card) for card in all_cards}) != len(all_cards):
        return None, "cross_field_card_collision"
    return {
        "hole_cards_by_position": {
            "BIGBLIND": big_blind,
            "SMALLBLIND": small_blind,
        },
        "public_cards_by_stage": public_by_stage,
        "public_cards": [
            card
            for stage in ("flop", "turn", "river")
            for card in public_by_stage.get(stage, [])
        ],
    }, ""


def _single_hand_record(
    records: Any,
    *,
    hand: int,
) -> dict[str, Any] | None:
    if not isinstance(records, list):
        return None
    matches = [
        item
        for item in records
        if isinstance(item, dict) and item.get("hand") == hand
    ]
    return matches[0] if len(matches) == 1 else None


def _normalize_wire_cards(value: Any) -> list[list[int]] | None:
    if not isinstance(value, list):
        return None
    normalized: list[list[int]] = []
    for card in value:
        if (
            not isinstance(card, (list, tuple))
            or len(card) != 2
            or type(card[0]) is not int
            or type(card[1]) is not int
            or not 0 <= card[0] <= 3
            or not 0 <= card[1] <= 12
        ):
            return None
        normalized.append([card[0], card[1]])
    return normalized


def _omitted_allin_thp_bindings(
    strict_match: dict[str, Any],
    wire_summary: dict[str, Any],
    *,
    expected_hands: int,
    expected_names: tuple[str, str],
    allow_provisional_wire: bool = False,
) -> tuple[list[dict[str, Any]] | None, list[str]]:
    """Bind each omitted runout to action-bound exact-prefix-or-complete THP truth."""

    omissions = wire_summary.get("omitted_allin_runout_boundaries", [])
    provisional = wire_summary.get(
        "provisional_omitted_allin_runout_boundaries",
        [],
    )
    if not isinstance(provisional, list):
        return None, ["provisional_omitted_allin_runout_boundaries_invalid"]
    if provisional and not allow_provisional_wire:
        return None, ["omitted_allin_runout_wire_not_finalized"]
    if not omissions and allow_provisional_wire:
        omissions = provisional
    if not isinstance(omissions, list):
        return None, ["omitted_allin_runout_boundaries_invalid"]
    if not omissions:
        return [], []
    seats = wire_summary.get("seats")
    if not isinstance(seats, dict) or len(seats) != 2:
        return None, ["omitted_allin_runout_seats_invalid"]
    seat_names = {
        str(label): str(seat.get("name") or "")
        for label, seat in seats.items()
        if isinstance(seat, dict)
    }
    if (
        len(seat_names) != 2
        or set(seat_names.values()) != set(expected_names)
        or len(set(seat_names.values())) != 2
    ):
        return None, ["omitted_allin_runout_player_identity_invalid"]

    grouped: dict[int, list[dict[str, Any]]] = {}
    for item in omissions:
        if not isinstance(item, dict) or type(item.get("hand")) is not int:
            return None, ["omitted_allin_runout_boundary_invalid"]
        hand = item["hand"]
        if not 1 <= hand <= expected_hands:
            return None, [f"omitted_allin_runout_hand_invalid:{hand}"]
        grouped.setdefault(hand, []).append(item)

    bindings: list[dict[str, Any]] = []
    records = strict_match.get("records")
    if not isinstance(records, list) or len(records) != expected_hands:
        return None, ["omitted_allin_runout_thp_records_invalid"]
    stage_counts = {"preflop": 0, "flop": 3, "turn": 4}
    for hand, boundaries in sorted(grouped.items()):
        labels = {str(item.get("conn") or "") for item in boundaries}
        if len(boundaries) != 2 or labels != set(seat_names):
            return None, [f"omitted_allin_runout_peer_boundary_invalid:{hand}"]
        stage_values = {str(item.get("stage") or "") for item in boundaries}
        count_values = {item.get("public_cards_observed") for item in boundaries}
        if len(stage_values) != 1 or len(count_values) != 1:
            return None, [f"omitted_allin_runout_boundary_mismatch:{hand}"]
        stage = next(iter(stage_values))
        observed_count = next(iter(count_values))
        if stage not in stage_counts or observed_count != stage_counts[stage]:
            return None, [f"omitted_allin_runout_prefix_invalid:{hand}"]
        if any(
            item.get("natural_hand_70") is not (hand == expected_hands)
            for item in boundaries
        ):
            return None, [f"omitted_allin_runout_terminal_flag_invalid:{hand}"]

        record = records[hand - 1]
        if record.get("index") != hand - 1:
            return None, [f"omitted_allin_runout_thp_hand_invalid:{hand}"]
        thp_actions = str(record.get("actions") or "")
        thp_action_streets, thp_action_issue = _oph._parse_thp_action_payload(
            thp_actions
        )
        expected_action_streets = {
            "preflop": 1,
            "flop": 2,
            "turn": 3,
        }[stage]
        if (
            thp_action_issue
            or thp_action_streets is None
            or len(thp_action_streets) != expected_action_streets
            or len(thp_action_streets[-1]) < 2
            or not thp_action_streets[-1][-2].startswith("r")
            or thp_action_streets[-1][-1] != "c"
        ):
            return None, [f"omitted_allin_runout_thp_action_invalid:{hand}"]
        parsed_cards, card_issue = _oph._parse_thp_card_payload(
            str(record.get("cards") or "")
        )
        if card_issue or parsed_cards is None:
            return None, [
                f"omitted_allin_runout_thp_cards_invalid:{hand}:{card_issue}"
            ]
        thp_public_cards = parsed_cards["public_cards"]
        thp_board_count = len(thp_public_cards)
        if thp_board_count not in {observed_count, 5}:
            return None, [
                "omitted_allin_runout_thp_board_shape_invalid:"
                f"{hand}:observed={observed_count}:thp={thp_board_count}"
            ]
        thp_board_scope = (
            "complete_runout"
            if thp_board_count == 5
            else "observed_wire_prefix"
        )
        earnings = record.get("earnings")
        if (
            not isinstance(earnings, list)
            or sorted(earnings) not in ([-20000, 20000], [0, 0])
        ):
            return None, [f"omitted_allin_runout_thp_earnings_invalid:{hand}"]

        players = record.get("players")
        if not isinstance(players, list) or len(players) != 2:
            return None, [f"omitted_allin_runout_thp_players_invalid:{hand}"]
        holes_by_name = {
            players[0]: parsed_cards["hole_cards_by_position"]["BIGBLIND"],
            players[1]: parsed_cards["hole_cards_by_position"]["SMALLBLIND"],
        }
        seat_binding_digests: dict[str, str] = {}
        for label in sorted(seat_names):
            seat = seats[label]
            name = seat_names[label]
            blind_record = _oph._single_hand_record(
                seat.get("blind_records"),
                hand=hand,
            )
            if (
                blind_record is None
                or blind_record.get("blind") not in {"BIGBLIND", "SMALLBLIND"}
                or players[0 if blind_record.get("blind") == "BIGBLIND" else 1]
                != name
            ):
                return None, [f"omitted_allin_runout_blind_binding_invalid:{hand}:{label}"]

            peer_name = next(
                candidate for candidate in expected_names if candidate != name
            )
            showdown = _oph._single_hand_record(
                seat.get("showdown_records"),
                hand=hand,
            )
            revealed = _oph._normalize_wire_cards(
                showdown.get("opponent_cards") if showdown else None
            )
            if (
                not isinstance(revealed, list)
                or sorted(tuple(card) for card in revealed)
                != sorted(tuple(card) for card in holes_by_name[peer_name])
            ):
                return None, [f"omitted_allin_runout_showdown_binding_invalid:{hand}:{label}"]

            public_record = _oph._single_hand_record(
                seat.get("public_card_records"),
                hand=hand,
            )
            observed: list[list[int]] = []
            if public_record is not None:
                streets = public_record.get("streets")
                if not isinstance(streets, dict):
                    return None, [f"omitted_allin_runout_public_binding_invalid:{hand}:{label}"]
                for street in ("flop", "turn", "river"):
                    cards = _oph._normalize_wire_cards(streets.get(street, []))
                    if cards is None:
                        return None, [f"omitted_allin_runout_public_binding_invalid:{hand}:{label}"]
                    observed.extend(cards)
            if observed != thp_public_cards[:observed_count]:
                return None, [f"omitted_allin_runout_public_prefix_mismatch:{hand}:{label}"]
            seat_binding_digests[label] = _oph.canonical_digest({
                "name": name,
                "blind": blind_record["blind"],
                "revealed_peer_hole": revealed,
                "observed_public_prefix": observed,
            })

        binding = {
            "hand": hand,
            "stage": stage,
            "public_cards_observed": observed_count,
            "thp_record_index": hand - 1,
            "thp_actions": thp_actions,
            "thp_action_streets": thp_action_streets,
            "thp_public_cards": thp_public_cards,
            "thp_public_card_count": thp_board_count,
            "thp_board_scope": thp_board_scope,
            "thp_holes_by_player": holes_by_name,
            "thp_earnings": earnings,
            "seat_binding_digests": seat_binding_digests,
        }
        bindings.append({**binding, "binding_digest": _oph.canonical_digest(binding)})
    return bindings, []
