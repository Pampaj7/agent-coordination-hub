"""MCP server for the relay.

``main`` is resolved lazily. Importing this package must stay free of the optional
``mcp`` dependency, because ``agent_relay`` is imported by the API and the CLI too.
"""

from __future__ import annotations

from typing import Any

__all__ = ["build_server", "main"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from agent_relay.mcp import server

        return getattr(server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
