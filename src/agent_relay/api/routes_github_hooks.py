"""Inbound GitHub webhooks.

Deliberately *not* mounted behind ``AuthDep``: GitHub cannot send the relay's
bearer token, so requiring it would mean either no webhooks or a shared token
pasted into a third party's settings page. These endpoints authenticate the only
way GitHub offers — an HMAC over the raw body — and that check is mandatory
whenever the feature is switched on at all.

The other rule here: a webhook must never 500. GitHub retries failures, and an
unexpected payload shape that returns 500 turns into an infinite retry loop
against a relay that will never like it any better. Anything we cannot map is
logged and answered with ``ignored``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

from agent_relay.api.deps import GitHubDep, SessionDep, SettingsDep
from agent_relay.services import events as event_service
from agent_relay.services import github_ingest

logger = logging.getLogger(__name__)

#: No AuthDep — see the module docstring. Verification is by signature only.
router = APIRouter(tags=["webhooks"])

EVENT_HEADER = "X-GitHub-Event"
DELIVERY_HEADER = "X-GitHub-Delivery"
SIGNATURE_HEADER = "X-Hub-Signature-256"


def _ignored(reason: str, event_name: str, delivery: str) -> dict[str, Any]:
    logger.info("github webhook ignored (%s): event=%s delivery=%s", reason, event_name, delivery)
    return {"status": "ignored", "event": None}


@router.post("/webhooks/github", tags=["webhooks"])
async def github_webhook(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    github: GitHubDep,
) -> dict[str, Any]:
    """Receive a GitHub webhook and turn it into a relay event.

    ``503`` when no secret is configured, ``401`` when the signature is missing or
    wrong, otherwise ``200`` with ``ignored`` / ``duplicate`` / ``created``.
    """
    secret = settings.github_webhook_secret
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GitHub webhooks are not configured on this relay (set GITHUB_WEBHOOK_SECRET).",
        )

    # The signature covers the bytes on the wire, so HMAC must run before any
    # parsing — a re-serialised body is a different body.
    body = await request.body()
    if not github_ingest.verify_signature(secret, body, request.headers.get(SIGNATURE_HEADER)):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Missing or invalid {SIGNATURE_HEADER}.",
        )

    event_name = (request.headers.get(EVENT_HEADER) or "").strip().lower()
    delivery = (request.headers.get(DELIVERY_HEADER) or "").strip()

    if event_name == "ping":
        return {"status": "pong", "event": None}

    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return _ignored("body is not json", event_name, delivery)
    if not isinstance(payload, dict):
        return _ignored("body is not a json object", event_name, delivery)

    external_id = github_ingest.external_id_for(event_name, delivery, payload)
    if github_ingest.already_ingested(session, github_ingest.SOURCE, external_id):
        # GitHub retries on any non-2xx and on its own schedule; a replay is normal.
        return {"status": "duplicate", "event": None}

    try:
        event = github_ingest.ingest_webhook(
            session,
            event_name=event_name,
            delivery_id=delivery,
            payload=payload,
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001 - a surprising payload must not 500 and retry forever
        session.rollback()
        logger.warning(
            "github webhook %s (delivery %s) failed: %s", event_name, delivery, type(exc).__name__
        )
        return {"status": "ignored", "event": None}

    if event is None:
        return _ignored("nothing worth recording", event_name, delivery)
    return {
        "status": "created",
        "event": event_service.to_out(event, github).model_dump(mode="json"),
    }
