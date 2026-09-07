"""Shared FastAPI dependencies: auth, settings, integrations."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from agent_relay.config import Settings, get_settings
from agent_relay.db.session import get_session
from agent_relay.services.github import GitHubService
from agent_relay.services.slack import SlackNotifier


def require_token(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Optional shared-secret auth.

    Disabled unless ``AGENT_RELAY_API_TOKEN`` is set. This is a speed bump for a
    trusted LAN, not an authentication system — see docs/ARCHITECTURE.md.
    """
    expected = settings.api_token
    if not expected:
        return
    scheme, _, presented = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(presented.strip(), expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


def get_github(settings: Annotated[Settings, Depends(get_settings)]) -> GitHubService:
    return GitHubService(settings)


def get_slack(settings: Annotated[Settings, Depends(get_settings)]) -> SlackNotifier:
    return SlackNotifier(settings)


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[Session, Depends(get_session)]
GitHubDep = Annotated[GitHubService, Depends(get_github)]
SlackDep = Annotated[SlackNotifier, Depends(get_slack)]
AuthDep = Depends(require_token)
