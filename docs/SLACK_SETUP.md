# Slack Setup

Slack is the **human-readable event bus**: *Slack communicates.* It mirrors relay events into
a channel so the three of you can see what your agents are doing without polling the API.

Slack is **optional and best-effort**. With `SLACK_WEBHOOK_URL` unset, the relay works
completely — events are stored, claims are enforced, `/context` and `/coordination/summary`
are unaffected. Nothing downstream depends on Slack.

There are two ways to wire Slack up, and this page is about the first:

| | Incoming webhook (this page) | Bot token (§7) |
|---|---|---|
| Direction | Out only — the relay posts | Both ways |
| Setup | One URL, two minutes | An app with scopes, Event Subscriptions, a public URL |
| Channels | One, fixed by the webhook | Per project, via `SLACK_CHANNEL_MAP` |
| Humans can reply | No | Yes — a threaded reply becomes an `ANSWER` and closes the question |
| Needs inbound connectivity | No | Yes |

Start with the webhook. Everything below applies to both paths — the rendering, the
best-effort semantics and most of the troubleshooting are shared. §7 covers the upgrade, and
[`INTEGRATIONS.md`](INTEGRATIONS.md) §2 is the full reference for it.

---

## 1. Create the Slack app

1. Go to <https://api.slack.com/apps> and click **Create New App** → **From scratch**.
2. Name it `Agent Relay`, pick your workspace, click **Create App**.
3. In the left sidebar open **Incoming Webhooks** and toggle **Activate Incoming Webhooks**
   to **On**.
4. Create the destination channel in Slack if it does not exist. Suggested: **`#agent-relay`**
   — an incoming webhook is bound to exactly one channel, so this path is one channel for all
   projects. Per-project routing needs the bot token (§7).
5. Back in the app settings, click **Add New Webhook to Workspace**, choose `#agent-relay`,
   and **Allow**.
6. Copy the generated URL. It looks like
   `https://hooks.slack.com/services/<workspace-id>/<webhook-id>/<token>`.

The webhook is bound to that one channel and can only post — it cannot read messages. That is
all this path needs, and all it should have. If you later want humans to be able to answer an
agent's question from Slack, §7 adds a bot token alongside this; the webhook keeps working.

---

## 2. Configure the relay

Copy the example file if you have not already, then edit `.env`:

```bash
cp .env.example .env
```

```dotenv
# --- Slack (optional) -------------------------------------------------------
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/<workspace-id>/<webhook-id>/<token>
# Comma-separated event types to forward. Empty = all event types.
SLACK_EVENT_TYPES=
SLACK_TIMEOUT_SECONDS=5.0
```

| Variable | Default | Meaning |
|---|---|---|
| `SLACK_WEBHOOK_URL` | *(unset)* | Incoming webhook URL. Unset or blank → Slack posting is silently disabled. |
| `SLACK_EVENT_TYPES` | *(unset = all)* | Comma-separated **allowlist** of event types to forward. Case-insensitive, whitespace tolerated. |
| `SLACK_TIMEOUT_SECONDS` | `5.0` | HTTP timeout for the webhook POST. On timeout the post is abandoned and logged. |

A blank value (`SLACK_WEBHOOK_URL=`) is treated as "not configured", not as an empty string —
so you can keep the key in `.env` without enabling it.

### Filtering examples

```dotenv
# Everything (default)
SLACK_EVENT_TYPES=

# Only the things a human should look at
SLACK_EVENT_TYPES=QUESTION,BLOCKED,DECISION,HANDOFF

# Ownership changes only
SLACK_EVENT_TYPES=CLAIM,RELEASE,HANDOFF
```

Filtering affects **Slack only**. Every event is always stored and always visible through
`/events`, `/context` and `/coordination/summary`.

Restart the server to pick up the change:

```bash
agent-relay serve
```

---

## 3. Verify

### 3.1 Check the integration is registered

```bash
agent-relay health
```

```bash
curl -s http://127.0.0.1:8077/health | jq
```

```json
{
  "status": "ok",
  "version": "0.1.0",
  "time": "2026-09-07T09:30:02Z",
  "database": "ok",
  "integrations": {
    "slack": "enabled",
    "github": "enabled",
    "auth": "open",
    "db_url": "sqlite:///./data/agent_relay.db"
  }
}
```

`database` is the result of a `SELECT 1` probe (`ok`, or `error: <ExceptionName>`), not the
URL — the URL is `integrations.db_url`, with any credentials redacted. `integrations.slack`
and `integrations.github` are the strings `enabled` / `disabled`, and `integrations.auth` is
`required` when `AGENT_RELAY_API_TOKEN` is set and `open` when it is not.

