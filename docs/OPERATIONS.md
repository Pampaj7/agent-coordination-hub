# Operations

The runbook: deploying the relay, upgrading a V1 database, running the scheduler, exposing the
webhook routes without exposing everything else, backing up SQLite, and finding the fault when
something stops working.

Audience: whoever runs the box. Agent-facing rules are in
[`AGENT_PROTOCOL.md`](AGENT_PROTOCOL.md); integration setup is in
[`INTEGRATIONS.md`](INTEGRATIONS.md).

---

## 1. Deployment

The relay is one process and one SQLite file. There is no queue, no cache, no worker pool and
nothing to orchestrate.

### 1.1 From a checkout, with uv

```bash
scripts/dev.sh setup      # uv sync + create .env from .env.example if missing
agent-relay serve         # or: uv run agent-relay serve
```

`serve` binds `AGENT_RELAY_HOST` (default `127.0.0.1`) on `AGENT_RELAY_PORT` (default `8077`).
`--host`, `--port` and `--reload` override. `--reload` is for development only: it restarts the
process, and with it the scheduler, on every file change.

Optional extras, neither of which is needed for the core relay:

```bash
uv sync --extra coordinator   # anthropic SDK, for /coordination/brief prose
uv sync --extra mcp           # MCP SDK, for agent-relay-mcp
uv sync --extra all           # both
```

For a long-lived install, run it under whatever supervisor the box already has. The relay has
no daemon mode of its own. Ready-to-edit units ship in [`deploy/`](../deploy):

```bash
# Linux
sudo cp deploy/agent-relay.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now agent-relay
journalctl -u agent-relay -f

# macOS
cp deploy/com.agent-relay.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.agent-relay.plist
```

Edit the user and paths in either file; leave configuration in the `.env` next to the
checkout rather than in the unit, so secrets do not end up world-readable in `/etc`.

### 1.2 Docker

```bash
docker build -t agent-relay:local .
docker run -d --name agent-relay -p 8077:8077 \
  -v agent-relay-data:/data --env-file .env agent-relay:local
```

The image is multi-stage (uv resolves the locked deps into a venv; the runtime stage is
`python:3.12-slim` and gets only that venv), runs as a non-root user, and declares a
stdlib-only `HEALTHCHECK` against `/health`.

Two things it sets for you, overriding `.env`: `AGENT_RELAY_HOST=0.0.0.0` (otherwise the port
is unreachable from outside the container) and
`AGENT_RELAY_DB_URL=sqlite:////data/agent_relay.db` — **four slashes**, an absolute path to the
mounted volume. Three slashes would put the database inside the container's filesystem, where
it dies with the container.

### 1.3 Docker Compose

```bash
cp .env.example .env      # required: compose declares env_file: .env
docker compose up -d --build
curl -s localhost:8077/health | jq
```

One service, one named volume (`relay-data:/data`), `restart: unless-stopped`, and the same
host/port/DB overrides as above. Upgrading is `docker compose up -d --build`; the volume — and
therefore the event log — survives.

### 1.4 Sizing and the database

The workload is a few hundred events a day from a handful of agents. SQLite with WAL,
`foreign_keys=ON` and `busy_timeout=5000` (all set on every connection) handles single-digit
concurrent writers comfortably, and every query hits an indexed table.

`AGENT_RELAY_DB_URL` accepts any SQLAlchemy URL, so a Postgres DSN is possible if that ever
stops being true. SQLite is what the project is built, tested and tuned for; the SQLite-only
pragmas above are skipped for other backends, and the additive migration (§2) uses plain
`ALTER TABLE … ADD COLUMN`.

### 1.5 Configuration reference

