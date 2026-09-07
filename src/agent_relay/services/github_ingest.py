"""GitHub ingestion: repository activity becomes relay events.

GitHub is the source of truth for code, issues and PRs. This module turns what
happens there into relay events so ``/context`` reflects the real state of the
repo without an agent having to narrate it ("I opened PR #17") and without a
human having to trust that the narration happened.

Two rules keep ingestion honest:

* Every event created here carries ``source="github"`` and an agent name derived
  from the GitHub actor (``github:leonardo``). Ingested activity must never be
  mistakable for a relay agent posting about its own work.
* Every delivery is written to the ``ingest_records`` ledger first. GitHub retries
  deliveries and pollers re-see the same PR on every tick; without the ledger one
  push becomes three identical UPDATE events.

Two ways in, same mapping: webhooks (preferred) and polling (the fallback for a
relay with no publicly reachable URL).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_relay.config import Settings, get_settings
from agent_relay.db.models import Event, IngestRecord
from agent_relay.models.enums import EventType
from agent_relay.models.schemas import EventCreate
from agent_relay.services.events import create_event
from agent_relay.services.github import GitHubService

logger = logging.getLogger(__name__)

#: Value written to ``Event.source`` and ``IngestRecord.source``.
SOURCE = "github"
#: Namespace for ingested actors. A relay agent can never own a name with a colon
#: in it by accident, so ``github:leonardo`` cannot collide with ``leo-codex``.
AGENT_PREFIX = "github:"
UNKNOWN_ACTOR = "unknown"

DEFAULT_TASK_PREFIX = "GH-"
SIGNATURE_SCHEME = "sha256"
#: GitHub sends a lowercase hex SHA-256 digest; anything else is malformed.
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")

MAX_COMMIT_LINKS = 5
MAX_TITLE_CHARS = 160
MAX_COMMENT_CHARS = 240
MAX_SUMMARY_CHARS = 2000
MAX_AGENT_CHARS = 128

#: Webhook actions worth recording. Anything else (labelled, synchronize, edited,
#: assigned...) is noise in a coordination log.
ISSUE_ACTIONS = frozenset({"opened", "closed", "reopened"})
PULL_ACTIONS = frozenset({"opened", "closed", "reopened", "ready_for_review"})


# --------------------------------------------------------------------- signature


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Validate GitHub's ``X-Hub-Signature-256`` over the *raw* request body.

    The header is ``sha256=<hex digest>`` of HMAC-SHA256(secret, body). This is the
    only thing standing between the public internet and the event log, so the
    comparison is constant-time and every malformed header is a rejection rather
    than a best-effort parse.
    """
    if not secret or not signature_header:
        return False
    scheme, separator, digest = signature_header.strip().partition("=")
    if not separator or scheme.lower() != SIGNATURE_SCHEME:
        return False
    presented = digest.strip().lower()
    if not _HEX_DIGEST.match(presented):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, presented)


# ---------------------------------------------------------------------- ledger


def already_ingested(session: Session, source: str, external_id: str) -> bool:
    """Has this delivery (or poll key) already produced an event?"""
    stmt = (
        select(IngestRecord.id)
        .where(IngestRecord.source == source, IngestRecord.external_id == external_id)
        .limit(1)
    )
    return session.execute(stmt).first() is not None


def record_ingest(
    session: Session, source: str, external_id: str, event_id: int | None
) -> IngestRecord:
    """Write the ledger entry. Flushed, not committed: the caller owns the transaction."""
    record = IngestRecord(source=source, external_id=external_id, event_id=event_id)
    session.add(record)
    session.flush()
    return record


def external_id_for(event_name: str, delivery_id: str | None, payload: Mapping[str, Any]) -> str:
    """Stable idempotency key for one webhook delivery.

    Normally the delivery id. If GitHub (or a proxy) drops that header we hash the
    body instead, so a retry of the same payload still deduplicates.
    """
    if delivery_id and delivery_id.strip():
        return delivery_id.strip()[:255]
    try:
        blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    except (TypeError, ValueError):  # a payload we cannot canonicalise
        blob = repr(payload).encode("utf-8", "replace")
    return f"{event_name}:{hashlib.sha256(blob).hexdigest()[:32]}"


# --------------------------------------------------------------------- helpers


