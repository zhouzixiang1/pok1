"""Replay event-ordering / causal-sequencing subsystem for official_wire_probe.

Extracted as a cohesive business cluster; ``official_wire_probe.py`` retains
thin delegate shells so external ``from official_wire_probe import <name>``
and ``monkeypatch.setattr(official_wire_probe, "<name>", ...)`` keep
resolving.

Business responsibility (single cohesive domain):
* Public ``replay_events`` entry point.
* Causal-order envelope reconstruction (``_causally_ordered_events``).
* Raw-bytes parser transition rebuild
  (``_causal_raw_transition_issue``) with its nested ``payload`` /
  ``raw_bytes`` / ``parse`` / ``apply_handshake`` helpers.
* Pending deferred street-boundary key set
  (``_pending_deferred_street_boundary_keys``).

Cross-references to symbols that remain in ``official_wire_probe`` (the
``OfficialWireReplay`` class, ``_dedupe_dicts``, the wire-message splitters
``split_server_messages`` / ``split_client_messages`` / ``classify_client_action``,
and the ``_CAUSAL_EVENT_FIELDS`` / ``WIRE_EVENT_CAUSAL_ORDER_SCHEMA_VERSION`` /
``MAX_WIRE_EVENT_RECORD_LAG_SEC`` constants) are reached through
``_owp.<name>`` so that test monkeypatches on ``official_wire_probe.<name>``
propagate.

CRITICAL (wave-3 lesson): EVERY intra-companion call to a moved symbol ALSO
routes through ``_owp.<name>(...)`` so monkeypatches on
``official_wire_probe.<name>`` propagate even when both call sites now live
in this companion.
"""
from __future__ import annotations

import codecs
import math
import re
from typing import Any

import official_wire_probe as _owp  # for cross-refs