Everything, with its default. All of it comes from the environment or `.env`; a blank value
(`FOO=`) means "not configured", not "empty string".

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_RELAY_HOST` | `127.0.0.1` | Bind address |
| `AGENT_RELAY_PORT` | `8077` | Bind port |
| `AGENT_RELAY_DB_URL` | `sqlite:///./data/agent_relay.db` | Database URL. Parent directory is created for file-backed SQLite |
| `AGENT_RELAY_LOG_LEVEL` | `INFO` | `DEBUG` when diagnosing |
| `AGENT_RELAY_API_TOKEN` | *(unset)* | Optional shared bearer token. Unset = no auth |
| `AGENT_RELAY_CONTEXT_LIMIT` | `10` | Default items per `/context` section |
| `AGENT_RELAY_CONTEXT_WINDOW_HOURS` | `72` | Default lookback for `/context`, `/coordination/summary`, `/coordination/brief`, `/coordination/overview` |
| `SLACK_WEBHOOK_URL` | *(unset)* | Outbound incoming-webhook URL |
| `SLACK_EVENT_TYPES` | *(unset = all)* | Allowlist of event types to forward to Slack |
| `SLACK_TIMEOUT_SECONDS` | `5.0` | Timeout for every Slack HTTP call |
| `SLACK_BOT_TOKEN` | *(unset)* | Bot token; makes Slack two-way |
| `SLACK_SIGNING_SECRET` | *(unset)* | Required to accept inbound Slack events |
| `SLACK_DEFAULT_CHANNEL` | *(unset)* | Channel id used when a project has none of its own |
| `SLACK_CHANNEL_MAP` | *(unset)* | `tether=C012AB,drends=C034CD` |
| `GITHUB_TOKEN` | *(unset)* | Read-only token for issue/PR reads. The relay never writes to GitHub |
| `GITHUB_OWNER` / `GITHUB_REPO` | *(unset)* | Enables link building and the read-only passthrough |
| `GITHUB_TASK_PREFIX` | `GH-` | Turns `GH-142` into issue 142 |
| `GITHUB_WEBHOOK_SECRET` | *(unset)* | Enables `POST /webhooks/github`; must match the hook's secret |
| `GITHUB_INGEST_PROJECT` | *(unset → `GITHUB_REPO`)* | Project ingested activity is filed under |
| `GITHUB_POLL_INTERVAL_SECONDS` | `0` (off) | Polling fallback interval |
| `AGENT_RELAY_HEARTBEAT_ONLINE_SECONDS` | `300` | Heartbeat age still counted `online` |
| `AGENT_RELAY_HEARTBEAT_IDLE_SECONDS` | `1800` | Heartbeat age still counted `idle`; beyond it, `offline` |
| `AGENT_RELAY_CLAIM_STALE_HOURS` | `24` | Claim idle time that is *reported* as stale |
| `AGENT_RELAY_CLAIM_EXPIRY_HOURS` | `0` (never) | Claim idle time that is *auto-released* |
| `AGENT_RELAY_SWEEPER_INTERVAL_SECONDS` | `300` | Sweep job interval; `0` disables the job |
| `ANTHROPIC_API_KEY` | *(unset)* | Enables LLM prose in `/coordination/brief` |
| `COORDINATOR_MODEL` | `claude-opus-5` | Model id |
| `COORDINATOR_MAX_TOKENS` | `16000` | Output cap for one briefing |
| `COORDINATOR_EFFORT` | `low` | `low\|medium\|high\|xhigh\|max`; anything else warns and uses `low` |
| `AGENT_RELAY_STATUS_INTERVAL_MINUTES` | `0` (off) | Scheduled Slack status post cadence |
| `AGENT_RELAY_STATUS_PROJECTS` | *(unset)* | Comma-separated projects to post status for |
| `WANDB_ENTITY` / `WANDB_PROJECT` | *(unset)* | Expand `wandb:<run>` shorthands into URLs |
| `MLFLOW_TRACKING_URI` | *(unset)* | Expand `mlflow:<run>` shorthands into URLs |
| `AGENT_RELAY_DASHBOARD` | `true` | `false` makes `/dashboard` and `/dashboard/projects` answer `404` |
| `AGENT_RELAY_PUBLIC_URL` | *(unset)* | Absolute URL the relay is reachable at; used in the A2A agent card |
| `AGENT_RELAY_URL` | `http://127.0.0.1:8077` | **Client-side.** Where the CLI and MCP server look for the relay |
| `AGENT_NAME` / `HUMAN_OWNER` | *(unset)* | **Client-side.** Identity for the CLI and MCP server |

Configuration is read once and cached, so **every change needs a restart**. Secrets are never
returned over HTTP or logged: `/health` and the startup summary redact database credentials and
omit every token.

---

## 2. Upgrading from V1

There is no migration command, and there is nothing to run by hand. `db/migrate.py` executes on
every boot, before the app serves a request, and brings an existing database up to the current
model definitions.

**What it does, exactly:**

