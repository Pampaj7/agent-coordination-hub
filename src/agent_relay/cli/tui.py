"""Live read-only dashboard — the pane you leave open in a split terminal all day.

Read-only is a hard property, not a habit: every request this module makes is a GET.
A dashboard that refreshes itself unattended next to a keyboard must never be able to
claim, release or post anything.

Layout priority is "what needs a human first": blockers, then open questions, then who
is working, then the stream. Counts and colour never carry meaning alone — every state
is also spelled out with an emoji and a word, because this output is read over SSH, in
tmux, and by agents capturing stdout.

Rendering is a set of pure functions over already-fetched dicts, so the layout is
testable without a terminal. ``run_tui`` is the only part that does I/O.
"""

from __future__ import annotations

import datetime as dt
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from rich import box
from rich.columns import Columns
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agent_relay.cli.client import RelayClient, RelayError
from agent_relay.cli.render import PRESENCE_GLYPHS, ago
from agent_relay.models.enums import EVENT_LABELS

#: Seconds between refreshes. Ten is slow enough to be free and fast enough that a
#: blocker posted by a teammate is on screen before you finish reading the line above.
DEFAULT_INTERVAL = 10.0

#: How many events the activity panel asks for. More than fits, so a short terminal
#: still shows the newest ones and a tall one is not half empty.
EVENT_LIMIT = 15

#: Reuse the canonical event vocabulary rather than keep a second copy that can drift.
GLYPHS: dict[str, str] = {str(event_type): label for event_type, label in EVENT_LABELS.items()}

#: Summaries are one-liners by contract but up to 2000 chars by schema. Clip hard so a
#: single chatty agent cannot push the panel below it off the screen.
SUMMARY_WIDTH = 70
REASON_WIDTH = 60


# --- small helpers ----------------------------------------------------------


def _clip(value: Any, limit: int) -> str:
    """Flatten to one line and truncate with an ellipsis instead of wrapping.

    A wrapped 2000-character summary destroys the layout of every panel under it; a
    clipped one still tells you which task to go and look at.
    """
    text = "" if value is None else " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def _cell(value: Any, limit: int, *, style: str = "", empty: str = "-") -> Text:
    """Build a cell as :class:`Text`, never a markup string.

    Relay content is written by other agents. Passing it to rich as a plain ``str``
    would let a summary containing ``[bold]`` reconfigure the display.
    """
    return Text(_clip(value, limit) or empty, style=style)


def _panel(title: str, body: RenderableType, *, style: str) -> Panel:
    return Panel(body, title=title, title_align="left", border_style=style, box=box.ROUNDED)


def _grid(*columns: tuple[str, int | None]) -> Table:
    """A compact table: narrow columns pinned, one flexible column that absorbs the rest.

    Every column clips with an ellipsis instead of wrapping — a wrapped cell turns one
    row into three and shoves the panel below it off the screen. Pinning the short
    columns matters on a narrow terminal: without it rich shrinks them all evenly and
    the emoji column, which is where the meaning lives, is the first thing to vanish.
    """
    table = Table(box=box.SIMPLE, show_edge=False, pad_edge=False, expand=True)
    for header, width in columns:
        table.add_column(
            header, width=width, no_wrap=True, overflow="ellipsis", ratio=None if width else 1
        )
    return table


def _hours(value: Any) -> str:
    try:
        return f"{float(value):.0f}h"
    except (TypeError, ValueError):
        return "-"


# --- fetching ---------------------------------------------------------------


@dataclass(slots=True)
class Frame:
    """One consistent snapshot. Kept on screen if the next fetch fails."""

    base_url: str
    project: str | None
    focus_project: str | None
    fetched_at: dt.datetime
    context: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    agents: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    tasks: list[dict[str, Any]] = field(default_factory=list)