def _causal_raw_transition_issue(
    events: list[dict[str, Any]],
    *,
    finalized: bool,
) -> str | None:
    """Rebuild every schema-v1 parser transition from captured raw bytes.

    ``messages`` and ``remaining`` are useful diagnostics, but they are not an
    authority boundary: both are stored in the same JSONL event as ``raw_hex``.
    A finalized replay therefore reconstructs the incremental UTF-8 decoder and
    the national no-delimiter tokenizers for each connection/direction and
    requires every stored transition to match exactly.
    """

    decoders: dict[tuple[str, str], codecs.IncrementalDecoder] = {}
    buffers: dict[tuple[str, str], str] = {}
    eof_finalized: set[tuple[str, str]] = set()
    terminated: set[tuple[str, str]] = set()
    name_requested: dict[str, bool] = {}

    def payload(event: dict[str, Any]) -> tuple[list[str], str] | None:
        messages = event.get("messages")
        remaining = event.get("remaining")
        if (
            not isinstance(messages, list)
            or any(not isinstance(message, str) for message in messages)
            or not isinstance(remaining, str)
        ):
            return None
        return list(messages), remaining

    def raw_bytes(event: dict[str, Any]) -> bytes | None:
        raw_hex = event.get("raw_hex")
        if not isinstance(raw_hex, str) or len(raw_hex) % 2:
            return None
        if raw_hex != raw_hex.lower() or re.fullmatch(r"[0-9a-f]*", raw_hex) is None:
            return None
        try:
            return bytes.fromhex(raw_hex)
        except ValueError:
            return None

    def parse(
        conn: str,
        direction: str,
        buffer: str,
        *,
        flush_boundary: bool,
    ) -> tuple[list[str], str, str] | None:
        if direction == "server_to_bot":
            messages, remaining = _owp.split_server_messages(
                buffer,
                flush_numeric=flush_boundary,
            )
            return messages, remaining, "server"
        if direction == "bot_to_server":
            allow_name = bool(name_requested.get(conn, False))
            messages, remaining = _owp.split_client_messages(
                buffer,
                allow_name=allow_name,
                flush_numeric=flush_boundary,
            )
            return (
                messages,
                remaining,
                "client_name" if allow_name else "client_action",
            )
        return None

    def apply_handshake(conn: str, direction: str, messages: list[str]) -> None:
        if direction == "server_to_bot" and "name" in messages:
            name_requested[conn] = True
        elif direction == "bot_to_server" and messages and name_requested.get(conn):
            name_requested[conn] = False

    for event in events:
        event_type = event.get("event_type")
        conn = event.get("conn")
        direction = event.get("direction")
        if not isinstance(event_type, str) or not isinstance(conn, str) or not isinstance(direction, str):
            return "causal_wire_event_shape_invalid"
        stored = payload(event)
        raw = raw_bytes(event)
        if stored is None or raw is None:
            return "causal_wire_event_payload_invalid"
        stored_messages, stored_remaining = stored

        if event_type in {"capture_finalized", "upstream_connect_failed"}:
            if (
                direction != "probe_lifecycle"
                or raw
                or stored_messages
                or stored_remaining
            ):
                return "causal_wire_lifecycle_payload_invalid"
            continue

        if direction not in {"server_to_bot", "bot_to_server"}:
            return "causal_wire_event_direction_invalid"
        key = (conn, direction)
        if key in terminated:
            return "causal_wire_event_after_terminal"
        decoder = decoders.setdefault(
            key,
            codecs.getincrementaldecoder("utf-8")("strict"),
        )
        buffer = buffers.get(key, "")

        if event_type == "data":
            if not raw or key in eof_finalized:
                return "causal_wire_data_payload_invalid"
            try:
                text = decoder.decode(raw, final=False)
            except UnicodeDecodeError:
                return "causal_wire_data_decode_mismatch"
            parsed = parse(
                conn,
                direction,
                buffer + text,
                flush_boundary=False,
            )
            if parsed is None:
                return "causal_wire_event_direction_invalid"
            messages, remaining, _mode = parsed
            if stored_messages != messages or stored_remaining != remaining:
                return "causal_wire_data_parse_mismatch"
            buffers[key] = remaining
            apply_handshake(conn, direction, messages)
            continue

        if event_type in {"idle_flush", "eof_flush"}:
            if raw or not buffer:
                return "causal_wire_flush_payload_invalid"
            if event_type == "idle_flush":
                pending_bytes, _flag = decoder.getstate()
                if pending_bytes:
                    return "causal_wire_idle_flush_utf8_incomplete"
            else:
                if key in eof_finalized:
                    return "causal_wire_duplicate_eof_flush"
                try:
                    buffer += decoder.decode(b"", final=True)
                except UnicodeDecodeError:
                    return "causal_wire_eof_decode_mismatch"
                eof_finalized.add(key)
            parsed = parse(
                conn,
                direction,
                buffer,
                flush_boundary=True,
            )
            if parsed is None:
                return "causal_wire_event_direction_invalid"
            messages, remaining, mode = parsed
            if event.get("deferred_parser_mode") != mode:
                return "causal_wire_event_deferred_parser_invalid"
            if (
                not messages
                or stored_messages != messages
                or stored_remaining != remaining
            ):
                return "causal_wire_deferred_parse_mismatch"
            buffers[key] = remaining
            apply_handshake(conn, direction, messages)
            continue

        if event_type == "stream_eof":
            if raw or stored_messages:
                return "causal_wire_stream_eof_payload_invalid"
            if key not in eof_finalized:
                try:
                    buffer += decoder.decode(b"", final=True)
                except UnicodeDecodeError:
                    return "causal_wire_eof_decode_mismatch"
                eof_finalized.add(key)
                parsed = parse(
                    conn,
                    direction,
                    buffer,
                    flush_boundary=True,
                )
                if parsed is None:
                    return "causal_wire_event_direction_invalid"
                messages, buffer, _mode = parsed
                if messages:
                    return "causal_wire_eof_flush_missing"
            if stored_remaining != buffer:
                return "causal_wire_event_terminal_remainder_mismatch"
            buffers[key] = buffer
            terminated.add(key)
            continue

        if event_type in {"stream_cancelled", "stream_error"}:
            if raw or stored_messages or stored_remaining != buffer:
                return "causal_wire_event_terminal_remainder_mismatch"
            pending_bytes, _flag = decoder.getstate()
            if pending_bytes:
                return "causal_wire_event_pending_utf8_unresolved"
            terminated.add(key)
            continue

        if event_type == "stream_encoding_error":
            if stored_messages or stored_remaining != buffer:
                return "causal_wire_encoding_error_payload_invalid"
            probe_decoder = codecs.getincrementaldecoder("utf-8")("strict")
            probe_decoder.setstate(decoder.getstate())
            try:
                probe_decoder.decode(raw, final=not raw)
            except UnicodeDecodeError:
                terminated.add(key)
                continue
            return "causal_wire_encoding_error_unproven"

        return "causal_wire_event_type_invalid"

    if finalized:
        for key, decoder in decoders.items():
            pending_bytes, _flag = decoder.getstate()
            if key not in terminated and pending_bytes:
                return "causal_wire_event_pending_utf8_unresolved"

    return None