def resolve_project(settings: Settings, project: str | None = None) -> str | None:
    """Which relay project ingested activity is filed under.

    Explicit argument wins, then ``GITHUB_INGEST_PROJECT``, then the repo name — a
    single-repo relay should not have to configure anything.
    """
    for candidate in (project, settings.github_ingest_project, settings.github_repo):
        if candidate and candidate.strip():
            return candidate.strip()
    return None


def _clip(text: str, limit: int) -> str:
    """Collapse whitespace and cut to a length the schema will accept."""
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _sub(payload: Mapping[str, Any], key: str) -> dict[str, Any]:
    """Nested object access that never raises on a surprising payload shape."""
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _actor(payload: Mapping[str, Any]) -> str:
    """``github:<login>`` for the human (or bot) behind the delivery."""
    login = _sub(payload, "sender").get("login") or _sub(payload, "pusher").get("name")
    name = str(login).strip() if login else ""
    return _clip(f"{AGENT_PREFIX}{name or UNKNOWN_ACTOR}", MAX_AGENT_CHARS)


def _task_for(number: int | None, prefix: str) -> str | None:
    return f"{prefix}{number}" if number is not None else None


def _referenced_task(prefix: str, *texts: str | None) -> str | None:
    """Find a task id (``GH-142``) mentioned in a PR title or branch name.

    A PR usually belongs to the issue it closes, not to its own number, so an
    explicit reference outranks the derived id.
    """
    if not prefix:
        return None
    pattern = re.compile(rf"{re.escape(prefix)}(\d+)", re.IGNORECASE)
    for text in texts:
        if text and (match := pattern.search(text)):
            return f"{prefix}{match.group(1)}"
    return None


def _metadata(event_name: str, action: Any, delivery: str | None) -> dict[str, Any]:
    meta: dict[str, Any] = {"github_event": event_name}
    if isinstance(action, str) and action:
        meta["action"] = action
    if delivery:
        meta["delivery"] = delivery
    return meta


def _labels(container: Mapping[str, Any]) -> list[str]:
    raw = container.get("labels")
    if not isinstance(raw, list):
        return []
    return [str(item.get("name")) for item in raw if isinstance(item, dict) and item.get("name")]


def _artifacts(*urls: Any) -> list[str]:
    return [str(url) for url in urls if isinstance(url, str) and url]


# -------------------------------------------------------------------- mapping


def event_from_webhook(
    event_name: str,
    payload: Mapping[str, Any],
    *,
    project: str,
    prefix: str = DEFAULT_TASK_PREFIX,
    delivery: str | None = None,
) -> EventCreate | None:
    """Map one GitHub webhook to a relay event, or ``None`` if it is not worth recording.

    Returning ``None`` liberally is the point: the relay is a coordination log, not
    a mirror of the repo's activity feed.
    """
    handlers = {
        "issues": _from_issue,
        "pull_request": _from_pull_request,
        "push": _from_push,
        "issue_comment": _from_issue_comment,
    }
    handler = handlers.get(event_name)
    if handler is None:
        return None
    return handler(payload, project=project, prefix=prefix, delivery=delivery)


def _from_issue(
    payload: Mapping[str, Any], *, project: str, prefix: str, delivery: str | None
) -> EventCreate | None:
    action = payload.get("action")
    if action not in ISSUE_ACTIONS:
        return None
    issue = _sub(payload, "issue")
    number = _number(issue.get("number"))
    if number is None:
        return None

    task = _task_for(number, prefix)
    title = _clip(str(issue.get("title") or ""), MAX_TITLE_CHARS)
    return EventCreate(
        event_type=EventType.UPDATE,
        agent=_actor(payload),
        project=project,
        task=task,
        summary=_clip(f"Issue {task} {action}: {title}", MAX_SUMMARY_CHARS),
        details={
            "action": action,
            "number": number,
            "title": title,
            "state": issue.get("state"),
            "labels": _labels(issue),
        },
        artifacts=_artifacts(issue.get("html_url")),
        metadata=_metadata("issues", action, delivery),
    )


