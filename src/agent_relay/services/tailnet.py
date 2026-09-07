"""Tailscale identity: who is actually calling.

The relay's shared bearer token answers "is this caller allowed in" but not "who is
this", so `human_owner` is self-declared and any agent can post as anyone. When every
request already arrives over a tailnet, that is throwing away an answer we have: the
peer is authenticated by WireGuard before the first byte reaches us, and Tailscale can
map the peer address to a real account.

So this module asks `tailscale whois` who owns the calling IP. No login screen, no
second secret to distribute, and `human_owner` stops being a claim and becomes a fact.

Two limits, both deliberate:

* **It only works when the peer address is real.** Behind a reverse proxy every request
  appears to come from the proxy, and `X-Forwarded-For` is caller-controlled, so
  trusting it would hand anyone the identity of their choice. The relay therefore reads
  the socket peer only. Run it on the tailnet directly, which is the deployment this
  is for.
* **It identifies the human, not the agent.** Agent names stay self-declared, because
  one person legitimately runs several. What it pins down is which human is behind them.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import logging
import shutil
import subprocess
from dataclasses import dataclass

from agent_relay.config import Settings

logger = logging.getLogger(__name__)

#: Identities are stable for the life of a node, so a short cache removes a subprocess
#: from the hot path of every request without risking a stale answer that matters.
CACHE_TTL_SECONDS = 300
WHOIS_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class TailnetIdentity:
    """Who Tailscale says the caller is."""

    login_name: str
    display_name: str | None
    node_name: str | None

    @property
    def short_name(self) -> str:
        """A short owner name a human would actually write.

        The email local part is often not it — "leonardo.gameplay666" is nobody's
        name. The display name's first word usually is, so prefer that and fall back
        to the local part. `AGENT_RELAY_OWNER_MAP` overrides both when the guess is
        wrong; this only has to be reasonable by default.
        """
        if self.display_name:
            first = self.display_name.strip().split()[0]
            if first.replace("-", "").replace("'", "").isalpha():
                return first.lower()
        return self.login_name.split("@", 1)[0]


_cache: dict[str, tuple[TailnetIdentity | None, dt.datetime]] = {}


def clear_cache() -> None:
    """Drop cached identities. Used by tests and after a tailnet membership change."""
    _cache.clear()


def is_tailnet_address(host: str) -> bool:
    """True for the 100.64.0.0/10 CGNAT range Tailscale uses, and its IPv6 ULA."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.version == 4:
        return address in ipaddress.ip_network("100.64.0.0/10")
    return address in ipaddress.ip_network("fd7a:115c:a1e0::/48")


def _run_whois(host: str, binary: str) -> dict[str, object] | None:
    try:
        # Fixed binary resolved via which(); the argument is an already-validated IP.
        result = subprocess.run(
            [binary, "whois", "--json", host],
            capture_output=True,
            text=True,
            timeout=WHOIS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("tailscale whois failed for %s: %s", host, type(exc).__name__)
        return None
    if result.returncode != 0:
        # Normal for an address that is not on the tailnet; not worth a warning.
        logger.debug("tailscale whois %s exited %s", host, result.returncode)
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("tailscale whois returned unparseable output for %s", host)
        return None
    return payload if isinstance(payload, dict) else None


def whois(
    host: str, settings: Settings, *, now: dt.datetime | None = None
) -> TailnetIdentity | None:
    """Resolve a peer address to a tailnet identity, or None if it cannot be.

    Never raises: an unidentifiable caller is a decision for the auth layer, not an
    error here.
    """
    now = now or dt.datetime.now(dt.UTC)
    cached = _cache.get(host)
    if cached is not None and (now - cached[1]).total_seconds() < CACHE_TTL_SECONDS:
        return cached[0]

    identity: TailnetIdentity | None = None
    if is_tailnet_address(host):
        binary = shutil.which(settings.tailscale_binary) or None
        if binary is None:
            logger.warning(
                "tailscale identity is enabled but %r is not on PATH", settings.tailscale_binary
            )
        else:
            payload = _run_whois(host, binary)
            identity = _identity_from(payload) if payload else None

    _cache[host] = (identity, now)
    return identity


def _identity_from(payload: dict[str, object]) -> TailnetIdentity | None:
    profile = payload.get("UserProfile")
    node = payload.get("Node")
    if not isinstance(profile, dict):
        return None
    login = profile.get("LoginName")
    if not isinstance(login, str) or not login:
        return None
    display = profile.get("DisplayName")
    node_name = node.get("Name") if isinstance(node, dict) else None
    return TailnetIdentity(
        login_name=login,
        display_name=display if isinstance(display, str) and display else None,
        node_name=node_name.rstrip(".") if isinstance(node_name, str) else None,
    )


def resolve_owner(host: str | None, settings: Settings) -> str | None:
    """The `human_owner` this caller should be recorded as, if we can tell."""
    if not host or not settings.tailscale_auth_enabled:
        return None
    identity = whois(host, settings)
    if identity is None:
        return None
    return settings.owner_for(identity.login_name) or identity.short_name
