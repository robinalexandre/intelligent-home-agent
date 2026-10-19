"""Short-lived, in-memory conversation state.

Voice interactions are bursty: a request, maybe a follow-up ("yes, lock it"), then
silence. We keep each conversation for a few minutes so follow-ups and confirmations
work, then forget it.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PendingAction:
    """A sensitive action awaiting the user's confirmation."""

    id: str
    domain: str
    service: str
    entity_ids: list[str]
    data: dict[str, Any]
    created_turn: int
    summary: str


@dataclass
class Conversation:
    id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    turn: int = 0
    pending: dict[str, PendingAction] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def add_pending(self, **kwargs: Any) -> PendingAction:
        action = PendingAction(id=uuid.uuid4().hex[:8], created_turn=self.turn, **kwargs)
        self.pending[action.id] = action
        return action

    def trim(self, max_messages: int) -> None:
        """Drop the oldest messages while keeping the history valid for the API.

        The history must start with a plain user message (not a tool_result), otherwise
        the Messages API rejects it.
        """
        if len(self.messages) <= max_messages:
            return
        msgs = self.messages[-max_messages:]
        while msgs and not _is_plain_user_message(msgs[0]):
            msgs.pop(0)
        self.messages = msgs


def _is_plain_user_message(message: dict[str, Any]) -> bool:
    if message.get("role") != "user":
        return False
    content = message.get("content")
    if isinstance(content, str):
        return True
    return not any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    )


class ConversationStore:
    def __init__(self, ttl_seconds: int, clock: Any = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._items: dict[str, Conversation] = {}

    def get(self, conversation_id: str) -> Conversation:
        self._evict_expired()
        conv = self._items.get(conversation_id)
        if conv is None:
            conv = Conversation(id=conversation_id, updated_at=self._clock())
            self._items[conversation_id] = conv
        return conv

    def touch(self, conv: Conversation) -> None:
        conv.updated_at = self._clock()

    def reset(self, conversation_id: str) -> None:
        self._items.pop(conversation_id, None)

    def _evict_expired(self) -> None:
        now = self._clock()
        expired = [k for k, c in self._items.items() if now - c.updated_at > self._ttl]
        for key in expired:
            del self._items[key]
