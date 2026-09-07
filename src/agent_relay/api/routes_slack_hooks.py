"""Inbound Slack: the Events API endpoint that closes the loop.

This router deliberately carries **no** ``AuthDep``. Slack cannot present the
relay's bearer token, so the request authenticates itself the only way Slack
offers: an HMAC signature over the raw body, verified before anything is parsed.

The other rule here is Slack's retry policy. Slack re-delivers anything that is not
answered ``200`` quickly, so every payload we understood — including the ones we
deliberately ignore — gets a ``200``. An exception escaping as a ``500`` would buy
us the same broken delivery three more times.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from agent_relay.api.deps import GitHubDep, SessionDep, SettingsDep
from agent_relay.config import Settings, get_settings
from agent_relay.models.schemas import EventOut
from agent_relay.services import events as event_service
from agent_relay.services.slack_bot import (
    SlackBot,
    already_ingested,
    ingest_slack_event,
    verify_slack_signature,
)

logger = logging.getLogger(__name__)

router = APIRouter()

TIMESTAMP_HEADER = "X-Slack-Request-Timestamp"
SIGNATURE_HEADER = "X-Slack-Signature"


def get_slack_bot(settings: Annotated[Settings, Depends(get_settings)]) -> SlackBot:
    return SlackBot(settings)


SlackBotDep = Annotated[SlackBot, Depends(get_slack_bot)]


class SlackHookResult(BaseModel):
    """What the relay tells Slack it did.

    ``challenge`` is only populated for Slack's one-off endpoint handshake; the rest
    of the body is for humans reading the delivery log in the Slack app console.
    """

    status: str
    event: EventOut | None = None
    challenge: str | None = None


@router.post("/webhooks/slack/events", response_model=SlackHookResult, tags=["slack"])
async def slack_events(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    github: GitHubDep,
    bot: SlackBotDep,
    background: BackgroundTasks,
) -> SlackHookResult:
    """Receive a Slack Events API delivery and, when it is a threaded reply, log it."""
    # The signature covers the bytes exactly as sent; re-serialising would break it.
    raw = await request.body()

    if not settings.slack_events_enabled:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Slack events are not configured on this relay.",
        )
    if not verify_slack_signature(
        settings.slack_signing_secret,
        request.headers.get(TIMESTAMP_HEADER),
        raw,
        request.headers.get(SIGNATURE_HEADER),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing Slack signature.",
        )

    try:
        envelope = json.loads(raw)
    except ValueError:
        logger.warning("slack delivery had a non-JSON body")
        return SlackHookResult(status="ignored")
    if not isinstance(envelope, dict):
        return SlackHookResult(status="ignored")
    payload: dict[str, Any] = envelope

    # Slack posts this once when the endpoint URL is saved, and will not enable the
    # subscription until the challenge comes back. It is signed like everything else.
    if payload.get("type") == "url_verification":
        return SlackHookResult(status="ok", challenge=str(payload.get("challenge") or ""))

    if payload.get("type") != "event_callback":
        return SlackHookResult(status="ignored")

    event_id = str(payload.get("event_id") or "")
    # Blocking ORM work must not run on the event loop: SQLite is configured with
    # busy_timeout=5000, so a contended write here would stall every other in-flight
    # request for up to five seconds. Same threadpool hop as every other blocking
    # route in this app.
    if event_id:
        seen: bool = await run_in_threadpool(already_ingested, session, event_id)
        if seen:
            # A retry of something we already stored. Answer 200 so Slack stops asking.
            return SlackHookResult(status="duplicate")

    try:
        event = await run_in_threadpool(ingest_slack_event, session, payload, settings)
    except Exception:
        # A 500 here makes Slack retry the same bad payload forever, so we log it
        # loudly and answer 200. (Ruff allows this broad catch: it is logged.)
        logger.exception("slack event ingestion failed")
        session.rollback()
        return SlackHookResult(status="ignored")

    if event is None:
        return SlackHookResult(status="ignored")

    # Acknowledge in-thread so the human can see their sentence became an event.
    # The ack arrives back here as a bot message, which the ingester drops.
    if event.slack_channel and bot.enabled:
        background.add_task(
            bot.post_thread_reply,
            event.slack_channel,
            str(payload.get("event", {}).get("thread_ts") or event.slack_ts or ""),
            f"Logged as {event.ref} ({event.event_type}) on {event.project}.",
        )

    return SlackHookResult(status="created", event=event_service.to_out(event, github))
