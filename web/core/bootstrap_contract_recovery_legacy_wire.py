"""Legacy wire causal-order false-failure subsystem for bootstrap_contract_recovery.

Extracted as a cohesive business cluster; ``bootstrap_contract_recovery.py``
retains thin delegate shells so external ``from bootstrap_contract_recovery import <name>``
and ``monkeypatch.setattr(bootstrap_contract_recovery, "<name>", ...)``
keep resolving.

Business responsibility (single cohesive domain): the legacy wire
causal-order false-failure proof builder:
* ``_legacy_wire_causalize`` (with its nested ``parse`` / ``apply_handshake``
  helpers) - rebuilds legacy parser transitions and attaches causal
  observations.
* ``_legacy_owned_replay_projection`` - reduces a current replay projection
  to the stored legacy replay schema.
* ``_legacy_replay_matches_stored`` (with its nested
  ``current_only_deferred_boundary`` helper) - proves the stored replay
  still matches the raw events.

Cross-references to symbols that remain in ``bootstrap_contract_recovery``
(the ``canonical_digest`` bot-artifact helper and the
``_LEGACY_WIRE_EVENT_FIELDS`` / ``_LEGACY_STORED_REPLAY_FIELDS`` /
``_LEGACY_POST_CLAIM_REPLAY_FIELDS`` / ``_LEGACY_FALSE_WIRE_ISSUES``
constants) are reached through ``_bcr.<name>`` so that test monkeypatches
on ``bootstrap_contract_recovery.<name>`` propagate.  ``official_wire_probe``
helpers (``OfficialWireReplay``, ``split_client_messages``,
``split_server_messages``, ``WIRE_EVENT_CAUSAL_ORDER_SCHEMA_VERSION``)
are imported lazily inside the function bodies exactly as in the original
module.

CRITICAL (wave-3 lesson): EVERY intra-companion call to a moved symbol ALSO
routes through ``_bcr.<name>(...)`` so monkeypatches on
``bootstrap_contract_recovery.<name>`` propagate even when both call sites
now live in this companion.
"""
from __future__ import annotations

import codecs
import math
import re
from typing import Any

import bootstrap_contract_recovery as _bcr  # for cross-refs