`"slack": "disabled"` means the webhook URL is unset, blank, or the server was not restarted.
`/health` reports the **webhook** only; the bot token and inbound events are not part of
`integrations`. To confirm those, read the relay's startup log line, which prints the non-secret
configuration summary including `slack_bot` and `slack_events`.

`/health` never requires a token, and is one of only a handful of routes that do not — see
[`ARCHITECTURE.md`](ARCHITECTURE.md) §8.

### 3.2 Post a test event with curl

```bash
curl -s -X POST http://127.0.0.1:8077/events \
  -H 'Content-Type: application/json' \
  -d '{
        "event_type": "UPDATE",
        "agent": "leo-codex",
        "human_owner": "leonardo",
        "project": "tether",
        "task": "GH-142",
        "branch": "exp/temporal-ablation",
        "summary": "Slack webhook smoke test",
        "artifacts": ["runs/ablation_horizon.csv"]
      }' | jq '{id, ref, event_type, created_at}'
```

```json
{
  "id": 1,
  "ref": "E-1",
  "event_type": "UPDATE",
  "created_at": "2026-09-07T09:31:44Z"
}
```

### 3.3 Post a test event with the CLI

```bash
agent-relay post update \
  --project tether \
  --task GH-142 \
  --summary "Slack webhook smoke test via CLI" \
  --artifact runs/ablation_horizon.csv
```

A message should appear in `#agent-relay` within a second or two. The `201` comes back
*before* the Slack post is attempted, so a fast response does not by itself prove Slack
worked — look at the channel, or at the server log.

---

## 4. What the messages look like

Each event renders as one Block Kit message with a fixed skeleton:

```
<glyph> <EVENT TYPE> · <PROJECT> · <task>      ← header, project upper-cased, task linked to GitHub
<agent>  (<human owner>)  → <target agent>     ← byline; the arrow only when the event is directed

<summary>

<Detail Title>          <Detail Title>         ← one titled bullet group per `details` key,
• value                 • value                  laid out two per row

Artifacts
• …

<branch>  ·  GitHub  ·  <ref>                  ← context footer, each part only when it exists
```

Details keys are humanised for the title (`eval_split` → *Eval split*), each value becomes
bullets, and a group is truncated after 6 bullets with an "…and N more" line. Layouts below
are the visual structure, not the raw webhook JSON.

### 🔄 UPDATE

```
🔄 UPDATE · TETHER · GH-142
leo-codex  (leonardo)

Temporal ablation H=8 done on SCARED-C: EPE +0.7% vs H=4 baseline

Horizon                     Eval split
• 8                         • SCARED-C

Epe delta                   Next
• +0.7%                     • run H=16 to check whether the gain saturates

Artifacts
• runs/ablation_horizon.csv

exp/temporal-ablation  ·  GitHub  ·  E-17
```

### ❓ QUESTION

```
❓ QUESTION · TETHER · GH-142
leo-codex  (leonardo)  → niccolo-claude

Q-19 In DRENDS, is invalid-depth masking applied before or after the resize to 256x320?

GitHub  ·  Q-19
```

The ref is prepended to the body of a `QUESTION` precisely so a human can quote it back.

### 💬 ANSWER

```
💬 ANSWER · TETHER · GH-142
niccolo-claude  (niccolo)  ↩ leo-codex

re Q-19  Masking is applied BEFORE the resize; resizing first interpolates invalid pixels into valid ones

Artifacts
• src/data/drends.py#L88

GitHub  ·  E-20
```

An `ANSWER` is the one event type whose arrow is `↩` rather than `→`, and the `re Q-19`
prefix appears only when it carries `in_reply_to`.

### 🔒 CLAIM

```
🔒 CLAIM · TETHER · GH-142
leo-codex  (leonardo)

Ablating prediction horizon H in {4,8,16} on SCARED-C

exp/temporal-ablation  ·  GitHub  ·  E-16
```

The body is the `--note` you passed to `agent-relay claim`, or `Claimed GH-142.` when you
passed none. Re-claiming a task you already hold posts nothing — no event is written.

### 🤝 HANDOFF

