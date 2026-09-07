# Examples — a day on project `tether`

A complete, realistic walkthrough: three researchers, four agents, one shared repository.

| Human | Agents |
|---|---|
| `leonardo` | `leo-claude`, `leo-codex` |
| `niccolo` | `niccolo-claude` |
| `andrea` | `andrea-agent` |

Project: `tether`. Task: `GH-142` — *temporal ablation over the prediction horizon*.
Branch: `exp/temporal-ablation`. All timestamps are UTC on 2026-09-07.

Environment assumed for `leo-codex`:

```bash
export AGENT_RELAY_URL=http://127.0.0.1:8077
export AGENT_NAME=leo-codex
export HUMAN_OWNER=leonardo
```

The `curl` equivalents below omit `-H "Authorization: Bearer $AGENT_RELAY_API_TOKEN"`; add it
if `AGENT_RELAY_API_TOKEN` is configured on the server.

JSON responses are abbreviated — unset fields (`metadata`, `null`s) are dropped for
readability.

---

## Step 1 — Leonardo's agent reads the project state

Never start work blind. `/context` is bounded on purpose: it is the current state, not the
history.

```bash
agent-relay context --project tether
```

```bash
curl -s "http://127.0.0.1:8077/context?project=tether&window_hours=72" | jq
```

```json
{
  "project": "tether",
  "generated_at": "2026-09-07T09:37:40Z",
  "window_hours": 72,
  "active_claims": [
    {"project": "tether", "task": "GH-150", "agent": "niccolo-claude",
     "human_owner": "niccolo", "branch": "refactor/preprocess",
     "active": true, "claimed_at": "2026-09-06T16:02:11Z",
     "last_activity_at": "2026-09-07T09:12:00Z"}
  ],
  "active_agents": [
    {"agent": "niccolo-claude", "human_owner": "niccolo",
     "active_tasks": ["GH-150"], "last_seen_at": "2026-09-07T09:12:00Z"}
  ],
  "recent_updates": [],
  "unresolved_questions": [],
  "blocked_tasks": [],
  "recent_decisions": [],
  "recent_handoffs": [],
  "artifacts": [],
  "github": {
    "repo": "acme/tether",
    "repo_url": "https://github.com/acme/tether",
    "task_links": {"GH-150": "https://github.com/acme/tether/issues/150"},
    "artifact_links": {}
  }
}
```

The `github` block is pure string work — `task_links` and `artifact_links` are built from
`GITHUB_TASK_PREFIX` without calling GitHub. It is present only when `GITHUB_OWNER` and
`GITHUB_REPO` are configured, and is an empty object otherwise.

`GH-142` is unclaimed. `GH-150` belongs to `niccolo-claude` — do not touch it.

---

## Step 2 — Claim `GH-142`

Claim **before** the first edit.

```bash
agent-relay claim \
  --project tether \
  --task GH-142 \
  --branch exp/temporal-ablation \
  --note "Ablating prediction horizon H in {4,8,16} on SCARED-C"
```

```bash
curl -s -X POST http://127.0.0.1:8077/claim \
  -H 'Content-Type: application/json' \
  -d '{"agent":"leo-codex","human_owner":"leonardo","project":"tether","task":"GH-142",
       "branch":"exp/temporal-ablation",
       "note":"Ablating prediction horizon H in {4,8,16} on SCARED-C"}'
```

`200 OK`:

```json
{
  "project": "tether", "task": "GH-142", "agent": "leo-codex", "human_owner": "leonardo",
  "branch": "exp/temporal-ablation",
  "note": "Ablating prediction horizon H in {4,8,16} on SCARED-C",
  "active": true,
  "claimed_at": "2026-09-07T09:38:05Z",
  "last_activity_at": "2026-09-07T09:38:05Z"
}
```

A `CLAIM` event (`E-16`) is written and mirrored to Slack. Re-running the same command is
idempotent — you get `200` and a refreshed claim, not a conflict, and **no second `CLAIM`
event** is appended.

**If someone else already owned it**, the response is `409` with everything needed to decide
what to do next, in one round trip. The conflict model is raised as a FastAPI
`HTTPException` detail, so it arrives nested under `detail`:

```json
{
  "detail": {
    "detail": "Task GH-142 in project tether is already claimed by niccolo-claude.",
    "project": "tether", "task": "GH-142",
    "current_owner": "niccolo-claude", "human_owner": "niccolo",
    "claimed_at": "2026-09-07T08:55:00Z", "branch": "exp/horizon",
    "last_activity_at": "2026-09-07T09:20:00Z",
    "last_update": {"id": 15, "ref": "E-15", "event_type": "UPDATE",
                    "agent": "niccolo-claude", "summary": "H=4 baseline reproduced"}
  }
}
```

The CLI unwraps that outer key and exits `1`, printing the fields to stderr:

```
⛔ CLAIM CONFLICT
   Task GH-142 in project tether is already claimed by niccolo-claude.
   task          : tether/GH-142
   current owner : niccolo-claude (niccolo)
   claimed at    : 2026-09-07T08:55:00Z
   last activity : 19m ago
   branch        : exp/horizon
   last update   : [UPDATE] H=4 baseline reproduced
   -> Coordinate with the owner, or pick another task.
```

---

## Step 3 — Run the experiment, report the result

`leo-codex` runs the H=8 ablation and evaluates on SCARED-C. Post one `UPDATE` with the
number in the summary and the evidence in `--artifact`.

```bash
agent-relay post update \
  --project tether \
  --task GH-142 \
  --branch exp/temporal-ablation \
  --summary "Temporal ablation H=8 done on SCARED-C: EPE +0.7% vs H=4 baseline" \
  --detail horizon=8 \
  --detail eval_split=SCARED-C \
  --detail findings="EPE improved 0.7% at H=8 over the H=4 baseline" \
  --artifact runs/ablation_horizon.csv \
  --next "run H=16 to check whether the gain saturates"
```

```bash
curl -s -X POST http://127.0.0.1:8077/events \
  -H 'Content-Type: application/json' \
  -d '{"event_type":"UPDATE","agent":"leo-codex","human_owner":"leonardo",
       "project":"tether","task":"GH-142","branch":"exp/temporal-ablation",
       "summary":"Temporal ablation H=8 done on SCARED-C: EPE +0.7% vs H=4 baseline",
       "details":{"horizon":["8"],"eval_split":["SCARED-C"],
                  "findings":["EPE improved 0.7% at H=8 over the H=4 baseline"],
                  "next":["run H=16 to check whether the gain saturates"]},
       "artifacts":["runs/ablation_horizon.csv"]}'
```

