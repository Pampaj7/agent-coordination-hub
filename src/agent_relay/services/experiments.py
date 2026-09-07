"""Experiment tracker references: W&B and MLflow, **link-only**.

The relay references an experiment tracker exactly the way it references GitHub: it
turns an identifier an agent already wrote down into a URL a human can click. It

* never calls the W&B or MLflow HTTP APIs,
* never imports their SDKs (they are not dependencies),
* and therefore cannot slow down, fail, or leak credentials when a tracker is down.

That is a deliberate scope choice, not a missing feature. The tracker owns run
metrics; the relay owns "who is running what, and where can I look at it". Anything
richer (pulling metrics, comparing runs) belongs in the tracker's own UI.

Recognised references, found inside an event's ``artifacts`` and ``metadata``:

* ``https://wandb.ai/<entity>/<project>/runs/<id>`` — passed through untouched.
* ``wandb:<run_id>`` — expanded via ``WANDB_ENTITY`` / ``WANDB_PROJECT``.
* ``wandb:<entity>/<project>/<run_id>`` — self-contained, needs no configuration.
* ``<tracking-uri>/#/experiments/<exp>/runs/<id>`` — passed through untouched.
* ``mlflow:<run_id>`` — expanded via ``MLFLOW_TRACKING_URI``, experiment ``0``.
* ``mlflow:<experiment_id>/<run_id>`` — same, with an explicit experiment id.

When the tracker is not configured the reference is still reported, with
``url = None``. Reporting "leo-codex mentioned run ``abc123``" without a link is far
more useful than dropping the reference on the floor, and it means adding the
environment variables later is a pure improvement with no backfill.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from agent_relay.config import Settings, get_settings
from agent_relay.services import state as state_service

Tracker = Literal["wandb", "mlflow"]

#: MLflow's default experiment. Used when a shorthand omits the experiment id.
DEFAULT_MLFLOW_EXPERIMENT = "0"
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

_WANDB_URL = re.compile(
    r"^https?://(?:[\w.-]+\.)?wandb\.ai/(?P<entity>[^/\s]+)/(?P<project>[^/\s]+)"
    r"/runs/(?P<run>[^/?#\s]+)",
    re.IGNORECASE,
)
#: MLflow's UI is a hash-router, so the run id always sits behind ``#/experiments``.
#: Matching on that shape rather than on a host means a self-hosted MLflow on any
#: domain is recognised without configuration.
_MLFLOW_URL = re.compile(
    r"^https?://\S+?/#/experiments/(?P<experiment>[^/\s]+)/runs/(?P<run>[^/?#\s]+)",
    re.IGNORECASE,
)
_SHORTHAND = re.compile(r"^(?P<tracker>wandb|mlflow):(?P<rest>\S+)$", re.IGNORECASE)


class ExperimentRun(BaseModel):
    """One experiment-tracker reference found in the event log.

    Defined here rather than in ``models/schemas.py`` because it is only ever
    produced by this module: nothing else in the relay knows what a run is.
    """

    tracker: Tracker
    run_id: str
    url: str | None = Field(
        default=None, description="None when the tracker is not configured on this relay."
    )
    raw: str = Field(description="The reference exactly as the agent wrote it.")


def _wandb_run(rest: str, settings: Settings) -> ExperimentRun | None:
    """``<run>`` | ``<project>/<run>`` | ``<entity>/<project>/<run>``."""
    parts = [p for p in rest.split("/") if p]
    if len(parts) == 1:
        entity, project, run = settings.wandb_entity, settings.wandb_project, parts[0]
    elif len(parts) == 2:
        entity, project, run = settings.wandb_entity, parts[0], parts[1]
    elif len(parts) == 3:
        entity, project, run = parts[0], parts[1], parts[2]
    else:
        return None

    url = f"https://wandb.ai/{entity}/{project}/runs/{run}" if entity and project else None
    return ExperimentRun(tracker="wandb", run_id=run, url=url, raw=f"wandb:{rest}")


def _mlflow_run(rest: str, settings: Settings) -> ExperimentRun | None:
    """``<run>`` | ``<experiment_id>/<run>``."""
    parts = [p for p in rest.split("/") if p]
    if len(parts) == 1:
        experiment, run = DEFAULT_MLFLOW_EXPERIMENT, parts[0]
    elif len(parts) == 2:
        experiment, run = parts[0], parts[1]
    else:
        return None

    base = (settings.mlflow_tracking_uri or "").rstrip("/")
    url = f"{base}/#/experiments/{experiment}/runs/{run}" if base else None
    return ExperimentRun(tracker="mlflow", run_id=run, url=url, raw=f"mlflow:{rest}")


def parse_reference(raw: object, *, settings: Settings | None = None) -> ExperimentRun | None:
    """Recognise one string. Returns ``None`` for anything that is not a run reference.

    Never raises: an unparseable artifact is simply not an experiment run.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None

    if (match := _WANDB_URL.match(text)) is not None:
        return ExperimentRun(tracker="wandb", run_id=match["run"], url=text, raw=text)
    if (match := _MLFLOW_URL.match(text)) is not None:
        return ExperimentRun(tracker="mlflow", run_id=match["run"], url=text, raw=text)

    match = _SHORTHAND.match(text)
    if match is None:
        return None

    resolved = settings or get_settings()
    rest = match["rest"]
    if match["tracker"].lower() == "wandb":
        return _wandb_run(rest, resolved)
    return _mlflow_run(rest, resolved)