def _causally_ordered_events(
    events: list[dict[str, Any]],
    *,
    finalized: bool,
) -> tuple[list[dict[str, Any]], str | None]:
    if not events:
        return (
            [],
            "causal_wire_capture_finalized_missing" if finalized else None,
        )
    field_presence = [
        any(field in event for field in _owp._CAUSAL_EVENT_FIELDS)
        for event in events
    ]
    if not any(field_presence):
        # Immutable oracle captures predate the causal-order envelope.  Their
        # append order remains the only available authority.
        return list(events), None
    if not all(field_presence):
        return [], "mixed_legacy_and_causal_wire_events"

    sources: dict[int, dict[str, Any]] = {}
    reused_observations: set[int] = set()
    pending_observations: dict[tuple[str, str], int] = {}
    max_observation_seq = 0
    last_observation_t = float("-inf")
    last_observation_dt = float("-inf")
    last_record_t = float("-inf")
    last_record_dt = float("-inf")
    recorder_epoch: float | None = None
    for expected_record_seq, event in enumerate(events, 1):
        if not _owp._CAUSAL_EVENT_FIELDS.issubset(event):
            return [], "causal_wire_event_fields_missing"
        if (
            type(event.get("causal_order_schema_version")) is not int
            or event["causal_order_schema_version"]
            != _owp.WIRE_EVENT_CAUSAL_ORDER_SCHEMA_VERSION
        ):
            return [], "causal_wire_event_schema_invalid"
        if (
            type(event.get("record_seq")) is not int
            or event["record_seq"] != expected_record_seq
        ):
            return [], "causal_wire_event_record_seq_invalid"
        observation_seq = event.get("observation_seq")
        if type(observation_seq) is not int or observation_seq <= 0:
            return [], "causal_wire_event_observation_seq_invalid"
        if not all(
            isinstance(event.get(field), (int, float))
            and not isinstance(event.get(field), bool)
            and math.isfinite(float(event[field]))
            for field in ("t", "dt", "observation_t", "observation_dt")
        ):
            return [], "causal_wire_event_time_invalid"
        record_t = float(event["t"])
        record_dt = float(event["dt"])
        observation_t = float(event["observation_t"])
        observation_dt = float(event["observation_dt"])
        event_epoch = record_t - record_dt
        observation_epoch = observation_t - observation_dt
        if recorder_epoch is None:
            recorder_epoch = event_epoch
        source = sources.get(observation_seq)
        if (
            record_dt < 0
            or observation_dt < 0
            or record_t < last_record_t
            or record_dt < last_record_dt
            or record_t < observation_t
            or record_dt + 0.000001 < observation_dt
            or abs(event_epoch - recorder_epoch) > 0.00001
            or abs(observation_epoch - recorder_epoch) > 0.00001
            or (
                source is None
                and record_t - observation_t > _owp.MAX_WIRE_EVENT_RECORD_LAG_SEC
            )
        ):
            return [], "causal_wire_event_record_time_invalid"
        last_record_t = record_t
        last_record_dt = record_dt

        key = (str(event.get("conn") or ""), str(event.get("direction") or ""))
        if source is None:
            if event.get("event_type") in {"idle_flush", "eof_flush"}:
                return [], "causal_wire_event_flush_source_missing"
            if observation_seq != max_observation_seq + 1:
                return [], "causal_wire_event_observation_gap"
            if (
                observation_t < last_observation_t
                or observation_dt < last_observation_dt
            ):
                return [], "causal_wire_event_observation_time_invalid"
            sources[observation_seq] = event
            max_observation_seq = observation_seq
            last_observation_t = observation_t
            last_observation_dt = observation_dt
            if event.get("event_type") in {
                "stream_eof",
                "stream_cancelled",
                "stream_error",
            }:
                pending_seq = pending_observations.get(key)
                pending_source = sources.get(pending_seq) if pending_seq else None
                expected_remaining = str(
                    (pending_source or {}).get("remaining") or ""
                )
                if str(event.get("remaining") or "") != expected_remaining:
                    return [], "causal_wire_event_terminal_remainder_mismatch"
            if event.get("raw_hex") not in {"", None}:
                if event.get("remaining"):
                    pending_observations[key] = observation_seq
                else:
                    pending_observations.pop(key, None)
            continue

        parser_mode = event.get("deferred_parser_mode")
        if (
            observation_seq in reused_observations
            or pending_observations.get(key) != observation_seq
            or event.get("event_type") not in {"idle_flush", "eof_flush"}
            or event.get("raw_hex") not in {"", None}
            or not event.get("messages")
            or source.get("raw_hex") in {"", None}
            or not source.get("remaining")
            or event.get("conn") != source.get("conn")
            or event.get("direction") != source.get("direction")
            or float(event["observation_t"]) != float(source["observation_t"])
            or float(event["observation_dt"]) != float(source["observation_dt"])
        ):
            return [], "causal_wire_event_observation_reuse_invalid"
        if parser_mode == "server" and event.get("direction") == "server_to_bot":
            parsed_messages, parsed_remaining = _owp.split_server_messages(
                str(source.get("remaining") or ""),
                flush_numeric=True,
            )
        elif parser_mode in {"client_name", "client_action"} and event.get(
            "direction"
        ) == "bot_to_server":
            parsed_messages, parsed_remaining = _owp.split_client_messages(
                str(source.get("remaining") or ""),
                allow_name=parser_mode == "client_name",
                flush_numeric=True,
            )
        else:
            return [], "causal_wire_event_deferred_parser_invalid"
        if (
            list(event.get("messages") or []) != parsed_messages
            or str(event.get("remaining") or "") != parsed_remaining
        ):
            return [], "causal_wire_event_deferred_parse_mismatch"
        reused_observations.add(observation_seq)
        if parsed_remaining:
            pending_observations[key] = observation_seq
        else:
            pending_observations.pop(key, None)

    final_markers = [
        (index, event)
        for index, event in enumerate(events)
        if event.get("event_type") == "capture_finalized"
    ]
    if len(final_markers) > 1:
        return [], "causal_wire_capture_finalized_duplicate"
    if final_markers:
        marker_index, marker = final_markers[0]
        if (
            marker_index != len(events) - 1
            or marker.get("conn") != "*"
            or marker.get("direction") != "probe_lifecycle"
            or marker.get("raw_hex") not in {"", None}
            or marker.get("messages") != []
            or marker.get("remaining") != ""
            or marker.get("details") not in ({}, None)
        ):
            return [], "causal_wire_capture_finalized_invalid"
    elif finalized:
        return [], "causal_wire_capture_finalized_missing"

    raw_transition_issue = _owp._causal_raw_transition_issue(
        events,
        finalized=finalized,
    )
    if raw_transition_issue is not None:
        return [], raw_transition_issue

    if finalized and pending_observations:
        return [], "causal_wire_event_pending_buffer_unresolved"

    ordered = sorted(
        events,
        key=lambda event: (
            int(event["observation_seq"]),
            1 if event.get("event_type") in {"idle_flush", "eof_flush"} else 0,
            int(event["record_seq"]),
        ),
    )
    return ordered, None