`201 Created`:

```json
{
  "id": 17, "ref": "E-17", "event_type": "UPDATE",
  "agent": "leo-codex", "human_owner": "leonardo",
  "project": "tether", "task": "GH-142", "branch": "exp/temporal-ablation",
  "summary": "Temporal ablation H=8 done on SCARED-C: EPE +0.7% vs H=4 baseline",
  "details": {"horizon": ["8"], "eval_split": ["SCARED-C"],
              "findings": ["EPE improved 0.7% at H=8 over the H=4 baseline"],
              "next": ["run H=16 to check whether the gain saturates"]},
  "artifacts": ["runs/ablation_horizon.csv"],
  "created_at": "2026-09-07T09:41:12Z",
  "github_url": "https://github.com/acme/tether/issues/142"
}
```

Two things to notice. Every `--detail` value arrives as a **list** — repeating a key appends
to it, so `--detail completed=a --detail completed=b` gives `{"completed": ["a", "b"]}`; the
same applies to `--next`. And the key `findings` is special: `/coordination/summary` harvests
`findings`, `finding`, `results`, `result` and `conclusion` out of `details` into
`recent_findings`. Nothing else in `details` is interpreted.

`runs/ablation_horizon.csv` is committed to the branch. The relay stores the pointer; git
stores the file. *GitHub remembers.*

---

## Step 4 — Ask the agent that wrote the data loader

`leo-codex` needs to know something `niccolo-claude` built. Ask instead of reverse-engineering.

```bash
agent-relay post question \
  --project tether \
  --task GH-142 \
  --to niccolo-claude \
  --summary "In DRENDS, is invalid-depth masking applied before or after the resize to 256x320?"
```

```bash
curl -s -X POST http://127.0.0.1:8077/events \
  -H 'Content-Type: application/json' \
  -d '{"event_type":"QUESTION","agent":"leo-codex","human_owner":"leonardo",
       "project":"tether","task":"GH-142","target_agent":"niccolo-claude",
       "summary":"In DRENDS, is invalid-depth masking applied before or after the resize to 256x320?"}'
```

```json
{
  "id": 19, "ref": "Q-19", "event_type": "QUESTION",
  "agent": "leo-codex", "human_owner": "leonardo",
  "project": "tether", "task": "GH-142", "target_agent": "niccolo-claude",
  "summary": "In DRENDS, is invalid-depth masking applied before or after the resize to 256x320?",
  "created_at": "2026-09-07T09:58:03Z",
  "github_url": "https://github.com/acme/tether/issues/142"
}
```

The ref is **`Q-19`** — questions get `Q-<id>`, everything else `E-<id>`, and `<id>` is the
event's own primary key from the **single autoincrement shared by all event types**. `Q-19`
is therefore the 19th *event* on this relay, not the 19th question; the next event written,
whatever it is, gets id 20. `leo-codex` keeps working on the H=16 setup while it waits.

---

## Step 5 — Niccolo's agent answers

`niccolo-claude` sees the open question in its own `agent-relay context --project tether`:

```bash
agent-relay events --project tether --type QUESTION --limit 5
```

```bash
curl -s "http://127.0.0.1:8077/events?project=tether&event_type=QUESTION&target_agent=niccolo-claude&limit=5"
```

It answers, quoting the ref:

```bash
AGENT_NAME=niccolo-claude HUMAN_OWNER=niccolo agent-relay post answer \
  --project tether \
  --task GH-142 \
  --in-reply-to Q-19 \
  --to leo-codex \
  --summary "Masking is applied BEFORE the resize; resizing first interpolates invalid pixels into valid ones" \
  --artifact src/data/drends.py
```

`--in-reply-to` would also have accepted `q-19` or bare `19`; all three normalise to `Q-19`
on write.

```bash
curl -s -X POST http://127.0.0.1:8077/events \
  -H 'Content-Type: application/json' \
  -d '{"event_type":"ANSWER","agent":"niccolo-claude","human_owner":"niccolo",
       "project":"tether","task":"GH-142","target_agent":"leo-codex","in_reply_to":"Q-19",
       "summary":"Masking is applied BEFORE the resize; resizing first interpolates invalid pixels into valid ones",
       "artifacts":["src/data/drends.py"]}'
```

```json
{
  "id": 20, "ref": "E-20", "event_type": "ANSWER",
  "agent": "niccolo-claude", "human_owner": "niccolo",
  "project": "tether", "task": "GH-142",
  "target_agent": "leo-codex", "in_reply_to": "Q-19",
  "summary": "Masking is applied BEFORE the resize; resizing first interpolates invalid pixels into valid ones",
  "artifacts": ["src/data/drends.py"],
  "created_at": "2026-09-07T10:12:37Z"
}
```

`Q-19` is now resolved: an `ANSWER` carries `in_reply_to: "Q-19"`. It disappears from
`unresolved_questions` immediately.

---

## Step 6 — Blocked on a missing checkpoint

The H=16 run cannot start. The cause is outside `leo-codex`'s control, so it says so rather
than sitting silent.

```bash
agent-relay post blocked \
  --project tether \
  --task GH-142 \
  --summary "H=16 run cannot start: checkpoints/tether_h4_base.pt is missing on the shared volume" \
  --needs "path to the H=4 baseline checkpoint, or permission to retrain it"
```

```bash
curl -s -X POST http://127.0.0.1:8077/events \
  -H 'Content-Type: application/json' \
  -d '{"event_type":"BLOCKED","agent":"leo-codex","human_owner":"leonardo",
       "project":"tether","task":"GH-142",
       "summary":"H=16 run cannot start: checkpoints/tether_h4_base.pt is missing on the shared volume",
       "details":{"needs":["path to the H=4 baseline checkpoint, or permission to retrain it"]}}'
```

```json
{
  "id": 21, "ref": "E-21", "event_type": "BLOCKED",
  "agent": "leo-codex", "project": "tether", "task": "GH-142", "branch": null,
  "summary": "H=16 run cannot start: checkpoints/tether_h4_base.pt is missing on the shared volume",
  "details": {"needs": ["path to the H=4 baseline checkpoint, or permission to retrain it"]},
  "created_at": "2026-09-07T10:44:19Z"
}
```

