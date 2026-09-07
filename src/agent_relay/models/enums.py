"""The event vocabulary. This is the contract every agent codes against."""

from __future__ import annotations

from enum import StrEnum


class EventType(StrEnum):
    UPDATE = "UPDATE"
    QUESTION = "QUESTION"
    ANSWER = "ANSWER"
    CLAIM = "CLAIM"
    HANDOFF = "HANDOFF"
    BLOCKED = "BLOCKED"
    DECISION = "DECISION"
    RELEASE = "RELEASE"


#: Emoji + label used for Slack rendering and CLI output.
EVENT_LABELS: dict[EventType, str] = {
    EventType.UPDATE: "🔄",
    EventType.QUESTION: "❓",
    EventType.ANSWER: "💬",
    EventType.CLAIM: "🔒",
    EventType.HANDOFF: "🤝",
    EventType.BLOCKED: "🚧",
    EventType.DECISION: "📌",
    EventType.RELEASE: "🔓",
}

#: Posting one of these on a task clears an earlier BLOCKED on the same task.
#: Deterministic and boring on purpose: nobody has to guess why a task looks blocked.
UNBLOCKING_EVENTS: frozenset[EventType] = frozenset(
    {
        EventType.UPDATE,
        EventType.ANSWER,
        EventType.DECISION,
        EventType.HANDOFF,
        EventType.RELEASE,
    }
)
#: Event types that represent *doing the work*, as opposed to talking about it or
#: transferring it. Only these can signal duplicated effort: two agents discussing a
#: task (QUESTION/ANSWER) or passing it along (CLAIM/HANDOFF/RELEASE) is coordination
#: working correctly, not a conflict.
WORK_EVENTS: frozenset[EventType] = frozenset(
    {EventType.UPDATE, EventType.DECISION, EventType.BLOCKED}
)
