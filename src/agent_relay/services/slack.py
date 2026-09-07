"""Slack integration.

Slack is a *view* of the event log, never its storage. Two invariants:

1. Rendering is human-readable. No raw JSON is ever posted.
2. Every failure is swallowed and logged. ``post_event`` cannot raise, so an agent
   can always log an event even when Slack is misconfigured, rate limited or down.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from agent_relay.config import Settings, get_settings
from agent_relay.models.enums import EVENT_LABELS, EventType

logger = logging.getLogger(__name__)

MAX_LIST_ITEMS = 6
MAX_FIELD_CHARS = 600
WEBHOOK_PATTERN = re.compile(r"https://hooks\.slack\.com/\S+")


def redact(text: str) -> str:
    """Strip webhook URLs out of anything we are about to log."""
    return WEBHOOK_PATTERN.sub("https://hooks.slack.com/<redacted>", text)


def _as_lines(value: Any) -> list[str]:
    """Normalise a details value into display lines."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, dict):
        return [f"{k}: {v}" for k, v in value.items()]
    return [str(value)]


def _bullets(values: list[str]) -> str:
    shown = values[:MAX_LIST_ITEMS]
    text = "\n".join(f"• {line}" for line in shown)
    if len(values) > MAX_LIST_ITEMS:
        text += f"\n• …and {len(values) - MAX_LIST_ITEMS} more"
    return text[:MAX_FIELD_CHARS]


def _humanise(key: str) -> str:
    return key.replace("_", " ").strip().capitalize()


def format_event(event: dict[str, Any], links: dict[str, str] | None = None) -> dict[str, Any]:
    """Render an event as a Slack Block Kit message.

    Shape (matching the team's agreed format)::

        🔄 UPDATE · TETHER · GH-142
        leo-codex

        Implemented H=8 and completed SCARED-C evaluation.

        *Findings*
        • EPE improved 0.7%

        Branch: exp/temporal-ablation
    """
    links = links or {}
    event_type = str(event.get("event_type", "UPDATE")).upper()
    try:
        glyph = EVENT_LABELS[EventType(event_type)]
    except ValueError:
        glyph = "•"

    project = str(event.get("project", "")).upper()
    task = event.get("task")
    header_bits = [f"{glyph} *{event_type}*", f"*{project}*"]
    if task:
        task_url = links.get("task")
        header_bits.append(f"<{task_url}|{task}>" if task_url else f"*{task}*")
    header = " · ".join(header_bits)

    byline = str(event.get("agent", "unknown"))
    if owner := event.get("human_owner"):
        byline += f"  _({owner})_"
    if target := event.get("target_agent"):
        arrow = "→" if event_type != "ANSWER" else "↩"
        byline += f"  {arrow} *{target}*"

    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{header}\n{byline}"}}
    ]

    body = str(event.get("summary", "")).strip()
    if ref := event.get("ref"):
        if event_type == "QUESTION":
            body = f"*{ref}* {body}"
        elif event_type == "ANSWER" and event.get("in_reply_to"):
            body = f"_re {event['in_reply_to']}_  {body}"
    if body:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body[:2900]}})

    # Structured details become titled bullet groups, two per row where they fit.
    fields: list[dict[str, str]] = []
    for key, value in (event.get("details") or {}).items():
        lines = _as_lines(value)
        if not lines:
            continue
        fields.append({"type": "mrkdwn", "text": f"*{_humanise(key)}*\n{_bullets(lines)}"})
    for chunk_start in range(0, len(fields), 2):
        blocks.append({"type": "section", "fields": fields[chunk_start : chunk_start + 2]})

    if artifacts := [str(a) for a in (event.get("artifacts") or [])]:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Artifacts*\n{_bullets(artifacts)}"},
            }
        )

    context_bits: list[str] = []
    if branch := event.get("branch"):
        branch_url = links.get("branch")
        context_bits.append(f"`{branch}`" if not branch_url else f"<{branch_url}|`{branch}`>")
    if links.get("task"):
        context_bits.append(f"<{links['task']}|GitHub>")
    if ref := event.get("ref"):
        context_bits.append(f"`{ref}`")
    if context_bits:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "  ·  ".join(context_bits)}],
            }
        )

    plain = f"{glyph} {event_type} · {project}" + (f" · {task}" if task else "")
    return {"text": f"{plain} — {byline}: {body}"[:1500], "blocks": blocks}


class SlackNotifier:
    """Best-effort Slack poster. Never raises."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def enabled(self) -> bool:
        return self.settings.slack_enabled

    def should_post(self, event_type: str) -> bool:
        if not self.enabled:
            return False
        allowed = self.settings.slack_event_type_filter
        return allowed is None or event_type.upper() in allowed

    async def post_event(self, event: dict[str, Any], links: dict[str, str] | None = None) -> bool:
        """Post one event. Returns True if Slack accepted it, False in every other case."""
        event_type = str(event.get("event_type", ""))
        if not self.should_post(event_type):
            return False
        payload = format_event(event, links)
        return await self._send(payload)

    async def _send(self, payload: dict[str, Any]) -> bool:
        url = self.settings.slack_webhook_url
        if not url:
            return False
        try:
            async with httpx.AsyncClient(timeout=self.settings.slack_timeout_seconds) as client:
                response = await client.post(url, json=payload)
            if response.status_code >= 400:
                # Body of a webhook error is short and non-secret ("invalid_payload").
                logger.warning(
                    "slack rejected message: status=%s body=%s",
                    response.status_code,
                    redact(response.text[:200]),
                )
                return False
            return True
        except Exception as exc:  # noqa: BLE001 - Slack must never break event storage
            logger.warning("slack post failed: %s: %s", type(exc).__name__, redact(str(exc)))
            return False
