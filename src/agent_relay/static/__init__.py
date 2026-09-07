"""Static assets served by the relay.

This is a package rather than a bare directory so the HTML ships as package data in
a wheel: hatchling includes every file under ``src/agent_relay``, and making the
directory importable also lets ``importlib.resources`` address it if we ever need to.
There is deliberately no build step here — see ``api/routes_dashboard.py``.
"""

from __future__ import annotations