def replay_events(
    events: list[dict[str, Any]],
    *,
    now: float | None = None,
    finalized: bool = False,
) -> dict[str, Any]:
    replay = _owp.OfficialWireReplay()
    ordered, causal_issue = _owp._causally_ordered_events(
        events,
        finalized=finalized,
    )
    if causal_issue is not None:
        replay.events_seen = len(events)
        replay.issues.append({
            "kind": "wire_event_causal_order_invalid",
            "conn": "?",
            "hand": None,
            "stage": None,
            "message": "",
            "reason": causal_issue,
        })
        return replay.summary(now=now, finalized=finalized)
    for event in ordered:
        consumed = event
        if "observation_seq" in event:
            consumed = dict(event)
            consumed["recorded_t"] = event.get("t")
            consumed["recorded_dt"] = event.get("dt")
            consumed["t"] = float(event["observation_t"])
            consumed["dt"] = float(event["observation_dt"])
        replay.consume_event(consumed)
    summary_now = now
    if finalized and summary_now is None and ordered:
        if "observation_t" in ordered[0]:
            summary_now = max(float(event["observation_t"]) for event in ordered)
        else:
            summary_now = max(float(event.get("t", 0.0) or 0.0) for event in ordered)
    summary = replay.summary(now=summary_now, finalized=finalized)
    if not finalized:
        # A no-delimiter client action may be present in raw bytes while its
        # semantic token is awaiting the recorder's bounded idle flush.  Only
        # that exact causal-envelope state may make a same-connection street
        # boundary provisional.  Legacy captures and genuinely unclosed
        # streets remain immediate issues, and finalized replay remains
        # fail-closed if the matching flush never arrives.
        provisional_boundary_keys = _owp._pending_deferred_street_boundary_keys(events)
        provisional = [
            issue
            for issue in list(summary.get("issues") or [])
            if issue.get("kind") == "street_boundary_unproved"
            and (
                str(issue.get("conn") or ""),
                float(issue.get("dt", -1.0) or -1.0),
                str(issue.get("message") or ""),
            ) in provisional_boundary_keys
        ]
        if provisional:
            summary["issues"] = [
                issue
                for issue in list(summary.get("issues") or [])
                if issue not in provisional
            ]
            summary["warnings"] = _owp._dedupe_dicts([
                *list(summary.get("warnings") or []),
                *[
                    {
                        **issue,
                        "kind": "provisional_street_boundary_unproved",
                        "strict_issue_kind": "street_boundary_unproved",
                    }
                    for issue in provisional
                ],
            ])
    return summary


