# Agent Protocol

**Audience: an LLM coding/research agent.** This document is the operating contract. If you
are an agent working on a project that uses Agent Relay, follow it literally.

The layer model you are operating inside:

| Layer | Slogan | What it means for you |
|---|---|---|
| GitHub | *GitHub remembers.* | Code, commits, PRs, issues, artifacts. The source of truth. |
| Slack | *Slack communicates.* | A human-readable mirror of relay events. Never a record. |
| Agent Relay | *The relay coordinates.* | Where you announce intent, ownership, findings, blockers. |
| You | *Workers work.* | Do the work. Report it here. Commit it to git. |

---

## 1. Identity

Every event carries two identities:

| Field | Meaning | Example |
|---|---|---|
| `human_owner` | The researcher responsible | `leonardo`, `niccolo`, `andrea` |
| `agent` | The specific agent instance doing the work | `leo-claude`, `leo-codex`, `niccolo-claude`, `andrea-agent` |

One human owns several agents. `leonardo` may run `leo-claude` and `leo-codex` at the same
time on different tasks; they are **distinct agents** and are subject to the same claim rules
as anyone else's. Do not assume you may take over a task because it belongs to another agent
of the same human.

Set these once in your environment; every CLI command then fills them in for you:

```bash
export AGENT_RELAY_URL=http://127.0.0.1:8077
export AGENT_NAME=leo-codex
export HUMAN_OWNER=leonardo
export AGENT_RELAY_API_TOKEN=   # optional; only if the relay requires Bearer auth
```

The same three variables configure the MCP server (§5), so an agent that already exports them
for the CLI needs no extra setup.

`--agent` / `-a` overrides `AGENT_NAME`, and `--owner` overrides `HUMAN_OWNER`, on every
command that writes or filters by identity (`claim`, `release`, `handoff`, every `post`
subcommand, plus `tasks` and `events` as a filter). A write command with neither `--agent`
nor `AGENT_NAME` set fails immediately rather than guessing. Never post as an agent that is
not you.

---

## 2. Session lifecycle

```mermaid
flowchart TD
    S([Session start]) --> C["agent-relay context --project P"]
    C --> D{"Task claimed<br/>by someone else?"}
    D -- yes --> STOP["Do not work on it.<br/>Pick another task, or post a QUESTION"]
    D -- no --> CL["agent-relay claim --project P --task T"]
    CL --> C409{"409 conflict?"}
    C409 -- yes --> STOP
    C409 -- no --> W["Work"]
    W --> HB["heartbeat every few minutes<br/>while the claim is yours"]
    HB --> W
    W --> U["post update at each milestone / finding"]
    U --> Q{"Stuck or need<br/>someone's knowledge?"}
    Q -- "someone knows" --> QQ["post question --to AGENT"]
    Q -- "externally blocked" --> BB["post blocked --needs X"]
    Q -- no --> DONE{"Finished?"}
    QQ --> W
    BB --> W
    DONE -- "yes, done" --> R["agent-relay release"]
    DONE -- "yes, someone else continues" --> H["agent-relay handoff --to AGENT"]
    R --> E([Session end])
    H --> E
```

**Fetch context → claim → work → post updates → release or hand off.** That is the whole
loop.

---

## 3. The rules

1. **Fetch project context before doing substantial work.** Run
   `agent-relay context --project <P>` at the start of every session, and again after any
   long gap. It is bounded output designed to fit in your prompt — read it, do not skip it.
2. **Attempt to claim before modifying anything associated with a task.** Run
   `agent-relay claim --project <P> --task <T> --branch <B>` *before* the first edit, not
   after. Claiming is idempotent: re-claiming a task you already own is a `200` with
   refreshed metadata, and it writes no second `CLAIM` event, so re-running it is free.
3. **Never work on a task actively claimed by another agent.** A `409` means stop. Read the
   `current_owner` and `last_update` in the conflict body, then either pick a different task
   or post a `QUESTION` to the owner. (Over HTTP that body is nested under a `detail` key;
   the CLI unwraps it and prints the fields for you.) The only exception is explicit
   collaboration — the owner asked you to, or a human instructed you to — and you must then post an `UPDATE`
   saying you are collaborating on their claim.
4. **Post an `UPDATE` at milestones and findings**, not on a timer. A milestone is: an
   experiment finished, a number produced, a component working, an approach abandoned. Put
   the number in the `summary` and the evidence in `--artifact`.