def busiest_project(tasks: list[dict[str, Any]]) -> str | None:
    """Pick the project ``/context`` and ``/coordination/summary`` should describe.

    Both endpoints require a project, so with no ``--project`` we must choose one
    rather than send a request that 422s. Fanning out over every project on every
    refresh would multiply the request count for panels nobody is looking at, so
    focus the project with the most going on — that is where attention belongs.
    """
    scores: dict[str, tuple[int, int]] = {}
    for task in tasks:
        name = str(task.get("project") or "")
        if not name:
            continue
        count, events = scores.get(name, (0, 0))
        scores[name] = (count + 1, events + int(task.get("event_count") or 0))
    if not scores:
        return None
    # Name last so a tie resolves the same way on every refresh; a focus that flickers
    # between two projects is worse than one that is merely arbitrary.
    return sorted(scores, key=lambda name: (-scores[name][0], -scores[name][1], name))[0]


def fetch_frame(
    client: RelayClient, *, project: str | None = None, event_limit: int = EVENT_LIMIT
) -> Frame:
    """Fetch one frame. GET only. Raises :class:`RelayError`; callers decide what that means."""
    tasks: list[dict[str, Any]] = client.get("/tasks", project=project) or []
    # With no --project the stream and roster stay global (all of them are your agents),
    # but the project-scoped panels have to name a single project. Their titles say which.
    focus = project or busiest_project(tasks)

    context: dict[str, Any] = {}
    summary: dict[str, Any] = {}
    if focus:
        context = client.get("/context", project=focus) or {}
        summary = client.get("/coordination/summary", project=focus) or {}

    agents: list[dict[str, Any]] = client.get("/agents", project=project) or []
    events: list[dict[str, Any]] = client.get("/events", project=project, limit=event_limit) or []

    return Frame(
        base_url=client.base_url,
        project=project,
        focus_project=focus,
        fetched_at=dt.datetime.now(dt.UTC),
        context=context,
        summary=summary,
        agents=agents,
        events=events,
        tasks=tasks,
    )


# --- rendering --------------------------------------------------------------


def render_header(status: dict[str, Any]) -> Panel:
    """Where you are pointed, whether it is answering, and how fresh this is.

    ``status`` keys: project, focus_project, base_url, fetched_at, interval, error.
    """
    error = status.get("error")
    project = status.get("project") or "all projects"
    focus = status.get("focus_project")
    if not status.get("project") and focus:
        project = f"all projects (detail: {focus})"

    line = Text()
    line.append("agent-relay", style="bold")
    line.append("  ·  ")
    line.append(_clip(project, 48), style="bold cyan")
    line.append("  ·  ")
    line.append(_clip(status.get("base_url"), 48), style="dim")

    state = Text()
    if error:
        state.append("🔴 unreachable", style="bold red")
    else:
        state.append("🟢 connected", style="green")
    state.append("  ·  ")
    fetched = status.get("fetched_at")
    when = fetched.strftime("%H:%M:%S") if isinstance(fetched, dt.datetime) else "never"
    state.append(f"updated {when}", style="dim")
    interval = status.get("interval")
    if isinstance(interval, (int, float)):
        state.append(f"  ·  every {interval:g}s", style="dim")
    state.append("  ·  q to quit", style="dim")

    body: list[RenderableType] = [line, state]
    if error:
        # Say what broke *and* that the numbers below are stale, so nobody acts on a
        # frame that is ten minutes old thinking it is live.
        body.append(Text(f"⚠️  {_clip(error, 100)} — showing the last good frame", style="red"))
    return _panel("RELAY", Group(*body), style="red" if error else "cyan")


def render_stats(context: dict[str, Any], agents: list[dict[str, Any]]) -> Panel:
    """Four numbers. The two that mean "someone is stuck" shout; the others stay quiet."""
    claims = len(context.get("active_claims") or [])
    blocked = len(context.get("blocked_tasks") or [])
    questions = len(context.get("unresolved_questions") or [])
    online = sum(1 for agent in agents if str(agent.get("status")) == "online")

    def stat(glyph: str, count: int, label: str, *, alarming: bool) -> Text:
        style = "bold red" if alarming and count else "bold"
        return Text(f"{glyph} {count} {label}", style=style)

    cells = [
        stat("🔒", claims, "active claims", alarming=False),
        stat("🚧", blocked, "blocked", alarming=True),
        stat("❓", questions, "open questions", alarming=True),
        stat("🟢", online, "agents online", alarming=False),
    ]
    # Columns re-flows on a narrow terminal instead of running off the right edge.
    return _panel(
        "AT A GLANCE",
        Columns(cells, equal=True, expand=True),
        style="red" if blocked or questions else "cyan",
    )