1. `create_all()` — creates missing tables. On a V1 database that is `ingest_records`. Existing
   tables are untouched.
2. `ALTER TABLE … ADD COLUMN` for each mapped column that is missing, with a literal scalar
   default inlined where the model has one. On a V1 database:
   `events.source`, `events.slack_ts`, `events.slack_channel`, and `agents.last_heartbeat_at`,
   `agents.host`, `agents.pid`, `agents.version`, `agents.status_note`, `agents.current_task`.
3. Backfills `events.source = 'agent'`, since rows written before the column existed came from
   agents.
4. Logs what it applied: `schema migration added: events.source, events.slack_ts, …`. A
   database that is already current logs nothing and is a no-op.

**What it will never do:** drop, rename or retype anything. That narrowness is the policy, and
it is enforced by being the only thing implemented. A `NOT NULL` column with no default cannot
be added to a table that already has rows, so that case is logged as an error and skipped rather
than corrupting live data. The first change that needs more than this is the signal to adopt
Alembic.

**Back up first anyway.** The migration is additive and idempotent, but a five-second copy
against a several-month event log is not a trade worth thinking about:

```bash
sqlite3 data/agent_relay.db ".backup data/agent_relay.pre-v2.db"
```

Then upgrade and start:

```bash
git pull && uv sync           # or: docker compose up -d --build
agent-relay serve
```

**Verify:**

```bash
agent-relay health
agent-relay agents            # empty until agents start heartbeating — expected
agent-relay events --limit 5  # your V1 history, still there
```

**Behaviour changes to expect on a V1 config**, with nothing new configured:

| Change | Effect |
|---|---|
| The sweeper starts (300s) | A query every five minutes. It **releases nothing** — `AGENT_RELAY_CLAIM_EXPIRY_HOURS` is `0` |
| New endpoints appear | `/heartbeat`, `/agents`, `/claims/stale`, `/claims/sweep`, `/coordination/brief`, `/coordination/overview`, `/experiments`, `/a2a`, `/.well-known/agent.json` |
| `/dashboard` is served | On by default. Set `AGENT_RELAY_DASHBOARD=false` to turn it off |
| Webhook routes exist but answer `503` | Until their secrets are configured |
| `/coordination/brief` works with no API key | Returns the rule-based briefing, `source: "deterministic"` |

Every V1 endpoint, payload shape and CLI command still behaves identically. Nothing was removed.

**Rolling back** to V1 code against a V2 database works: the added columns are nullable (or
defaulted) and V1 simply never selects them. `ingest_records` is ignored. You lose the V2
features, not the data.

---

## 3. The scheduler

One `asyncio` task, started with the app and stopped with it, waking every 30 seconds to run
whatever is due. Jobs are isolated: one raising is logged (`scheduled job <name> failed: …`) and
the loop and the other jobs carry on. Startup logs the plan:
`scheduler started: {'sweep': '300s'}`.

| Job | Knob | Default | What it does |
|---|---|---|---|
| `sweep` | `AGENT_RELAY_SWEEPER_INTERVAL_SECONDS` | `300` (**on**) | Runs the claim-hygiene pass. `0` disables the job entirely |
| `poll` | `GITHUB_POLL_INTERVAL_SECONDS` | `0` (off) | One GitHub polling tick. Also needs `GITHUB_OWNER`/`GITHUB_REPO` |
| `status` | `AGENT_RELAY_STATUS_INTERVAL_MINUTES` | `0` (off) | One briefing per project posted to Slack. Also needs a non-empty `AGENT_RELAY_STATUS_PROJECTS` |

### 3.1 Claim hygiene

Two separate settings, and the separation is the point:

| Knob | Default | Decides |
|---|---|---|
| `AGENT_RELAY_CLAIM_STALE_HOURS` | `24` | What a human is **told about** (`GET /claims/stale`, the `still_stale` list) |
| `AGENT_RELAY_CLAIM_EXPIRY_HOURS` | `0` = never | What the relay is **allowed to take away** |

Out of the box the relay reports and never acts. Run it that way for a while: watch
`agent-relay stale` for a week, confirm the claims it flags really are abandoned, and only then
consider setting an expiry (`48` is a reasonable first value — comfortably longer than a long
run, comfortably shorter than a weekend).

