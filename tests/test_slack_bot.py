"""Bidirectional Slack: the round trip is the point.

V1 could only speak. With a bot token, ``chat.postMessage`` hands back a ``ts`` we
store on the event — and a human's threaded reply carries it straight back, which is
how a sentence typed in Slack becomes an ANSWER on the right question.

No network and no credentials: ``httpx.AsyncClient.post`` is replaced with a
recorder, exactly as ``tests/test_slack.py`` does for the webhook path.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_relay.api.routes import public_router, router
from agent_relay.api.routes_slack_hooks import router as slack_hooks_router
from agent_relay.config import Settings, get_settings
from agent_relay.db.models import Event
from agent_relay.db.session import init_db, reset_engine, session_scope
from agent_relay.services.slack_bot import (
    MAX_SIGNATURE_AGE_SECONDS,
    SlackBot,
    clean_slack_text,
    resolve_project_channel,
    verify_slack_signature,
)
from tests.conftest import INTEGRATION_ENV, post_event

BOT_TOKEN = "xoxb-000-111-xxxxxxxxSECRETxxxxxxxx"
SIGNING_SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
DEFAULT_CHANNEL = "C0DEFAULT"
EVENTS_PATH = "/webhooks/slack/events"
POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"

#: Cleared alongside conftest's list so a developer's real bot token cannot leak in.
SLACK_BOT_ENV = (
    "SLACK_BOT_TOKEN",
    "SLACK_SIGNING_SECRET",
    "SLACK_DEFAULT_CHANNEL",
    "SLACK_CHANNEL_MAP",
)


# --------------------------------------------------------------------------- fakes


@dataclass
class SlackAPI:
    """Stands in for the Slack Web API. Records calls, or fails on demand."""

    behaviour: str = "ok"
    calls: list[dict[str, Any]] = field(default_factory=list)
    _seq: int = 0

    async def post(self, url: str, json: dict[str, Any] | None = None, **_: Any) -> httpx.Response:
        self.calls.append({"url": url, "json": json or {}})
        request = httpx.Request("POST", url)
        if self.behaviour == "raise":
            raise httpx.ConnectError("connection refused to slack.com")
        if self.behaviour == "timeout":
            raise httpx.ReadTimeout("slack took too long")
        if self.behaviour == "http_error":
            return httpx.Response(500, text="server_error", request=request)
        if self.behaviour == "not_ok":
            # Slack's signature failure mode: HTTP 200, application-level error.
            return httpx.Response(
                200, json={"ok": False, "error": "channel_not_found"}, request=request
            )
        self._seq += 1
        return httpx.Response(
            200,
            json={
                "ok": True,
                "ts": f"1700000000.{self._seq:06d}",
                "channel": (json or {}).get("channel"),
            },
            request=request,
        )

    def posted_channels(self) -> list[str | None]:
        return [call["json"].get("channel") for call in self.calls]


@dataclass
class Relay:
    client: TestClient
    api: SlackAPI
    settings: Settings


def build_app() -> FastAPI:
    """The V1 surface plus the inbound Slack router (main.py wires this itself)."""
    application = FastAPI()
    application.include_router(public_router)
    application.include_router(router)
    application.include_router(slack_hooks_router)
    return application


@pytest.fixture
def relay(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Relay]]:
    """Factory for a relay configured with a given Slack environment."""

    def build(**env: str) -> Relay:
        monkeypatch.chdir(tmp_path)  # no stray .env from the developer's checkout
        for name in (*INTEGRATION_ENV, *SLACK_BOT_ENV):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("AGENT_RELAY_DB_URL", f"sqlite:///{tmp_path}/test.db")
        for name, value in env.items():
            monkeypatch.setenv(name, value)

        get_settings.cache_clear()
        reset_engine()
        settings = get_settings()
        init_db(settings)

        api = SlackAPI()
        monkeypatch.setattr(httpx.AsyncClient, "post", api.post, raising=False)
        return Relay(client=TestClient(build_app()), api=api, settings=settings)

    yield build
    reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def bot_relay(relay: Callable[..., Relay]) -> Relay:
    """The normal V2 configuration: bot token, signing secret, one default channel."""
    return relay(
        SLACK_BOT_TOKEN=BOT_TOKEN,
        SLACK_SIGNING_SECRET=SIGNING_SECRET,
        SLACK_DEFAULT_CHANNEL=DEFAULT_CHANNEL,
    )


# --------------------------------------------------------------------------- helpers


def sign(
    body: bytes, *, timestamp: str | None = None, secret: str = SIGNING_SECRET
) -> dict[str, str]:
    stamp = timestamp or str(int(time.time()))
    digest = hmac.new(
        secret.encode(), b"v0:" + stamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    return {
        "X-Slack-Request-Timestamp": stamp,
        "X-Slack-Signature": f"v0={digest}",
        "Content-Type": "application/json",
    }


def deliver(
    relay: Relay,
    envelope: dict[str, Any],
    *,
    timestamp: str | None = None,
    secret: str = SIGNING_SECRET,
    tamper: bool = False,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """POST an Events API envelope the way Slack would, signed over the raw body."""
    raw = json.dumps(envelope).encode()
    signed = sign(raw, timestamp=timestamp, secret=secret)
    if tamper:
        raw = json.dumps({**envelope, "team_id": "T-EVIL"}).encode()
    return relay.client.post(
        EVENTS_PATH, content=raw, headers=signed if headers is None else headers
    )


def reply_envelope(
    *,
    channel: str | None,
    thread_ts: str,
    user: str = "U0LEO",
    text: str = "Use H=8 — the 0.7% holds on the held-out split.",
    event_id: str = "Ev00000001",
    **overrides: Any,
) -> dict[str, Any]:
    inner: dict[str, Any] = {
        "type": "message",
        "channel": channel,
        "user": user,
        "text": text,
        "ts": f"{float(thread_ts) + 1:.6f}",
        "thread_ts": thread_ts,
    }
    inner.update(overrides)
    return {"type": "event_callback", "event_id": event_id, "team_id": "T0", "event": inner}


def announce(relay: Relay, event: dict[str, Any]) -> tuple[str | None, str | None]:
    """Where did the server's own Slack post land?

    This used to re-post the event by hand, imitating wiring that did not exist yet.
    It does not imitate anything now: ``POST /events`` dispatches through
    ``slack_bot.announce`` in a background task, and TestClient runs background tasks
    before returning — so by the time a test calls this, the real code has already
    posted and recorded the result. Reading it back is what makes these tests cover
    the production path instead of a parallel implementation of it.
    """
    row = stored(event["id"])
    return row.slack_ts, row.slack_channel


def stored(event_id: int) -> Event:
    with session_scope() as session:
        row = session.get(Event, event_id)
        assert row is not None
        return row


def open_questions(relay: Relay, project: str = "tether") -> list[str]:
    context = relay.client.get("/context", params={"project": project}).json()
    return [q["ref"] for q in context["unresolved_questions"]]


# ------------------------------------------------------------------ signature verification


def test_a_genuine_signature_is_accepted() -> None:
    body = b'{"type":"event_callback"}'
    stamp = str(int(time.time()))
    headers = sign(body, timestamp=stamp)
    assert verify_slack_signature(SIGNING_SECRET, stamp, body, headers["X-Slack-Signature"])


def test_a_signature_from_the_wrong_secret_is_rejected() -> None:
    body = b'{"type":"event_callback"}'
    stamp = str(int(time.time()))
    forged = sign(body, timestamp=stamp, secret="not-the-signing-secret")
    assert not verify_slack_signature(SIGNING_SECRET, stamp, body, forged["X-Slack-Signature"])


def test_a_tampered_body_is_rejected() -> None:
    stamp = str(int(time.time()))
    headers = sign(b'{"amount":1}', timestamp=stamp)
    assert not verify_slack_signature(
        SIGNING_SECRET, stamp, b'{"amount":1000000}', headers["X-Slack-Signature"]
    )


@pytest.mark.parametrize(
    ("timestamp", "signature"),
    [
        (None, "v0=abc"),  # missing timestamp header
        (str(int(time.time())), None),  # missing signature header
        ("not-a-number", "v0=abc"),  # unparseable timestamp
    ],
)
def test_missing_or_malformed_headers_are_rejected(
    timestamp: str | None, signature: str | None
) -> None:
    assert not verify_slack_signature(SIGNING_SECRET, timestamp, b"{}", signature)


def test_an_old_timestamp_is_rejected_even_with_a_valid_signature() -> None:
    """A signature never expires on its own, so a replayed request must be aged out."""
    body = b'{"type":"event_callback"}'
    stale = str(int(time.time()) - MAX_SIGNATURE_AGE_SECONDS - 60)
    headers = sign(body, timestamp=stale)
    # The HMAC itself is perfectly valid...
    digest = hmac.new(
        SIGNING_SECRET.encode(), b"v0:" + stale.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    assert headers["X-Slack-Signature"] == f"v0={digest}"
    # ...and the request is still refused.
    assert not verify_slack_signature(SIGNING_SECRET, stale, body, headers["X-Slack-Signature"])


def test_no_signing_secret_means_nothing_verifies() -> None:
    assert not verify_slack_signature(None, str(int(time.time())), b"{}", "v0=abc")


# ------------------------------------------------------------------------- the endpoint


def test_url_verification_echoes_the_challenge(bot_relay: Relay) -> None:
    response = deliver(bot_relay, {"type": "url_verification", "challenge": "3eZbrw1a"})
    assert response.status_code == 200
    assert response.json()["challenge"] == "3eZbrw1a"


def test_url_verification_still_needs_a_valid_signature(bot_relay: Relay) -> None:
    response = deliver(
        bot_relay, {"type": "url_verification", "challenge": "3eZbrw1a"}, secret="wrong"
    )
    assert response.status_code == 401


def test_endpoint_is_503_when_the_bot_is_not_configured(relay: Callable[..., Relay]) -> None:
    plain = relay()  # the V1 configuration: no bot token, no signing secret
    assert plain.settings.slack_events_enabled is False
    response = deliver(plain, {"type": "url_verification", "challenge": "x"})
    assert response.status_code == 503


def test_a_bad_signature_is_401(bot_relay: Relay) -> None:
    envelope = reply_envelope(channel=DEFAULT_CHANNEL, thread_ts="1700000000.000001")
    assert deliver(bot_relay, envelope, secret="wrong-secret").status_code == 401
    assert deliver(bot_relay, envelope, tamper=True).status_code == 401
    assert deliver(bot_relay, envelope, headers={}).status_code == 401


# --------------------------------------------------------------------------- outbound


def test_ok_false_is_a_failure_even_though_http_is_200(bot_relay: Relay) -> None:
    """Slack's classic trap: 200 OK with {"ok": false} means the post did not happen."""
    bot_relay.api.behaviour = "not_ok"
    event = post_event(bot_relay.client, event_type="UPDATE")

    assert announce(bot_relay, event) == (None, None)
    assert bot_relay.api.calls[0]["url"] == POST_MESSAGE_URL
    assert stored(event["id"]).slack_ts is None