```
🤝 HANDOFF · TETHER · GH-142
leo-codex  (leonardo)  → andrea-agent

Preprocessing rerun for SCARED-C is yours: regenerate masked depth for seq 8-12

Continue from               Inputs
• 82bd18f                   • runs/ablation_horizon.csv
                            • configs/ablation_h8.yaml

Warnings
• Do not touch scripts/preprocess_scared.py until GH-150 lands

exp/temporal-ablation  ·  GitHub  ·  E-24
```

### 🚧 BLOCKED

```
🚧 BLOCKED · TETHER · GH-142
leo-codex  (leonardo)

H=16 run cannot start: checkpoints/tether_h4_base.pt is missing on the shared volume

Needs
• path to the H=4 baseline checkpoint, or permission to retrain it

GitHub  ·  E-21
```

No branch in the footer: `agent-relay post blocked` has no `--branch` option.

### 📌 DECISION

```
📌 DECISION · TETHER · GH-142
niccolo-claude  (niccolo)

Invalid-depth masking stays before resize across all loaders; documented in docs/data.md

GitHub  ·  E-22
```

### 🔓 RELEASE

```
🔓 RELEASE · TETHER · GH-142
andrea-agent  (andrea)

Preprocessing rerun complete for seq 8-12; ablation table regenerated at 4f10c93

exp/temporal-ablation  ·  GitHub  ·  E-28
```

A `RELEASE` carries no artifacts — `agent-relay release` takes only `--summary`, and the
branch shown is the one recorded on the claim being released. A forced release
(`--force`) appends `(force-released a claim held by <agent>)` to the summary.

Glyph reference:

| Glyph | Event type |
|---|---|
| 🔄 | UPDATE |
| ❓ | QUESTION |
| 💬 | ANSWER |
| 🔒 | CLAIM |
| 🤝 | HANDOFF |
| 🚧 | BLOCKED |
| 📌 | DECISION |
| 🔓 | RELEASE |

The `GH-142` link is present only when `GITHUB_OWNER`/`GITHUB_REPO` are configured;
`GITHUB_TASK_PREFIX` (default `GH-`) is what turns the task id into an issue number.

---

## 5. Slack is best-effort — by design

| Property | Behaviour |
|---|---|
| **When** | The webhook POST happens in a background task, *after* the HTTP response has been returned to the agent. |
| **Failure** | Any error — timeout, 404, 429, network down — is logged and swallowed. The API call already succeeded. |
| **Event loss** | Impossible via Slack. The event is committed to SQLite before Slack is contacted. A missing Slack message never means a missing event. |
| **Latency** | Agents never wait on Slack. `SLACK_TIMEOUT_SECONDS` bounds the background attempt only. |
| **Retries** | None. A dropped Slack message stays dropped; re-read the event with `agent-relay events`. |
| **Disabled** | With no webhook configured, the background step does not run and everything else is identical. |

If Slack and the relay ever disagree, the relay is right. If the relay and GitHub ever
disagree about code, GitHub is right.

---

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| No messages at all, `/health` shows `"slack": "disabled"` | Webhook not set, set to a blank value, or the server was not restarted | Set `SLACK_WEBHOOK_URL` in `.env`, restart `agent-relay serve` |
| `/health` shows `"slack": "enabled"`, still no messages | `SLACK_EVENT_TYPES` excludes the type you are posting | Widen or empty the allowlist; check spelling — values are matched uppercase |
| Some types appear, others do not | Same as above | Confirm with `agent-relay events --project P --limit 5 --json` that the event exists; if it does, the filter is the cause |
| `404 invalid_token` / `no_service` in the server log | Webhook URL wrong, revoked, or the app was uninstalled | Regenerate the webhook in **Incoming Webhooks** and update `.env` |
| `channel_not_found` | The channel was deleted or archived, or the webhook was pointed at a private channel the app was removed from | Create a new webhook against a live channel |
| `429 rate_limited` in the log | Slack throttles incoming webhooks at roughly one message per second per webhook; a burst of events exceeded it | Narrow `SLACK_EVENT_TYPES` (e.g. drop `UPDATE`), and post on milestones rather than per file edit. There are no retries — the events are still in the relay. |
| Timeouts in the log | Slow network, or `SLACK_TIMEOUT_SECONDS` too low | Raise `SLACK_TIMEOUT_SECONDS`. Note this only affects the background attempt, never agent latency. |
| Bot-path problems: Request URL will not verify, `401` on deliveries, replies not becoming events | Inbound Slack is configured separately | [`INTEGRATIONS.md`](INTEGRATIONS.md) §2.6 |
| Messages appear but with no GitHub link | GitHub not configured, or the task id does not match `GITHUB_TASK_PREFIX` | Set `GITHUB_OWNER`/`GITHUB_REPO`; use task ids like `GH-142` |
| Events missing entirely (not just in Slack) | An agent is posting to a different `AGENT_RELAY_URL`, or a different project name | `agent-relay health`, then `agent-relay events --project P --limit 20` |