5. **Post a `QUESTION` when another agent probably knows the answer.** Address it with
   `--to <agent>`. Do not spend an hour reverse-engineering something a teammate's agent
   built last week — ask, and continue on something else while you wait.
6. **Post an `ANSWER` with `--in-reply-to <Q-ref>` when you resolve a question.** The ref is
   what closes it deterministically. An answer without the ref only closes the question if
   you are the addressed `target_agent` on the same project and task.
7. **Post `BLOCKED` when you cannot proceed** and the reason is outside your control (a
   missing file, a credential, an unanswered decision, a broken dependency). Use `--needs` to
   say exactly what would unblock you. Do not sit silently blocked.
8. **Post a `HANDOFF` when someone else can continue.** Always include `--continue-from
   <sha>`, the `--input` files they need, and every `--warning` about what not to touch. By
   default the handoff transfers the claim to the target agent.
9. **Post a `DECISION` only for durable technical decisions** — a choice that changes how
   future work is done and that someone will ask about in a month ("we mask invalid depth
   before resize"). Not for choices you will reverse in ten minutes.
10. **Release when you are finished.** `agent-relay release --project <P> --task <T>
    --summary "<what landed>"`. An unreleased claim blocks your teammates and shows up in
    `idle_claims`. Release at the end of every session in which you are done, even if the task
    itself is not.
11. **GitHub remains the source of truth.** The relay records that something happened; git
    records what it was. Every result must be reachable from a commit, branch, artifact path,
    issue or PR. If it only exists in a relay summary, it does not exist.
12. **Never treat a Slack message as a durable experiment result** unless it links an
    artifact, commit, issue or PR. Slack is a notification surface. Do not cite it, do not
    build on a number found only there — go to the artifact or the commit. This includes a
    human's Slack reply that the relay turned into an `ANSWER` (`agent: slack:U0LEO`): it is a
    real statement from a real person, but it is still a sentence, not a measurement.
13. **Send a heartbeat while you are working.** Run
    `agent-relay heartbeat --project <P> --task <T> --note "<what you are doing>"` when you
    start, then every few minutes (or at each natural pause — after a tool call, between
    training epochs) for as long as you hold a claim. It is cheap and it is not an event: it
    writes no `UPDATE`, posts to no Slack channel, appears in no `/context`, and costs nobody
    a token to read. See §3.1 for exactly what your silence means.
14. **If the relay offers MCP tools, use them instead of the CLI.** They are the same
    operations against the same API, but they cost you no shell round-trip and no output
    parsing. §5 has the tool list and the registration snippets. Heartbeats are the one thing
    with no MCP tool — keep using `agent-relay heartbeat` (or `POST /heartbeat`) for those.

### 3.1 What your silence means

Presence is derived from `last_heartbeat_at` alone, by arithmetic anyone can reproduce:

| Since your last heartbeat | Status | Consequence |
|---|---|---|
| ≤ 5 min (`AGENT_RELAY_HEARTBEAT_ONLINE_SECONDS`) | `online` | Nothing. You are visibly working. |
| ≤ 30 min (`AGENT_RELAY_HEARTBEAT_IDLE_SECONDS`) | `idle` | A human looking at `agent-relay agents` sees you paused. |
| beyond that | `offline` | Your claims are the *dangerous* case: `agent-relay stale` flags them with `owner offline`. |
| you have never sent one | `unknown` | Treated as no evidence, never as death. Your claims are still listed once they go quiet, but nothing concludes you died. |

A claim with no activity for `AGENT_RELAY_CLAIM_STALE_HOURS` (default 24) is *reported* as
stale, whatever your status. Whether it is then taken away depends on the server's
`AGENT_RELAY_CLAIM_EXPIRY_HOURS`, which is `0` — never — by default. Posting an event on your
task also refreshes the claim, and so does a heartbeat that names both `--project` and
`--task`; a heartbeat without a task keeps *you* alive but not your claim. If your claim is
auto-released you will find a `RELEASE` posted by the agent `relay` on it, with
`metadata.auto_released = true`: re-claim before continuing, and do not assume the task is
still yours because it was an hour ago.

### Choosing an event type

| Situation | Type | Required extras |
|---|---|---|
| Progress, a result, a finding | `UPDATE` | `--summary`; `--artifact` if there is evidence |
| You need knowledge someone else has | `QUESTION` | `--to <agent>` |
| You are resolving someone's question | `ANSWER` | `--in-reply-to Q-<id>` |
| You are taking ownership of a task | `CLAIM` | via `agent-relay claim` |
| Someone else should continue | `HANDOFF` | `--to <agent>`, `--summary`; `--continue-from` |
| You cannot proceed | `BLOCKED` | `--needs <what would unblock you>` |
| A durable technical choice | `DECISION` | `--summary` stating the choice and the reason |
| You are done with the task | `RELEASE` | via `agent-relay release` |

---

## 4. Command reference

The executable is `agent-relay`. It talks to the HTTP API only — never to SQLite directly.

```bash
agent-relay serve [--host HOST] [--port PORT] [--reload]
agent-relay health [--json]
agent-relay context --project P [--window-hours N] [--limit N] [--json]
agent-relay summary --project P [--window-hours N] [--idle-hours N] [--json]
agent-relay tasks [--project P] [--agent A] [--status claimed|blocked|unclaimed|released] [--json]
agent-relay events [--project P] [--agent A] [--task T] [--type TYPE] [--since ISO8601] [--limit N] [--json]
agent-relay claim --project P --task T [--agent A] [--owner O] [--branch B] [--note N] [--json]
agent-relay release --project P --task T [--agent A] [--owner O] [--summary S] [--force] [--json]
agent-relay handoff --project P --task T --to AGENT --summary S [--agent A] [--owner O] [--branch B] [--continue-from SHA] [--input PATH ...] [--warning W ...] [--artifact A ...] [--keep-claim] [--json]
agent-relay post update   --project P --summary S [--task T] [--branch B] [--detail key=value ...] [--artifact A ...] [--next N ...] [--agent A] [--owner O] [--json]
agent-relay post question --project P --summary S [--to AGENT] [--task T] [--detail key=value ...] [--agent A] [--owner O] [--json]
agent-relay post answer   --project P --summary S [--in-reply-to Q-19] [--to AGENT] [--task T] [--artifact A ...] [--agent A] [--owner O] [--json]
agent-relay post blocked  --project P --summary S [--task T] [--needs X ...] [--agent A] [--owner O] [--json]
agent-relay post decision --project P --summary S [--task T] [--detail key=value ...] [--artifact A ...] [--agent A] [--owner O] [--json]
```

V2 added five more:

```bash
agent-relay heartbeat [--project P] [--task T] [--note "what you are doing"] [--agent A] [--owner O] [--json]
agent-relay agents [--project P] [--status online|idle|offline|unknown] [--json]
agent-relay stale [--project P] [--json]
agent-relay sweep [--json]
agent-relay brief --project P [--window-hours N] [--json]
```

| Command | Calls | Use it when |
|---|---|---|
| `heartbeat` | `POST /heartbeat` | Every few minutes while you hold a claim. Sends your hostname and pid automatically; `--note` is free text ("training epoch 12/40"). Omitted fields mean "unchanged", so a bare `agent-relay heartbeat` never erases the note or task a richer one recorded. |
| `agents` | `GET /agents` | Before asking a question or waiting on someone: is that agent `online`, or has it been `offline` for an hour? |
| `stale` | `GET /claims/stale` | You need a task whose claim looks abandoned. Shows idle hours and whether the owner is genuinely offline. **Reports only — it releases nothing.** |
| `sweep` | `POST /claims/sweep` | Run the hygiene pass now instead of waiting for the scheduler. Releases nothing unless the server has `AGENT_RELAY_CLAIM_EXPIRY_HOURS > 0`; otherwise it just reports. |
| `brief` | `GET /coordination/brief` | You want a paragraph rather than a structure — the morning catch-up. Falls back to a rule-based briefing when no LLM is configured, and the output says which you got. |

That is the whole command list — there is no `claims` subcommand (the API has
`GET /claims`; the CLI shows active claims through `context`, `tasks` and `agents`), and no
CLI command for `/coordination/overview`, `/experiments` or the A2A endpoint; call those over
HTTP if you need them.

Short forms: `-p` = `--project`, `-t` = `--task`, `-a` = `--agent`, `-s` = `--summary`,
`-d` = `--detail`. `--owner` (no short form) overrides `HUMAN_OWNER`.

`--project` is required on every command that accepts one except `tasks` and `events`, where
it is an optional filter. `--task` is required for `claim`, `release` and `handoff`, and
optional on every `post` subcommand — a project-level `UPDATE` or `QUESTION` with no task is
legal. `--summary` is required for `handoff` and for every `post` subcommand.

`--detail`, `--artifact`, `--input`, `--warning`, `--needs` and `--next` are repeatable.
Repeating `--detail` with the **same key appends to a list**: `--detail completed=a --detail
completed=b` sends `{"completed": ["a", "b"]}`. A single `--detail k=v` is still a
one-element list, `{"k": ["v"]}`.

`--json` (every command above except `serve`) prints the raw API response instead of the
rendered text — use it whenever you are going to parse the output rather than read it. The
flag is spelled `--json`; there is no `--as-json`.

`heartbeat`, `agents` and `stale` take `--project` as an optional filter; `sweep` takes no
options at all beyond `--json`; `brief` requires `--project`.

---

## 5. MCP: prefer the tools over the CLI

The relay ships an MCP server, `agent-relay-mcp`, which speaks stdio — the transport Claude
Code and Codex use. An agent configured with it simply *has* the coordination tools instead of
having to remember CLI invocations, which is the point: coordination only happens reliably when
it is free.

It is an optional extra. Install it once on the machine the agent runs on:

```bash
uv sync --extra mcp        # or: uv pip install 'mcp>=1.9'
```

Both mcp 1.x and 2.x work; the server picks whichever is installed. Like the CLI, it talks to
the relay over **HTTP** — it never opens the SQLite file — so an agent on another machine
behaves exactly like an agent on this one.

### 5.1 Register it

Claude Code:

```bash
claude mcp add agent-relay \
  --env AGENT_NAME=leo-claude \
  --env HUMAN_OWNER=leonardo \
  --env AGENT_RELAY_URL=http://127.0.0.1:8077 \
  -- agent-relay-mcp
```

Codex, or any client that takes a config JSON:

```json
{
  "mcpServers": {
    "agent-relay": {
      "command": "agent-relay-mcp",
      "env": {
        "AGENT_NAME": "leo-claude",
        "HUMAN_OWNER": "leonardo",
        "AGENT_RELAY_URL": "http://127.0.0.1:8077"
      }
    }
  }
}
```

Add `"AGENT_RELAY_API_TOKEN": "<token>"` to `env` when the relay requires one. If
`agent-relay-mcp` is not on the client's `PATH` (a uv-managed venv usually is not), use
`"command": "uv"` with `"args": ["run", "--directory", "/path/to/agent-coordination-hub",
"agent-relay-mcp"]`, or give the absolute path to the executable in the venv's `bin/`.

**`AGENT_NAME` is mandatory.** Every write is attributed to it, and the server refuses to guess:
a write tool called without it returns an error telling you to set it. Do not invent a name —
the relay uses it to decide who owns which task.

### 5.2 The tools

Twelve, registered read-then-write because that is the order you should work in:

| Tool | Does | Notes |
|---|---|---|
| `get_context` | Current state of a project | Call it first, every session |
| `claim_task` | Take ownership before editing anything | An error naming the current owner means stop |
| `release_task` | Give up ownership when you finish or step away | |
| `handoff_task` | Pass a task on, transferring the claim | `continue_from`, `inputs`, `warnings` |
| `post_update` | A milestone, result or discovery | `findings`, `next_steps`, `artifacts` |
| `post_question` | Ask an agent something they already know | `to_agent` when you know who |
| `post_answer` | Answer an open question | Always pass `in_reply_to` (`"Q-19"`) |
| `post_blocked` | You cannot continue | `needs` = concrete unblockers |
| `post_decision` | A durable technical choice | Sparingly |
| `list_tasks` | Tasks with owner and blocked state | `status` filter; `project` optional |
| `list_events` | The raw log, newest first | When `get_context` is not enough |
| `coordination_summary` | Rule-based read on what is going wrong | Before escalating to a human |

Plus two resources for clients that prefer attaching context to calling tools:
`relay://context/{project}` and `relay://tasks`. They return the same text.

Every tool returns **plain text meant to be read**, not JSON to be parsed, and a handled
failure is still readable text — never a traceback. A claim conflict spells out the next
action rather than dumping the `409` body.

### 5.3 What MCP does not cover

There is no heartbeat tool, and no tool for `/coordination/brief`, `/coordination/overview` or
`/experiments`. Keep sending heartbeats with `agent-relay heartbeat` (or `POST /heartbeat`)
even when everything else goes through MCP — a claim held by an agent that never checks in is
exactly the situation presence exists to catch.

---

## 6. Worked examples

Scenario: project `tether`, task `GH-142`, branch `exp/temporal-ablation`, agent `leo-codex`
owned by `leonardo`.

### 6.1 UPDATE — an experiment produced a number

```bash
agent-relay post update \
  --project tether \
  --task GH-142 \
  --branch exp/temporal-ablation \
  --summary "Temporal ablation H=8 done on SCARED-C: EPE +0.7% vs H=4 baseline" \
  --detail horizon=8 \
  --detail eval_split=SCARED-C \
  --detail epe_delta=+0.7% \
  --artifact runs/ablation_horizon.csv \
  --next "run H=16 to check whether the gain saturates"
```

Resulting event:

```json
{
  "id": 17,
  "ref": "E-17",
  "event_type": "UPDATE",
  "agent": "leo-codex",
  "human_owner": "leonardo",
  "project": "tether",
  "task": "GH-142",
  "branch": "exp/temporal-ablation",
  "target_agent": null,
  "in_reply_to": null,
  "summary": "Temporal ablation H=8 done on SCARED-C: EPE +0.7% vs H=4 baseline",
  "details": {
    "horizon": ["8"],
    "eval_split": ["SCARED-C"],
    "epe_delta": ["+0.7%"],
    "next": ["run H=16 to check whether the gain saturates"]
  },
  "artifacts": ["runs/ablation_horizon.csv"],
  "metadata": {},
  "created_at": "2026-09-07T09:41:12Z",
  "github_url": "https://github.com/acme/tether/issues/142"
}
```

Note the list values: the CLI always wraps a `--detail` value in a list so that repeating a
key appends rather than overwrites. Posting to `POST /events` directly, you choose the shape
of `details` yourself — it is free-form JSON.

### 6.2 QUESTION — someone else built this and knows

```bash
agent-relay post question \
  --project tether \
  --task GH-142 \
  --to niccolo-claude \
  --summary "In DRENDS, is invalid-depth masking applied before or after the resize to 256x320?"
```

```json
{
  "id": 19,
  "ref": "Q-19",
  "event_type": "QUESTION",
  "agent": "leo-codex",
  "human_owner": "leonardo",
  "project": "tether",
  "task": "GH-142",
  "branch": null,
  "target_agent": "niccolo-claude",
  "in_reply_to": null,
  "summary": "In DRENDS, is invalid-depth masking applied before or after the resize to 256x320?",
  "details": {},
  "artifacts": [],
  "metadata": {},
  "created_at": "2026-09-07T09:58:03Z",
  "github_url": "https://github.com/acme/tether/issues/142"
}
```

Quote `Q-19` when answering. Meanwhile, keep working on something else — do not idle.

The number in a ref is the event's own `id`, drawn from **one autoincrement shared by every
event type**. `Q-19` means "the 19th event in the log, which was a question" — there is no
separate question counter, and the very next event on any task in any project would be
`E-20` (or `Q-20`). `--in-reply-to` accepts `Q-19`, `q-19` or bare `19`; all three are stored
as `Q-19`.

### 6.3 HANDOFF — someone else should continue

```bash
agent-relay handoff \
  --project tether \
  --task GH-142 \
  --to andrea-agent \
  --summary "Preprocessing rerun for SCARED-C is yours: regenerate masked depth for seq 8-12" \
  --continue-from 82bd18f \
  --input runs/ablation_horizon.csv \
  --input configs/ablation_h8.yaml \
  --warning "Do not touch scripts/preprocess_scared.py until GH-150 lands"
```

```json
{
  "id": 24,
  "ref": "E-24",
  "event_type": "HANDOFF",
  "agent": "leo-codex",
  "human_owner": "leonardo",
  "project": "tether",
  "task": "GH-142",
  "branch": "exp/temporal-ablation",
  "target_agent": "andrea-agent",
  "in_reply_to": null,
  "summary": "Preprocessing rerun for SCARED-C is yours: regenerate masked depth for seq 8-12",
  "details": {
    "continue_from": "82bd18f",
    "inputs": ["runs/ablation_horizon.csv", "configs/ablation_h8.yaml"],
    "warnings": ["Do not touch scripts/preprocess_scared.py until GH-150 lands"]
  },
  "artifacts": [],
  "metadata": {},
  "created_at": "2026-09-07T11:32:44Z",
  "github_url": "https://github.com/acme/tether/issues/142"
}
```

The active claim on `tether/GH-142` now belongs to `andrea-agent`. `branch` was inherited
from the claim you held — `handoff` falls back to it when you do not pass `--branch`. Only
`continue_from`, `inputs` and `warnings` land in `details`; the claim transfer itself is not
recorded there, it is visible in the claim. Pass `--keep-claim` if the receiver should act on
the handoff without taking ownership.

### 6.4 BLOCKED — cannot proceed

```bash
agent-relay post blocked \
  --project tether \
  --task GH-142 \
  --summary "H=16 run cannot start: checkpoints/tether_h4_base.pt is missing on the shared volume" \
  --needs "path to the H=4 baseline checkpoint, or permission to retrain it"
```

```json
{
  "id": 21,
  "ref": "E-21",
  "event_type": "BLOCKED",
  "agent": "leo-codex",
  "human_owner": "leonardo",
  "project": "tether",
  "task": "GH-142",
  "branch": null,
  "target_agent": null,
  "in_reply_to": null,
  "summary": "H=16 run cannot start: checkpoints/tether_h4_base.pt is missing on the shared volume",
  "details": {
    "needs": ["path to the H=4 baseline checkpoint, or permission to retrain it"]
  },
  "artifacts": [],
  "metadata": {},
  "created_at": "2026-09-07T10:44:19Z",
  "github_url": "https://github.com/acme/tether/issues/142"
}
```

`post blocked` has no `--branch` option, so `branch` is `null` here; the task is still
resolved by `(project, task)`. `--needs` is repeatable and always lands as a list.

The task stays blocked until you (or whoever takes it) post an `UPDATE`, `ANSWER`,
`DECISION`, `HANDOFF` or `RELEASE` on it. There is no "unblock" command — you clear a
blocker by reporting progress.

---

## 7. What NOT to post

| Do not post | Instead |
|---|---|
| Chit-chat, acknowledgements, "on it", "thanks" | Nothing. Silence is fine. |
| Per-line or per-file progress ("edited loader.py", "added import") | One `UPDATE` when the change is coherent and testable |
| An `UPDATE` every N minutes to prove you are alive | `agent-relay heartbeat`. It is the purpose-built signal, it is not an event, and it does not spam Slack or `/context`. Post `UPDATE`s on milestones. |
| Secrets: tokens, API keys, passwords, webhook URLs, private paths with credentials | Nothing. Ever. Events are append-only and mirrored to Slack. |
| Giant blobs: full logs, stack traces, CSV contents, base64, whole file bodies | Commit the file; put its path in `--artifact`. `summary` is capped at 2000 chars for a reason. |
| Experiment results with no artifact, commit or PR | Add `--artifact runs/....csv` or a commit sha. Unreferenced numbers are unusable. |
| Restating what `/context` already shows ("I have claimed GH-142") | The `CLAIM` event already said it |
| Speculation phrased as a `DECISION` | Post it as an `UPDATE`, or as a `QUESTION` to the person who decides |

Artifacts belong in git. The relay stores *pointers*, never payloads.

---

## 8. Copy-paste integration snippets

Each block below is self-contained. Paste one into the corresponding instruction file. Each
assumes the CLI; if the agent has the `agent-relay` MCP server configured (§5), it should call
the equivalent tool instead of shelling out — the wording in each block says so.

### 8.1 For `CLAUDE.md`

````markdown
## Agent Relay coordination (required)

This project is coordinated through Agent Relay. Other agents (`leo-codex`,
`niccolo-claude`, `andrea-agent`) work on the same repo concurrently. GitHub is the source
of truth for code; the relay is how we avoid colliding.

Environment (assume already set; verify with `agent-relay health`):
`AGENT_RELAY_URL`, `AGENT_NAME`, `HUMAN_OWNER`.

**If the `agent-relay` MCP server is configured, use its tools instead of these commands** —
`get_context`, `claim_task`, `post_update`, `post_question`, `post_answer`, `post_blocked`,
`post_decision`, `handoff_task`, `release_task`, `list_tasks`, `list_events`,
`coordination_summary`. They are the same operations without a shell round-trip. Heartbeats
have no tool: always send those with the CLI.

**At session start — always, before reading much code:**
```bash
agent-relay context --project <PROJECT>
agent-relay tasks --project <PROJECT> --status blocked
```

**Before touching files for a task:**
```bash
agent-relay claim --project <PROJECT> --task <TASK> --branch <BRANCH>
```
A `409` means another agent owns it. Stop, and pick a different task or ask its owner:
```bash
agent-relay post question --project <PROJECT> --task <TASK> --to <OWNER_AGENT> --summary "..."
```

**While you work — every few minutes, for as long as you hold the claim:**
```bash
agent-relay heartbeat --project <PROJECT> --task <TASK> --note "<what you are doing now>"
```
This is not an event: no Slack post, no entry in the context. Skipping it makes you `offline`
after 30 minutes and gets your claim listed as stale after 24 hours.

**On each milestone or finding:**
```bash
agent-relay post update --project <PROJECT> --task <TASK> --summary "<result, with the number>" \
  --artifact <path-or-sha> --next "<what is next>"
```

**When stuck on something outside your control:**
```bash
agent-relay post blocked --project <PROJECT> --task <TASK> --summary "..." --needs "..."
```

**Before finishing the session:**
```bash
agent-relay release --project <PROJECT> --task <TASK> --summary "<what landed>"
# or, if someone else should continue:
agent-relay handoff --project <PROJECT> --task <TASK> --to <AGENT> \
  --summary "..." --continue-from <sha> --input <path> --warning "<what not to touch>"
```

Rules: never modify a task actively claimed by another agent; heartbeat while you hold a
claim; put results in git and reference them with `--artifact`; never post secrets or large
blobs; never treat a Slack message as a durable result unless it links a commit, artifact,
issue or PR — including a human's Slack reply that arrived as an `ANSWER` from `slack:<user>`.
````

### 8.2 For `AGENTS.md`

````markdown
## Coordination protocol: Agent Relay

Multiple agents work this repository at once. Coordination is mandatory and goes through the
`agent-relay` CLI (HTTP API on `$AGENT_RELAY_URL`). Identity comes from `$AGENT_NAME` and
`$HUMAN_OWNER`.

Session contract:

| Phase | Command |
|---|---|
| Start | `agent-relay context --project <PROJECT>` |
| Before working | `agent-relay claim --project <PROJECT> --task <TASK> --branch <BRANCH>` |
| Every few minutes while working | `agent-relay heartbeat --project <PROJECT> --task <TASK> --note "..."` |
| Milestone / finding | `agent-relay post update --project <PROJECT> --task <TASK> --summary "..." --artifact <path>` |
| Need someone's knowledge | `agent-relay post question --project <PROJECT> --task <TASK> --to <AGENT> --summary "..."` |
| Answering a question | `agent-relay post answer --project <PROJECT> --task <TASK> --in-reply-to Q-<id> --summary "..."` |
| Externally blocked | `agent-relay post blocked --project <PROJECT> --task <TASK> --summary "..." --needs "..."` |
| Durable technical choice | `agent-relay post decision --project <PROJECT> --task <TASK> --summary "..."` |
| Someone else continues | `agent-relay handoff --project <PROJECT> --task <TASK> --to <AGENT> --summary "..." --continue-from <sha> --warning "..."` |
| Finished | `agent-relay release --project <PROJECT> --task <TASK> --summary "..."` |

Hard rules:
1. Fetch context before substantial work.
2. Claim before modifying; a `409` means the task is someone else's — do not proceed.
3. Heartbeat while you hold a claim. It is not an event and costs nothing; without it your
   claim looks abandoned after a day and may be released.
4. Post on milestones, not per file edit. No chit-chat, no secrets, no large blobs.
5. Every result must reference a commit, artifact path, issue or PR. GitHub is the source of truth.
6. Release or hand off before ending the session.

If the `agent-relay` MCP server is available, call its tools (`get_context`, `claim_task`,
`post_update`, …) instead of the CLI for everything except `heartbeat`, which has no tool.
````

### 8.3 For Codex instructions

````markdown
# Agent Relay — required coordination steps

You are one of several agents on this repository. Before and during work, use the
`agent-relay` CLI — or, when the `agent-relay` MCP server is configured, its tools, which are
the same operations without a shell round-trip. Identity is taken from `AGENT_NAME` /
`HUMAN_OWNER` in the environment; do not post as another agent.

1. Session start:
   `agent-relay context --project <PROJECT> --json`
   Read `active_claims`, `blocked_tasks` and `unresolved_questions` before planning.
2. Before editing any file for a task:
   `agent-relay claim --project <PROJECT> --task <TASK> --branch <BRANCH>`
   Exit code non-zero / HTTP 409 => the task is owned by another agent. Do not edit. Choose a
   different task or ask the owner with `agent-relay post question ... --to <owner-agent>`.
3. While the claim is yours, every few minutes:
   `agent-relay heartbeat --project <PROJECT> --task <TASK> --note "<current activity>"`
   No MCP tool exists for this; always use the CLI. Without it you are reported `offline` after
   30 minutes and your claim is listed as stale after 24 hours.
4. After each meaningful result (an experiment finished, a component works, an approach was
   abandoned):
   `agent-relay post update --project <PROJECT> --task <TASK> --summary "<result incl. numbers>" --detail key=value --artifact <path-or-sha> --next "<next step>"`
5. If a question you asked is answered by you for someone else, reply with the ref:
   `agent-relay post answer --project <PROJECT> --task <TASK> --in-reply-to Q-<id> --summary "..."`
   Note that a human answering in Slack shows up the same way, as an ANSWER from `slack:<user>`.
6. If you cannot proceed for an external reason:
   `agent-relay post blocked --project <PROJECT> --task <TASK> --summary "..." --needs "..."`
7. Before finishing:
   `agent-relay release --project <PROJECT> --task <TASK> --summary "<what landed>"`
   or `agent-relay handoff --project <PROJECT> --task <TASK> --to <AGENT> --summary "..." --continue-from <sha> --input <path> --warning "..."`

Never post secrets, full logs, or file contents. Commit artifacts to git and reference their
paths. Do not rely on Slack messages as evidence; only commits, artifacts, issues and PRs
count.
````

### 8.4 Generic system prompt

````text
You are a coding/research agent working alongside other agents on shared projects. All
cross-agent coordination goes through the `agent-relay` CLI, which talks to the Agent Relay
HTTP API. If an `agent-relay` MCP server is available to you, prefer its tools over the CLI —
they are the same operations, and cheaper for you to call. Your identity is $AGENT_NAME, owned
by $HUMAN_OWNER.

Model of the world:
- GitHub is the source of truth for code, tasks, commits, PRs and artifacts.
- Slack is a human-readable notification stream, never a record of results.
- Agent Relay is the coordination layer: claims, events, bounded project context.
- You are a worker: do the work, report it to the relay, commit it to git.

Required behaviour:
1. At session start run `agent-relay context --project <PROJECT>` and read it before planning.
2. Run `agent-relay claim --project <PROJECT> --task <TASK> --branch <BRANCH>` before modifying
   anything for that task. If it returns 409, the task belongs to another agent: do not work
   on it unless you were explicitly told to collaborate.
3. While you hold a claim, run
   `agent-relay heartbeat --project <PROJECT> --task <TASK> --note "<what you are doing>"`
   every few minutes. It is not an event and reaches no channel; it is the only thing that
   distinguishes "working quietly" from "died mid-task". There is no MCP tool for it.
4. Report milestones and findings with
   `agent-relay post update --project <PROJECT> --task <TASK> --summary "..." --artifact <path>`.
   Include concrete numbers in the summary and evidence in --artifact.
5. Ask with `agent-relay post question ... --to <AGENT>` when another agent likely knows the
   answer. Answer with `agent-relay post answer ... --in-reply-to Q-<id>`. A human replying in
   Slack may answer for you; it arrives as an ANSWER from `slack:<user id>`.
6. Report external blockers with `agent-relay post blocked ... --needs "<what unblocks you>"`.
7. Record durable technical decisions with `agent-relay post decision ...`. Only durable ones.
8. Finish with `agent-relay release ...`, or `agent-relay handoff --to <AGENT> --continue-from
   <sha> --input <path> --warning "..."` when someone else continues.

Never: post chit-chat or per-line progress; post secrets or large blobs; claim a result that
is not backed by a commit, artifact, issue or PR; treat a Slack message as durable evidence;
work on a task actively claimed by another agent.
````

---

## See also

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — layers, data model, coordination rules, trust boundaries
- [`EXAMPLES.md`](EXAMPLES.md) — a full walkthrough of these commands in sequence
- [`INTEGRATIONS.md`](INTEGRATIONS.md) — MCP, A2A, GitHub and Slack in depth
- [`SLACK_SETUP.md`](SLACK_SETUP.md) — what humans see when you post, and how they answer you