def _from_pull_request(
    payload: Mapping[str, Any], *, project: str, prefix: str, delivery: str | None
) -> EventCreate | None:
    action = payload.get("action")
    if action not in PULL_ACTIONS:
        return None
    pull = _sub(payload, "pull_request")
    number = _number(pull.get("number")) or _number(payload.get("number"))
    if number is None:
        return None

    merged = action == "closed" and bool(pull.get("merged"))
    branch = _sub(pull, "head").get("ref")
    base = _sub(pull, "base").get("ref")
    title = _clip(str(pull.get("title") or ""), MAX_TITLE_CHARS)
    # A PR carrying "GH-142" belongs to that task; only fall back to its own number.
    task = _referenced_task(prefix, title, str(branch or "")) or _task_for(number, prefix)

    if merged:
        headline = f"PR #{number} merged into {base or 'the base branch'}: {title}"
    elif action == "closed":
        headline = f"PR #{number} closed without merging: {title}"
    elif action == "ready_for_review":
        headline = f"PR #{number} ready for review: {title}"
    else:
        headline = f"PR #{number} {action}: {title}"

    return EventCreate(
        # Merging is the durable decision: it is the moment the change becomes the
        # repo's answer. Everything else about a PR is just progress.
        event_type=EventType.DECISION if merged else EventType.UPDATE,
        agent=_actor(payload),
        project=project,
        task=task,
        branch=_clip(str(branch), 255) if branch else None,
        summary=_clip(headline, MAX_SUMMARY_CHARS),
        details={
            "action": action,
            "pr_number": number,
            "merged": merged,
            "state": pull.get("state"),
            "base": base,
            "draft": bool(pull.get("draft")),
            "title": title,
        },
        artifacts=_artifacts(pull.get("html_url")),
        metadata=_metadata("pull_request", action, delivery),
    )


def _from_push(
    payload: Mapping[str, Any], *, project: str, prefix: str, delivery: str | None
) -> EventCreate | None:
    ref = str(payload.get("ref") or "")
    if not ref.startswith("refs/heads/") or payload.get("deleted"):
        return None  # tags and branch deletions are not coordination signal
    commits = [c for c in (payload.get("commits") or []) if isinstance(c, dict)]
    if not commits:
        return None  # a zero-commit push (a force-push to the same tree, a branch create)

    branch = ref.removeprefix("refs/heads/")
    # `"".splitlines()` is empty, so indexing it on a commit with no message raised
    # IndexError and the route dropped the entire push. `git commit --allow-empty-message`
    # is enough to trigger it.
    first_line = (str(commits[0].get("message") or "").splitlines() or [""])[0]
    subject = _clip(first_line, MAX_TITLE_CHARS)
    plural = "" if len(commits) == 1 else "s"
    headline = f"{len(commits)} commit{plural} pushed to {branch}"
    if subject:
        headline = f"{headline}: {subject}"

    urls = _artifacts(*[c.get("url") for c in commits])
    artifacts = urls[:MAX_COMMIT_LINKS]
    if len(urls) > MAX_COMMIT_LINKS:
        artifacts.append(f"…and {len(urls) - MAX_COMMIT_LINKS} more commits")

    messages = [str(c.get("message") or "") for c in commits]
    return EventCreate(
        event_type=EventType.UPDATE,
        agent=_actor(payload),
        project=project,
        task=_referenced_task(prefix, branch, *messages),
        branch=_clip(branch, 255),
        summary=_clip(headline, MAX_SUMMARY_CHARS),
        details={
            "action": "push",
            "branch": branch,
            "commits": len(commits),
            "compare": payload.get("compare"),
        },
        artifacts=artifacts,
        metadata=_metadata("push", payload.get("action"), delivery),
    )


def _from_issue_comment(
    payload: Mapping[str, Any], *, project: str, prefix: str, delivery: str | None
) -> EventCreate | None:
    if payload.get("action") != "created":
        return None  # edits and deletions would rewrite history; the log is append-only
    issue = _sub(payload, "issue")
    comment = _sub(payload, "comment")
    number = _number(issue.get("number"))
    if number is None:
        return None

    task = _task_for(number, prefix)
    body = _clip(str(comment.get("body") or ""), MAX_COMMENT_CHARS)
    author = str(_sub(comment, "user").get("login") or _sub(payload, "sender").get("login") or "")
    who = f" by {author}" if author else ""
    headline = f"Comment on {task}{who}"
    if body:
        headline = f"{headline}: {body}"

    return EventCreate(
        event_type=EventType.UPDATE,
        agent=_actor(payload),
        project=project,
        task=task,
        summary=_clip(headline, MAX_SUMMARY_CHARS),
        details={"action": "comment", "number": number, "comment": body, "author": author or None},
        artifacts=_artifacts(comment.get("html_url")),
        metadata=_metadata("issue_comment", payload.get("action"), delivery),
    )