An auto-release is a forced `RELEASE` posted by the agent name `relay`, with
`metadata: {"auto_released": true, "previous_owner": "<agent>", "idle_hours": <n>}`, so a human
reading the log can always tell a machine release from a colleague taking a task over. One
failing claim never aborts the pass: it is rolled back, logged, reported in `SweepReport.errors`
and counted as still-stale.

`POST /claims/sweep` (or `agent-relay sweep`) runs the same pass on demand, with the same rules.

### 3.2 Scheduled status posts

```dotenv
AGENT_RELAY_STATUS_INTERVAL_MINUTES=240
AGENT_RELAY_STATUS_PROJECTS=tether,drends
```

Each tick builds one brief per listed project and posts it — via `chat.postMessage` when a bot
token and a routed channel exist, otherwise via `SLACK_WEBHOOK_URL`, otherwise just logged. It
is **not** written back to the event log: a roll-up is a view of events, not an event, and
storing it would make the next roll-up summarise itself. See §7 for what it costs.

### 3.3 Turning it all off

```dotenv
AGENT_RELAY_SWEEPER_INTERVAL_SECONDS=0
GITHUB_POLL_INTERVAL_SECONDS=0
AGENT_RELAY_STATUS_INTERVAL_MINUTES=0
```

With no job enabled the scheduler task is never created.

---

## 4. Exposing the webhook routes safely

The relay is **not** designed to be a public service. There is no per-agent identity, and with
`AGENT_RELAY_API_TOKEN` unset there is no authentication at all. Only two routes ever need to be
reachable from the internet:

```
POST /webhooks/github
POST /webhooks/slack/events
```

Publish those two and nothing else.

### 4.1 Reverse proxy

Terminate TLS at nginx/Caddy/Traefik and proxy only the webhook paths to the relay. A minimal
nginx sketch:

```nginx
location = /webhooks/github        { proxy_pass http://127.0.0.1:8077; }
location = /webhooks/slack/events  { proxy_pass http://127.0.0.1:8077; }
location / { return 404; }
```

Two proxy requirements that are not optional:

- **Pass the body through byte-for-byte.** Both signatures are computed over the raw bytes. Any
  module that re-encodes, pretty-prints, or strips whitespace from JSON turns every delivery
  into a `401`.
- **Forward the signature headers**: `X-Hub-Signature-256` and `X-GitHub-Event` /
  `X-GitHub-Delivery` for GitHub; `X-Slack-Signature` and `X-Slack-Request-Timestamp` for
  Slack. Some proxies strip unknown `X-` headers by default.

Keep the relay itself bound to `127.0.0.1` (or a private interface) so the proxy is the only
path in.

### 4.2 Tunnel

For a laptop or a machine with no inbound connectivity, a tunnel (`cloudflared`, `ngrok`,
`tailscale funnel`) works and needs no proxy config. Two caveats: the URL usually changes on
restart, so both the GitHub hook and the Slack Request URL need re-pointing; and a tunnel
publishes the **whole** app, including `/events`, `/claim` and `/dashboard`. If you tunnel, set
`AGENT_RELAY_API_TOKEN` — the webhook routes still work (they are signature-authenticated), and
everything else demands the token.

### 4.3 No public URL at all

You do not need one. Use the GitHub poller (`GITHUB_POLL_INTERVAL_SECONDS=300`) and the outbound
Slack webhook, and skip inbound entirely. You lose merge/push/comment ingestion and the Slack
reply round trip; everything else is unchanged.

### 4.4 HMAC is the authentication

The webhook routes deliberately do **not** require the bearer token — GitHub and Slack cannot
send one, and pasting the team's shared secret into a third party's settings page would also
hand it `POST /events` and `POST /claim`. The signature is per-integration, rotatable without
touching a single agent, and proves the body was not modified. See
[`ARCHITECTURE.md`](ARCHITECTURE.md) §8.

Consequences worth being deliberate about:

- Verification is mandatory whenever the feature is on. Without a secret the route answers `503`
  rather than accepting anything; there is no "skip validation" switch.
- Slack additionally enforces a **5-minute timestamp window**, because a signature stays valid
  forever and a captured request could otherwise be replayed at any point. If deliveries start
  failing after a suspend/resume, check the clock.
- These routes always answer `200` for anything they understood-and-dropped. A `401` in a
  delivery log is a real signature failure, not a mapping problem.