def _legacy_wire_causalize(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, int]]]:
    """Rebuild legacy parser transitions and attach causal observations.

    The old recorder appended an idle-flush semantic event after forwarding
    the raw bytes.  A fast official-EXE response could therefore be recorded
    between the raw action and its semantic flush.  This verifier trusts only
    ``raw_hex`` and the official incremental parsers, then makes that already
    observed action causally precede the response.  It is deliberately scoped
    to the exact old ``data``/``idle_flush``/``stream_eof`` event vocabulary.
    """

    from official_wire_probe import (
        WIRE_EVENT_CAUSAL_ORDER_SCHEMA_VERSION,
        split_client_messages,
        split_server_messages,
    )

    if not isinstance(events, list) or not events:
        raise ValueError("legacy wire capture is empty")
    decoders: dict[tuple[str, str], codecs.IncrementalDecoder] = {}
    buffers: dict[tuple[str, str], str] = {}
    pending: dict[tuple[str, str], dict[str, Any]] = {}
    terminated: set[tuple[str, str]] = set()
    consumed_sources: set[int] = set()
    name_requested: dict[str, bool] = {}
    causal: list[dict[str, Any]] = []
    bindings: list[dict[str, int]] = []
    observation_seq = 0
    recorder_epoch: float | None = None
    last_t = float("-inf")
    last_dt = float("-inf")

    def parse(
        conn: str,
        direction: str,
        buffer: str,
        *,
        flush: bool,
    ) -> tuple[list[str], str, str]:
        if direction == "server_to_bot":
            messages, remaining = split_server_messages(
                buffer,
                flush_numeric=flush,
            )
            return messages, remaining, "server"
        if direction == "bot_to_server":
            allow_name = bool(name_requested.get(conn, False))
            messages, remaining = split_client_messages(
                buffer,
                allow_name=allow_name,
                flush_numeric=flush,
            )
            return (
                messages,
                remaining,
                "client_name" if allow_name else "client_action",
            )
        raise ValueError("legacy wire direction is invalid")

    def apply_handshake(
        conn: str,
        direction: str,
        messages: list[str],
    ) -> None:
        if direction == "server_to_bot" and "name" in messages:
            name_requested[conn] = True
        elif direction == "bot_to_server" and messages and name_requested.get(conn):
            name_requested[conn] = False

    for record_seq, source_event in enumerate(events, 1):
        if not isinstance(source_event, dict) or set(source_event) != _bcr._LEGACY_WIRE_EVENT_FIELDS:
            raise ValueError("legacy wire event shape is invalid")
        event = dict(source_event)
        if event.get("details") != {}:
            raise ValueError("legacy wire event details are not empty")
        if (
            not isinstance(event.get("ts"), str)
            or not isinstance(event.get("conn"), str)
            or event.get("conn") not in {"A", "B"}
            or event.get("direction") not in {"server_to_bot", "bot_to_server"}
            or not isinstance(event.get("messages"), list)
            or any(not isinstance(item, str) for item in event["messages"])
            or not isinstance(event.get("remaining"), str)
            or not isinstance(event.get("raw_repr"), str)
        ):
            raise ValueError("legacy wire event payload is invalid")
        if not all(
            isinstance(event.get(field), (int, float))
            and not isinstance(event.get(field), bool)
            and math.isfinite(float(event[field]))
            for field in ("t", "dt")
        ):
            raise ValueError("legacy wire event time is invalid")
        event_t = float(event["t"])
        event_dt = float(event["dt"])
        epoch = event_t - event_dt
        if recorder_epoch is None:
            recorder_epoch = epoch
        if (
            event_dt < 0
            or event_t < last_t
            or event_dt < last_dt
            or abs(epoch - recorder_epoch) > 0.00001
        ):
            raise ValueError("legacy wire event time order is invalid")
        last_t, last_dt = event_t, event_dt
        raw_hex = event.get("raw_hex")
        if (
            not isinstance(raw_hex, str)
            or len(raw_hex) % 2
            or raw_hex != raw_hex.lower()
            or re.fullmatch(r"[0-9a-f]*", raw_hex) is None
        ):
            raise ValueError("legacy wire raw bytes are invalid")
        raw = bytes.fromhex(raw_hex)
        key = (event["conn"], event["direction"])
        if key in terminated:
            raise ValueError("legacy wire event follows stream EOF")
        decoder = decoders.setdefault(
            key,
            codecs.getincrementaldecoder("utf-8")("strict"),
        )
        buffer = buffers.get(key, "")
        event_type = event.get("event_type")

        if event_type == "data":
            if not raw:
                raise ValueError("legacy data event has no raw bytes")
            text = decoder.decode(raw, final=False)
            messages, remaining, _mode = parse(
                event["conn"],
                event["direction"],
                buffer + text,
                flush=False,
            )
            if event["messages"] != messages or event["remaining"] != remaining:
                raise ValueError("legacy data parser transition mismatch")
            observation_seq += 1
            observation = {
                "observation_seq": observation_seq,
                "observation_t": event_t,
                "observation_dt": event_dt,
                "source_record_seq": record_seq,
            }
            if remaining:
                pending[key] = observation
            else:
                pending.pop(key, None)
            buffers[key] = remaining
            apply_handshake(event["conn"], event["direction"], messages)
            causal.append({
                **event,
                "causal_order_schema_version": (
                    WIRE_EVENT_CAUSAL_ORDER_SCHEMA_VERSION
                ),
                "record_seq": record_seq,
                "observation_seq": observation_seq,
                "observation_t": event_t,
                "observation_dt": event_dt,
            })
            continue

        if event_type == "idle_flush":
            source = pending.get(key)
            pending_bytes, _flag = decoder.getstate()
            if (
                raw
                or source is None
                or int(source["observation_seq"]) in consumed_sources
                or pending_bytes
                or not buffer
            ):
                raise ValueError("legacy idle flush has no unique raw source")
            messages, remaining, mode = parse(
                event["conn"],
                event["direction"],
                buffer,
                flush=True,
            )
            if (
                not messages
                or remaining
                or event["messages"] != messages
                or event["remaining"] != remaining
            ):
                raise ValueError("legacy idle flush parser transition mismatch")
            consumed_sources.add(int(source["observation_seq"]))
            pending.pop(key, None)
            buffers[key] = remaining
            apply_handshake(event["conn"], event["direction"], messages)
            bindings.append({
                "flush_record_seq": record_seq,
                "source_record_seq": int(source["source_record_seq"]),
                "observation_seq": int(source["observation_seq"]),
            })
            causal.append({
                **event,
                "causal_order_schema_version": (
                    WIRE_EVENT_CAUSAL_ORDER_SCHEMA_VERSION
                ),
                "record_seq": record_seq,
                "observation_seq": int(source["observation_seq"]),
                "observation_t": float(source["observation_t"]),
                "observation_dt": float(source["observation_dt"]),
                "deferred_parser_mode": mode,
            })
            continue

        if event_type == "stream_eof":
            if raw or event["messages"]:
                raise ValueError("legacy stream EOF payload is invalid")
            buffer += decoder.decode(b"", final=True)
            messages, remaining, _mode = parse(
                event["conn"],
                event["direction"],
                buffer,
                flush=True,
            )
            if messages or event["remaining"] != remaining or remaining:
                raise ValueError("legacy stream EOF leaves unproved bytes")
            observation_seq += 1
            buffers[key] = remaining
            pending.pop(key, None)
            terminated.add(key)
            causal.append({
                **event,
                "causal_order_schema_version": (
                    WIRE_EVENT_CAUSAL_ORDER_SCHEMA_VERSION
                ),
                "record_seq": record_seq,
                "observation_seq": observation_seq,
                "observation_t": event_t,
                "observation_dt": event_dt,
            })
            continue

        raise ValueError("legacy wire event type is outside the defect profile")

    terminal_tail = events[-2:]
    if (
        len(terminal_tail) != 2
        or {event.get("conn") for event in terminal_tail} != {"A", "B"}
        or any(
            event.get("direction") != "bot_to_server"
            or event.get("event_type") != "stream_eof"
            for event in terminal_tail
        )
    ):
        raise ValueError("legacy wire capture has no exact terminal EOF pair")
    if (
        pending
        or terminated != {
            ("A", "bot_to_server"),
            ("B", "bot_to_server"),
        }
        or any(decoder.getstate()[0] for decoder in decoders.values())
    ):
        raise ValueError("legacy wire capture is not cleanly terminated")
    return causal, bindings