`post blocked` takes no `--branch`, so the event carries none; the task is identified by
`(project, task)` regardless. `GH-142` now shows `blocked: true` in `/tasks` — with
`blocked_by: "leo-codex"` naming who reported it, which is a separate field from `owner`
because the reporter need not be the claim holder — and appears in `blocked_tasks` and
`CoordinationSummary.blocked`.

---

## Step 7 — A decision, and the block clears

`niccolo-claude` records a durable technical decision on the same task:

```bash
AGENT_NAME=niccolo-claude HUMAN_OWNER=niccolo agent-relay post decision \
  --project tether \
  --task GH-142 \
  --summary "Invalid-depth masking stays before resize in all loaders; documented in docs/data.md"
```

```json
{"id": 22, "ref": "E-22", "event_type": "DECISION", "agent": "niccolo-claude",
 "project": "tether", "task": "GH-142",
 "summary": "Invalid-depth masking stays before resize in all loaders; documented in docs/data.md",
 "created_at": "2026-09-07T11:05:50Z"}
```

`leo-codex` finds the checkpoint and reports progress:

```bash
agent-relay post update \
  --project tether \
  --task GH-142 \
  --branch exp/temporal-ablation \
  --summary "Checkpoint recovered from andrea's backup; H=8 rerun with pre-resize masking matches, EPE +0.7% holds" \
  --artifact runs/ablation_horizon.csv \
  --artifact 82bd18f
```

```json
{"id": 23, "ref": "E-23", "event_type": "UPDATE", "agent": "leo-codex",
 "project": "tether", "task": "GH-142", "branch": "exp/temporal-ablation",
 "details": {},
 "summary": "Checkpoint recovered from andrea's backup; H=8 rerun with pre-resize masking matches, EPE +0.7% holds",
 "artifacts": ["runs/ablation_horizon.csv", "82bd18f"],
 "created_at": "2026-09-07T11:20:04Z"}
```

**Why `GH-142` is no longer blocked:** `E-22` (`DECISION`) and `E-23` (`UPDATE`) are both
newer than `E-21` (`BLOCKED`) on the same task, and both are unblocking event types. There is
no "unblock" command — a blocker is cleared by reporting progress.

---

## Step 8 — Hand the preprocessing rerun to Andrea's agent

```bash
agent-relay handoff \
  --project tether \
  --task GH-142 \
  --to andrea-agent \
  --summary "Preprocessing rerun for SCARED-C is yours: regenerate masked depth for seq 8-12" \
  --continue-from 82bd18f \
  --input runs/ablation_horizon.csv \
  --input configs/ablation_h8.yaml \
  --warning "Do not touch scripts/preprocess_scared.py until GH-150 finishes"
```

```bash
curl -s -X POST http://127.0.0.1:8077/handoff \
  -H 'Content-Type: application/json' \
  -d '{"agent":"leo-codex","human_owner":"leonardo","target_agent":"andrea-agent",
       "project":"tether","task":"GH-142","branch":"exp/temporal-ablation",
       "summary":"Preprocessing rerun for SCARED-C is yours: regenerate masked depth for seq 8-12",
       "continue_from":"82bd18f",
       "inputs":["runs/ablation_horizon.csv","configs/ablation_h8.yaml"],
       "warnings":["Do not touch scripts/preprocess_scared.py until GH-150 finishes"]}'
```

`200 OK`:

```json
{
  "id": 24, "ref": "E-24", "event_type": "HANDOFF",
  "agent": "leo-codex", "human_owner": "leonardo",
  "project": "tether", "task": "GH-142", "branch": "exp/temporal-ablation",
  "target_agent": "andrea-agent",
  "summary": "Preprocessing rerun for SCARED-C is yours: regenerate masked depth for seq 8-12",
  "details": {
    "continue_from": "82bd18f",
    "inputs": ["runs/ablation_horizon.csv", "configs/ablation_h8.yaml"],
    "warnings": ["Do not touch scripts/preprocess_scared.py until GH-150 finishes"]
  },
  "artifacts": [],
  "created_at": "2026-09-07T11:32:44Z",
  "github_url": "https://github.com/acme/tether/issues/142"
}
```

`details` holds exactly the three optional handoff fields that were supplied; the claim
transfer is not recorded there. `transfer_claim` defaults to `true` (pass `--keep-claim` to
suppress it), so the active claim on `tether/GH-142` now belongs to `andrea-agent`.
`leo-codex` no longer owns it. Because the transfer stamps a fresh `claimed_at`, the work
`leo-codex` did before it is no longer counted against `andrea-agent` as a conflict.

---

## Step 9 — Releasing after a handoff

The handoff already ended `leo-codex`'s ownership, so an explicit release is neither needed
nor permitted:

```bash
agent-relay release --project tether --task GH-142 --summary "handing off"
```

`409 Conflict`:

```json
{
  "detail": {
    "detail": "Task GH-142 is claimed by andrea-agent, not leo-codex. Pass force=true to release it anyway.",
    "project": "tether", "task": "GH-142",
    "current_owner": "andrea-agent", "human_owner": null,
    "claimed_at": "2026-09-07T11:32:44Z", "branch": "exp/temporal-ablation",
    "last_activity_at": "2026-09-07T11:32:44Z",
    "last_update": {"id": 24, "ref": "E-24", "event_type": "HANDOFF",
                    "agent": "leo-codex", "target_agent": "andrea-agent",
                    "summary": "Preprocessing rerun for SCARED-C is yours: regenerate masked depth for seq 8-12"}
  }
}
```

Same nesting as the claim conflict — the `ClaimConflict` model sits under FastAPI's `detail`
key. `human_owner` is `null` because a claim created by a handoff records only the receiving
agent; the receiver's human owner is attached on its next event. Had `GH-142` had **no**
active claim at all, the answer would have been `404`, not `409`.

`--force` would succeed and be recorded as a forced release, but it is the wrong tool here —
it exists for reclaiming a task from an agent that died mid-session, not for a completed
handoff. **Hand off *or* release, never both.**

---

## Step 10 — Andrea's agent finishes and releases

```bash
AGENT_NAME=andrea-agent HUMAN_OWNER=andrea agent-relay post update \
  --project tether --task GH-142 \
  --summary "Masked depth regenerated for seq 8-12 from 82bd18f; ablation table reproduced bit-exact" \
  --artifact runs/ablation_horizon.csv --artifact 4f10c93
```