# ------------------------------------------------------------------- ingestion


def _persist(session: Session, payload: EventCreate, external_id: str) -> Event:
    """Create the event, stamp its provenance, and write the ledger entry — atomically."""
    event = create_event(session, payload, commit=False)
    # Set after creation rather than through EventCreate: `source` is provenance the
    # relay decides, never something a payload can claim for itself.
    event.source = SOURCE
    record_ingest(session, SOURCE, external_id, event.id)
    return event


def ingest_webhook(
    session: Session,
    *,
    event_name: str,
    delivery_id: str | None,
    payload: Mapping[str, Any],
    project: str | None = None,
    settings: Settings | None = None,
) -> Event | None:
    """Ingest one webhook delivery. ``None`` means skipped, ignored or duplicate."""
    settings = settings or get_settings()
    resolved = resolve_project(settings, project)
    if resolved is None:
        logger.info("github webhook %s dropped: no project configured", event_name)
        return None

    external_id = external_id_for(event_name, delivery_id, payload)
    if already_ingested(session, SOURCE, external_id):
        logger.info("github webhook %s delivery %s already ingested", event_name, external_id)
        return None

    create = event_from_webhook(
        event_name,
        payload,
        project=resolved,
        prefix=settings.github_task_prefix,
        delivery=delivery_id or None,
    )
    if create is None:
        return None

    event = _persist(session, create, external_id)
    session.commit()
    session.refresh(event)
    logger.info("ingested github %s as %s", event_name, event.ref)
    return event


# --------------------------------------------------------------------- polling


def _event_for_open_pull(pull: Mapping[str, Any], *, project: str, prefix: str) -> EventCreate:
    number = _number(pull.get("number"))
    title = _clip(str(pull.get("title") or ""), MAX_TITLE_CHARS)
    branch = pull.get("branch")
    author = str(pull.get("author") or "").strip()
    state = "draft PR" if pull.get("draft") else "PR"
    task = _referenced_task(prefix, title, str(branch or "")) or _task_for(number, prefix)

    return EventCreate(
        event_type=EventType.UPDATE,
        agent=_clip(f"{AGENT_PREFIX}{author or UNKNOWN_ACTOR}", MAX_AGENT_CHARS),
        project=project,
        task=task,
        branch=_clip(str(branch), 255) if branch else None,
        summary=_clip(f"Open {state} #{number}: {title}", MAX_SUMMARY_CHARS),
        details={
            "action": "open_pull",
            "pr_number": number,
            "base": pull.get("base"),
            "draft": bool(pull.get("draft")),
            "title": title,
        },
        artifacts=_artifacts(pull.get("url")),
        metadata=_metadata("poll", "open_pull", None),
    )


async def poll_once(
    session: Session, github: GitHubService, settings: Settings | None = None
) -> list[Event]:
    """One polling tick: announce open PRs the relay has not seen before.

    The fallback for a relay with no publicly reachable URL. The ledger keys on
    ``pr:<number>:<state>`` so a restart re-reads the same open PRs without
    re-announcing them, and never raises — a poller that can take the relay down
    is worse than no poller.
    """
    settings = settings or github.settings
    resolved = resolve_project(settings)
    if not settings.github_enabled or resolved is None:
        return []

    try:
        pulls = await github.list_open_pulls()
    except Exception as exc:  # noqa: BLE001 - GitHub being down must not break a tick
        logger.warning("github poll failed: %s", type(exc).__name__)
        return []

    created: list[Event] = []
    for pull in pulls:
        number = _number(pull.get("number"))
        if number is None:
            continue
        external_id = f"pr:{number}:{'draft' if pull.get('draft') else 'open'}"
        if already_ingested(session, SOURCE, external_id):
            continue
        create = _event_for_open_pull(pull, project=resolved, prefix=settings.github_task_prefix)
        created.append(_persist(session, create, external_id))

    if created:
        session.commit()
        for event in created:
            session.refresh(event)
    return created
