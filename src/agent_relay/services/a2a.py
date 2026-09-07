"""A2A (agent-to-agent) discovery and message ingestion — a deliberate subset.

Purpose: let an agent built on *someone else's* framework find this relay and talk to
it without first reading our OpenAPI schema. That needs exactly two things — a
published Agent Card and one method that accepts a message — so that is what is
implemented.

Supported
---------
* ``GET /.well-known/agent.json`` — an A2A-style Agent Card (see :func:`agent_card`).
* ``message/send`` over JSON-RPC 2.0, with a **text** part, returning a ``message``
  result carrying a **text** part.
* Three intents, matched with deterministic keyword/regex rules (no LLM, no model
  call, no ambiguity a human cannot reproduce by reading the regexes below)::

      context for <project>
      tasks for <project>
      post update for <project>[ task <TASK>]: <summary>

Not supported, on purpose
-------------------------
* ``message/stream``, ``tasks/get``, ``tasks/cancel``, ``tasks/resubscribe``,
  ``tasks/pushNotificationConfig/*`` — the relay has no long-running A2A task
  lifecycle, so advertising one would be a lie. ``capabilities.streaming`` and
  ``capabilities.pushNotifications`` are both ``false``.
* Non-text parts (files, structured data), multi-turn ``contextId`` threading, and
  A2A authentication schemes. The relay's own optional bearer token guards the RPC
  endpoint instead.
* Natural language beyond the three intents above. Anything else gets a polite
  result listing what *is* understood, never a guess.

This module is transport-agnostic: the JSON-RPC envelope (ids, error codes) is the
router's job, not ours. Everything here takes and returns plain dicts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from agent_relay import __version__
from agent_relay.config import Settings
from agent_relay.models.enums import EventType
from agent_relay.models.schemas import EventCreate
from agent_relay.services import context as context_service
from agent_relay.services import events as event_service
from agent_relay.services import state as state_service

#: Who an A2A caller is recorded as when it does not identify itself. Distinct from
#: any real agent name so the event log never lies about provenance.
DEFAULT_AGENT = "a2a-client"
#: Prefix that keeps an external peer's chosen name out of the team's namespace.
AGENT_PREFIX = "a2a:"


def _namespaced_agent(name: str) -> str:
    """`leo-codex` -> `a2a:leo-codex`, and an already-prefixed name is left alone."""
    cleaned = name.strip() or DEFAULT_AGENT
    return cleaned if cleaned.startswith(AGENT_PREFIX) else f"{AGENT_PREFIX}{cleaned}"


DEFAULT_PORT_URL = "http://127.0.0.1:8077"
MAX_LIST_ROWS = 10

SUPPORTED_INTENTS = (
    "context for <project>",
    "tasks for <project>",
    "post update for <project>[ task <TASK>]: <summary>",
)

# --- intent grammar -------------------------------------------------------------
# One regex per intent, readable end to end. A project name is whatever follows a
# preposition (or the keyword itself), restricted to the characters a project name
# is allowed to use.
_NAME = r"[A-Za-z0-9][\w.\-]*"
_FOR = rf"(?:for|in|on|about|of)\s+(?:the\s+)?(?:project\s+)?(?P<project>{_NAME})"
_CONTEXT_WORDS = r"context|status|brief|briefing|situation|summary|state|catch\s+me\s+up"

_POST_RE = re.compile(
    rf"\b(?:post|log|record|add|send)\b[^:]*?\bupdate\b[^:]*?{_FOR}(?P<tail>[^:]*):\s*"
    r"(?P<summary>.+)",
    re.IGNORECASE | re.DOTALL,
)
_TASKS_RE = re.compile(rf"\btasks?\b.*?{_FOR}", re.IGNORECASE | re.DOTALL)
_TASKS_BARE_RE = re.compile(rf"\btasks?\s+(?P<project>{_NAME})\s*$", re.IGNORECASE)
_CONTEXT_RE = re.compile(rf"\b(?:{_CONTEXT_WORDS})\b.*?{_FOR}", re.IGNORECASE | re.DOTALL)
_CONTEXT_BARE_RE = re.compile(rf"\b(?:{_CONTEXT_WORDS})\s+(?P<project>{_NAME})\s*$", re.IGNORECASE)
_TASK_REF_RE = re.compile(rf"\btask\s+(?P<task>{_NAME})", re.IGNORECASE)
_ISSUE_REF_RE = re.compile(r"\b(?P<task>[A-Z]{2,}-\d+)\b")


@dataclass(frozen=True)
class Intent:
    """What we understood. ``name`` is ``unsupported`` when we understood nothing."""

    name: str
    project: str | None = None
    task: str | None = None
    summary: str | None = None


def parse_intent(text: str) -> Intent:
    """Deterministic keyword matching. Order matters: the most specific rule wins."""
    cleaned = " ".join(text.split())
    if not cleaned:
        return Intent("unsupported")

    if (match := _POST_RE.search(cleaned)) is not None:
        tail = match["tail"] or ""
        task = _TASK_REF_RE.search(tail) or _ISSUE_REF_RE.search(tail)
        return Intent(
            "post_update",
            project=match["project"],
            task=task["task"] if task else None,
            summary=match["summary"].strip() or None,
        )
    for pattern in (_TASKS_RE, _TASKS_BARE_RE):
        if (match := pattern.search(cleaned)) is not None:
            return Intent("list_tasks", project=match["project"])
    for pattern in (_CONTEXT_RE, _CONTEXT_BARE_RE):
        if (match := pattern.search(cleaned)) is not None:
            return Intent("get_context", project=match["project"])
    return Intent("unsupported")


# --- agent card -----------------------------------------------------------------


def base_url(settings: Settings) -> str:
    """Where other agents should reach this relay."""
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    host = settings.host or "127.0.0.1"
    return f"http://{host}:{settings.port}" if settings.port else DEFAULT_PORT_URL


def _skills() -> list[dict[str, Any]]:
    """One skill per real capability. ``x-transports`` says how each is reachable.

    ``http`` means "call the REST endpoint"; ``a2a`` means "an intent ``message/send``
    understands". Claiming and coordination summaries are HTTP-only today, and the
    card says so rather than implying an intent that does not exist.
    """
    return [
        {
            "id": "post_event",
            "name": "Post a coordination event",
            "description": (
                "Record a structured event (UPDATE, QUESTION, ANSWER, BLOCKED, DECISION, "
                "HANDOFF) on a project, optionally against a task."
            ),
            "tags": ["coordination", "events", "write"],
            "examples": [
                "post update for tether: finished the H=8 ablation",
                "post update for tether task GH-142: evaluation is green",
            ],
            "x-transports": ["a2a", "http"],
            "x-http": "POST /events",
        },
        {
            "id": "get_context",
            "name": "Get project context",
            "description": (
                "Bounded briefing for a project: active claims, blocked tasks, unresolved "
                "questions, recent updates, decisions and handoffs."
            ),
            "tags": ["coordination", "context", "read"],
            "examples": ["context for tether", "what is the status of drends"],
            "x-transports": ["a2a", "http"],
            "x-http": "GET /context?project=<project>",
        },
        {
            "id": "claim_task",
            "name": "Claim a task",
            "description": (
                "Take exclusive ownership of a task so two agents cannot work it at once. "
                "Returns the current owner if someone already holds it."
            ),
            "tags": ["coordination", "claims", "write"],
            "examples": ["claim GH-142 in tether"],
            "x-transports": ["http"],
            "x-http": "POST /claim",
        },
        {
            "id": "coordination_summary",
            "name": "Coordination summary",
            "description": (
                "Rule-based team situational awareness for a project: conflicts, blockers, "
                "open questions, idle claims and suggested next steps. No LLM."
            ),
            "tags": ["coordination", "summary", "read"],
            "examples": ["coordination summary for tether"],
            "x-transports": ["http"],
            "x-http": "GET /coordination/summary?project=<project>",
        },
    ]


def agent_card(settings: Settings) -> dict[str, Any]:
    """The Agent Card served at ``/.well-known/agent.json``.

    Discovery must work before authentication, so this is served openly and contains
    nothing secret: a name, a URL, and an honest list of what the relay can do.
    """
    url = base_url(settings)
    return {
        "name": "agent-relay",
        "description": (
            "Coordination relay for a small team running multiple coding and research "
            "agents: structured events, task claims, and project context. Speaks a subset "
            "of A2A — an Agent Card plus message/send with text parts. Streaming, push "
            "notifications and the task lifecycle methods are not implemented."
        ),
        "version": __version__,
        "url": f"{url}/a2a",
        "documentationUrl": f"{url}/docs",
        "provider": {"organization": "agent-relay", "url": url},
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": _skills(),
        # Non-standard, but honesty is worth a custom key: this is exactly what the
        # RPC endpoint accepts. Anything else answers -32601.
        "x-supported-methods": ["message/send"],
        "x-supported-intents": list(SUPPORTED_INTENTS),
        "x-authentication": "Optional bearer token on /a2a; the Agent Card is always open.",
    }


# --- message handling -----------------------------------------------------------


def extract_text(params: dict[str, Any]) -> str:
    """Pull the text out of an A2A ``message/send`` params object.

    Accepts both ``kind`` and ``type`` part discriminators (the spec renamed it), and
    falls back to a bare ``text`` field so a trivial client is not punished for it.
    """
    message = params.get("message")
    if not isinstance(message, dict):
        message = params if isinstance(params.get("parts"), list) else {}

    chunks: list[str] = []
    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("kind") or part.get("type")
        text = part.get("text")
        if isinstance(text, str) and kind in (None, "text", "text/plain"):
            chunks.append(text)
    if chunks:
        return "\n".join(chunks)

    for source in (message, params):
        text = source.get("text")
        if isinstance(text, str):
            return text
    return ""


def _caller(params: dict[str, Any], key: str, default: str | None = None) -> str | None:
    """Read an identity hint from ``params.metadata`` or ``params.message.metadata``."""
    message = params.get("message")
    sources = [
        params.get("metadata"),
        message.get("metadata") if isinstance(message, dict) else None,
    ]
    for source in sources:
        if isinstance(source, dict):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return default


def text_message(text: str, *, intent: str, **extra: Any) -> dict[str, Any]:
    """An A2A ``message`` result with a single text part."""
    metadata: dict[str, Any] = {"intent": intent}
    metadata.update({k: v for k, v in extra.items() if v is not None})
    return {
        "kind": "message",
        "role": "agent",
        "messageId": uuid4().hex,
        "parts": [{"kind": "text", "text": text}],
        "metadata": metadata,
    }


def unsupported_result(reason: str) -> dict[str, Any]:
    """Never a guess, never an error — say what is understood and stop."""
    lines = [reason, "", "Understood instructions:"]
    lines.extend(f"  - {intent}" for intent in SUPPORTED_INTENTS)
    return text_message("\n".join(lines), intent="unsupported", supported=list(SUPPORTED_INTENTS))


def _render_context(project: str, session: Session, settings: Settings) -> str:
    ctx = context_service.build_context(
        session,
        project,
        window_hours=settings.context_window_hours,
        limit=settings.context_recent_limit,
    )
    lines = [f"{project} — context ({ctx.window_hours}h window)"]
    if ctx.active_claims:
        held = ", ".join(f"{c.task} ({c.agent})" for c in ctx.active_claims[:MAX_LIST_ROWS])
        lines.append(f"active claims: {held}")
    else:
        lines.append("active claims: none")
    if ctx.blocked_tasks:
        blocked = ", ".join(
            f"{t.task}: {t.blocked_reason or 'no reason given'}"
            for t in ctx.blocked_tasks[:MAX_LIST_ROWS]
        )
        lines.append(f"blocked: {blocked}")
    if ctx.unresolved_questions:
        lines.append("open questions:")
        lines.extend(
            f"  {q.ref} {q.from_agent} -> {q.to_agent or 'team'}: {q.question}"
            for q in ctx.unresolved_questions[:MAX_LIST_ROWS]
        )
    if ctx.recent_updates:
        lines.append("recent updates:")
        lines.extend(
            f"  {e.ref} {e.agent}{f' [{e.task}]' if e.task else ''}: {e.summary}"
            for e in ctx.recent_updates[:MAX_LIST_ROWS]
        )
    if ctx.recent_decisions:
        lines.append("recent decisions:")
        lines.extend(
            f"  {e.ref} {e.agent}: {e.summary}" for e in ctx.recent_decisions[:MAX_LIST_ROWS]
        )
    if len(lines) == 2:  # header + "active claims: none"
        lines.append("nothing recorded for this project yet.")
    return "\n".join(lines)


def _render_tasks(project: str, session: Session) -> str:
    rows = state_service.build_tasks(session, project=project, limit=MAX_LIST_ROWS)
    if not rows:
        return f"{project} — no tasks recorded yet."
    lines = [f"{project} — {len(rows)} task(s), most recently active first"]
    lines.extend(
        f"  {row.task} [{row.status}]"
        + (f" owner={row.owner}" if row.owner else "")
        + (f" blocked: {row.blocked_reason}" if row.blocked and row.blocked_reason else "")
        for row in rows
    )
    return "\n".join(lines)


def handle_message(
    session: Session, params: dict[str, Any], *, settings: Settings
) -> dict[str, Any]:
    """Map an A2A ``message/send`` payload onto the relay's existing services.

    Reads and writes go through the same service functions the REST API uses, so an
    A2A caller cannot reach a code path a normal agent could not.
    """
    text = extract_text(params)
    if not text.strip():
        return unsupported_result("The message carried no text part.")

    intent = parse_intent(text)
    if intent.name == "unsupported" or not intent.project:
        return unsupported_result(f"I could not map {text.strip()!r} onto a relay action.")

    if intent.name == "get_context":
        return text_message(
            _render_context(intent.project, session, settings),
            intent="get_context",
            project=intent.project,
        )

    if intent.name == "list_tasks":
        return text_message(
            _render_tasks(intent.project, session),
            intent="list_tasks",
            project=intent.project,
        )

    # post_update: the only writing intent, and it writes a plain UPDATE event.
    if not intent.summary:
        return unsupported_result("An update needs a summary after the colon.")
    # The caller picks its own name, so it must be namespaced the way GitHub and Slack
    # traffic is. Without this an external peer can post as "leo-codex" and the event
    # log cannot tell the team's own agent from a stranger's framework.
    agent = _namespaced_agent(_caller(params, "agent", DEFAULT_AGENT) or DEFAULT_AGENT)
    event = event_service.create_event(
        session,
        EventCreate(
            event_type=EventType.UPDATE,
            agent=agent,
            human_owner=_caller(params, "human_owner"),
            project=intent.project,
            task=intent.task,
            summary=intent.summary,
            metadata={"source": "a2a"},
        ),
        commit=False,
    )
    # `metadata.source` is a free-form field the caller could also have set; the column
    # is what every consumer reads, so set it here and not from the payload.
    event.source = "a2a"
    session.commit()
    session.refresh(event)
    return text_message(
        f"Recorded {event.ref}: UPDATE on {intent.project}"
        + (f" task {intent.task}" if intent.task else "")
        + f" as {agent}.",
        intent="post_update",
        project=intent.project,
        task=intent.task,
        ref=event.ref,
        event_id=event.id,
    )
