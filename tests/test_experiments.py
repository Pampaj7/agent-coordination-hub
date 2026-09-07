"""Experiment tracking is link-only, and must behave the same whether W&B and MLflow
are configured, misconfigured, or unheard of.

The contract these tests pin down: a reference an agent wrote down is *never* lost.
It gains a URL when the tracker is configured and keeps its raw form when it is not.
No test here touches the network, and none needs tracker credentials.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_relay.api.routes_v2 import (
    a2a_router,
    a2a_rpc_router,
    experiments_router,
    overview_router,
)
from agent_relay.config import Settings, get_settings
from agent_relay.db.session import init_db, reset_engine, session_scope
from agent_relay.main import create_app
from agent_relay.services.experiments import extract_runs, parse_reference, runs_for_project
from tests.conftest import INTEGRATION_ENV, event_payload, post_event

TRACKER_ENV = ("WANDB_ENTITY", "WANDB_PROJECT", "MLFLOW_TRACKING_URI", "AGENT_RELAY_PUBLIC_URL")

WANDB_URL = "https://wandb.ai/leo-lab/tether/runs/9x8y7z"
MLFLOW_URL = "http://mlflow.internal:5000/#/experiments/4/runs/abc123"


def v2_app() -> FastAPI:
    """The relay plus the V2 routers.

    ``main.create_app`` is owned elsewhere, so the routers are wired here instead of
    there — the dependency stack (auth, settings, session) is otherwise identical to
    production.
    """
    app = create_app()
    existing = {getattr(route, "path", None) for route in app.routes}
    for router in (experiments_router, overview_router, a2a_router, a2a_rpc_router):
        if not any(getattr(route, "path", None) in existing for route in router.routes):
            app.include_router(router)
    return app


@pytest.fixture
def build_settings(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., Settings]]:
    """Settings built from an explicit environment, with no developer .env in reach."""
    monkeypatch.chdir(tmp_path)
    for name in (*INTEGRATION_ENV, *TRACKER_ENV):
        monkeypatch.delenv(name, raising=False)

    def build(**env: str) -> Settings:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        return get_settings()

    yield build
    get_settings.cache_clear()


@pytest.fixture
def tracked_client(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A relay that knows the team's W&B entity/project."""
    monkeypatch.chdir(tmp_path)
    for name in (*INTEGRATION_ENV, *TRACKER_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_RELAY_DB_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("WANDB_ENTITY", "leo-lab")
    monkeypatch.setenv("WANDB_PROJECT", "tether")
    get_settings.cache_clear()
    reset_engine()
    init_db(get_settings())
    with TestClient(v2_app()) as client:
        yield client
    reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def untracked_client(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The same relay with no tracker configured at all."""
    monkeypatch.chdir(tmp_path)
    for name in (*INTEGRATION_ENV, *TRACKER_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENT_RELAY_DB_URL", f"sqlite:///{tmp_path}/test.db")
    get_settings.cache_clear()
    reset_engine()
    init_db(get_settings())
    with TestClient(v2_app()) as client:
        yield client
    reset_engine()
    get_settings.cache_clear()


# ------------------------------------------------------------------- shorthand


def test_wandb_shorthand_expands_when_configured(
    build_settings: Callable[..., Settings],
) -> None:
    settings = build_settings(WANDB_ENTITY="leo-lab", WANDB_PROJECT="tether")
    run = parse_reference("wandb:9x8y7z", settings=settings)
    assert run is not None
    assert run.tracker == "wandb"
    assert run.run_id == "9x8y7z"
    assert run.url == WANDB_URL
    assert run.raw == "wandb:9x8y7z"


def test_wandb_shorthand_without_config_keeps_the_raw_reference(
    build_settings: Callable[..., Settings],
) -> None:
    run = parse_reference("wandb:9x8y7z", settings=build_settings())
    assert run is not None
    assert run.url is None  # nothing to link to, but the reference survives
    assert (run.tracker, run.run_id, run.raw) == ("wandb", "9x8y7z", "wandb:9x8y7z")


def test_wandb_shorthand_can_carry_its_own_entity_and_project(
    build_settings: Callable[..., Settings],
) -> None:
    run = parse_reference("wandb:other-lab/drends/run7", settings=build_settings())
    assert run is not None
    assert run.url == "https://wandb.ai/other-lab/drends/runs/run7"
    assert run.run_id == "run7"


def test_mlflow_shorthand_defaults_to_experiment_zero(
    build_settings: Callable[..., Settings],
) -> None:
    settings = build_settings(MLFLOW_TRACKING_URI="http://mlflow.internal:5000/")
    run = parse_reference("mlflow:abc123", settings=settings)
    assert run is not None
    assert run.tracker == "mlflow"
    assert run.url == "http://mlflow.internal:5000/#/experiments/0/runs/abc123"


def test_mlflow_shorthand_accepts_an_explicit_experiment_id(
    build_settings: Callable[..., Settings],
) -> None:
    settings = build_settings(MLFLOW_TRACKING_URI="http://mlflow.internal:5000")
    run = parse_reference("mlflow:4/abc123", settings=settings)
    assert run is not None
    assert run.url == MLFLOW_URL
    assert run.run_id == "abc123"


def test_mlflow_shorthand_without_config_keeps_the_raw_reference(
    build_settings: Callable[..., Settings],
) -> None:
    run = parse_reference("mlflow:4/abc123", settings=build_settings())
    assert run is not None
    assert run.url is None
    assert run.raw == "mlflow:4/abc123"


# ------------------------------------------------------------------ full urls


def test_full_urls_pass_through_untouched(build_settings: Callable[..., Settings]) -> None:
    """A URL an agent already resolved is never rewritten, configured or not."""
    settings = build_settings()
    wandb = parse_reference(f"{WANDB_URL}?workspace=user-leo", settings=settings)
    mlflow = parse_reference(MLFLOW_URL, settings=settings)
    assert wandb is not None and mlflow is not None
    assert wandb.tracker == "wandb"
    assert wandb.run_id == "9x8y7z"
    assert wandb.url == f"{WANDB_URL}?workspace=user-leo"
    assert mlflow.tracker == "mlflow"
    assert mlflow.run_id == "abc123"
    assert mlflow.url == MLFLOW_URL


@pytest.mark.parametrize(
    "artifact",
    [
        "runs/ablation_horizon.csv",
        "https://github.com/leo/tether/commit/abc1234",
        "a1b2c3d4",
        "commit a1b2c3d4",
        "wandb:",
        "",
        "   ",
        "notatracker:xyz",
    ],
)
def test_unknown_artifacts_are_ignored(
    artifact: str, build_settings: Callable[..., Settings]
) -> None:
    assert parse_reference(artifact, settings=build_settings()) is None


def test_non_strings_are_ignored(build_settings: Callable[..., Settings]) -> None:
    settings = build_settings()
    assert parse_reference(None, settings=settings) is None
    assert parse_reference(42, settings=settings) is None
    assert parse_reference({"wandb": "abc"}, settings=settings) is None


# --------------------------------------------------------------- extract_runs


def test_extract_runs_reads_artifacts_and_metadata(
    build_settings: Callable[..., Settings],
) -> None:
    settings = build_settings(WANDB_ENTITY="leo-lab", WANDB_PROJECT="tether")
    event = {
        "artifacts": ["runs/ablation.csv", "wandb:run-a"],
        "metadata": {"tracker_url": MLFLOW_URL, "runs": ["wandb:run-b"], "steps": 4000},
    }
    runs = extract_runs(event, settings=settings)
    assert [(r.tracker, r.run_id) for r in runs] == [
        ("wandb", "run-a"),
        ("mlflow", "abc123"),
        ("wandb", "run-b"),
    ]


def test_extract_runs_dedupes_the_same_run_named_twice(
    build_settings: Callable[..., Settings],
) -> None:
    settings = build_settings(WANDB_ENTITY="leo-lab", WANDB_PROJECT="tether")
    event = {"artifacts": ["wandb:9x8y7z", WANDB_URL], "metadata": {}}
    assert len(extract_runs(event, settings=settings)) == 1


def test_extract_runs_survives_junk(build_settings: Callable[..., Settings]) -> None:
    """A malformed event must never break the endpoint that scans it."""
    settings = build_settings()
    assert extract_runs({"artifacts": None, "metadata": None}, settings=settings) == []
    assert extract_runs({"artifacts": [1, None, {}], "metadata": []}, settings=settings) == []
    assert extract_runs(object(), settings=settings) == []


# ----------------------------------------------------------- runs_for_project


def test_runs_for_project_is_newest_first_and_deduplicated(
    tracked_client: TestClient,
) -> None:
    post_event(tracked_client, artifacts=["wandb:run-a"])
    post_event(tracked_client, artifacts=["wandb:run-b"])
    post_event(tracked_client, artifacts=["wandb:run-a", "mlflow:run-c"])

    with session_scope() as session:
        runs = runs_for_project(session, "tether")

    assert [r.run_id for r in runs] == ["run-a", "run-c", "run-b"]
    assert runs[0].url == "https://wandb.ai/leo-lab/tether/runs/run-a"
    assert runs[1].url is None  # MLflow is not configured on this relay


def test_runs_for_project_respects_the_limit(tracked_client: TestClient) -> None:
    for index in range(5):
        post_event(tracked_client, artifacts=[f"wandb:run-{index}"])
    with session_scope() as session:
        assert len(runs_for_project(session, "tether", limit=2)) == 2


# ------------------------------------------------------------------- endpoint


def test_experiments_endpoint_filters_by_project(tracked_client: TestClient) -> None:
    post_event(tracked_client, project="tether", artifacts=["wandb:run-a"])
    post_event(tracked_client, project="drends", task="GH-201", artifacts=["wandb:run-b"])

    tether = tracked_client.get("/experiments", params={"project": "tether"}).json()
    drends = tracked_client.get("/experiments", params={"project": "drends"}).json()
    everything = tracked_client.get("/experiments").json()

    assert [r["run_id"] for r in tether] == ["run-a"]
    assert [r["run_id"] for r in drends] == ["run-b"]
    assert {r["run_id"] for r in everything} == {"run-a", "run-b"}
    assert tether[0]["url"] == "https://wandb.ai/leo-lab/tether/runs/run-a"


def test_experiments_endpoint_reports_raw_references_when_unconfigured(
    untracked_client: TestClient,
) -> None:
    post_event(untracked_client, artifacts=["wandb:run-a", "runs/ablation.csv"])
    body = untracked_client.get("/experiments").json()
    assert body == [{"tracker": "wandb", "run_id": "run-a", "url": None, "raw": "wandb:run-a"}]


def test_experiments_endpoint_is_empty_on_a_fresh_relay(untracked_client: TestClient) -> None:
    assert untracked_client.get("/experiments").json() == []
    untracked_client.post("/events", json=event_payload())  # only a csv artifact
    assert untracked_client.get("/experiments").json() == []
