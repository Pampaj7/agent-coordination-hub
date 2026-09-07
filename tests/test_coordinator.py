"""The LLM coordinator, and its promise to degrade rather than fail.

None of these tests need an API key or the Anthropic SDK: the point of the design is
that the relay is fully useful without either.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_relay.config import Settings, get_settings
from agent_relay.db.session import init_db, reset_engine, session_scope
from agent_relay.main import app
from agent_relay.services import coordination as coordination_service
from agent_relay.services import coordinator
from tests.conftest import INTEGRATION_ENV, post_event


def summary_for(project: str = "tether") -> Any:
    with session_scope() as session:
        return coordination_service.build_summary(session, project)


def seed(client: TestClient) -> None:
    client.post("/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-142"})
    post_event(
        client,
        summary="Ran the H=8 ablation.",
        details={"findings": ["EPE improved 0.7%"]},
        artifacts=[],
    )
    post_event(
        client,
        event_type="BLOCKED",
        agent="andrea-agent",
        task="GH-151",
        summary="missing checkpoint",
        details={},
        artifacts=[],
    )
    post_event(
        client,
        event_type="QUESTION",
        target_agent="niccolo-claude",
        summary="Masking before or after resize?",
        details={},
        artifacts=[],
    )


# ------------------------------------------------------------------ deterministic


def test_quiet_project_is_reported_as_quiet(client: TestClient) -> None:
    summary = summary_for("nothing-here")
    assert coordinator.is_quiet(summary) is True
    assert "quiet" in coordinator.deterministic_brief(summary)


def test_deterministic_brief_covers_what_matters(client: TestClient) -> None:
    seed(client)
    brief = coordinator.deterministic_brief(summary_for())

    assert "leo-codex" in brief
    assert "GH-142" in brief
    assert "GH-151" in brief and "missing checkpoint" in brief
    assert "Q-4" in brief or "niccolo-claude" in brief
    assert "EPE improved 0.7%" in brief


def test_brief_endpoint_works_without_an_api_key(client: TestClient) -> None:
    seed(client)
    body = client.get("/coordination/brief", params={"project": "tether"}).json()

    assert body["source"] == "deterministic"
    assert body["model"] is None
    assert body["brief"]
    # The auditable facts always travel with the prose.
    assert body["summary"]["project"] == "tether"
    assert [b["task"] for b in body["summary"]["blocked"]] == ["GH-151"]


def test_facts_block_contains_only_snapshot_facts(client: TestClient) -> None:
    seed(client)
    facts = coordinator._summary_facts(summary_for())
    assert "GH-142" in facts and "GH-151" in facts
    assert "leo-codex" in facts
    assert "rule-derived suggested actions" in facts


# ------------------------------------------------------------------ with a fake SDK


class FakeBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, text: str, stop_reason: str = "end_turn") -> None:
        self.content = [FakeBlock(text)]
        self.stop_reason = stop_reason


def install_fake_anthropic(
    monkeypatch: pytest.MonkeyPatch, behaviour: str = "ok", captured: dict | None = None
) -> None:
    """Stand in for the optional SDK so we can test both success and every failure."""
    module = types.ModuleType("anthropic")

    class RateLimitError(Exception):
        pass

    class APIStatusError(Exception):
        def __init__(self, message: str = "boom", status_code: int = 500) -> None:
            super().__init__(message)
            self.status_code = status_code

    class APIConnectionError(Exception):
        pass

    class Messages:
        async def create(self, **kwargs: Any) -> FakeResponse:
            if captured is not None:
                captured.update(kwargs)
            if behaviour == "rate_limit":
                raise RateLimitError("slow down")
            if behaviour == "status":
                raise APIStatusError("server error", 500)
            if behaviour == "connection":
                raise APIConnectionError("no route")
            if behaviour == "refusal":
                return FakeResponse("", stop_reason="refusal")
            if behaviour == "empty":
                return FakeResponse("")
            return FakeResponse("Leo is on GH-142. GH-151 is blocked on a checkpoint.")

    class AsyncAnthropic:
        def __init__(self, api_key: str | None = None) -> None:
            self.api_key = api_key
            self.messages = Messages()

        async def close(self) -> None:
            return None

    module.AsyncAnthropic = AsyncAnthropic  # type: ignore[attr-defined]
    module.RateLimitError = RateLimitError  # type: ignore[attr-defined]
    module.APIStatusError = APIStatusError  # type: ignore[attr-defined]
    module.APIConnectionError = APIConnectionError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", module)


def keyed_settings(**overrides: Any) -> Settings:
    return Settings(AGENT_RELAY_DB_URL="sqlite://", ANTHROPIC_API_KEY="sk-ant-test", **overrides)


@pytest.mark.anyio
async def test_llm_brief_is_used_when_available(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(client)
    captured: dict[str, Any] = {}
    install_fake_anthropic(monkeypatch, "ok", captured)

    brief, source = await coordinator.write_brief(summary_for(), keyed_settings())

    assert source == "llm"
    assert "GH-142" in brief
    # The request must carry the model, a system prompt, and the snapshot facts.
    assert captured["model"] == "claude-opus-5"
    assert "never invent" in captured["system"].lower() or "Never invent" in captured["system"]
    assert "GH-151" in captured["messages"][0]["content"]
    assert captured["thinking"] == {"type": "adaptive"}
    assert captured["output_config"] == {"effort": "low"}


@pytest.mark.anyio
@pytest.mark.parametrize("behaviour", ["rate_limit", "status", "connection", "refusal", "empty"])
async def test_every_llm_failure_degrades_to_the_deterministic_brief(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, behaviour: str
) -> None:
    seed(client)
    install_fake_anthropic(monkeypatch, behaviour)

    brief, source = await coordinator.write_brief(summary_for(), keyed_settings())

    assert source == "deterministic"
    assert "GH-151" in brief  # still a real, useful briefing


@pytest.mark.anyio
async def test_missing_sdk_degrades_instead_of_crashing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(client)
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import raises

    brief, source = await coordinator.write_brief(summary_for(), keyed_settings())
    assert source == "deterministic"
    assert brief


@pytest.mark.anyio
async def test_a_quiet_project_never_calls_the_model(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: dict[str, Any] = {}
    install_fake_anthropic(monkeypatch, "ok", called)

    _brief, source = await coordinator.write_brief(summary_for("nothing-here"), keyed_settings())

    assert source == "deterministic"
    assert called == {}, "spending a token on an empty project is waste"


@pytest.fixture
def keyed_client(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.chdir(tmp_path)
    for name in INTEGRATION_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("AGENT_RELAY_DB_URL", f"sqlite:///{tmp_path}/test.db")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    get_settings.cache_clear()
    reset_engine()
    init_db(get_settings())
    with TestClient(app) as test_client:
        yield test_client
    reset_engine()
    get_settings.cache_clear()


def test_brief_endpoint_reports_the_llm_source(
    keyed_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_anthropic(monkeypatch, "ok")
    seed(keyed_client)

    body = keyed_client.get("/coordination/brief", params={"project": "tether"}).json()
    assert body["source"] == "llm"
    assert body["model"] == "claude-opus-5"
    assert body["summary"]["project"] == "tether"