### 4.5 Rotating secrets

| Secret | Rotate by | Downtime |
|---|---|---|
| `GITHUB_WEBHOOK_SECRET` | New value in the GitHub hook **and** `.env`, then restart | Deliveries between the two edits `401`. GitHub retries, and you can redeliver from **Recent Deliveries** afterwards |
| `SLACK_SIGNING_SECRET` | Regenerate in **Basic Information**, update `.env`, restart | Same; Slack retries |
| `SLACK_BOT_TOKEN` | Reinstall the app, update `.env`, restart | Posts fail (logged) meanwhile; no events are lost |
| `SLACK_WEBHOOK_URL` | Regenerate under **Incoming Webhooks**, update `.env`, restart | Posts fail (logged) meanwhile |
| `AGENT_RELAY_API_TOKEN` | Update `.env` **and** every agent's environment, restart | Agents using the old value get `401` until updated |
| `GITHUB_TOKEN` | Revoke, issue a new read-only token, restart | Link building is unaffected; only the read passthrough degrades |
| `ANTHROPIC_API_KEY` | Update `.env`, restart | Briefings fall back to deterministic meanwhile |

Nothing in the database depends on any of these, so rotation is always "edit `.env`, restart".

---

## 5. Backup and restore

The entire state is the SQLite file. Losing it loses the event log; nothing else is durable.

### 5.1 The WAL matters

The relay runs SQLite in **WAL mode**, so at any instant the state is spread across three files:

```
data/agent_relay.db        the main database
data/agent_relay.db-wal    committed pages not yet checkpointed
data/agent_relay.db-shm    shared-memory index for the WAL
```

Copying only `agent_relay.db` from a running relay can therefore miss recent commits — the
classic silent-data-loss backup. Use one of these instead.

**Online, consistent, no downtime (preferred):**

```bash
sqlite3 data/agent_relay.db ".backup '/backups/agent_relay-$(date +%F).db'"
# or, equivalently:
sqlite3 data/agent_relay.db "VACUUM INTO '/backups/agent_relay-$(date +%F).db'"
```

Both produce a single self-contained file with no `-wal`/`-shm` companion. That file is the
backup.

**Cold copy:** stop the relay, then copy **all three** files (or run the checkpoint above first
and copy just the `.db`).

**Never** copy the live `.db` alone while the relay is running.

### 5.2 Docker

```bash
docker compose exec relay python - <<'PY'
import sqlite3; sqlite3.connect("/data/agent_relay.db").execute("VACUUM INTO '/data/backup.db'")
PY
docker compose cp relay:/data/backup.db ./agent_relay-$(date +%F).db
```