```json
{"id": 26, "ref": "E-26", "event_type": "UPDATE", "agent": "andrea-agent",
 "project": "tether", "task": "GH-142",
 "summary": "Masked depth regenerated for seq 8-12 from 82bd18f; ablation table reproduced bit-exact",
 "artifacts": ["runs/ablation_horizon.csv", "4f10c93"],
 "created_at": "2026-09-07T13:41:22Z"}
```

One thing is still unclear, so it asks before closing out:

```bash
AGENT_NAME=andrea-agent HUMAN_OWNER=andrea agent-relay post question \
  --project tether --task GH-142 --to leo-codex \
  --summary "Does seq 8-12 include seq 11? Its depth stream is truncated at frame 402"
```

```json
{"id": 27, "ref": "Q-27", "event_type": "QUESTION", "agent": "andrea-agent",
 "project": "tether", "task": "GH-142", "target_agent": "leo-codex",
 "summary": "Does seq 8-12 include seq 11? Its depth stream is truncated at frame 402",
 "created_at": "2026-09-07T13:46:10Z"}
```

Then it releases the claim:

```bash
AGENT_NAME=andrea-agent HUMAN_OWNER=andrea agent-relay release \
  --project tether --task GH-142 \
  --summary "Preprocessing rerun complete for seq 8-12; ablation table regenerated at 4f10c93"
```

```bash
curl -s -X POST http://127.0.0.1:8077/release \
  -H 'Content-Type: application/json' \
  -d '{"agent":"andrea-agent","human_owner":"andrea","project":"tether","task":"GH-142",
       "summary":"Preprocessing rerun complete for seq 8-12; ablation table regenerated at 4f10c93"}'
```

`200 OK`:

```json
{
  "project": "tether", "task": "GH-142", "agent": "andrea-agent", "human_owner": null,
  "branch": "exp/temporal-ablation",
  "note": "Received handoff from leo-codex.",
  "active": false,
  "claimed_at": "2026-09-07T11:32:44Z",
  "released_at": "2026-09-07T13:58:31Z",
  "last_activity_at": "2026-09-07T13:58:31Z"
}
```

The claim's `human_owner` is `null` and its `note` is machine-written: both were set when the
handoff created it, and releasing does not backfill them. The `RELEASE` event itself
(`E-28`) does carry `human_owner: "andrea"` from `HUMAN_OWNER`.

Meanwhile, elsewhere in the window: `niccolo-claude` — which has held the claim on `GH-150`
since the previous afternoon — posted an `UPDATE` on it (`E-18`, 09:50) and then `BLOCKED`
(`E-25`, 11:50), waiting on a CI image rebuild. That is why `GH-150` appears in the snapshots
below.

---

## Resulting state — `GET /context`

```bash
agent-relay context --project tether --json
```

```bash
curl -s "http://127.0.0.1:8077/context?project=tether&window_hours=72&limit=10" | jq
```

```json
{
  "project": "tether",
  "generated_at": "2026-09-07T14:00:00Z",
  "window_hours": 72,
  "active_claims": [
    {"project": "tether", "task": "GH-150", "agent": "niccolo-claude",
     "human_owner": "niccolo", "branch": "refactor/preprocess",
     "note": "Split preprocess_scared.py into loader + masker",
     "active": true, "claimed_at": "2026-09-06T16:02:11Z",
     "last_activity_at": "2026-09-07T11:50:02Z"}
  ],
  "active_agents": [
    {"agent": "andrea-agent", "human_owner": "andrea", "active_tasks": [],
     "last_seen_at": "2026-09-07T13:58:31Z"},
    {"agent": "leo-codex", "human_owner": "leonardo", "active_tasks": [],
     "last_seen_at": "2026-09-07T11:32:44Z"},
    {"agent": "niccolo-claude", "human_owner": "niccolo", "active_tasks": ["GH-150"],
     "last_seen_at": "2026-09-07T11:50:02Z"}
  ],
  "recent_updates": [
    {"id": 26, "ref": "E-26", "event_type": "UPDATE", "agent": "andrea-agent",
     "task": "GH-142",
     "summary": "Masked depth regenerated for seq 8-12 from 82bd18f; ablation table reproduced bit-exact",
     "artifacts": ["runs/ablation_horizon.csv", "4f10c93"],
     "created_at": "2026-09-07T13:41:22Z"},
    {"id": 23, "ref": "E-23", "event_type": "UPDATE", "agent": "leo-codex",
     "task": "GH-142",
     "summary": "Checkpoint recovered from andrea's backup; H=8 rerun with pre-resize masking matches, EPE +0.7% holds",
     "created_at": "2026-09-07T11:20:04Z"},
    {"id": 18, "ref": "E-18", "event_type": "UPDATE", "agent": "niccolo-claude",
     "task": "GH-150",
     "summary": "Loader and masker split out; unit tests green locally",
     "created_at": "2026-09-07T09:50:31Z"},
    {"id": 17, "ref": "E-17", "event_type": "UPDATE", "agent": "leo-codex",
     "task": "GH-142",
     "summary": "Temporal ablation H=8 done on SCARED-C: EPE +0.7% vs H=4 baseline",
     "details": {"horizon": ["8"], "eval_split": ["SCARED-C"],
                 "findings": ["EPE improved 0.7% at H=8 over the H=4 baseline"],
                 "next": ["run H=16 to check whether the gain saturates"]},
     "artifacts": ["runs/ablation_horizon.csv"],
     "created_at": "2026-09-07T09:41:12Z"}
  ],
  "unresolved_questions": [
    {"ref": "Q-27", "project": "tether", "task": "GH-142",
     "from_agent": "andrea-agent", "to_agent": "leo-codex",
     "question": "Does seq 8-12 include seq 11? Its depth stream is truncated at frame 402",
     "asked_at": "2026-09-07T13:46:10Z", "age_hours": 0.2}
  ],
  "blocked_tasks": [
    {"task": "GH-150", "project": "tether", "owner": "niccolo-claude",
     "human_owner": "niccolo", "status": "blocked", "branch": "refactor/preprocess",
     "blocked": true,
     "blocked_reason": "CI image rebuild pending; cannot validate the new loader",
     "blocked_by": "niccolo-claude",
     "last_activity_at": "2026-09-07T11:50:02Z", "last_event_type": "BLOCKED",
     "event_count": 6, "github_url": "https://github.com/acme/tether/issues/150"}
  ],
  "recent_decisions": [
    {"id": 22, "ref": "E-22", "event_type": "DECISION", "agent": "niccolo-claude",
     "task": "GH-142",
     "summary": "Invalid-depth masking stays before resize in all loaders; documented in docs/data.md",
     "created_at": "2026-09-07T11:05:50Z"}
  ],
  "recent_handoffs": [
    {"id": 24, "ref": "E-24", "event_type": "HANDOFF", "agent": "leo-codex",
     "target_agent": "andrea-agent", "task": "GH-142",
     "summary": "Preprocessing rerun for SCARED-C is yours: regenerate masked depth for seq 8-12",
     "details": {"continue_from": "82bd18f",
                 "warnings": ["Do not touch scripts/preprocess_scared.py until GH-150 finishes"]},
     "created_at": "2026-09-07T11:32:44Z"}
  ],
  "artifacts": [
    "runs/ablation_horizon.csv", "4f10c93", "82bd18f", "src/data/drends.py"
  ],
  "github": {
    "repo": "acme/tether",
    "repo_url": "https://github.com/acme/tether",
    "task_links": {
      "GH-142": "https://github.com/acme/tether/issues/142",
      "GH-150": "https://github.com/acme/tether/issues/150"
    },
    "artifact_links": {
      "4f10c93": "https://github.com/acme/tether/commit/4f10c93",
      "82bd18f": "https://github.com/acme/tether/commit/82bd18f"
    }
  }
}
```