def _candidate_strings(value: Any) -> list[str]:
    """Flatten one metadata/artifact value into the strings worth checking."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [item for item in value if isinstance(item, str)]
    return []


def _artifacts_of(event_like: Any) -> list[Any]:
    """Accept a DB ``Event``, an ``EventOut``/``EventCreate``, or a plain dict."""
    if isinstance(event_like, dict):
        return list(event_like.get("artifacts") or [])
    raw = getattr(event_like, "artifacts", None)
    if raw is None:
        raw = getattr(event_like, "artifacts_json", None)
    return list(raw or [])


def _metadata_of(event_like: Any) -> dict[str, Any]:
    if isinstance(event_like, dict):
        meta = event_like.get("metadata")
    else:
        meta = getattr(event_like, "metadata", None)
        if meta is None:
            meta = getattr(event_like, "metadata_json", None)
    return dict(meta) if isinstance(meta, dict) else {}


def extract_runs(event_like: Any, *, settings: Settings | None = None) -> list[ExperimentRun]:
    """Every experiment reference in one event, in the order it was written.

    De-duplicated on ``(tracker, run_id)`` — the same run named twice in one event
    (once as a shorthand, once as a URL) is still one run.
    """
    resolved = settings or get_settings()
    found: list[ExperimentRun] = []
    seen: set[tuple[str, str]] = set()

    candidates: list[str] = []
    for artifact in _artifacts_of(event_like):
        candidates.extend(_candidate_strings(artifact))
    for value in _metadata_of(event_like).values():
        candidates.extend(_candidate_strings(value))

    for candidate in candidates:
        run = parse_reference(candidate, settings=resolved)
        if run is None:
            continue
        key = (run.tracker, run.run_id)
        if key in seen:
            continue
        seen.add(key)
        found.append(run)
    return found


def runs_for_project(
    session: Session,
    project: str | None = None,
    *,
    limit: int = DEFAULT_LIMIT,
    settings: Settings | None = None,
) -> list[ExperimentRun]:
    """Runs referenced by a project's events, de-duplicated, newest first.

    ``project=None`` scans every project. The scan is a Python fold over the event
    log for the same reason the coordination rules are: at this scale it is instant,
    and it is trivial to verify by hand.
    """
    resolved = settings or get_settings()
    events = state_service.fetch_events(session, project=project)

    runs: list[ExperimentRun] = []
    seen: set[tuple[str, str]] = set()
    for event in reversed(events):  # fetch_events is id-ascending; newest first here
        for run in extract_runs(event, settings=resolved):
            key = (run.tracker, run.run_id)
            if key in seen:
                continue
            seen.add(key)
            runs.append(run)
        if len(runs) >= limit:
            break
    return runs[:limit]