Server logs are the diagnostic surface: raise verbosity with `AGENT_RELAY_LOG_LEVEL=DEBUG`.

---

## 7. Upgrading to the bidirectional bot

Everything above stays. This adds a second, richer path alongside it.

**What you get**

- **Humans can answer from Slack.** A threaded reply to a `QUESTION` the relay posted becomes an
  `ANSWER` event with `in_reply_to` set, which closes the question in `/context` and
  `/coordination/summary`. A reply to any other event becomes an `UPDATE` — a human note on that
  piece of work. Either way the relay acknowledges in-thread.
- **Per-project channels.** `SLACK_CHANNEL_MAP=tether=C012AB,drends=C034CD`, with
  `SLACK_DEFAULT_CHANNEL` catching anything unmapped. Three projects stop sharing one firehose.
- **Scheduled team status posts.** With `AGENT_RELAY_STATUS_INTERVAL_MINUTES` and
  `AGENT_RELAY_STATUS_PROJECTS`, the scheduler posts a briefing per project on a cadence.

**What it costs**

- A Slack app with `chat:write` and `channels:history` (plus `groups:history` for private
  channels) — more than the post-only permission a webhook has.
- A **publicly reachable URL** for `POST /webhooks/slack/events`, because Slack has to be able to
  reach you. See [`OPERATIONS.md`](OPERATIONS.md) §4 for exposing only that route safely.

**How the round trip works**

`chat.postMessage` returns the message `ts`; the relay stores it on the event as `slack_ts`. A
human's threaded reply carries that same string back as `thread_ts`, which is how the relay finds
the exact event being answered. Inbound requests are authenticated by Slack's v0 signature over
the raw body plus a 5-minute replay window — not by the relay's bearer token, which Slack cannot
send.

**Configuration**

```dotenv
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
SLACK_DEFAULT_CHANNEL=C0DEFAULT
SLACK_CHANNEL_MAP=tether=C012AB,drends=C034CD
```

Both the token and the signing secret are required before `/webhooks/slack/events` accepts
anything; without them it answers `503` and Slack will not verify the Request URL. Restart after
editing `.env`.

**What the bot token buys you.** Storing the thread anchor requires posting with the bot
token, because only `chat.postMessage` returns a message `ts`. Once `SLACK_BOT_TOKEN` is set,
`POST /events` posts through the bot automatically and records where the message landed, so
every event becomes answerable in its thread. With only a webhook the relay can talk but not
listen.

The full setup (scopes, Event Subscriptions, which bot events to subscribe to, what is ignored,
and inbound-specific troubleshooting) is in [`INTEGRATIONS.md`](INTEGRATIONS.md) §2. It is not
repeated here so the two pages cannot drift.

---

## 8. Security

- **Never commit the webhook URL.** It is a bearer credential: anyone holding it can post to
  your channel. It lives in `.env`, which is gitignored. `.env.example` must stay blank.
- The relay's `/health` and configuration summary redact secrets: `SLACK_WEBHOOK_URL`,
  `GITHUB_TOKEN` and `AGENT_RELAY_API_TOKEN` are never returned over HTTP or logged.
- **Do not put secrets in event summaries or details.** Events are append-only — there is no
  edit and no delete — and they are mirrored to a channel other people can read.
- If a webhook leaks, revoke it in the Slack app's **Incoming Webhooks** page and generate a
  new one. Nothing in the relay needs to change beyond `.env` and a restart.
- The webhook grants post-only access to one channel. On this path, do not grant the app
  additional scopes — the relay reads nothing from Slack. If you take the §7 upgrade, grant
  exactly `chat:write` and `channels:history` (plus `groups:history` for a private channel) and
  nothing more.
- **The bot token and signing secret are credentials too.** The bot token can post as the app
  anywhere it is invited; the signing secret is what stops anyone from forging an inbound event.
  Both live in `.env`. Rotation is "edit `.env`, restart" — see
  [`OPERATIONS.md`](OPERATIONS.md) §4.5.
- **A Slack reply becomes a permanent event.** A human answering in a thread writes an append-only
  record authored `slack:<user id>`. Anyone who can type in that channel can write to the relay's
  log — which is the feature, and worth knowing before inviting the bot into a wide channel.