(The runtime image has no `sqlite3` CLI; Python's `sqlite3` module is there.) Or stop the
container and copy the whole `relay-data` volume.

### 5.3 Restore

```bash
# 1. stop the relay
docker compose stop relay          # or kill the serve process
# 2. move the current files aside — do not delete them yet
mv data/agent_relay.db{,.broken}; rm -f data/agent_relay.db-wal data/agent_relay.db-shm
# 3. put the backup in place
cp /backups/agent_relay-2026-09-07.db data/agent_relay.db
# 4. start; migrate.py runs and is a no-op on a current schema
docker compose start relay && curl -s localhost:8077/health | jq .status
```

Restoring a backup taken from an **older** version is fine: the boot migration brings it
forward. Restoring one from a **newer** version into older code is also fine (§2).

Verify afterwards:

```bash
agent-relay events --limit 5
sqlite3 data/agent_relay.db "PRAGMA integrity_check; SELECT count(*) FROM events;"
```

### 5.4 What is worth backing up besides the database

Only `.env` — and it holds every secret you have, so put it wherever your other credentials
live, not next to the database dumps. Everything else is in git.

---

## 6. The security model, stated plainly

Read this before putting the relay anywhere but a trusted network.

- **The network is the security boundary.** The relay is built for a LAN, a VPN or localhost.
  It is not hardened for the public internet, and nothing in it is a substitute for that.
- **Authentication is one of three modes.** Tailnet identity (`AGENT_RELAY_TAILSCALE_AUTH`), a
  shared token (`AGENT_RELAY_API_TOKEN`), or open. See §6.1 — if everyone reaches the relay over
  Tailscale, identity is strictly better than the token and costs nothing to run.
- **There is still no per-*agent* identity.** `agent` is a string in the request body, and that
  is deliberate: one person legitimately runs `leo-claude` and `leo-codex` at once. What tailnet
  identity pins down is the *human* behind them. With only a shared token, nothing proves either.
- **Anyone who gets in can force-release a claim.** There is no ownership check beyond the
  claim's own `agent` field, which the caller supplies.
- **Everything is readable to anyone who gets in.** There is no per-project access control and
  no redaction of stored content. Do not put secrets in event summaries, details or artifacts —
  the log is append-only, so there is no edit and no delete, and events are mirrored to Slack.
- **Three routes are open even with a token set**, by design: `/health`,
  `/.well-known/agent.json` and `/dashboard` (the HTML page only — its `fetch` calls hit the
  token-protected API and prompt the viewer for the token, which it keeps in the browser's
  `localStorage`). `/dashboard/projects` is token-protected like every other API call.
- **The two webhook routes authenticate by signature instead** (§4.4), which is stronger than
  the token for that purpose, not weaker.
- **The relay never writes to GitHub.** A read-only fine-grained token is sufficient and is what
  you should issue.
- **Ingested activity cannot impersonate an agent.** GitHub and Slack authors are namespaced
  (`github:leonardo`, `slack:U0LEO`) and `events.source` is stamped by the relay after the event
  is created, never taken from the payload.

If you need more than this — real per-user auth, per-project isolation, an audit trail of who
read what — this is the wrong tool, and saying so is cheaper than bolting it on.

---

### 6.1 Tailnet identity

If every caller reaches the relay over Tailscale — the deployment this is built for — you can
delete the shared token entirely:

```bash
AGENT_RELAY_TAILSCALE_AUTH=true
AGENT_RELAY_OWNER_MAP=leonardo.gameplay666@gmail.com=leonardo,nic@example.com=niccolo
```

The peer is authenticated by WireGuard before the first byte reaches the relay, so the relay just
asks `tailscale whois` who owns the calling address. What that buys:

| | Shared token | Tailnet identity |
|---|---|---|
| Secret to distribute and rotate | Yes | **None** |
| Who is calling | Unknown | The actual account |
| `human_owner` | Self-declared, can be anything | **Set from identity; a declared value is overridden** |
| Works off-tailnet | Yes | No — that is the point |

That last row is the substantive change. With a token, an agent can post `human_owner: andrea`
and the log will say andrea. With identity on, the relay overwrites it with who the caller
actually is. Verified against the real tailnet: a request declaring `andrea` from Leonardo's
machine is recorded as `leonardo`.

**Two conditions, both hard:**

1. **The relay must be reached directly over the tailnet.** Behind a reverse proxy every request
   appears to come from the proxy, and `X-Forwarded-For` is set by the caller — trusting it would
   let anyone assume any identity, so the relay reads the socket peer only and refuses everyone
   instead. That is the safe failure, but it means "put nginx in front of it" breaks identity.
2. **The `tailscale` CLI must be on the relay host's PATH** (`AGENT_RELAY_TAILSCALE_BINARY` to
   override). Lookups are cached for five minutes, so this is not a subprocess per request.

Set `AGENT_RELAY_ALLOW_TOKEN=true` to keep accepting the shared token as a fallback. It is off by
default on purpose: with identity on, a token that still works is a way to stay anonymous.

Anything on the relay host itself resolves to the host's own tailnet identity, so a shell on that
box is equivalent to being that user. That is the same trust you already grant by running the
service there.

## 7. What the coordinator costs

The LLM coordinator is the only thing in the relay that costs money, and it is off entirely
without a credential. When it is off, `/coordination/brief` still works and returns a
rule-based briefing.

### 7.1 If you only have a Claude subscription

A Claude subscription (Pro/Max) powers Claude Code — it is **not** API access, and there is no
key to extract from it. Most small teams are in exactly this position, and nothing here is
blocked by it:

| | Needs an API key? |
|---|---|
| Everything else in the relay | No |
| `agent-relay brief` / `GET /coordination/brief` | No — falls back to the rule-based briefing |
| Prose written by a model | Yes, *or* see below |

The fallback is not a stub. It names who is working on what, what is blocked and why, which
questions are unanswered and for how long, the recent findings, and the next actions — all from
the deterministic rules:

