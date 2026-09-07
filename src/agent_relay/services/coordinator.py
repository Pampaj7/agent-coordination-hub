"""The optional LLM coordinator.

This sits **on top of** ``coordination.build_summary``, never in place of it. The
deterministic summary is still computed first and is still the thing you can audit;
the model's only job is to turn it into a short briefing a human will actually read
at 9am. Consequences of that ordering, all deliberate:

* No API key, no problem. Every endpoint that uses this degrades to the deterministic
  summary rather than failing.
* The model is given facts and told not to invent any. It cannot see the database,
  only the already-computed summary, so it has nothing to hallucinate *from*.
* A model outage costs a prose paragraph, never a coordination decision.

The Anthropic SDK is an optional extra (``uv sync --extra coordinator``) so the base
install stays as small as V1's.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from agent_relay.config import Settings
from agent_relay.services import coordination as coordination_service
from agent_relay.services.github import GitHubService

if TYPE_CHECKING:  # pragma: no cover - typing only
    from anthropic.types import MessageParam, OutputConfigParam

    from agent_relay.models.schemas import CoordinationSummary

logger = logging.getLogger(__name__)

#: The SDK types `effort` as a literal. COORDINATOR_EFFORT is free-form env text, so
#: it is validated here rather than being sent to the API and rejected there.
VALID_EFFORT = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "low"

SDK_MISSING = (
    "The coordinator needs the Anthropic SDK. Install it with: uv sync --extra coordinator"
)

SYSTEM_PROMPT = """You write the standup briefing for a small research team whose \
coding and research agents coordinate through a tool called Agent Relay.

You are given a deterministic, rule-derived snapshot of one project. Your job is to \
turn it into a short briefing the team reads at a glance.

Rules:
- Use only facts present in the snapshot. Never invent a task, agent, finding, number \
or blocker. If the snapshot is empty, say the project is quiet.
- Lead with what needs a human: blockers, unresolved questions, possible duplicated work.
- Name agents and task ids exactly as given.
- Be specific and terse. No filler, no cheerleading, no restating the whole snapshot.
- At most ~200 words. Short paragraphs or tight bullets.
- Do not invent next steps beyond what the snapshot's suggested actions imply."""


class CoordinatorUnavailable(RuntimeError):
    """Raised when a briefing was requested but the LLM cannot be reached."""


def _summary_facts(summary: CoordinationSummary) -> str:
    """Flatten the deterministic summary into the only facts the model may use."""
    lines: list[str] = [
        f"project: {summary.project}",
        f"window: last {summary.window_hours}h",
    ]

    if summary.active_agents:
        lines.append("active agents and their claimed tasks:")
        lines += [
            f"  - {agent}: {', '.join(tasks) or 'no tasks'}"
            for agent, tasks in summary.active_agents.items()
        ]
    else:
        lines.append("active agents: none")

    if summary.blocked:
        lines.append("blocked:")
        lines += [f"  - {b.task} ({b.agent}): {b.reason}" for b in summary.blocked]
    if summary.possible_conflicts:
        lines.append("possible duplicated work:")
        lines += [f"  - {c.task}: {c.reason}" for c in summary.possible_conflicts]
    if summary.unresolved_questions:
        lines.append("unresolved questions:")
        lines += [
            f"  - {q.ref} {q.from_agent} -> {q.to_agent or 'anyone'} "
            f"(open {q.age_hours}h): {q.question}"
            for q in summary.unresolved_questions
        ]
    if summary.recent_findings:
        lines.append("recent findings:")
        lines += [f"  - {f}" for f in summary.recent_findings]
    if summary.recent_decisions:
        lines.append("recent decisions:")
        lines += [f"  - {d}" for d in summary.recent_decisions]
    if summary.idle_claims:
        lines.append("idle claims:")
        lines += [
            f"  - {c.task} held by {c.agent}, last activity {c.last_activity_at}"
            for c in summary.idle_claims
        ]
    if summary.suggested_actions:
        lines.append("rule-derived suggested actions:")
        lines += [f"  - {a}" for a in summary.suggested_actions]

    return "\n".join(lines)


def _effort(settings: Settings) -> str:
    """Validate the configured effort, warning rather than failing on a typo."""
    effort = (settings.coordinator_effort or "").strip().lower()
    if effort in VALID_EFFORT:
        return effort
    logger.warning(
        "COORDINATOR_EFFORT=%r is not one of %s; using %r",
        settings.coordinator_effort,
        ", ".join(VALID_EFFORT),
        DEFAULT_EFFORT,
    )
    return DEFAULT_EFFORT


def is_quiet(summary: CoordinationSummary) -> bool:
    """Nothing worth spending a token on."""
    return not any(
        (
            summary.active_agents,
            summary.blocked,
            summary.possible_conflicts,
            summary.unresolved_questions,
            summary.recent_findings,
            summary.recent_decisions,
        )
    )


