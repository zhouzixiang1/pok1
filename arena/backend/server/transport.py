"""Shared raw-TCP transport for national competition clients.

Client actions are short UTF-8 tokens without a required newline.  A TCP read
is not a message boundary, so variable-length actions such as ``raise 200``
are committed only after a short idle boundary (or an explicit newline).
"""

from __future__ import annotations

import asyncio
import codecs
from collections.abc import Awaitable, Callable
import inspect
import time
from typing import Any


MAX_CLIENT_BUFFER_BYTES = 16_384
MAX_TEAM_NAME_BYTES = 256
DEFAULT_CLIENT_IDLE_FLUSH_SEC = 0.01
FIXED_ACTIONS = ("allin", "check", "call", "fold")
VARIABLE_ACTIONS = ("raise ", "bet ")
ACTION_PREFIXES = FIXED_ACTIONS + VARIABLE_ACTIONS


class NationalProtocolError(RuntimeError):
    """A client stream cannot be represented by the national protocol."""


def _is_action_prefix(value: str) -> bool:
    return any(token.startswith(value) for token in ACTION_PREFIXES)


def _starts_action(value: str) -> bool:
    return any(value.startswith(token) for token in ACTION_PREFIXES)


def pop_client_action(buffer: str, *, terminal: bool) -> tuple[str | None, str]:
    """Pop one client action without treating the current TCP read as a frame.

    ``terminal`` means an explicit newline or an idle boundary has proved that
    no more bytes belong to the current token.  Leading/trailing spaces are
    intentionally preserved so the official validator can reject them.
    """

    while buffer.startswith(("\r", "\n")):
        buffer = buffer[1:]
    if not buffer:
        return None, ""

    if "\n" in buffer:
        line, remainder = buffer.split("\n", 1)
        if line.endswith("\r"):
            line = line[:-1]
        return (line if line else None), remainder

    for token in FIXED_ACTIONS:
        if buffer == token:
            return (token, "") if terminal else (None, buffer)
        if buffer.startswith(token):
            remainder = buffer[len(token):]
            if _starts_action(remainder):
                return token, remainder
            if _is_action_prefix(remainder):
                return None, buffer
            return (buffer, "") if terminal else (None, buffer)

    for prefix in VARIABLE_ACTIONS:
        if not buffer.startswith(prefix):
            continue
        pos = len(prefix)
        while pos < len(buffer) and buffer[pos].isdigit():
            pos += 1
        if pos == len(prefix):
            return (buffer, "") if terminal else (None, buffer)
        if pos == len(buffer):
            return (buffer, "") if terminal else (None, buffer)
        remainder = buffer[pos:]
        if _starts_action(remainder):
            return buffer[:pos], remainder
        if _is_action_prefix(remainder):
            return None, buffer
        return (buffer, "") if terminal else (None, buffer)

    if _is_action_prefix(buffer):
        return None, buffer
    return (buffer, "") if terminal else (None, buffer)