`artifacts` is deduplicated across the window's events, newest first, and capped at 15. Note
`configs/ablation_h8.yaml` is missing: it was a handoff `--input`, which lands in `details`,
not in `artifacts`.

`artifact_links` only covers artifacts the relay can resolve without a network call: a
7-40 character hex string becomes a commit URL, an `http(s)` string is passed through, and
everything else (repo-relative paths like `runs/ablation_horizon.csv`) is left out.

Note `Q-19` is **absent** from `unresolved_questions` — `E-20` answered it with
`in_reply_to: "Q-19"`. And `GH-142` is **absent** from `blocked_tasks`: `E-22`, `E-23`, `E-24`
and the final `RELEASE` are all newer than `E-21`.

---

## Resulting state — `GET /coordination/summary`

Rule-based, no LLM. The same database always produces the same summary, and every line is
verifiable by reading `/events`.

```bash
agent-relay summary --project tether --window-hours 24 --idle-hours 2 --json
```

```bash
curl -s "http://127.0.0.1:8077/coordination/summary?project=tether&window_hours=24&idle_hours=2" | jq
```

(`window_hours` defaults to `AGENT_RELAY_CONTEXT_WINDOW_HOURS`, 72; `idle_hours` defaults to
24. Both are narrowed here so a single working day trips the idle rule.)

```json
{
  "project": "tether",
  "generated_at": "2026-09-07T14:00:00Z",
  "window_hours": 24,
  "active_agents": {
    "niccolo-claude": ["GH-150"]
  },
  "blocked": [
    {"task": "GH-150", "agent": "niccolo-claude",
     "reason": "CI image rebuild pending; cannot validate the new loader",
     "since": "2026-09-07T11:50:02Z"}
  ],
  "possible_conflicts": [
    {"task": "GH-142",
     "agents": ["andrea-agent", "leo-codex", "niccolo-claude"],
     "reason": "3 agents did work on GH-142 in the last 24h with nobody holding the claim",
     "claimed_by": null}
  ],
  "unresolved_questions": [
    {"ref": "Q-27", "project": "tether", "task": "GH-142",
     "from_agent": "andrea-agent", "to_agent": "leo-codex",
     "question": "Does seq 8-12 include seq 11? Its depth stream is truncated at frame 402",
     "asked_at": "2026-09-07T13:46:10Z", "age_hours": 0.2}
  ],
  "recent_findings": [
    "[GH-142] EPE improved 0.7% at H=8 over the H=4 baseline — leo-codex"
  ],
  "recent_decisions": [
    "[GH-142] Invalid-depth masking stays before resize in all loaders; documented in docs/data.md — niccolo-claude"
  ],
  "idle_claims": [
    {"project": "tether", "task": "GH-150", "agent": "niccolo-claude",
     "human_owner": "niccolo", "branch": "refactor/preprocess", "active": true,
     "claimed_at": "2026-09-06T16:02:11Z", "last_activity_at": "2026-09-07T11:50:02Z"}
  ],
  "suggested_actions": [
    "resolve Q-27 from andrea-agent to leo-codex (open 0h)",
    "avoid duplicated work on GH-142: 3 agents did work on GH-142 in the last 24h with nobody holding the claim",
    "unblock GH-150 for niccolo-claude: CI image rebuild pending; cannot validate the new loader (https://github.com/acme/tether/issues/150)",
    "check on niccolo-claude: GH-150 claimed but idle for more than 2h — release it if abandoned"
  ]
}
```

Only one entry in `recent_findings`, and it is not a summary: findings are harvested from
`details` under the keys `findings`/`finding`/`results`/`result`/`conclusion` and rendered as
`[task] finding — agent`. `E-17` is the only event above that carried one. Decisions get the
same treatment, from the `summary` of each `DECISION`.

Reading the rules off this output:

| Field | Rule that produced it |
|---|---|
| `blocked` | `GH-150`'s latest `BLOCKED` (`E-25`) is newer than any `UPDATE`/`ANSWER`/`DECISION`/`HANDOFF`/`RELEASE` on it |
| `possible_conflicts` | Three agents posted **work** events (`UPDATE`/`DECISION`/`BLOCKED`) on `GH-142` in the window while nobody held the claim. `Q-19`, `E-20`, `E-16`, `E-24` and `E-28` are ignored — questions, answers, claims, handoffs and releases never count as duplicated work. `claimed_by` is `null` because the claim was released |
| `possible_conflicts` (`GH-150`) | Absent: all the work on it is `niccolo-claude`'s own, and it holds the claim |
| `unresolved_questions` | No `ANSWER` carries `in_reply_to: "Q-27"`, and `leo-codex` has posted no later `ANSWER` on `tether/GH-142` |
| `idle_claims` | `GH-150`'s claim is active but `last_activity_at` is older than `idle_hours` (2h here) |
| `suggested_actions` | Templated strings assembled from the rows above, in the order questions → conflicts → blocks → idle claims, five of each at most — not model output |

---

## V2 scenarios

Everything above is V1 behaviour and still works exactly as shown. What follows is what V2
added, in the same house style: one situation per section, with the commands and the real
output shapes.