```console
$ agent-relay brief --project tether
=== BRIEF · tether (last 72h · rule-based) ===

Working now: leo-codex on GH-142; niccolo-claude on GH-138. Blocked: GH-151 (andrea-agent) —
missing depth-encoder checkpoint. Open questions: Q-3 leo-codex→niccolo-claude (0.0h).
Findings: [GH-142] EPE improved 0.7% on SCARED-C — leo-codex. Next: resolve Q-3 …
```

**Want prose anyway? Let one of your own agents write it.** Your agents already are LLMs with
subscription access, so the relay does not need its own. `summary --json` is about 900
characters and is built to be handed straight to an agent:

```bash
agent-relay summary --project tether --json
```

Point a Claude Code session at that each morning with an instruction like *"write the team
standup from this snapshot; invent nothing"* — the same constraint the built-in coordinator
uses. Costs nothing beyond the subscription you already pay for.

If you later get Console API access, set `ANTHROPIC_API_KEY` (or `ANTHROPIC_AUTH_TOKEN`, or run
`ant auth login` and set `AGENT_RELAY_COORDINATOR=true`) and the endpoint starts returning
`source: "llm"` with no other change.

### 7.2 What a call costs when you do have a key

**When a call happens** — and only then:

| Trigger | Frequency |
|---|---|
| `GET /coordination/brief` (`agent-relay brief`) | Once per request, on demand |
| The scheduler's `status` job | Once per project in `AGENT_RELAY_STATUS_PROJECTS`, per interval |

**What one call is.** The input is the flattened deterministic summary of one project — active
agents and their tasks, blockers, conflicts, open questions, recent findings and decisions, idle
claims, suggested actions — plus a short system prompt. That is a small number of lines, not the
event log. The output is capped by `COORDINATOR_MAX_TOKENS` (16000) and the prompt asks for
~200 words, so the real output is far smaller.

**What keeps it small:**

- `COORDINATOR_EFFORT` defaults to `low`. A status brief is a small job; `low` keeps the cost
  negligible without hurting the result. Raise it only if you can point at a briefing that was
  actually worse for it.
- A quiet project never reaches the model at all — `is_quiet()` short-circuits to the
  deterministic briefing, so an idle weekend costs nothing.
- Every failure path (no key, extra not installed, rate limit, API error, connection error,
  refusal, empty response) falls back rather than retrying.

**Budgeting the scheduled post.** Calls per day = (projects in `AGENT_RELAY_STATUS_PROJECTS`) ×
(1440 / `AGENT_RELAY_STATUS_INTERVAL_MINUTES`), minus every tick where a project was quiet. Two
projects every four hours is 12 calls a day, worst case. Start there rather than at hourly.

If you want the feature switched off but the endpoint kept, unset `ANTHROPIC_API_KEY`: `brief`
keeps working, `source` reads `deterministic`, and `model` is `null`.

---

## 8. When something breaks

Always start here:

```bash
agent-relay health          # or: curl -s localhost:8077/health | jq
```

`status` is `ok`/`degraded`, `database` is the result of a live `SELECT 1`, and `integrations`
reports `slack`, `github`, `auth` and the redacted `db_url`. Then read the log — it is the
diagnostic surface, and `AGENT_RELAY_LOG_LEVEL=DEBUG` widens it. The startup line prints the
full non-secret configuration summary (`slack_bot`, `slack_events`, `github_webhooks`,
`github_polling`, `coordinator_llm`, `auto_release`, `dashboard`), which answers "is this
feature even on" faster than reading `.env`.

### By subsystem

