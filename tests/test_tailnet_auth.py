"""Tailnet identity as the authentication scheme.

The security claim being tested: with this on, `human_owner` is a fact rather than a
self-declaration, and there is no header a caller can set to change who they are.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_relay.config import Settings, get_settings
from agent_relay.db.session import init_db, reset_engine
from agent_relay.main import app
from agent_relay.services import tailnet
from tests.conftest import INTEGRATION_ENV, event_payload

LEO_IP = "100.107.14.70"
NICCOLO_IP = "100.99.1.2"
OFF_TAILNET_IP = "10.64.227.128"

WHOIS = {
    LEO_IP: {
        "Node": {"Name": "aau142982.tailc62d0c.ts.net."},
        "UserProfile": {
            "LoginName": "leonardo.gameplay666@gmail.com",
            "DisplayName": "leonardo pampaloni",
        },
    },
    NICCOLO_IP: {
        "Node": {"Name": "niccolo-laptop.tailc62d0c.ts.net."},
        "UserProfile": {"LoginName": "niccolo@example.com", "DisplayName": "Niccolo"},
    },
}


class FakeWhois:
    """Stands in for the `tailscale whois` subprocess."""

    def __init__(self, table: dict[str, dict[str, Any]] | None = None) -> None:
        self.table = WHOIS if table is None else table
        self.calls: list[str] = []

    def __call__(self, args: list[str], **kwargs: Any) -> Any:
        self.calls.append(args[-1])
        payload = self.table.get(args[-1])

        class Result:
            returncode = 0 if payload else 1
            stdout = json.dumps(payload) if payload else ""
            stderr = ""

        return Result()


@pytest.fixture(autouse=True)
def _clear_identity_cache() -> Iterator[None]:
    tailnet.clear_cache()
    yield
    tailnet.clear_cache()


def build_relay(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    whois: FakeWhois,
    **env: str,
) -> TestClient:
    monkeypatch.chdir(tmp_path)
    for name in INTEGRATION_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_RELAY_DB_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("AGENT_RELAY_TAILSCALE_AUTH", "true")
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    monkeypatch.setattr(tailnet.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(tailnet.subprocess, "run", whois)

    get_settings.cache_clear()
    reset_engine()
    init_db(get_settings())
    return TestClient(app, client=(LEO_IP, 41234))


@pytest.fixture
def whois() -> FakeWhois:
    return FakeWhois()


# ------------------------------------------------------------------ the happy path


def test_a_known_peer_is_admitted_with_no_token(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, whois: FakeWhois
) -> None:
    with build_relay(tmp_path, monkeypatch, whois) as client:
        assert client.get("/events").status_code == 200
    reset_engine()
    get_settings.cache_clear()


def test_identity_overrides_a_self_declared_owner(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, whois: FakeWhois
) -> None:
    """The whole point: a field the caller can set is not an attribution."""
    with build_relay(tmp_path, monkeypatch, whois) as client:
        created = client.post(
            "/events", json=event_payload(agent="leo-codex", human_owner="andrea")
        ).json()
        # The caller claimed to be andrea; the tailnet says otherwise.
        assert created["human_owner"] == "leonardo"
    reset_engine()
    get_settings.cache_clear()


def test_the_owner_map_renames_a_login(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, whois: FakeWhois
) -> None:
    with build_relay(
        tmp_path,
        monkeypatch,
        whois,
        AGENT_RELAY_OWNER_MAP="leonardo.gameplay666@gmail.com=leo",
    ) as client:
        created = client.post("/events", json=event_payload(human_owner=None)).json()
        assert created["human_owner"] == "leo"
    reset_engine()
    get_settings.cache_clear()


def test_claims_are_attributed_too(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, whois: FakeWhois
) -> None:
    with build_relay(tmp_path, monkeypatch, whois) as client:
        claim = client.post(
            "/claim",
            json={
                "agent": "leo-codex",
                "project": "tether",
                "task": "GH-142",
                "human_owner": "somebody-else",
            },
        ).json()
        assert claim["human_owner"] == "leonardo"
    reset_engine()
    get_settings.cache_clear()


# ------------------------------------------------------------------ refusals


def test_an_unknown_peer_is_refused(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, whois: FakeWhois
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in INTEGRATION_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_RELAY_DB_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("AGENT_RELAY_TAILSCALE_AUTH", "true")
    monkeypatch.setattr(tailnet.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(tailnet.subprocess, "run", whois)
    get_settings.cache_clear()
    reset_engine()
    init_db(get_settings())

    # A caller from the shared LAN, not the tailnet.
    with TestClient(app, client=(OFF_TAILNET_IP, 5000)) as client:
        response = client.get("/events")
        assert response.status_code == 401
        assert "tailnet" in response.json()["detail"]
    # An off-tailnet address must not even reach the subprocess.
    assert OFF_TAILNET_IP not in whois.calls
    reset_engine()
    get_settings.cache_clear()


def test_a_forwarded_for_header_cannot_grant_identity(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, whois: FakeWhois
) -> None:
    """X-Forwarded-For is caller-controlled, so it must never be consulted."""
    monkeypatch.chdir(tmp_path)
    for name in INTEGRATION_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_RELAY_DB_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("AGENT_RELAY_TAILSCALE_AUTH", "true")
    monkeypatch.setattr(tailnet.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(tailnet.subprocess, "run", whois)
    get_settings.cache_clear()
    reset_engine()
    init_db(get_settings())

    with TestClient(app, client=(OFF_TAILNET_IP, 5000)) as client:
        response = client.get(
            "/events",
            headers={"X-Forwarded-For": LEO_IP, "X-Real-IP": LEO_IP},
        )
        assert response.status_code == 401, "a spoofable header must not authenticate"
    reset_engine()
    get_settings.cache_clear()


def test_a_tailnet_address_the_tailnet_does_not_know_is_refused(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = FakeWhois(table={})
    monkeypatch.chdir(tmp_path)
    for name in INTEGRATION_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_RELAY_DB_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("AGENT_RELAY_TAILSCALE_AUTH", "true")
    monkeypatch.setattr(tailnet.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(tailnet.subprocess, "run", empty)
    get_settings.cache_clear()
    reset_engine()
    init_db(get_settings())

    with TestClient(app, client=(LEO_IP, 41234)) as client:
        assert client.get("/events").status_code == 401
    reset_engine()
    get_settings.cache_clear()


def test_health_stays_open_for_monitoring(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, whois: FakeWhois
) -> None:
    monkeypatch.chdir(tmp_path)
    for name in INTEGRATION_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_RELAY_DB_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("AGENT_RELAY_TAILSCALE_AUTH", "true")
    monkeypatch.setattr(tailnet.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(tailnet.subprocess, "run", whois)
    get_settings.cache_clear()
    reset_engine()
    init_db(get_settings())

    with TestClient(app, client=(OFF_TAILNET_IP, 5000)) as client:
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["integrations"]["tailscale_auth"] == "enabled"
    reset_engine()
    get_settings.cache_clear()


# ------------------------------------------------------------------ unit level


def test_only_tailnet_ranges_are_treated_as_tailnet() -> None:
    assert tailnet.is_tailnet_address("100.107.14.70") is True
    assert tailnet.is_tailnet_address("100.64.0.1") is True
    assert tailnet.is_tailnet_address("fd7a:115c:a1e0::fb39:e47") is True
    # Just outside the CGNAT range, and ordinary private/public space.
    assert tailnet.is_tailnet_address("100.128.0.1") is False
    assert tailnet.is_tailnet_address("10.64.227.128") is False
    assert tailnet.is_tailnet_address("8.8.8.8") is False
    assert tailnet.is_tailnet_address("not-an-ip") is False


def test_whois_is_cached_so_it_is_not_a_subprocess_per_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    whois = FakeWhois()
    monkeypatch.setattr(tailnet.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(tailnet.subprocess, "run", whois)
    settings = Settings(AGENT_RELAY_DB_URL="sqlite://", AGENT_RELAY_TAILSCALE_AUTH=True)

    for _ in range(5):
        assert tailnet.whois(LEO_IP, settings) is not None
    assert len(whois.calls) == 1, "identities are stable; one lookup should serve"

    # ...but the cache expires rather than pinning a departed member forever.
    later = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=tailnet.CACHE_TTL_SECONDS + 1)
    assert tailnet.whois(LEO_IP, settings, now=later) is not None
    assert len(whois.calls) == 2


def test_whois_never_raises_when_the_binary_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tailnet.shutil, "which", lambda _: None)
    settings = Settings(AGENT_RELAY_DB_URL="sqlite://", AGENT_RELAY_TAILSCALE_AUTH=True)
    assert tailnet.whois(LEO_IP, settings) is None


def test_whois_survives_a_hanging_or_broken_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess as sp

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise sp.TimeoutExpired(cmd="tailscale", timeout=5)

    monkeypatch.setattr(tailnet.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(tailnet.subprocess, "run", boom)
    settings = Settings(AGENT_RELAY_DB_URL="sqlite://", AGENT_RELAY_TAILSCALE_AUTH=True)
    assert tailnet.whois(LEO_IP, settings) is None


def test_garbage_output_is_not_an_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    class Junk:
        returncode = 0
        stdout = "not json at all"
        stderr = ""

    monkeypatch.setattr(tailnet.shutil, "which", lambda _: "/usr/bin/tailscale")
    monkeypatch.setattr(tailnet.subprocess, "run", lambda *a, **k: Junk())
    settings = Settings(AGENT_RELAY_DB_URL="sqlite://", AGENT_RELAY_TAILSCALE_AUTH=True)
    assert tailnet.whois(LEO_IP, settings) is None
