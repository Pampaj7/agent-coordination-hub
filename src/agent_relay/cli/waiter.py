"""Wait for something to land in an agent's inbox, then act on it.

Agents cannot be woken. Claude Code and Codex are turn-based processes: they run,
call tools, answer, and stop. There is no event loop inside them listening for a
message, so no amount of pushing from the relay would reach one.

What *can* be woken is a shell. This watches an inbox and, when something new
arrives, runs a command — which may well be the command that starts an agent. The
relay does not wake your agent; it wakes a process that launches your agent. That is
the honest version of "agents waking each other", and it is enough:

    agent-relay wait --exec 'claude -p "check your agent-relay inbox and act on it"'

Polling, not long-polling: an inbox is small, the interval is seconds, and a plain
GET loop has no server state to get wrong and recovers from a restarted relay by
itself.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

from agent_relay.cli.client import RelayClient, RelayError

#: Long enough that idle watching is free, short enough to feel immediate.
DEFAULT_INTERVAL = 15.0


def inbox_refs(inbox: dict[str, Any]) -> set[str]:
    """The set of item refs currently in an inbox.

    Refs rather than a count: an item answered while another arrives leaves the count
    unchanged, and that is precisely the moment you must not miss.
    """
    refs = {str(q.get("ref")) for q in inbox.get("questions_for_me") or []}
    refs |= {str(h.get("ref")) for h in inbox.get("handoffs_to_me") or []}
    refs |= {
        f"{t.get('project')}/{t.get('task')}" for t in inbox.get("my_tasks_needing_attention") or []
    }
    return refs


def describe(inbox: dict[str, Any], new_refs: set[str]) -> str:
    """One line per new item, for a human watching the terminal."""
    lines: list[str] = []
    for q in inbox.get("questions_for_me") or []:
        if str(q.get("ref")) in new_refs:
            lines.append(f"❓ {q['ref']} from {q['from_agent']}: {q['question']}")
    for h in inbox.get("handoffs_to_me") or []:
        if str(h.get("ref")) in new_refs:
            lines.append(f"🤝 {h['ref']} from {h['from_agent']}: {h['summary']}")
    for t in inbox.get("my_tasks_needing_attention") or []:
        if f"{t.get('project')}/{t.get('task')}" in new_refs:
            lines.append(f"🚧 {t['project']}/{t['task']}: {t['reason']}")
    return "\n".join(lines)


def watch(
    client: RelayClient,
    agent: str,
    *,
    project: str | None = None,
    interval: float = DEFAULT_INTERVAL,
    once: bool = False,
    command: str | None = None,
    emit: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    max_polls: int | None = None,
) -> int:
    """Poll until something new appears. Returns the number of wake-ups delivered.

    Seeded with whatever is already in the inbox, so starting the watcher does not
    immediately fire on a backlog you have already seen. Pass ``max_polls`` to bound
    it, which is what makes this testable without a clock.
    """
    try:
        seen = inbox_refs(client.get("/inbox", agent=agent, project=project))
    except RelayError as exc:
        emit(f"⚠️  {exc}")
        return 0

    emit(f"watching {agent}'s inbox every {interval:g}s — {len(seen)} item(s) already there")
    delivered = 0
    polls = 0
    while max_polls is None or polls < max_polls:
        polls += 1
        sleep(interval)
        try:
            inbox = client.get("/inbox", agent=agent, project=project)
        except RelayError as exc:
            # A relay restart or a dropped tunnel is normal; keep watching.
            emit(f"⚠️  {exc}")
            continue

        current = inbox_refs(inbox)
        new_refs = current - seen
        seen = current
        if not new_refs:
            continue

        delivered += 1
        emit(describe(inbox, new_refs))
        if command:
            emit(f"→ running: {command}")
            # shell=True is the point: the command is the user's own, from their own
            # shell config, and is usually a pipeline that launches an agent.
            result = subprocess.run(command, shell=True, check=False)
            if result.returncode != 0:
                emit(f"⚠️  command exited {result.returncode}")
        if once:
            break
    return delivered


def run(
    agent: str,
    *,
    project: str | None = None,
    interval: float = DEFAULT_INTERVAL,
    once: bool = False,
    command: str | None = None,
) -> int:
    """Entry point for the CLI. Ctrl-C exits quietly."""
    if command:
        # Fail early on a command the shell cannot even parse, rather than at 3am on
        # the first message that arrives.
        shlex.split(command)
    try:
        return watch(
            RelayClient(),
            agent,
            project=project,
            interval=interval,
            once=once,
            command=command,
        )
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        return 0