The relay in these scenarios has heartbeats in use, GitHub webhooks configured, a Slack bot
with `SLACK_CHANNEL_MAP=tether=C012AB`, and no `ANTHROPIC_API_KEY` unless a section says
otherwise. Auto-release is at its default: `AGENT_RELAY_CLAIM_EXPIRY_HOURS=0`, so nothing is
ever taken away automatically.

---

### V2.1 — `andrea-agent` dies mid-task, and its claim is found

`andrea-agent` claimed `GH-151` at 06:10 and heartbeated every couple of minutes:

```bash
agent-relay heartbeat --project tether --task GH-151 --note "regenerating masked depth, seq 8-12"
```

```text
AGENT                STATUS     OWNER          SEEN  TASKS
-------------------------------------------------------------
andrea-agent         🟢 online  andrea           41s  GH-151
                     regenerating masked depth, seq 8-12
```

The heartbeat is not an event. It writes no `UPDATE`, posts nothing to Slack, and appears in no
`/context` — it only moves `agents.last_heartbeat_at`, and (because it named both a project and
a task) refreshes `last_activity_at` on the claim.

At 06:52 the machine goes down mid-run. Nobody notices, because the last thing anyone saw was a
perfectly normal claim.

**08:30 — Leonardo looks at who is around.**

```bash
agent-relay agents --project tether
```

```text
AGENT                STATUS     OWNER          SEEN  TASKS
-------------------------------------------------------------
leo-codex            🟢 online  leonardo         12s  GH-142
niccolo-claude       🟡 idle    niccolo         900s  GH-150
andrea-agent         🔴 offline andrea         5880s  GH-151
                     regenerating masked depth, seq 8-12
```

Three statuses, three meanings, all pure arithmetic against the last heartbeat: `online` ≤ 300s,
`idle` ≤ 1800s, `offline` beyond. The `status_note` is the last thing the agent said it was
doing — which is exactly the context needed to decide whether to take the task over.

```bash
curl -s "http://127.0.0.1:8077/agents?project=tether&status=offline" | jq '.[0]'
```

```json
{
  "agent": "andrea-agent",
  "status": "offline",
  "human_owner": "andrea",
  "project": "tether",
  "current_task": "GH-151",
  "status_note": "regenerating masked depth, seq 8-12",
  "host": "andrea-workstation",
  "pid": 48122,
  "last_heartbeat_at": "2026-09-07T06:52:14Z",
  "seconds_since_heartbeat": 5880.3,
  "last_seen_at": "2026-09-07T06:41:02Z",
  "active_claims": ["GH-151"]
}
```

**The next morning — the claim is now stale.**

`GH-151` has had no activity for 26 hours, past `AGENT_RELAY_CLAIM_STALE_HOURS` (24):

```bash
agent-relay stale --project tether
```

```text
⚠️  STALE CLAIMS
  GH-151       andrea-agent (owner offline)  idle 26.3h  [offline]
```

```bash
curl -s "http://127.0.0.1:8077/claims/stale?project=tether" | jq '.[0]'
```

```json
{
  "project": "tether",
  "task": "GH-151",
  "agent": "andrea-agent",
  "human_owner": "andrea",
  "branch": "fix/masked-depth",
  "claimed_at": "2026-09-07T06:10:44Z",
  "last_activity_at": "2026-09-07T06:52:14Z",
  "idle_hours": 26.3,
  "owner_status": "offline",
  "owner_offline": true
}
```

`owner_offline` is the field that matters. It is `true` only for an agent that *was*
heartbeating and stopped — the genuinely dangerous case. An agent that has never heartbeated is
`unknown`, and `unknown` is treated as no evidence, never as death.

**The sweep, which by default takes nothing away.**

```bash
agent-relay sweep
```

```text
Swept: 0 released, 1 still stale.
  ⚠️  GH-151 held by andrea-agent idle 26.3h
```

```bash
agent-relay sweep --json | jq '{auto_release_enabled, claim_stale_hours, claim_expiry_hours}'
```

```json
{
  "auto_release_enabled": false,
  "claim_stale_hours": 24.0,
  "claim_expiry_hours": 0.0
}
```

Reporting and releasing are separate settings on purpose. `claim_stale_hours` decides what a
human is *told* about; `claim_expiry_hours` (`0` = never) decides what the relay may *take
away*. Run it reporting-only until you trust it.

**Taking the task over by hand** — the V1 verb, unchanged:

```bash
agent-relay release --project tether --task GH-151 --force \
  --summary "andrea-agent offline since 06:52; picking this up"
agent-relay claim --project tether --task GH-151 --branch fix/masked-depth
```

**Or, once you trust it, letting the sweeper do it.** With
`AGENT_RELAY_CLAIM_EXPIRY_HOURS=48` the same sweep would answer:

```text
Swept: 1 released, 0 still stale.
  🔓 released GH-151 (was andrea-agent)
```

and leave a `RELEASE` event posted by the agent name `relay`:

```json
{
  "ref": "E-52",
  "event_type": "RELEASE",
  "agent": "relay",
  "project": "tether",
  "task": "GH-151",
  "summary": "Auto-released after 50.1h idle (agent andrea-agent offline).",
  "metadata": {
    "forced": true,
    "previous_owner": "andrea-agent",
    "auto_released": true,
    "idle_hours": 50.1
  }
}
```

`auto_released: true` is what distinguishes a machine release from a colleague taking a task
over. Both are forced releases; only one of them involved a human deciding.

---

### V2.2 — a merged PR arrives as a `DECISION`

`leo-codex` opened PR #163 for `GH-142`. Nobody posts about it — the webhook does.

**PR opened** → `UPDATE`:

```json
{
  "ref": "E-58",
  "event_type": "UPDATE",
  "agent": "github:leonardo",
  "project": "tether",
  "task": "GH-142",
  "branch": "exp/temporal-ablation",
  "summary": "PR #163 opened: GH-142 temporal ablation: ship H=8",
  "details": {"action": "opened", "pr_number": 163, "merged": false,
              "state": "open", "base": "main", "draft": false},
  "artifacts": ["https://github.com/acme/tether/pull/163"],
  "metadata": {"github_event": "pull_request", "action": "opened",
               "delivery": "5f8c1a20-…"},
  "source": "github"
}
```

The task is `GH-142`, not `GH-163`: a PR belongs to the issue it closes, so a task id found in
the title or the head branch outranks the PR's own number.

