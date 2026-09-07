"""Bidirectional Slack (V2).

A webhook can only speak. A **bot token** makes Slack a two-way surface:
``chat.postMessage`` returns the message ``ts``, we store it on the event, and a
human's threaded reply carries that same value as ``thread_ts``. That single string
is the whole trick — it is how a sentence typed into Slack finds the exact question
it answers and becomes a real ANSWER event in the relay.

Three invariants, inherited from the V1 notifier and one of our own:

1. Nothing here raises. Slack is a view of the log, never a dependency of it.
2. Slack answers ``200 OK`` with ``{"ok": false}`` on failure, so the HTTP status
   alone proves nothing — every response body is checked.
3. Inbound events are deduplicated. Slack retries aggressively; without the
   ``ingest_records`` ledger one human reply would become three ANSWER events.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_relay.config import Settings, get_settings
from agent_relay.db.models import Event, IngestRecord
from agent_relay.db.session import session_scope
from agent_relay.models.enums import EventType
from agent_relay.models.schemas import EventCreate
from agent_relay.services.events import create_event
from agent_relay.services.slack import SlackNotifier, format_event, redact

logger = logging.getLogger(__name__)

SLACK_API_ROOT = "https://slack.com/api"
SOURCE = "slack"

#: Slack signs every request; anything older than this is a replay, not a delivery.
MAX_SIGNATURE_AGE_SECONDS = 300

#: EventCreate caps summaries at 2000 characters. Leave room for the ellipsis.
MAX_SUMMARY_CHARS = 1900

#: Edits and deletions are not new statements, so they are not new events.
IGNORED_SUBTYPES = frozenset(
    {"bot_message", "message_changed", "message_deleted", "channel_join", "channel_leave"}
)

#: ``<@U123>``, ``<@U123|leo>``, ``<#C456|general>``, ``<!here>`` — Slack's mention markup.
_MENTION = re.compile(r"<[@#!][^>|]*(?:\|([^>]*))?>")
#: ``<https://example.com|label>`` — keep the label, or the bare URL when there is none.
_LINK = re.compile(r"<(https?://[^>|]+)(?:\|([^>]*))?>")
_WHITESPACE = re.compile(r"[ \t]*\n[ \t]*")


def clean_slack_text(text: str) -> str:
    """Turn a Slack message into plain prose fit for an event summary.

    Mention markup carries opaque ids (``<@U0246>``), which are noise in a log an
    agent reads back later; the human-readable label is kept when Slack supplies one.
    """
    plain = _LINK.sub(lambda m: m.group(2) or m.group(1), text)
    plain = _MENTION.sub(lambda m: m.group(1) or "", plain)
    plain = plain.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    plain = _WHITESPACE.sub("\n", plain).strip()
    if len(plain) > MAX_SUMMARY_CHARS:
        plain = plain[:MAX_SUMMARY_CHARS].rstrip() + "…"
    return plain


def verify_slack_signature(
    signing_secret: str | None,
    timestamp: str | None,
    body: bytes,
    signature: str | None,
) -> bool:
    """Slack's v0 request signature, with replay protection.

    ``v0=<hmac_sha256(signing_secret, "v0:<timestamp>:<raw body>")>``, compared in
    constant time. The timestamp check is not optional: a signature stays valid
    forever, so without it a captured request could be replayed at any point.

    The body must be the *raw* bytes — re-serialising the parsed JSON changes
    whitespace and key order, and the signature no longer matches.
    """
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        age = abs(time.time() - int(timestamp))
    except ValueError:
        return False  # non-numeric timestamp: not a Slack delivery
    if age > MAX_SIGNATURE_AGE_SECONDS:
        return False

    basestring = b"v0:" + timestamp.encode("utf-8") + b":" + body
    digest = hmac.new(signing_secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(
        f"v0={digest}".encode(),
        # Starlette decodes headers as latin-1, so a raw high byte yields a non-ASCII
        # str and the str form of compare_digest raises TypeError. Comparing bytes is
        # still constant-time and cannot raise on arbitrary input.
        signature.encode("utf-8", "ignore"),
    )


def resolve_project_channel(settings: Settings, project: str | None) -> str | None:
    """Which channel an event for ``project`` belongs in, or None when unrouted.

    Per-project routing keeps three projects out of one firehose; the default
    channel catches everything that has no entry of its own.
    """
    return settings.channel_for(project)


class SlackBot:
    """Two-way Slack client. Best effort in both directions: never raises."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def enabled(self) -> bool:
        return self.settings.slack_bot_enabled

    async def post_event(
        self,
        event_payload: dict[str, Any],
        links: dict[str, str] | None = None,
        channel: str | None = None,
    ) -> tuple[str | None, str | None]:
        """Post one event with the bot token. Returns ``(ts, channel)``, or ``(None, None)``.

        The ``ts`` is the point of using a bot token at all — stored on the event, it
        is the anchor a human's threaded reply is later matched against.
        """
        target = channel or resolve_project_channel(self.settings, event_payload.get("project"))
        if not self.enabled or not target:
            return None, None

        # Same rendering as the webhook path — one formatter, one look in Slack.
        body: dict[str, Any] = {"channel": target, **format_event(event_payload, links)}
        data = await self._call("chat.postMessage", body)
        if data is None:
            return None, None
        ts = data.get("ts")
        if not ts:
            logger.warning("slack accepted the message but returned no ts")
            return None, None
        return str(ts), str(data.get("channel") or target)

    async def post_thread_reply(self, channel: str, thread_ts: str, text: str) -> bool:
        """Reply inside a thread, e.g. to tell the human their answer was recorded."""
        if not self.enabled or not channel or not thread_ts:
            return False
        body = {"channel": channel, "thread_ts": thread_ts, "text": text[:2900]}
        return await self._call("chat.postMessage", body) is not None

    async def _call(self, method: str, body: dict[str, Any]) -> dict[str, Any] | None:
        """One Web API call. Returns the response payload, or None on any failure."""
        token = self.settings.slack_bot_token
        if not token:
            return None
        try:
            async with httpx.AsyncClient(timeout=self.settings.slack_timeout_seconds) as client:
                response = await client.post(
                    f"{SLACK_API_ROOT}/{method}",
                    json=body,
                    headers={
                        # The token never appears in a log line: nothing here logs
                        # the request headers, and errors are logged by code only.
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                )
        except Exception as exc:  # noqa: BLE001 - Slack must never break event storage
            logger.warning("slack %s failed: %s: %s", method, type(exc).__name__, redact(str(exc)))
            return None

        if response.status_code >= 400:
            logger.warning("slack %s rejected: status=%s", method, response.status_code)
            return None
        try:
            data = response.json()
        except ValueError:
            logger.warning("slack %s returned a non-JSON body", method)
            return None

        # The classic Slack trap: transport succeeded, the call did not. A 200 with
        # {"ok": false, "error": "channel_not_found"} is a failure like any other.
        if not isinstance(data, dict) or not data.get("ok"):
            error = data.get("error", "unknown") if isinstance(data, dict) else "malformed"
            logger.warning("slack %s failed: error=%s", method, error)
            return None
        return data


# ------------------------------------------------------------------ inbound: Slack -> relay


def find_anchor_event(session: Session, *, thread_ts: str, channel: str | None) -> Event | None:
    """The event whose Slack copy started this thread, if we posted it."""
    stmt = select(Event).where(Event.slack_ts == thread_ts).order_by(Event.id.desc())
    for event in session.execute(stmt).scalars():
        # A ts is only unique per channel, so a known channel must agree.
        if not event.slack_channel or not channel or event.slack_channel == channel:
            return event
    return None


def already_ingested(session: Session, external_id: str) -> bool:
    """Has this Slack event id already been turned into an event?"""
    stmt = select(IngestRecord.id).where(
        IngestRecord.source == SOURCE, IngestRecord.external_id == external_id
    )
    return session.execute(stmt).first() is not None


def answer_from_slack_reply(
    session: Session,
    *,
    channel: str,
    thread_ts: str,
    user: str,
    text: str,
    settings: Settings,
) -> EventCreate | None:
    """Turn a human's threaded Slack reply into the event it is actually answering.

    Returns None when the reply is not ours to record: no bot configured (we cannot
    have posted the thread), no matching anchor event, or nothing left after
    stripping Slack markup.
    """
    if not settings.slack_bot_enabled:
        return None

    anchor = find_anchor_event(session, thread_ts=thread_ts, channel=channel)
    if anchor is None:
        # A reply to a message we did not post. Someone else's thread, not our log.
        return None

    summary = clean_slack_text(text)
    if not summary:
        return None

    # "slack:" is load-bearing: it says at a glance that a human typed this, not an
    # agent, so nobody reads a Slack aside as a machine-verified report.
    author = f"slack:{user}" if user else "slack:unknown"
    metadata: dict[str, Any] = {
        "slack_user": user,
        "slack_channel": channel,
        "slack_thread_ts": thread_ts,
        "anchor_ref": anchor.ref,
    }

    if anchor.event_type == EventType.QUESTION.value:
        return EventCreate(
            event_type=EventType.ANSWER,
            agent=author,
            project=anchor.project,
            task=anchor.task,
            summary=summary,
            # The answer goes back to whoever asked, and cites the question's ref so
            # /context stops listing it as unresolved.
            target_agent=anchor.agent,
            in_reply_to=anchor.ref,
            metadata=metadata,
        )

    # A reply to anything else is a human note on that piece of work.
    return EventCreate(
        event_type=EventType.UPDATE,
        agent=author,
        project=anchor.project,
        task=anchor.task,
        summary=summary,
        metadata=metadata,
    )


def _reply_details(envelope: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the parts of an Events API envelope we act on, or None to ignore it."""
    inner = envelope.get("event")
    if not isinstance(inner, dict) or inner.get("type") != "message":
        return None
    # Our own acknowledgements come back to us as events; ignoring bots stops the loop.
    if inner.get("bot_id") or inner.get("subtype") in IGNORED_SUBTYPES:
        return None

    thread_ts = inner.get("thread_ts")
    ts = inner.get("ts")
    # A top-level message answers nothing: without a thread there is no anchor.
    if not thread_ts or thread_ts == ts:
        return None
    channel = inner.get("channel")
    if not channel:
        return None
    return {
        "channel": str(channel),
        "thread_ts": str(thread_ts),
        "ts": str(ts or thread_ts),
        "user": str(inner.get("user") or inner.get("username") or ""),
        "text": str(inner.get("text") or ""),
    }


def ingest_slack_event(
    session: Session, envelope: dict[str, Any], settings: Settings
) -> Event | None:
    """Handle one Events API envelope. Returns the created event, or None if ignored.

    Idempotent on Slack's ``event_id``: a retried delivery finds its ledger row and
    creates nothing.
    """
    external_id = str(envelope.get("event_id") or "")
    if not external_id or already_ingested(session, external_id):
        return None

    reply = _reply_details(envelope)
    if reply is None:
        return None

    payload = answer_from_slack_reply(
        session,
        channel=reply["channel"],
        thread_ts=reply["thread_ts"],
        user=reply["user"],
        text=reply["text"],
        settings=settings,
    )
    if payload is None:
        return None

    event = create_event(session, payload, commit=False)
    event.source = SOURCE
    # The reply's own ts, not the anchor's: replies to *this* message thread back here.
    event.slack_ts = reply["ts"]
    event.slack_channel = reply["channel"]
    session.add(IngestRecord(source=SOURCE, external_id=external_id, event_id=event.id))

    try:
        session.commit()
    except IntegrityError:
        # Two retries of the same delivery raced. The unique (source, external_id)
        # index is what makes the ledger authoritative rather than advisory.
        session.rollback()
        logger.info("slack event %s was already ingested concurrently", external_id)
        return None
    session.refresh(event)
    return event


async def announce(
    payload: dict[str, Any], links: dict[str, str] | None, settings: Settings
) -> None:
    """Post one event to Slack by the best route available, and remember where it landed.

    This is the outbound half of the round trip. Posting through the *bot* returns a
    message ``ts``; storing that ts on the event is what later lets a human's threaded
    reply find the event it is answering. Posting through an incoming *webhook* cannot
    return a ts, so with only ``SLACK_WEBHOOK_URL`` set the relay can talk but not
    listen — which is exactly the difference between the two Slack setups.

    Runs as a background task after the response, and never raises.
    """
    bot = SlackBot(settings)
    channel = resolve_project_channel(settings, payload.get("project"))
    if bot.enabled and channel:
        ts, posted_channel = await bot.post_event(payload, links, channel)
        if ts and (event_id := payload.get("id")) is not None:
            _remember_slack_message(int(event_id), ts, posted_channel)
        return

    # No bot token (or no channel to post to): fall back to the webhook.
    await SlackNotifier(settings).post_event(payload, links)


def _remember_slack_message(event_id: int, ts: str, channel: str | None) -> None:
    """Store the Slack coordinates of a posted event, in its own session.

    The request's session is long gone by the time this runs, and a failure here must
    not be able to undo the event that was already committed.
    """
    try:
        with session_scope() as session:
            event = session.get(Event, event_id)
            if event is None:  # pragma: no cover - only if the row was deleted
                return
            event.slack_ts = ts
            event.slack_channel = channel
            session.commit()
    except Exception as exc:  # noqa: BLE001 - losing the anchor must not break anything
        logger.warning("could not record slack ts for event %s: %s", event_id, type(exc).__name__)