def _legacy_owned_replay_projection(observed: Any) -> dict[str, Any]:
    if not isinstance(observed, dict):
        raise ValueError("current replay projection is invalid")
    if set(observed) != (
        _bcr._LEGACY_STORED_REPLAY_FIELDS | _bcr._LEGACY_POST_CLAIM_REPLAY_FIELDS
    ):
        raise ValueError("current replay projection schema is unsupported")
    if any(observed.get(field) != [] for field in (
        _bcr._LEGACY_POST_CLAIM_REPLAY_FIELDS
    )):
        raise ValueError("legacy replay unexpectedly contains all-in runout proof")
    return {
        field: observed[field] for field in _bcr._LEGACY_STORED_REPLAY_FIELDS
    }


def _legacy_replay_matches_stored(
    events: list[dict[str, Any]],
    stored: dict[str, Any],
) -> str:
    from official_wire_probe import OfficialWireReplay

    count = stored.get("events_seen")
    if (
        type(count) is not int
        or count not in {len(events), len(events) - 1}
        or (
            count == len(events) - 1
            and events[-1].get("event_type") != "stream_eof"
        )
    ):
        raise ValueError("stored replay event count is invalid")
    replay = OfficialWireReplay()
    for event in events[:count]:
        replay.consume_event(event)
    pending = stored.get("pending_expected_actions")
    if not isinstance(pending, list):
        raise ValueError("stored replay pending actions are invalid")
    if pending:
        first = pending[0]
        if (
            not isinstance(first, dict)
            or first.get("conn") not in replay.seats
            or replay.seats[first["conn"]].expected_since is None
            or not isinstance(first.get("waited_sec"), (int, float))
            or isinstance(first.get("waited_sec"), bool)
        ):
            raise ValueError("stored replay pending clock is invalid")
        frozen_now = (
            float(replay.seats[first["conn"]].expected_since)
            + float(first["waited_sec"])
        )
    else:
        frozen_now = max(float(event["t"]) for event in events[:count])
    observed = replay.summary(now=frozen_now)
    if set(stored) != _bcr._LEGACY_STORED_REPLAY_FIELDS:
        raise ValueError("stored legacy replay schema is invalid")
    projected = _bcr._legacy_owned_replay_projection(observed)
    stored_issues = stored.get("issues")
    observed_issues = projected.get("issues")
    if not isinstance(stored_issues, list) or not isinstance(
        observed_issues,
        list,
    ):
        raise ValueError("legacy replay issues are invalid")

    def current_only_deferred_boundary(issue: Any) -> bool:
        if not isinstance(issue, dict) or issue.get("kind") != (
            "street_boundary_unproved"
        ):
            return False
        previous_stage = issue.get("previous_stage")
        observed_stage = issue.get("observed_stage")
        if (
            {"preflop": "flop", "flop": "turn", "turn": "river"}.get(
                previous_stage
            ) != observed_stage
            or issue.get("stage") != previous_stage
            or issue.get("pending_expected_action") is not True
            or not str(issue.get("message") or "").startswith(
                f"{observed_stage}|"
            )
            or issue.get("reason")
            != (
                "next public street requires an exact completed prior street "
                "or a previously proved called-all-in runout"
            )
        ):
            return False
        action_suffix = issue.get("action_suffix")
        if (
            not isinstance(action_suffix, list)
            or not 1 <= len(action_suffix) <= 2
            or any(
                not isinstance(item, dict)
                or set(item) != {
                    "actor",
                    "action_type",
                    "stage",
                    "inferred",
                }
                or item.get("actor") not in {"player", "opponent"}
                or item.get("action_type")
                not in {"raise", "call", "check"}
                or item.get("stage") != previous_stage
                or item.get("inferred") is not False
                for item in action_suffix
            )
        ):
            return False
        owners = [
            stored_issue
            for stored_issue in stored_issues
            if isinstance(stored_issue, dict)
            and stored_issue.get("kind") in _bcr._LEGACY_FALSE_WIRE_ISSUES
            and stored_issue.get("conn") == issue.get("conn")
            and stored_issue.get("hand") == issue.get("hand")
            and stored_issue.get("stage") == observed_stage
            and isinstance(stored_issue.get("dt"), (int, float))
            and not isinstance(stored_issue.get("dt"), bool)
            and isinstance(issue.get("dt"), (int, float))
            and not isinstance(issue.get("dt"), bool)
            and 0.0 <= float(stored_issue["dt"]) - float(issue["dt"]) < 60.0
        ]
        return len(owners) == 1

    projected["issues"] = [
        issue
        for issue in observed_issues
        if not current_only_deferred_boundary(issue)
    ]
    if projected != stored:
        raise ValueError("stored legacy replay does not match raw events")
    return _bcr.canonical_digest(stored)