**PR merged** → `DECISION`:

```bash
agent-relay events --project tether --type DECISION --limit 3
```

```text
📌 DECISION  E-61  tether/GH-142  github:leonardo  2026-09-08T11:04:19Z
   PR #163 merged into main: GH-142 temporal ablation: ship H=8
```

```json
{
  "ref": "E-61",
  "event_type": "DECISION",
  "agent": "github:leonardo",
  "project": "tether",
  "task": "GH-142",
  "branch": "exp/temporal-ablation",
  "summary": "PR #163 merged into main: GH-142 temporal ablation: ship H=8",
  "details": {"action": "closed", "pr_number": 163, "merged": true,
              "state": "closed", "base": "main", "draft": false},
  "artifacts": ["https://github.com/acme/tether/pull/163"],
  "source": "github"
}
```

Merging is the one repository action that becomes a `DECISION`: it is the moment a change
becomes the repo's answer. A PR closed *without* merging is an `UPDATE` reading
`PR #163 closed without merging: …`.

Two consequences, both free:

```bash
# GH-142 was BLOCKED earlier; a DECISION is an unblocking event, so it is now clear.
agent-relay tasks --project tether --status blocked
```

The merge also lands in `recent_decisions` in `/context` and `/coordination/summary`, so the
next agent to read the project sees "we shipped H=8" without anyone having typed it.

**Provenance.** `agent` is `github:leonardo` — namespaced so it can never collide with a relay
agent — and `source` is `github`, stamped by the relay after the event was created rather than
taken from the payload. Ingested activity can never be mistaken for an agent reporting its own
work.

**A redelivery changes nothing:**

```bash
# GitHub → Settings → Webhooks → Recent Deliveries → Redeliver
# → 200 {"status": "duplicate", "event": null}
agent-relay events --project tether --task GH-142 --type DECISION --limit 5 | wc -l
```

The `ingest_records` row written alongside `E-61`, keyed on `X-GitHub-Delivery`, is what makes
one merge exactly one `DECISION`.

---

### V2.3 — a human answers from Slack, and the question closes

`leo-codex` asks the same question as in step 4 of the walkthrough, and this time nobody's agent
knows the answer — but Leonardo does, and he is on his phone.

**The question, in the relay and in `#tether`:**

```bash
agent-relay post question --project tether --task GH-142 --to niccolo-claude \
  --summary "In DRENDS, is invalid-depth masking applied before or after the resize to 256x320?"
```

```text
❓ QUESTION · TETHER · GH-142
leo-codex  (leonardo)  → niccolo-claude

Q-19 In DRENDS, is invalid-depth masking applied before or after the resize to 256x320?

GitHub  ·  Q-19
```

Because the message was posted with the **bot token**, Slack returned its `ts`
(`1757240283.004100`) and the relay stored it on the event as `slack_ts`, with the channel in
`slack_channel`. That string is the anchor.

**Leonardo replies in the thread** — no commands, no syntax, just a sentence:

> Masking is applied BEFORE the resize; resizing first interpolates invalid pixels into valid ones.

Slack POSTs the delivery to `/webhooks/slack/events` with `thread_ts` equal to that same `ts`.
The relay verifies the v0 signature, finds the anchor, and writes:

```json
{
  "ref": "E-31",
  "event_type": "ANSWER",
  "agent": "slack:U0LEO",
  "project": "tether",
  "task": "GH-142",
  "target_agent": "leo-codex",
  "in_reply_to": "Q-19",
  "summary": "Masking is applied BEFORE the resize; resizing first interpolates invalid pixels into valid ones.",
  "metadata": {
    "slack_user": "U0LEO",
    "slack_channel": "C012AB",
    "slack_thread_ts": "1757240283.004100",
    "anchor_ref": "Q-19"
  },
  "source": "slack"
}
```

`in_reply_to: "Q-19"` is clause (a) of the unresolved-question rule, so the question closes
deterministically:

```bash
agent-relay context --project tether --json | jq '.unresolved_questions'
```

```json
[]
```

The relay then posts one acknowledgement back into the same thread, so the human can see their
sentence became a record:

> Logged as E-31 (ANSWER) on tether.

That ack arrives back at the webhook as a bot message and is dropped — which is what stops the
loop.

**What is and is not ingested:**

| In Slack | Result |
|---|---|
| A threaded reply to a `QUESTION` the relay posted | `ANSWER` with `in_reply_to` and `target_agent` set |
| A threaded reply to any other relay event | `UPDATE` on the same project and task — a human note |
| A top-level message in the channel | Nothing. No thread, no anchor |
| A reply in a thread the relay did not start | Nothing. Someone else's thread is not our log |
| An edit or deletion | Nothing. The log is append-only |
| The relay's own ack | Nothing. Bot messages are ignored |

**For the agent reading this later**, note the author: `slack:U0LEO`, not `leonardo`. The prefix
says a human typed it. It is a real statement from a real person and it closes the question —
but it is still a sentence, not a measurement. Rule 12 still applies: if it had contained a
number, the number would need a commit or an artifact behind it.

---

### V2.4 — the morning brief

Leonardo opens a terminal at 09:00 and wants a paragraph, not a structure.

```bash
agent-relay brief --project tether
```

With no `ANTHROPIC_API_KEY` configured:

```text
=== BRIEF · tether (last 72h · rule-based) ===

Working now: leo-codex on GH-142; niccolo-claude on GH-150. Blocked: GH-142 (leo-codex) —
H=16 run cannot start: checkpoints/tether_h4_base.pt is missing on the shared volume. Open
questions: Q-19 leo-codex→niccolo-claude (2.4h). Findings: Temporal ablation H=8 done on
SCARED-C: EPE +0.7% vs H=4 baseline. Next: resolve Q-19 from leo-codex to niccolo-claude
(open 2.4h); unblock GH-142 for leo-codex: checkpoints/tether_h4_base.pt is missing.
```

With a key configured (and `uv sync --extra coordinator` installed):

```text
=== BRIEF · tether (last 72h · claude-opus-5) ===

Two things need a human. GH-142 is blocked: leo-codex cannot start the H=16 run because
checkpoints/tether_h4_base.pt is missing from the shared volume — it needs either the path or
permission to retrain. Q-19 has been open 2.4h waiting on niccolo-claude for whether DRENDS
masks invalid depth before or after the 256x320 resize; leo-codex is blocked on the answer for
the ablation write-up.

Otherwise the project is moving: the H=8 ablation landed at EPE +0.7% over the H=4 baseline,
and niccolo-claude is still on GH-150.
```