def _pending_deferred_street_boundary_keys(
    events: list[dict[str, Any]],
) -> set[tuple[str, float, str]]:
    """Bind each pending client buffer to its first later public boundary."""

    if not events or not all(
        _owp._CAUSAL_EVENT_FIELDS.issubset(event)
        for event in events
    ):
        return set()
    pending: dict[tuple[str, str], int] = {}
    sources: dict[int, dict[str, Any]] = {}
    for event in events:
        observation_seq = event.get("observation_seq")
        if type(observation_seq) is not int:
            return set()
        key = (
            str(event.get("conn") or ""),
            str(event.get("direction") or ""),
        )
        if observation_seq not in sources:
            sources[observation_seq] = event
            if event.get("raw_hex") not in {"", None}:
                if event.get("remaining"):
                    pending[key] = observation_seq
                else:
                    pending.pop(key, None)
            continue
        if (
            event.get("event_type") in {"idle_flush", "eof_flush"}
            and pending.get(key) == observation_seq
        ):
            if event.get("remaining"):
                pending[key] = observation_seq
            else:
                pending.pop(key, None)
    boundary_keys: set[tuple[str, float, str]] = set()
    for (conn, direction), source_observation_seq in pending.items():
        if direction != "bot_to_server" or not conn:
            continue
        source = sources.get(source_observation_seq) or {}
        pending_messages, pending_remaining = _owp.split_client_messages(
            str(source.get("remaining") or ""),
            allow_name=False,
            flush_numeric=True,
        )
        if (
            len(pending_messages) != 1
            or pending_remaining
            or _owp.classify_client_action(pending_messages[0])[2] is not None
        ):
            continue
        candidates: list[tuple[int, float, str]] = []
        for event in events:
            if (
                event.get("conn") != conn
                or event.get("direction") != "server_to_bot"
                or type(event.get("observation_seq")) is not int
                or int(event["observation_seq"]) <= source_observation_seq
            ):
                continue
            for message in event.get("messages") or []:
                if str(message).startswith(("flop|", "turn|", "river|")):
                    candidates.append((
                        int(event["observation_seq"]),
                        float(event.get("observation_dt", -1.0) or -1.0),
                        str(message),
                    ))
        if candidates:
            _observation_seq, boundary_dt, message = min(candidates)
            boundary_keys.add((conn, boundary_dt, message))
    return boundary_keys