class NationalTCPClient:
    """One national client connection with content-driven stream framing."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        idle_flush_sec: float = DEFAULT_CLIENT_IDLE_FLUSH_SEC,
        max_buffer_bytes: int = MAX_CLIENT_BUFFER_BYTES,
        wire_sink: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.name = ""
        self.closed = False
        self.idle_flush_sec = max(0.001, float(idle_flush_sec))
        self.max_buffer_bytes = max(256, int(max_buffer_bytes))
        self.wire_sink = wire_sink
        self._buffer = ""
        self._buffer_bytes = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")

    @property
    def peername(self) -> Any:
        return self.writer.get_extra_info("peername")

    async def _notify(self, **event: Any) -> None:
        if self.wire_sink is None:
            return
        try:
            result = self.wire_sink({"timestamp": time.time(), **event})
            if inspect.isawaitable(result):
                await result
        except Exception:
            # Observability must not alter protocol behaviour.
            return

    async def send_line(self, message: str) -> None:
        if self.closed:
            return
        payload = (message + "\n").encode("utf-8")
        self.writer.write(payload)
        await self.writer.drain()
        await self._notify(
            direction="server_to_bot",
            phase="message",
            payload=message,
            byte_count=len(payload),
        )

    async def _read_chunk(self, timeout: float) -> bool:
        try:
            chunk = await asyncio.wait_for(self.reader.read(4096), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        if not chunk:
            self.closed = True
            try:
                tail = self._decoder.decode(b"", final=True)
            except UnicodeDecodeError as exc:
                raise NationalProtocolError("client_invalid_utf8") from exc
            if tail:
                self._buffer += tail
            return False
        self._buffer_bytes += len(chunk)
        await self._notify(
            direction="bot_to_server",
            phase="chunk",
            payload=chunk.decode("utf-8", errors="backslashreplace"),
            byte_count=len(chunk),
        )
        if self._buffer_bytes > self.max_buffer_bytes:
            raise NationalProtocolError("client_buffer_limit_exceeded")
        try:
            self._buffer += self._decoder.decode(chunk, final=False)
        except UnicodeDecodeError as exc:
            raise NationalProtocolError("client_invalid_utf8") from exc
        return True

    async def recv_name(self, timeout: float) -> str | None:
        deadline = asyncio.get_running_loop().time() + max(0.001, float(timeout))
        while not self.closed:
            if "\n" in self._buffer:
                name, self._buffer = self._buffer.split("\n", 1)
                self._buffer_bytes = len(self._buffer.encode("utf-8"))
                name = name.removesuffix("\r")
                return await self._finish_name(name)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            wait = min(remaining, self.idle_flush_sec) if self._buffer else remaining
            if not await self._read_chunk(wait):
                if self._buffer:
                    name, self._buffer = self._buffer, ""
                    self._buffer_bytes = 0
                    return await self._finish_name(name)
                if self.closed:
                    return None
        return None

    async def _finish_name(self, name: str) -> str:
        if not name or len(name.encode("utf-8")) > MAX_TEAM_NAME_BYTES:
            raise NationalProtocolError("invalid_team_name")
        if any(ord(char) < 32 for char in name):
            raise NationalProtocolError("invalid_team_name_control_character")
        await self._notify(
            direction="bot_to_server",
            phase="message",
            payload=name,
            message_type="name",
            byte_count=len(name.encode("utf-8")),
        )
        return name

    async def recv_action(self, timeout: float) -> str | None:
        deadline = asyncio.get_running_loop().time() + max(0.001, float(timeout))
        while not self.closed:
            action, remainder = pop_client_action(self._buffer, terminal=False)
            if action is not None:
                return await self._finish_action(action, remainder)
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            wait = min(remaining, self.idle_flush_sec) if self._buffer else remaining
            if not await self._read_chunk(wait):
                if self._buffer:
                    action, remainder = pop_client_action(self._buffer, terminal=True)
                    if action is not None:
                        return await self._finish_action(action, remainder)
                if self.closed:
                    return None
        return None

    async def _finish_action(self, action: str, remainder: str) -> str:
        self._buffer = remainder
        self._buffer_bytes = len(remainder.encode("utf-8"))
        extra, _unused = pop_client_action(remainder, terminal=True)
        if extra is not None:
            self._buffer = ""
            self._buffer_bytes = 0
            action = f"protocol_multiple_actions:{action}|{extra}"
        await self._notify(
            direction="bot_to_server",
            phase="message",
            payload=action,
            message_type="action",
            byte_count=len(action.encode("utf-8")),
        )
        return action

    async def recv_line(self, timeout: float) -> str | None:
        """Compatibility name used by the national game engine."""

        try:
            return await self.recv_action(timeout)
        except NationalProtocolError as exc:
            return f"protocol_error:{exc}"

    async def close(self, timeout: float = 1.0) -> None:
        self.closed = True
        self.writer.close()
        try:
            await asyncio.wait_for(self.writer.wait_closed(), timeout=timeout)
        except (asyncio.TimeoutError, OSError, ConnectionError):
            pass