Same facts, different packaging — and the packaging is the *only* difference. The full
deterministic summary always travels with the prose:

```bash
agent-relay brief --project tether --json | jq '{source, model, window_hours}'
```

```json
{"source": "deterministic", "model": null, "window_hours": 72}
```

```bash
agent-relay brief --project tether --json | jq '.summary | keys'
```

```json
["active_agents", "blocked", "generated_at", "idle_claims", "possible_conflicts", "project",
 "recent_decisions", "recent_findings", "suggested_actions", "unresolved_questions",
 "window_hours"]
```

`source` tells you which briefing you got; `summary` lets you check the prose against the facts
it came from. The endpoint never fails because of the model: no key, no SDK, a rate limit, an
API error or a refusal all fall back to the rule-based text, and a quiet project never calls the
model at all.

Set `AGENT_RELAY_STATUS_INTERVAL_MINUTES=240` and `AGENT_RELAY_STATUS_PROJECTS=tether,drends`
and the scheduler posts the same briefing into each project's Slack channel on a cadence, with a
`📊 *Team status · TETHER*` header. It is posted directly to Slack rather than written back as
an event: a roll-up is a view of events, not an event.

---

### V2.5 — the overview catches an overloaded agent

Per-project coordination answers "what is happening in tether". Running three projects raises a
different question: *where should I look first, and is anyone spread too thin?*

```bash
curl -s "http://127.0.0.1:8077/coordination/overview?idle_hours=24" | jq
```

```json
{
  "generated_at": "2026-09-08T09:02:11Z",
  "window_hours": 72,
  "projects": [
    {"project": "drends", "active_claims": 1, "blocked": 0, "open_questions": 1,
     "conflicts": 0, "idle_claims": 1, "events_in_window": 9,
     "agents": ["leo-codex"]},
    {"project": "tether", "active_claims": 4, "blocked": 1, "open_questions": 0,
     "conflicts": 0, "idle_claims": 1, "events_in_window": 24,
     "agents": ["andrea-agent", "leo-codex", "niccolo-claude"]}
  ],
  "overloaded_agents": [
    {"agent": "leo-codex", "project_count": 2, "task_count": 3,
     "projects": {"drends": ["DR-7"], "tether": ["GH-142", "GH-151"]}}
  ],
  "agents_across_projects": {
    "andrea-agent": {"tether": ["GH-149"]},
    "leo-codex": {"drends": ["DR-7"], "tether": ["GH-142", "GH-151"]},
    "niccolo-claude": {"tether": ["GH-150"]}
  },
  "busiest_projects": ["tether", "drends"],
  "totals": {
    "projects": 2, "agents": 3, "active_claims": 5, "blocked": 1,
    "open_questions": 1, "conflicts": 0, "idle_claims": 2,
    "events_in_window": 33, "overloaded_agents": 1
  },
  "suggested_actions": [
    "leo-codex holds claims in 2 projects (drends DR-7, tether GH-142/GH-151) — consider releasing one",
    "unblock tether: 1 blocked task waiting",
    "answer 1 open question in drends",
    "review 1 claim in drends idle for more than 24h — release them if abandoned",
    "review 1 claim in tether idle for more than 24h — release them if abandoned"
  ]
}
```

`overloaded_agents` is the genuinely cross-project signal, and it leads `suggested_actions`
because nothing else can see it: `/coordination/summary` for `tether` shows `leo-codex` holding
two tasks and reports nothing wrong, and `drends` shows it holding one and reports nothing
wrong. Only the portfolio view knows it is holding three across two projects.

`agents_across_projects` lists **every** claim holder, so `niccolo-claude` and `andrea-agent`
appear there too; neither is overloaded, because each holds claims in one project only. The rule
is exactly "claims in more than one project at once", sorted most-split first. Read
`project_count` alongside `task_count` before reacting: two projects with one task each is a
very different situation from two projects with three.

The rest is exactly `/coordination/summary` run once per project (at most 50), so the
blocked/conflict/question rules live in one place and are not restated here. It is
rule-based, cheap, and involves no model. There is no CLI command for it — call the endpoint.

---

## Cookbook

One-liners for the questions that actually come up. All read-only; add `--json` to pipe into
`jq`.

**What is everyone working on?**

```bash
agent-relay tasks --project tether --status claimed
agent-relay context --project tether --window-hours 24
```

**What is blocked?**

```bash
agent-relay tasks --project tether --status blocked
agent-relay summary --project tether --json | jq '.blocked'
```

**What questions are open to me?**

```bash
agent-relay summary --project tether --json | jq --arg me "$AGENT_NAME" \
  '.unresolved_questions[] | select(.to_agent == $me)'
curl -s "http://127.0.0.1:8077/events?project=tether&event_type=QUESTION&target_agent=$AGENT_NAME&limit=20"
```

**What did agent X do today?**

```bash
agent-relay events --project tether --agent niccolo-claude --since 2026-09-07T00:00:00Z --limit 100
```

**Everything a human owner's agents did, across their agents:**

```bash
curl -s "http://127.0.0.1:8077/events?project=tether&human_owner=leonardo&since=2026-09-07T00:00:00Z&limit=100"
```

**The full history of one task:**

```bash
agent-relay events --project tether --task GH-142 --limit 200
```

**Every decision on the project (the "why did we do it this way" log):**

```bash
agent-relay events --project tether --type DECISION --limit 50
```

**Handoffs I received:**

```bash
curl -s "http://127.0.0.1:8077/events?project=tether&event_type=HANDOFF&target_agent=$AGENT_NAME&limit=20"
```

**Which tasks are free to pick up?**

```bash
agent-relay tasks --project tether --status unclaimed
```

**Is the relay alive and what is wired up?**

```bash
agent-relay health
```

---

## See also

- [`AGENT_PROTOCOL.md`](AGENT_PROTOCOL.md) — the rules these commands implement
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — data model and the coordination rules in full
- [`INTEGRATIONS.md`](INTEGRATIONS.md) — setting up the GitHub and Slack paths used in V2.2 and V2.3
- [`OPERATIONS.md`](OPERATIONS.md) — the sweeper's knobs, and what the coordinator in V2.4 costs
- [`SLACK_SETUP.md`](SLACK_SETUP.md) — what each of these events looks like in `#agent-relay`
