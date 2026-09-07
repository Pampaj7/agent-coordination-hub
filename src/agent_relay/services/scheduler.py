"""Background jobs.

Three periodic jobs, all optional and all disabled by default:

``sweep``   release or report claims whose owner has gone quiet
``poll``    pull GitHub activity in when no webhook can reach us
``status``  post a team briefing to Slack on a cadence

This is an ``asyncio`` task, not Celery, not a cron container. The relay is one
process for three people; a scheduler that needs its own infrastructure would cost
more than the problem it solves. Each job is isolated: one raising never stops the
loop or the others, and every job is written to be safe to run twice.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from collections.abc import Awaitable, Callable

from agent_relay.config import Settings
from agent_relay.db.models import utcnow
from agent_relay.db.session import session_scope

logger = logging.getLogger(__name__)

#: How often the loop wakes to decide whether anything is due.
TICK_SECONDS = 30


class Scheduler:
    """A single asyncio task running the due jobs on each tick."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._last_run: dict[str, dt.datetime] = {}
        #: Set by tests to observe what ran.
        self.runs: dict[str, int] = {"sweep": 0, "poll": 0, "status": 0}

    # ------------------------------------------------------------------ lifecycle

    @property
    def enabled(self) -> bool:
        return bool(self._job_plan())

    def _job_plan(self) -> dict[str, float]:
        """Job name -> interval in seconds, for the jobs that are switched on."""
        plan: dict[str, float] = {}
        settings = self.settings
        # The sweeper runs whenever claims can go stale, which is always — but it is
        # only interesting (and only writes) when auto-release is on or something is
        # actually stale, so the cost of leaving it on is a query every few minutes.
        if settings.sweeper_interval_seconds > 0:
            plan["sweep"] = settings.sweeper_interval_seconds
        if settings.github_polling_enabled:
            plan["poll"] = settings.github_poll_interval_seconds
        if settings.status_interval_minutes > 0 and settings.status_project_list:
            plan["status"] = settings.status_interval_minutes * 60
        return plan

    def start(self) -> None:
        if self._task is not None or not self.enabled:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="agent-relay-scheduler")
        logger.info("scheduler started: %s", {k: f"{v:g}s" for k, v in self._job_plan().items()})

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            await self.tick()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=TICK_SECONDS)

    # ------------------------------------------------------------------ jobs

    def _due(self, job: str, interval: float, now: dt.datetime) -> bool:
        last = self._last_run.get(job)
        return last is None or (now - last).total_seconds() >= interval

    async def tick(self, now: dt.datetime | None = None) -> list[str]:
        """Run every job that is due. Returns the names of the jobs that ran."""
        now = now or utcnow()
        jobs: dict[str, Callable[[], Awaitable[None]]] = {
            "sweep": self.run_sweep,
            "poll": self.run_poll,
            "status": self.run_status,
        }
        ran: list[str] = []
        for job, interval in self._job_plan().items():
            if not self._due(job, interval, now):
                continue
            self._last_run[job] = now
            try:
                await jobs[job]()
                self.runs[job] += 1
                ran.append(job)
            except Exception as exc:  # noqa: BLE001 - one bad job must not stop the loop
                logger.warning("scheduled job %s failed: %s: %s", job, type(exc).__name__, exc)
        return ran

    async def run_sweep(self) -> None:
        """Release or report claims whose owner has gone quiet."""
        from agent_relay.services import presence  # deferred: avoids an import cycle

        with session_scope() as session:
            report = presence.sweep(session, self.settings)
        released = getattr(report, "released", [])
        if released:
            logger.info("sweeper auto-released %d stale claim(s)", len(released))

    async def run_poll(self) -> None:
        """Pull GitHub activity in for teams the webhook cannot reach."""
        from agent_relay.services import github_ingest  # deferred: avoids an import cycle
        from agent_relay.services.github import GitHubService  # deferred import

        with session_scope() as session:
            created = await github_ingest.poll_once(
                session, GitHubService(self.settings), self.settings
            )
        if created:
            logger.info("github poll ingested %d event(s)", len(created))

    async def run_status(self) -> None:
        """Post a per-project briefing to Slack."""
        from agent_relay.services import coordination  # deferred import
        from agent_relay.services.coordinator import write_brief  # deferred import

        for project in self.settings.status_project_list:
            # Compute the summary and CLOSE the session before awaiting the model.
            # Holding the read transaction across a multi-second LLM call blocks WAL
            # checkpointing for every other writer in the process.
            with session_scope() as session:
                summary = coordination.build_summary(
                    session, project, window_hours=self.settings.context_window_hours
                )
            brief, _source = await write_brief(summary, self.settings)
            await self.post_status(project, brief)

    async def post_status(self, project: str, brief: str) -> None:
        """Send the briefing to Slack, preferring the bot token over the webhook.

        Posted directly rather than through the event log: a status roll-up is a view
        of events, not an event, and writing it back would make the next roll-up
        summarise itself.
        """
        import httpx  # deferred import

        payload = {
            "text": f"Team status · {project.upper()}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"📊 *Team status · {project.upper()}*\n\n{brief}",
                    },
                }
            ],
        }
        settings = self.settings
        timeout = settings.slack_timeout_seconds
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                channel = settings.channel_for(project)
                if settings.slack_bot_enabled and channel:
                    await client.post(
                        "https://slack.com/api/chat.postMessage",
                        headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
                        json={"channel": channel, **payload},
                    )
                elif settings.slack_webhook_url:
                    await client.post(settings.slack_webhook_url, json=payload)
                else:
                    logger.info("status brief for %s (Slack not configured):\n%s", project, brief)
        except Exception as exc:  # noqa: BLE001 - a missed status post must not stop the loop
            logger.warning("status post failed: %s: %s", type(exc).__name__, exc)