def render_blocked(context: dict[str, Any]) -> Panel:
    """Blocked tasks first: this is the only panel that can cost someone a whole day."""
    blocked = context.get("blocked_tasks") or []
    title = "🚧 BLOCKED" + (f" · {context['project']}" if context.get("project") else "")
    if not blocked:
        return _panel(
            title,
            Text("✅ No blocked tasks — nothing needs attention", style="green"),
            style="green",
        )

    table = _grid(("TASK", 12), ("REPORTED BY", 13), ("OWNER", 12), ("REASON", None), ("FOR", 4))
    for task in blocked:
        # blocked_by is whoever raised the flag; owner is whoever holds the claim. They
        # differ often enough (you report a block on someone else's task) that collapsing
        # them into one column sends people to the wrong person.
        table.add_row(
            _cell(task.get("task"), 20, style="bold"),
            _cell(task.get("blocked_by"), 20, style="red", empty="unknown"),
            _cell(task.get("owner"), 20, empty="unclaimed"),
            _cell(task.get("blocked_reason"), REASON_WIDTH, empty="(no reason given)"),
            Text(ago(task.get("last_activity_at")), style="dim"),
        )
    return _panel(title, table, style="red")


def render_questions(context: dict[str, Any]) -> Panel:
    """Open questions: somebody is idle waiting for one of these."""
    questions = context.get("unresolved_questions") or []
    title = "❓ OPEN QUESTIONS" + (f" · {context['project']}" if context.get("project") else "")
    if not questions:
        return _panel(
            title,
            Text("✅ No open questions — nobody is waiting on an answer", style="green"),
            style="green",
        )

    table = _grid(("REF", 7), ("FROM → TO", 24), ("AGE", 4), ("QUESTION", None))
    for question in questions:
        route = f"{question.get('from_agent') or '?'} → {question.get('to_agent') or 'anyone'}"
        table.add_row(
            _cell(question.get("ref"), 12, style="bold yellow"),
            _cell(route, 34),
            Text(_hours(question.get("age_hours")), style="dim"),
            _cell(question.get("question"), SUMMARY_WIDTH),
        )
    return _panel(title, table, style="yellow")


def render_agents(agents: list[dict[str, Any]]) -> Panel:
    """Who is actually alive, and what they are holding while alive."""
    if not agents:
        return _panel(
            "👥 WHO IS WORKING",
            Text("💤 No agents have checked in yet — nobody is running `agent-relay heartbeat`"),
            style="cyan",
        )

    table = _grid(("AGENT", 16), ("STATUS", 11), ("OWNER", 10), ("TASKS", 14), ("NOTE", None))
    for agent in agents:
        status = str(agent.get("status") or "unknown")
        # Emoji *and* the word: colour alone is unreadable for a chunk of people and
        # disappears entirely when this output is piped into a log.
        glyph = PRESENCE_GLYPHS.get(status, "⚪")
        table.add_row(
            _cell(agent.get("agent"), 24, style="bold"),
            Text(f"{glyph} {status}", style="green" if status == "online" else "dim"),
            _cell(agent.get("human_owner"), 18),
            _cell(", ".join(agent.get("active_claims") or []), 28),
            _cell(agent.get("status_note"), SUMMARY_WIDTH, style="dim", empty=""),
        )
    return _panel("👥 WHO IS WORKING", table, style="cyan")


def render_activity(events: list[dict[str, Any]]) -> Panel:
    """The stream. Newest first, because that is the order you scan a pane in."""
    if not events:
        return _panel(
            "📜 RECENT ACTIVITY",
            Text("💤 No activity recorded yet — post an event to start the log"),
            style="cyan",
        )

    table = _grid(("", 2), ("AGO", 4), ("AGENT", 13), ("TASK", 10), ("SUMMARY", None))
    for event in events:
        event_type = str(event.get("event_type") or "")
        table.add_row(
            Text(GLYPHS.get(event_type, "•")),
            Text(ago(event.get("created_at")), style="dim"),
            _cell(event.get("agent"), 20),
            _cell(event.get("task"), 18, style="dim"),
            _cell(event.get("summary"), SUMMARY_WIDTH),
        )
    return _panel("📜 RECENT ACTIVITY", table, style="cyan")


