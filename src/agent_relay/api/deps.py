"""Shared FastAPI dependencies: auth, settings, integrations."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from agent_relay.config import Settings, get_settings
from agent_relay.db.session import get_session
from agent_relay.services import tailnet
from agent_relay.services.github import GitHubService
from agent_relay.services.slack import SlackNotifier


def _bearer_matches(authorization: str | None, expected: str) -> bool:
    scheme, _, presented = (authorization or "").partition(" ")
    if scheme.lower() != "bearer":
        return False
    return secrets.compare_digest(
        # Bytes, not str: compare_digest raises TypeError on non-ASCII input, and the
        # Authorization header is attacker-controlled.
        presented.strip().encode("utf-8", "ignore"),
        expected.encode(),
    )


def require_token(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Decide whether this caller may act, by whichever scheme is configured.

    Three modes, in order of preference:

    * **Tailnet identity** (``AGENT_RELAY_TAILSCALE_AUTH``). The peer is already
      authenticated by WireGuard before we see it, so we ask Tailscale who owns the
      calling address. Nothing to distribute, nothing to rotate, and the caller cannot
      choose their own answer.
    * **Shared token** (``AGENT_RELAY_API_TOKEN``). A speed bump for a trusted network.
    * **Open.** The default, for localhost.

    The peer address is read from the socket, never from ``X-Forwarded-For`` — that
    header is set by the caller, so trusting it would let anyone assume any identity.
    Behind a reverse proxy the socket peer is the proxy, so identity mode will refuse
    everyone rather than silently trust a spoofable header. That is the safe failure.
    """
    if settings.tailscale_auth_enabled:
        host = request.client.host if request.client else None
        identity = tailnet.whois(host, settings) if host else None
        if identity is not None:
            # Hand the resolved identity to the route layer so human_owner stops being
            # self-declared.
            request.state.tailnet_identity = identity
            return
        if settings.tailscale_allow_token and settings.api_token:
            if _bearer_matches(authorization, settings.api_token):
                return
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Not a recognised tailnet peer, and no valid bearer token.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                f"Caller {host or 'unknown'} is not a recognised tailnet peer. "
                "Connect over Tailscale, or disable AGENT_RELAY_TAILSCALE_AUTH."
            ),
        )

    expected = settings.api_token
    if not expected:
        return
    if not _bearer_matches(authorization, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def caller_owner(request: Request, settings: Settings) -> str | None:
    """The human_owner this request should be attributed to, if the tailnet knows."""
    identity = getattr(request.state, "tailnet_identity", None)
    if identity is None:
        return None
    return settings.owner_for(identity.login_name) or identity.short_name


def get_github(settings: Annotated[Settings, Depends(get_settings)]) -> GitHubService:
    return GitHubService(settings)


def get_slack(settings: Annotated[Settings, Depends(get_settings)]) -> SlackNotifier:
    return SlackNotifier(settings)


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[Session, Depends(get_session)]
GitHubDep = Annotated[GitHubService, Depends(get_github)]
SlackDep = Annotated[SlackNotifier, Depends(get_slack)]
AuthDep = Depends(require_token)