def test_posting_stores_the_slack_ts_on_the_event(bot_relay: Relay) -> None:
    event = post_event(bot_relay.client, event_type="UPDATE")
    ts, channel = announce(bot_relay, event)

    assert ts == "1700000000.000001"
    assert channel == DEFAULT_CHANNEL
    row = stored(event["id"])
    assert row.slack_ts == ts
    assert row.slack_channel == DEFAULT_CHANNEL
    # Rendered by the same formatter as the webhook path — never a raw JSON dump.
    assert "UPDATE" in str(bot_relay.api.calls[0]["json"]["blocks"])


def test_slack_being_down_does_not_lose_the_event(bot_relay: Relay) -> None:
    bot_relay.api.behaviour = "raise"
    event = post_event(bot_relay.client, event_type="UPDATE")

    assert announce(bot_relay, event) == (None, None)
    assert len(bot_relay.client.get("/events", params={"project": "tether"}).json()) == 1


def test_per_project_channel_routing(relay: Callable[..., Relay]) -> None:
    routed = relay(
        SLACK_BOT_TOKEN=BOT_TOKEN,
        SLACK_SIGNING_SECRET=SIGNING_SECRET,
        SLACK_DEFAULT_CHANNEL=DEFAULT_CHANNEL,
        SLACK_CHANNEL_MAP="tether=C111,drends=C222",
    )
    assert resolve_project_channel(routed.settings, "tether") == "C111"
    assert resolve_project_channel(routed.settings, "drends") == "C222"
    assert resolve_project_channel(routed.settings, "unlisted") == DEFAULT_CHANNEL

    for project in ("tether", "drends", "unlisted"):
        announce(routed, post_event(routed.client, project=project))
    assert routed.api.posted_channels() == ["C111", "C222", DEFAULT_CHANNEL]