def render_actions(summary: dict[str, Any]) -> Panel:
    """The relay's own rule-based suggestions — cheap, deterministic, occasionally right."""
    actions = summary.get("suggested_actions") or []
    title = "🧭 SUGGESTED ACTIONS" + (f" · {summary['project']}" if summary.get("project") else "")
    if not actions:
        return _panel(
            title,
            Text("✅ Nothing suggested — coordination looks clean", style="green"),
            style="green",
        )
    body = Group(*(Text(f"• {_clip(action, 110)}") for action in actions))
    return _panel(title, body, style="magenta")


def render_dashboard(
    frame: Frame | None, *, interval: float = DEFAULT_INTERVAL, error: str | None = None
) -> RenderableType:
    """Compose one full frame. ``frame`` is None only before the first successful fetch."""
    status: dict[str, Any] = {
        "project": frame.project if frame else None,
        "focus_project": frame.focus_project if frame else None,
        "base_url": frame.base_url if frame else "",
        "fetched_at": frame.fetched_at if frame else None,
        "interval": interval,
        "error": error,
    }
    if frame is None:
        return Group(
            render_header(status),
            Text("\nWaiting for the relay to answer…", style="dim"),
        )
    return Group(
        render_header(status),
        render_stats(frame.context, frame.agents),
        render_blocked(frame.context),
        render_questions(frame.context),
        render_agents(frame.agents),
        render_activity(frame.events),
        render_actions(frame.summary),
    )


# --- the loop ---------------------------------------------------------------


def _wait_or_quit(seconds: float) -> bool:
    """Sleep for ``seconds``; return True if the user pressed ``q``.

    Falls back to a plain sleep when there is no tty (piped, CI, a wrapped script):
    there is no keyboard to read, and blocking on one would wedge the loop.
    """
    stream = sys.stdin
    try:
        import select
        import termios
        import tty

        if not stream.isatty():
            raise OSError("not a tty")
        descriptor = stream.fileno()
        saved = termios.tcgetattr(descriptor)
    except (ImportError, OSError, ValueError):
        # ImportError: not POSIX. OSError/ValueError: stdin is a pipe or already closed.
        time.sleep(seconds)
        return False

    try:
        tty.setcbreak(descriptor)
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            ready, _, _ = select.select([stream], [], [], remaining)
            if ready and stream.read(1).lower() == "q":
                return True
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)


def run_tui(
    client: RelayClient,
    *,
    project: str | None = None,
    interval: float = DEFAULT_INTERVAL,
    once: bool = False,
    console: Console | None = None,
) -> None:
    """Render the dashboard once, or live until the user quits.

    ``--once`` deliberately lets :class:`RelayError` escape so the command exits
    non-zero and a script can tell that the relay is down. The live loop must not:
    a dashboard that dies because the relay restarted is a dashboard you stop trusting.
    """
    out = console or Console()
    if once:
        out.print(render_dashboard(fetch_frame(client, project=project), interval=interval))
        return

    frame: Frame | None = None
    error: str | None = None
    try:
        # screen=True keeps the scrollback intact and hands the terminal back on exit;
        # Live restores the cursor for us, including on Ctrl-C.
        with Live(console=out, screen=True, auto_refresh=False, vertical_overflow="crop") as live:
            while True:
                try:
                    frame = fetch_frame(client, project=project)
                    error = None
                except RelayError as exc:
                    # Keep the last good frame on screen and say it is stale. The relay
                    # coming back needs no action from the user.
                    error = str(exc)
                live.update(render_dashboard(frame, interval=interval, error=error), refresh=True)
                if _wait_or_quit(interval):
                    return
    except KeyboardInterrupt:
        # Ctrl-C is how people close a dashboard. It is not an error.
        return