def deterministic_brief(summary: CoordinationSummary) -> str:
    """The fallback briefing, written without a model.

    Used when there is no API key, when the model call fails, and when the project is
    quiet. It is deliberately decent on its own — the LLM is an upgrade, not a
    dependency.
    """
    if is_quiet(summary):
        return f"{summary.project}: quiet — no activity in the last {summary.window_hours}h."

    parts: list[str] = []
    if summary.active_agents:
        working = "; ".join(
            f"{agent} on {', '.join(tasks)}" if tasks else f"{agent} (no task claimed)"
            for agent, tasks in summary.active_agents.items()
        )
        parts.append(f"Working now: {working}.")
    if summary.blocked:
        parts.append(
            "Blocked: "
            + "; ".join(f"{b.task} ({b.agent}) — {b.reason}" for b in summary.blocked)
            + "."
        )
    if summary.unresolved_questions:
        parts.append(
            "Open questions: "
            + "; ".join(
                f"{q.ref} {q.from_agent}→{q.to_agent or 'anyone'} ({q.age_hours}h)"
                for q in summary.unresolved_questions
            )
            + "."
        )
    if summary.possible_conflicts:
        parts.append(
            "Possible duplicated work: "
            + "; ".join(c.reason for c in summary.possible_conflicts)
            + "."
        )
    if summary.recent_findings:
        parts.append("Findings: " + "; ".join(summary.recent_findings) + ".")
    if summary.suggested_actions:
        parts.append("Next: " + "; ".join(summary.suggested_actions[:3]) + ".")
    return " ".join(parts)


async def write_brief(summary: CoordinationSummary, settings: Settings) -> tuple[str, str]:
    """Return ``(brief, source)`` where source is "llm" or "deterministic".

    Never raises for an LLM problem: an unreachable model degrades to the deterministic
    briefing, because a status post that fails to arrive is worse than a plainer one.
    """
    if is_quiet(summary):
        return deterministic_brief(summary), "deterministic"
    if not settings.coordinator_enabled:
        return deterministic_brief(summary), "deterministic"

    try:
        # Imported lazily: the SDK is an optional extra, so the base install stays small.
        import anthropic
    except ImportError:
        logger.warning("coordinator enabled but the anthropic SDK is not installed")
        return deterministic_brief(summary), "deterministic"

    # Passing api_key=None leaves the SDK's own resolution intact: ANTHROPIC_API_KEY,
    # then ANTHROPIC_AUTH_TOKEN, then an `ant auth login` profile on disk.
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key or None)
    messages: list[MessageParam] = [
        {
            "role": "user",
            "content": (
                f"Write the briefing for this project snapshot.\n\n{_summary_facts(summary)}"
            ),
        }
    ]
    try:
        response = await client.messages.create(
            model=settings.coordinator_model,
            max_tokens=settings.coordinator_max_tokens,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            output_config=cast("OutputConfigParam", {"effort": _effort(settings)}),
            messages=messages,
        )
    except anthropic.RateLimitError:
        logger.warning("coordinator rate limited; falling back to the deterministic brief")
        return deterministic_brief(summary), "deterministic"
    except anthropic.APIStatusError as exc:
        logger.warning("coordinator API error %s; falling back", exc.status_code)
        return deterministic_brief(summary), "deterministic"
    except anthropic.APIConnectionError:
        logger.warning("coordinator unreachable; falling back to the deterministic brief")
        return deterministic_brief(summary), "deterministic"
    except Exception as exc:  # noqa: BLE001 - the contract is that a briefing always returns
        # Anything else the SDK can throw: a response the client cannot validate, or a
        # TypeError from an older SDK that lacks a parameter we pass. A status post that
        # 500s is worse than a plainer one.
        logger.warning("coordinator call failed: %s: %s", type(exc).__name__, exc)
        return deterministic_brief(summary), "deterministic"
    finally:
        await client.close()

    if response.stop_reason == "refusal":
        logger.warning("coordinator declined the request; falling back")
        return deterministic_brief(summary), "deterministic"

    # `block.type` is the discriminator, so this narrows to TextBlock; a getattr()
    # check would read the same but leave the union unnarrowed.
    text = "\n".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        return deterministic_brief(summary), "deterministic"
    return text, "llm"


async def build_brief(
    session: Session,
    project: str,
    *,
    settings: Settings,
    window_hours: int | None = None,
    github: GitHubService | None = None,
) -> dict[str, Any]:
    """Deterministic summary first, prose second. The summary is always returned."""
    # build_summary scans the project's event log with the sync ORM. `build_brief` is
    # awaited from an async route, so it has to hop to the threadpool or it blocks the
    # event loop for every other in-flight request.
    summary = await run_in_threadpool(
        coordination_service.build_summary,
        session,
        project,
        window_hours=window_hours or settings.context_window_hours,
        github=github,
    )
    brief, source = await write_brief(summary, settings)
    return {
        "project": project,
        "generated_at": summary.generated_at,
        "window_hours": summary.window_hours,
        "brief": brief,
        "source": source,
        "model": settings.coordinator_model if source == "llm" else None,
        # The auditable facts travel with the prose, always.
        "summary": summary,
    }
