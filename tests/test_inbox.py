"""The inbox: what is waiting for one specific agent.

`/context` is what is going on; this is what needs *you*. The distinction is the whole
point — an actionable item buried in forty lines of ambient state gets missed.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import post_event


def inbox(client: TestClient, agent: str, **params: Any) -> dict[str, Any]:
    response = client.get("/inbox", params={"agent": agent, **params})
    assert response.status_code == 200, response.text
    return response.json()


def test_an_empty_inbox_is_a_real_answer(client: TestClient) -> None:
    body = inbox(client, "niccolo-claude")
    assert body["questions_for_me"] == []
    assert body["handoffs_to_me"] == []
    assert body["my_tasks_needing_attention"] == []
    assert body["my_claims"] == []


def test_a_question_addressed_to_me_appears_with_the_command_that_closes_it(
    client: TestClient,
) -> None:
    post_event(
        client,
        event_type="QUESTION",
        agent="leo-codex",
        target_agent="niccolo-claude",
        summary="Masking before or after resize?",
        details={},
        artifacts=[],
    )
    mine = inbox(client, "niccolo-claude")["questions_for_me"]
    assert len(mine) == 1
    assert mine[0]["ref"] == "Q-1"
    assert mine[0]["from_agent"] == "leo-codex"
    # The item carries its own next action: an agent should not have to construct it.
    assert "--in-reply-to Q-1" in mine[0]["answer_with"]
    assert "post answer" in mine[0]["answer_with"]

    # ...and it is not in anybody else's inbox.
    assert inbox(client, "andrea-agent")["questions_for_me"] == []
    assert inbox(client, "leo-codex")["questions_for_me"] == []


def test_answering_empties_the_inbox(client: TestClient) -> None:
    post_event(
        client,
        event_type="QUESTION",
        agent="leo-codex",
        target_agent="niccolo-claude",
        summary="before or after resize?",
        details={},
        artifacts=[],
    )
    assert inbox(client, "niccolo-claude")["questions_for_me"]

    post_event(
        client,
        event_type="ANSWER",
        agent="niccolo-claude",
        in_reply_to="Q-1",
        summary="before resize",
        details={},
        artifacts=[],
    )
    assert inbox(client, "niccolo-claude")["questions_for_me"] == []


def test_a_handoff_carries_its_warnings_into_the_inbox(client: TestClient) -> None:
    """The warnings are the point of a handoff — the receiver must not have to hunt."""
    client.post("/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-142"})
    client.post(
        "/handoff",
        json={
            "agent": "leo-codex",
            "target_agent": "andrea-agent",
            "project": "tether",
            "task": "GH-142",
            "summary": "Preprocessing complete.",
            "continue_from": "82bd18f",
            "inputs": ["data/scared_processed/"],
            "warnings": ["Do not touch scripts/preprocess_scared.py until GH-150 finishes"],
        },
    )
    handoffs = inbox(client, "andrea-agent")["handoffs_to_me"]
    assert len(handoffs) == 1
    assert handoffs[0]["from_agent"] == "leo-codex"
    assert handoffs[0]["continue_from"] == "82bd18f"
    assert handoffs[0]["inputs"] == ["data/scared_processed/"]
    assert "preprocess_scared.py" in handoffs[0]["warnings"][0]
    # The claim moved too, so the receiver sees it as theirs.
    assert inbox(client, "andrea-agent")["my_claims"] == ["tether/GH-142"]
    # And the sender no longer holds it.
    assert inbox(client, "leo-codex")["my_claims"] == []


def test_my_own_blocked_task_shows_up_as_needing_attention(client: TestClient) -> None:
    client.post("/claim", json={"agent": "leo-codex", "project": "tether", "task": "GH-142"})
    post_event(
        client,
        event_type="BLOCKED",
        agent="leo-codex",
        summary="missing checkpoint",
        details={},
        artifacts=[],
    )
    stalled = inbox(client, "leo-codex")["my_tasks_needing_attention"]
    assert len(stalled) == 1
    assert stalled[0]["task"] == "GH-142"
    assert "missing checkpoint" in stalled[0]["reason"]

    # Somebody else's blocked task is not my problem.
    assert inbox(client, "andrea-agent")["my_tasks_needing_attention"] == []


def test_the_inbox_can_be_scoped_to_one_project(client: TestClient) -> None:
    for project in ("tether", "drends"):
        post_event(
            client,
            event_type="QUESTION",
            project=project,
            agent="leo-codex",
            target_agent="niccolo-claude",
            summary=f"question about {project}",
            details={},
            artifacts=[],
        )
    assert len(inbox(client, "niccolo-claude")["questions_for_me"]) == 2
    scoped = inbox(client, "niccolo-claude", project="tether")["questions_for_me"]
    assert [q["project"] for q in scoped] == ["tether"]


def test_agent_is_required(client: TestClient) -> None:
    assert client.get("/inbox").status_code == 422


def test_the_rendered_text_tells_an_agent_what_to_do(client: TestClient) -> None:
    """An agent reads text, so the text has to carry the next action."""
    from agent_relay.services.inbox import Inbox, as_text

    post_event(
        client,
        event_type="QUESTION",
        agent="leo-codex",
        target_agent="niccolo-claude",
        summary="before or after resize?",
        details={},
        artifacts=[],
    )
    text = as_text(Inbox.model_validate(inbox(client, "niccolo-claude")))
    assert "QUESTIONS FOR YOU" in text
    assert "agent-relay post answer" in text

    empty = as_text(Inbox.model_validate(inbox(client, "andrea-agent")))
    assert "Nothing waiting" in empty, "an empty inbox must say so, not render blank"


def test_a_question_to_a_sibling_agent_still_reaches_the_person(client: TestClient) -> None:
    """Regression: a question aimed at one of a person's agents was invisible to the others.

    Found in real use. A handoff was addressed to `niccolo-sonnet-5` — a name from the
    setup instructions — while Niccolo actually ran `nachomar-gpt`. His agent correctly
    refused to touch a task claimed by an identity that was not his, posted BLOCKED, and
    waited for a human to intervene. Questions and handoffs are aimed at a *person*; only
    claims are aimed at a process.
    """
    # Two agents, same human. Register them by having each post something.
    post_event(client, agent="nachomar-gpt", human_owner="niccolo", summary="hello")
    post_event(client, agent="niccolo-sonnet-5", human_owner="niccolo", summary="hello too")

    post_event(
        client,
        event_type="QUESTION",
        agent="pampaj-opus-5",
        human_owner="pampaj",
        target_agent="niccolo-sonnet-5",
        summary="do we have IEEE Xplore access?",
        details={},
        artifacts=[],
    )

    # The agent that is actually running sees it...
    mine = inbox(client, "nachomar-gpt")["questions_for_me"]
    assert len(mine) == 1
    assert mine[0]["addressed_to"] == "niccolo-sonnet-5", "and is told who it was sent to"

    # ...and so does the named one, if it ever comes online.
    assert len(inbox(client, "niccolo-sonnet-5")["questions_for_me"]) == 1

    # Someone else's agent still does not.
    assert inbox(client, "pampaj-opus-5")["questions_for_me"] == []


def test_a_handoff_to_a_sibling_agent_reaches_the_person(client: TestClient) -> None:
    post_event(client, agent="nachomar-gpt", human_owner="niccolo", summary="hello")
    post_event(client, agent="niccolo-sonnet-5", human_owner="niccolo", summary="hello too")
    client.post(
        "/handoff",
        json={
            "agent": "pampaj-opus-5",
            "target_agent": "niccolo-sonnet-5",
            "project": "tether",
            "task": "GH-3",
            "summary": "search IEEE Xplore",
            "warnings": ["do not claim the gap without checking"],
        },
    )
    handoffs = inbox(client, "nachomar-gpt")["handoffs_to_me"]
    assert len(handoffs) == 1
    assert handoffs[0]["addressed_to"] == "niccolo-sonnet-5"
    assert "do not claim the gap" in handoffs[0]["warnings"][0]


def test_an_agent_with_no_known_owner_only_sees_its_own(client: TestClient) -> None:
    """Without an owner there are no siblings to infer — fall back to the exact name."""
    post_event(
        client,
        event_type="QUESTION",
        agent="pampaj-opus-5",
        target_agent="some-unregistered-agent",
        summary="anyone?",
        details={},
        artifacts=[],
    )
    assert len(inbox(client, "some-unregistered-agent")["questions_for_me"]) == 1
    assert inbox(client, "another-stranger")["questions_for_me"] == []


def test_the_text_says_when_a_message_was_addressed_elsewhere(client: TestClient) -> None:
    from agent_relay.services.inbox import Inbox, as_text

    post_event(client, agent="nachomar-gpt", human_owner="niccolo", summary="hello")
    post_event(client, agent="niccolo-sonnet-5", human_owner="niccolo", summary="hello too")
    post_event(
        client,
        event_type="QUESTION",
        agent="pampaj-opus-5",
        target_agent="niccolo-sonnet-5",
        summary="IEEE access?",
        details={},
        artifacts=[],
    )
    text = as_text(Inbox.model_validate(inbox(client, "nachomar-gpt")))
    assert "sent to niccolo-sonnet-5" in text, "the agent must know it was not the named one"