def test_the_bot_is_disabled_without_a_token() -> None:
    bot = SlackBot(Settings(AGENT_RELAY_DB_URL="sqlite://"))
    assert bot.enabled is False
    assert asyncio.run(bot.post_event({"project": "tether"}, None, "C123")) == (None, None)
    assert asyncio.run(bot.post_thread_reply("C123", "1.0", "hi")) is False


# ----------------------------------------------------------------- the thread round trip


def test_a_threaded_reply_answers_the_question_and_closes_it(bot_relay: Relay) -> None:
    """The payoff: a human types in Slack, and the relay's open question is resolved."""
    question = post_event(
        bot_relay.client,
        event_type="QUESTION",
        summary="Which horizon should we ship, H=6 or H=8?",
        target_agent="andrea-agent",
        details={},
        artifacts=[],
    )
    ts, channel = announce(bot_relay, question)
    assert ts is not None and channel is not None
    assert open_questions(bot_relay) == [question["ref"]]

    response = deliver(
        bot_relay,
        reply_envelope(
            channel=channel,
            thread_ts=ts,
            user="U0LEO",
            text="<@U0BOT> Ship H=8 — see <https://gh.io/142|the issue>.",
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "created"

    answer = body["event"]
    assert answer["event_type"] == "ANSWER"
    assert answer["in_reply_to"] == question["ref"]
    assert answer["agent"] == "slack:U0LEO"
    assert answer["target_agent"] == question["agent"]
    assert answer["project"] == "tether" and answer["task"] == "GH-142"
    # Slack markup is stripped: an agent reading this back sees prose, not ids.
    assert answer["summary"] == "Ship H=8 — see the issue."

    row = stored(answer["id"])
    assert row.source == "slack"
    assert row.slack_channel == channel

    # ...and the question is no longer waiting on anybody.
    assert open_questions(bot_relay) == []


def test_the_human_gets_an_in_thread_acknowledgement(bot_relay: Relay) -> None:
    question = post_event(bot_relay.client, event_type="QUESTION", summary="H=6 or H=8?")
    ts, channel = announce(bot_relay, question)
    bot_relay.api.calls.clear()

    answer = deliver(bot_relay, reply_envelope(channel=channel, thread_ts=str(ts))).json()["event"]

    acks = [c["json"] for c in bot_relay.api.calls if c["json"].get("thread_ts")]
    assert len(acks) == 1
    assert acks[0]["channel"] == channel
    assert acks[0]["thread_ts"] == ts
    assert answer["ref"] in acks[0]["text"]


def test_a_reply_to_a_non_question_becomes_an_update(bot_relay: Relay) -> None:
    update = post_event(bot_relay.client, event_type="UPDATE")
    ts, channel = announce(bot_relay, update)

    body = deliver(bot_relay, reply_envelope(channel=channel, thread_ts=str(ts))).json()
    assert body["status"] == "created"
    assert body["event"]["event_type"] == "UPDATE"
    assert body["event"]["in_reply_to"] is None
    assert body["event"]["agent"] == "slack:U0LEO"


def test_a_reply_to_an_unknown_thread_is_ignored(bot_relay: Relay) -> None:
    post_event(bot_relay.client, event_type="UPDATE")  # never announced to Slack
    before = len(bot_relay.client.get("/events").json())

    body = deliver(
        bot_relay, reply_envelope(channel=DEFAULT_CHANNEL, thread_ts="1699999999.999999")
    ).json()
    assert body["status"] == "ignored"
    assert body["event"] is None
    assert len(bot_relay.client.get("/events").json()) == before


def test_bot_messages_are_ignored(bot_relay: Relay) -> None:
    """Our own posts come back as events; ingesting them would be an echo loop."""
    event = post_event(bot_relay.client, event_type="QUESTION", summary="H=6 or H=8?")
    ts, channel = announce(bot_relay, event)
    before = len(bot_relay.client.get("/events").json())

    from_bot = deliver(
        bot_relay,
        reply_envelope(channel=channel, thread_ts=str(ts), bot_id="B0RELAY", event_id="Ev-bot"),
    ).json()
    from_subtype = deliver(
        bot_relay,
        reply_envelope(
            channel=channel, thread_ts=str(ts), subtype="bot_message", event_id="Ev-subtype"
        ),
    ).json()
    edited = deliver(
        bot_relay,
        reply_envelope(
            channel=channel, thread_ts=str(ts), subtype="message_changed", event_id="Ev-edit"
        ),
    ).json()

    assert [from_bot["status"], from_subtype["status"], edited["status"]] == ["ignored"] * 3
    assert len(bot_relay.client.get("/events").json()) == before


def test_unthreaded_messages_are_ignored(bot_relay: Relay) -> None:
    """A top-level message answers nothing: there is no anchor to attach it to."""
    event = post_event(bot_relay.client, event_type="QUESTION", summary="H=6 or H=8?")
    announce(bot_relay, event)
    before = len(bot_relay.client.get("/events").json())

    envelope = reply_envelope(channel=DEFAULT_CHANNEL, thread_ts="1700000000.000001")
    del envelope["event"]["thread_ts"]
    assert deliver(bot_relay, envelope).json()["status"] == "ignored"
    assert len(bot_relay.client.get("/events").json()) == before


def test_the_same_slack_event_id_is_ingested_once(bot_relay: Relay) -> None:
    """Slack retries deliveries; the ingest ledger is what stops duplicate answers."""
    question = post_event(bot_relay.client, event_type="QUESTION", summary="H=6 or H=8?")
    ts, channel = announce(bot_relay, question)
    envelope = reply_envelope(channel=channel, thread_ts=str(ts), event_id="Ev0RETRY")

    first = deliver(bot_relay, envelope).json()
    second = deliver(bot_relay, envelope).json()
    third = deliver(bot_relay, envelope).json()

    assert first["status"] == "created"
    assert [second["status"], third["status"]] == ["duplicate", "duplicate"]
    answers = bot_relay.client.get("/events", params={"event_type": "ANSWER"}).json()
    assert len(answers) == 1
    assert answers[0]["id"] == first["event"]["id"]


def test_a_different_reply_in_the_same_thread_is_a_second_answer(bot_relay: Relay) -> None:
    question = post_event(bot_relay.client, event_type="QUESTION", summary="H=6 or H=8?")
    ts, channel = announce(bot_relay, question)

    deliver(bot_relay, reply_envelope(channel=channel, thread_ts=str(ts), event_id="Ev-1"))
    deliver(
        bot_relay,
        reply_envelope(
            channel=channel, thread_ts=str(ts), event_id="Ev-2", user="U0AND", text="Agreed."
        ),
    )
    answers = bot_relay.client.get("/events", params={"event_type": "ANSWER"}).json()
    assert {a["agent"] for a in answers} == {"slack:U0LEO", "slack:U0AND"}


def test_an_empty_reply_creates_nothing(bot_relay: Relay) -> None:
    event = post_event(bot_relay.client, event_type="QUESTION", summary="H=6 or H=8?")
    ts, channel = announce(bot_relay, event)
    before = len(bot_relay.client.get("/events").json())

    body = deliver(
        bot_relay, reply_envelope(channel=channel, thread_ts=str(ts), text="   <@U0BOT>  ")
    ).json()
    assert body["status"] == "ignored"
    assert len(bot_relay.client.get("/events").json()) == before


def test_unknown_envelopes_are_answered_200(bot_relay: Relay) -> None:
    """Slack retries anything that is not a fast 200, including our own 500s."""
    assert deliver(bot_relay, {"type": "something_new"}).json()["status"] == "ignored"
    raw = b"not json at all"
    response = bot_relay.client.post(EVENTS_PATH, content=raw, headers=sign(raw))
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"


# --------------------------------------------------------------- unit-level checks


def test_slack_markup_is_stripped_and_long_replies_truncated() -> None:
    assert clean_slack_text("<@U0BOT> ping <#C1|general>") == "ping general"
    assert clean_slack_text("see <https://example.com>") == "see https://example.com"
    assert clean_slack_text("a &amp; b &lt;c&gt;") == "a & b <c>"
    long_reply = clean_slack_text("x" * 5000)
    assert len(long_reply) <= 2000 and long_reply.endswith("…")


def test_the_round_trip_works_through_the_real_server_wiring(bot_relay: Relay) -> None:
    """Regression: the outbound half of two-way Slack was never wired up.

    `SlackBot.post_event` existed and the inbound path was correct, but nothing in
    `src/` called it — `POST /events` went out through the webhook notifier, which
    cannot return a message ts. With `slack_ts` always NULL no threaded reply could
    ever find its anchor, so every human answer was silently ignored. This test drives
    only the public API, so it fails if that wiring is ever removed again.
    """
    question = post_event(
        bot_relay.client,
        event_type="QUESTION",
        target_agent="niccolo-claude",
        summary="Masking before or after resize?",
        details={},
        artifacts=[],
    )
    assert question["ref"] == "Q-1"

    # The server posted it and remembered where, with no help from this test.
    row = stored(question["id"])
    assert row.slack_ts, "POST /events must record the Slack ts via the bot"
    assert row.slack_channel

    open_now = bot_relay.client.get("/context", params={"project": "tether"}).json()
    assert [q["ref"] for q in open_now["unresolved_questions"]] == ["Q-1"]

    # A human replies in that Slack thread.
    response = deliver(
        bot_relay,
        reply_envelope(
            channel=row.slack_channel,
            thread_ts=row.slack_ts,
            user="U04NIC",
            text="before resize, in the loader",
            event_id="Ev-roundtrip",
        ),
    )
    assert response.json()["status"] == "created"

    answer = response.json()["event"]
    assert answer["event_type"] == "ANSWER"
    assert answer["in_reply_to"] == "Q-1"
    assert answer["agent"].startswith("slack:")

    # The payoff: the question is closed.
    after = bot_relay.client.get("/context", params={"project": "tether"}).json()
    assert after["unresolved_questions"] == []


def test_a_non_ascii_signature_is_rejected_not_a_500(bot_relay: Relay) -> None:
    """Regression: an unauthenticated 500 on a public endpoint, and a retry storm.

    Starlette decodes header bytes as latin-1, so a raw high byte gives a non-ASCII
    str, and `hmac.compare_digest` on str raises TypeError for non-ASCII input. The
    check runs before the handler's try block, so it escaped as a 500 — which is
    exactly what makes Slack retry the same bad payload forever.
    """
    # The raw byte 0xE9 is what a hostile client puts on the wire; Starlette hands the
    # handler the latin-1 decoding of it, which is a non-ASCII str.
    raw_signature = b"v0=\xe9" + b"a" * 63
    assert not raw_signature.decode("latin-1").isascii()

    body = json.dumps({"type": "event_callback", "event": {}}).encode()
    response = bot_relay.client.post(
        EVENTS_PATH,
        content=body,
        headers=[
            (b"content-type", b"application/json"),
            (b"x-slack-request-timestamp", str(int(time.time())).encode()),
            (b"x-slack-signature", raw_signature),
        ],
    )
    assert response.status_code == 401, "a malformed signature is a rejection, not a crash"