| Subsystem | Symptom | Check, in order |
|---|---|---|
| **Relay itself** | CLI says it cannot reach the relay | Is the process up? Is `AGENT_RELAY_URL` right (agents on other machines cannot use `127.0.0.1`)? Is the relay bound to `127.0.0.1` when it should be `0.0.0.0`? |
| | `401` on everything | `AGENT_RELAY_API_TOKEN` is set on the server but missing or stale in the agent's environment |
| | `database: "error: …"` in `/health` | Disk full, file permissions, or the SQLite path is not writable. Docker: is the volume mounted at `/data` and is `AGENT_RELAY_DB_URL` the four-slash absolute form? |
| | A config change had no effect | Settings are cached at startup — restart |
| **Claims** | `409` nobody expected | `agent-relay tasks --project P --status claimed` and `agent-relay agents` — is the owner `offline`? |
| | A claim never goes stale | `last_activity_at` is refreshed by any event on the task *and* by a heartbeat naming the same project and task. An agent heartbeating with `--task` keeps its claim fresh by design |
| | Nothing is auto-released | Expected: `AGENT_RELAY_CLAIM_EXPIRY_HOURS` is `0` by default. `agent-relay sweep --json` shows `auto_release_enabled` |
| **Presence** | Every agent is `unknown` | Nobody is sending heartbeats. `unknown` ≠ `offline`; it means "no evidence" |
| | An agent is `offline` but working | Its loop stopped heartbeating (a long blocking call). Heartbeat at natural pauses, or raise `AGENT_RELAY_HEARTBEAT_IDLE_SECONDS` |
| **Scheduler** | Nothing runs | Startup log has no `scheduler started` line → every job is disabled. `--reload` restarts it on each code change |
| | `scheduled job X failed` repeatedly | The message names the exception type. The other jobs are unaffected |
| **GitHub ingestion** | `503`/`401`/`200 ignored` in Recent Deliveries | [`INTEGRATIONS.md`](INTEGRATIONS.md) §1.7 |
| | Polling ingests nothing | `GITHUB_POLL_INTERVAL_SECONDS > 0`, `GITHUB_OWNER`/`GITHUB_REPO` set, token valid. It only announces open PRs, and only ones not already in `ingest_records` |
| **Slack outbound** | No messages | `/health` `slack: enabled`? `SLACK_EVENT_TYPES` filtering the type out? Slack answers `200 {"ok": false}` on failure, so read the log line, not the status |
| | `429 rate_limited` | Incoming webhooks throttle around one message per second. Narrow `SLACK_EVENT_TYPES`; there are no retries, and the events are still in the relay |
| **Slack inbound** | Request URL will not verify | [`INTEGRATIONS.md`](INTEGRATIONS.md) §2.6 |
| | Replies ingested but questions stay open | The thread's anchor was not a `QUESTION` |
| **Coordinator** | `source` is always `deterministic` | No `ANTHROPIC_API_KEY`; the `coordinator` extra not installed (logged: "the anthropic SDK is not installed"); a rate limit or API error (logged); or the project is quiet |
| | Effort setting ignored | `COORDINATOR_EFFORT` must be one of `low\|medium\|high\|xhigh\|max`; a typo warns and falls back to `low` |
| **Dashboard** | `404` | `AGENT_RELAY_DASHBOARD=false` |
| | Page loads, panels are empty, token prompt keeps returning | The token the browser stored is wrong. Clear it with the button on the page (it lives in `localStorage` under `agent_relay_token`) |
| **MCP** | Tools missing or writes rejected | [`INTEGRATIONS.md`](INTEGRATIONS.md) §3.4 — usually `mcp` not installed, or `AGENT_NAME` missing from the client's `env` block |
| **A2A** | `-32601` | Only `message/send` exists |
| | `unsupported` results | The text did not match one of the three intents; the reply lists what does |

### Reading the database directly

The JSON columns are stored as TEXT precisely so this works:

```bash
sqlite3 data/agent_relay.db \
  "SELECT id, created_at, source, event_type, agent, project, task, summary
     FROM events ORDER BY id DESC LIMIT 20;"
sqlite3 data/agent_relay.db \
  "SELECT source, external_id, event_id, received_at FROM ingest_records ORDER BY id DESC LIMIT 20;"
sqlite3 data/agent_relay.db \
  "SELECT name, last_heartbeat_at, current_task, status_note FROM agents;"
```

Read-only queries against a live relay are safe under WAL. Do not write to the file behind the
relay's back — the event log is append-only for a reason, and `task_claims` invariants are
enforced in the service layer, not by you at a `sqlite3` prompt.

---

## See also

- [`INTEGRATIONS.md`](INTEGRATIONS.md) — setting up GitHub, Slack, MCP, A2A and experiment links
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — §5 inbound paths, §7 the LLM's place, §8 trust boundaries
- [`SLACK_SETUP.md`](SLACK_SETUP.md) — the outbound webhook path and message rendering
- [`AGENT_PROTOCOL.md`](AGENT_PROTOCOL.md) — what agents are expected to do
