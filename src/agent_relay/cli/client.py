"""Thin HTTP client used by the CLI.

The CLI never touches SQLite. Agents may run on other machines, and a single write
path (the API) is what keeps the event log consistent.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:8077"


class RelayError(RuntimeError):
    """An error worth showing to a human, already formatted."""

    def __init__(self, message: str, payload: Any | None = None) -> None:
        super().__init__(message)
        self.payload = payload


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


class RelayClient:
    def __init__(self, base_url: str | None = None, token: str | None = None) -> None:
        self.base_url = (base_url or env("AGENT_RELAY_URL", DEFAULT_URL) or DEFAULT_URL).rstrip("/")
        self.token = token or env("AGENT_RELAY_API_TOKEN")

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            response = httpx.request(
                method,
                url,
                json=json_body,
                params=clean_params,
                headers=self._headers(),
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            raise RelayError(
                f"Cannot reach the relay at {self.base_url}: {type(exc).__name__}. "
                "Is it running? Try `agent-relay serve` or set AGENT_RELAY_URL."
            ) from exc

        if response.status_code >= 400:
            try:
                payload = response.json()
            except (ValueError, json.JSONDecodeError):
                payload = {"detail": response.text[:500]}
            raise RelayError(
                f"HTTP {response.status_code} from {method} {path}",
                payload.get("detail", payload) if isinstance(payload, dict) else payload,
            )
        if not response.content:
            return None
        return response.json()

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, body: dict[str, Any]) -> Any:
        return self.request("POST", path, json_body=body)
