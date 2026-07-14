"""Unified LLM client interface for Claude and OpenAI backends."""

import asyncio
import json
import logging
import os
from collections import OrderedDict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_MAX_RETRIES = 20
_RETRIABLE_KEYWORDS = ("Connection error", "timed out", "503", "502", "429", "overloaded", "524")


def _get_max_retries() -> int:
    return int(os.environ.get("POKERSKILL_MAX_RETRIES", str(_DEFAULT_MAX_RETRIES)))

POKER_ACTION_TOOL = {
    "name": "poker_action",
    "description": "Submit your poker decision",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["f", "k", "c", "b"],
                "description": "f=fold, k=check, c=call, b=bet/raise",
            },
            "amount": {
                "type": "number",
                "description": "Bet/raise amount in BB (only for action=b)",
            },
            "reasoning": {
                "type": "string",
                "description": "Brief 1-2 sentence explanation",
            },
        },
        "required": ["action"],
    },
}


def create_llm_client(config: Any) -> "BaseLLMClient":
    """Factory: create the appropriate LLM client based on config.backend."""
    backend = config.backend.lower()
    if backend == "claude":
        return ClaudeLLMClient(config)
    elif backend == "openai":
        return OpenAILLMClient(config)
    else:
        raise ValueError(f"Unknown backend: {backend}. Use claude/openai.")


class BaseLLMClient:
    """Base interface for LLM clients."""

    async def chat(self, hand_id: int, system_prompt: str, user_prompt: str) -> str:
        raise NotImplementedError

    def end_hand(self, hand_id: int) -> None:
        pass

    async def close(self) -> None:
        pass


class ClaudeLLMClient(BaseLLMClient):
    """Claude (Anthropic) client with tool_use for structured output."""

    def __init__(self, config: Any):
        import anthropic
        api_key = config.llm_api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        base_url = config.llm_base_url or None
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            **({"base_url": base_url} if base_url else {}),
        )
        self._model = config.model
        self._temperature = config.temperature
        self._max_tokens = config.max_tokens
        self._thinking_budget = config.thinking_budget
        self._conversations: OrderedDict = OrderedDict()
        self._max_conversations = config.num_concurrent + 2

    async def chat(self, hand_id: int, system_prompt: str, user_prompt: str) -> str:
        if hand_id not in self._conversations:
            if len(self._conversations) >= self._max_conversations:
                self._conversations.popitem(last=False)
            self._conversations[hand_id] = []

        self._conversations[hand_id].append({"role": "user", "content": user_prompt})

        kwargs: Dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system_prompt,
            "messages": self._conversations[hand_id],
            "tools": [POKER_ACTION_TOOL],
            "tool_choice": {"type": "tool", "name": "poker_action"},
        }

        if self._thinking_budget > 0:
            kwargs["temperature"] = 1
            kwargs["max_tokens"] = self._thinking_budget + 2048
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": self._thinking_budget}
        else:
            kwargs["temperature"] = self._temperature

        max_retries = _get_max_retries()
        for attempt in range(max_retries + 1):
            try:
                response = await self._client.messages.create(**kwargs)
                break
            except Exception as e:
                err_str = str(e)
                is_retriable = any(k in err_str for k in _RETRIABLE_KEYWORDS)
                if not is_retriable or attempt == max_retries:
                    self._conversations[hand_id].pop()
                    raise
                wait = min(2 ** attempt, 30)
                logger.warning(
                    f"LLM retry {attempt+1}/{max_retries} for hand#{hand_id} "
                    f"in {wait}s: {err_str[:120]}"
                )
                await asyncio.sleep(wait)

        result = ""
        tool_use_id = None
        for block in response.content:
            if block.type == "tool_use" and block.name == "poker_action":
                result = json.dumps(block.input)
                tool_use_id = block.id
                break

        if not result:
            for block in response.content:
                if hasattr(block, "text"):
                    result = block.text
                    break

        assistant_content = [
            {"type": b.type, **({"text": b.text} if b.type == "text" else
             {"id": b.id, "name": b.name, "input": b.input} if b.type == "tool_use" else {})}
            for b in response.content if b.type in ("text", "tool_use")
        ]
        self._conversations[hand_id].append({"role": "assistant", "content": assistant_content})
        if tool_use_id:
            self._conversations[hand_id].append({
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}],
            })
        return result

    def end_hand(self, hand_id: int) -> None:
        self._conversations.pop(hand_id, None)

    async def close(self) -> None:
        await self._client.close()


class OpenAILLMClient(BaseLLMClient):
    """OpenAI client with json_object response format."""

    def __init__(self, config: Any):
        from openai import AsyncOpenAI
        api_key = config.llm_api_key or os.environ.get("OPENAI_API_KEY", "")
        base_url = config.llm_base_url or None
        self._client = AsyncOpenAI(
            api_key=api_key,
            **({"base_url": base_url} if base_url else {}),
        )
        self._model = config.model
        self._temperature = config.temperature
        self._max_tokens = config.max_tokens
        self._thinking_budget = config.thinking_budget
        self._conversations: OrderedDict = OrderedDict()
        self._max_conversations = config.num_concurrent + 2

    async def chat(self, hand_id: int, system_prompt: str, user_prompt: str) -> str:
        if hand_id not in self._conversations:
            if len(self._conversations) >= self._max_conversations:
                self._conversations.popitem(last=False)
            self._conversations[hand_id] = [
                {"role": "system", "content": system_prompt},
            ]

        self._conversations[hand_id].append({"role": "user", "content": user_prompt})

        kwargs: Dict[str, Any] = {
            "model": self._model,
            "messages": self._conversations[hand_id],
            "response_format": {"type": "json_object"},
        }

        is_reasoning_model = any(
            self._model.startswith(p) for p in ("o1", "o3", "o4")
        )

        if is_reasoning_model:
            if self._thinking_budget > 0:
                kwargs["reasoning_effort"] = "high"
            kwargs.pop("response_format", None)
        else:
            kwargs["temperature"] = self._temperature
            kwargs["max_tokens"] = self._max_tokens

        max_retries = _get_max_retries()
        for attempt in range(max_retries + 1):
            try:
                response = await self._client.chat.completions.create(**kwargs)
                break
            except Exception as e:
                err_str = str(e)
                is_retriable = any(k in err_str for k in _RETRIABLE_KEYWORDS)
                if not is_retriable or attempt == max_retries:
                    self._conversations[hand_id].pop()
                    raise
                wait = min(2 ** attempt, 30)
                logger.warning(
                    f"LLM retry {attempt+1}/{max_retries} for hand#{hand_id} "
                    f"in {wait}s: {err_str[:120]}"
                )
                await asyncio.sleep(wait)

        result = response.choices[0].message.content or ""

        self._conversations[hand_id].append({"role": "assistant", "content": result})
        return result

    def end_hand(self, hand_id: int) -> None:
        self._conversations.pop(hand_id, None)

    async def close(self) -> None:
        await self._client.close()
