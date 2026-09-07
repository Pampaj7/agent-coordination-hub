#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# End-to-end smoke test for agent-relay.
#
#   scripts/smoke_test.sh              # test a relay already running
#   scripts/smoke_test.sh --start      # start a throwaway relay first
#   BASE_URL=http://box:8077 scripts/smoke_test.sh
#
# Dependencies: bash, curl, python3 (stdlib only). No jq.
# Exits non-zero if any step fails.
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

BASE_URL="${BASE_URL:-http://127.0.0.1:8077}"
START_SERVER=0
PROJECT="smoke-$$"
TASK="GH-$$"
AGENT_A="leo-codex"
AGENT_B="niccolo-claude"
AGENT_C="andrea-agent"
WAIT_SECONDS="${WAIT_SECONDS:-30}"

FAILURES=0
STEPS=0
SERVER_PID=""
TMPDIR_SMOKE=""

# --- output ----------------------------------------------------------------
if [ -t 1 ]; then
    C_GREEN=$'\033[0;32m'; C_RED=$'\033[0;31m'; C_DIM=$'\033[2m'; C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
    C_GREEN=""; C_RED=""; C_DIM=""; C_BOLD=""; C_OFF=""
fi

pass() { STEPS=$((STEPS + 1)); printf '%sPASS%s %s\n' "${C_GREEN}" "${C_OFF}" "$1"; }
fail() {
    STEPS=$((STEPS + 1))
    FAILURES=$((FAILURES + 1))
    printf '%sFAIL%s %s\n' "${C_RED}" "${C_OFF}" "$1"
    if [ -n "${2-}" ]; then
        printf '     %s%s%s\n' "${C_DIM}" "$2" "${C_OFF}"
    fi
}
info() { printf '%s%s%s\n' "${C_DIM}" "$1" "${C_OFF}"; }

usage() {
    cat <<'EOF'
Usage: scripts/smoke_test.sh [--start] [--help]

  --start   launch a relay on a temporary SQLite database and stop it on exit
  --help    this message

Environment:
  BASE_URL               relay base URL (default http://127.0.0.1:8077)
  AGENT_RELAY_API_TOKEN  sent as "Authorization: Bearer ..." when set
  WAIT_SECONDS           how long to wait for /health when starting (default 30)
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --start) START_SERVER=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "error: unknown argument '$1'" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

# --- cleanup ---------------------------------------------------------------
cleanup() {
    if [ -n "${SERVER_PID}" ] && kill -0 "${SERVER_PID}" 2>/dev/null; then
        info "stopping relay (pid ${SERVER_PID})"
        pkill -TERM -P "${SERVER_PID}" 2>/dev/null || true
        kill "${SERVER_PID}" 2>/dev/null || true
        wait "${SERVER_PID}" 2>/dev/null || true
    fi
    if [ -n "${TMPDIR_SMOKE}" ] && [ -d "${TMPDIR_SMOKE}" ]; then
        rm -rf "${TMPDIR_SMOKE}"
    fi
}
trap cleanup EXIT INT TERM

# --- tiny helpers ----------------------------------------------------------

# _json <dotted.path> — read JSON on stdin, print the field ("" when absent).
# Numeric path segments index into lists: e.g. _json '0.ref' or _json 'items.0.id'.
_json() {
    python3 -c '
import json, sys

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for key in sys.argv[1].split("."):
    if isinstance(data, list):
        try:
            data = data[int(key)]
        except (ValueError, IndexError):
            data = None
    elif isinstance(data, dict):
        data = data.get(key)
    else:
        data = None
    if data is None:
        break
if data is None:
    print("")
elif isinstance(data, (dict, list)):
    print(json.dumps(data))
else:
    print(data)
' "$1"
}

# _count — read a JSON list on stdin, print its length (0 if not a list).
_count() {
    python3 -c '
import json, sys

try:
    data = json.load(sys.stdin)
except Exception:
    data = None
print(len(data) if isinstance(data, list) else 0)
'
}

# _urlenc <string> — percent-encode a query-string value.
_urlenc() {
    python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"
}

# _req <METHOD> <PATH> [JSON_BODY] — sets STATUS and BODY.
STATUS=""
BODY=""
_req() {
    local method="$1" path="$2" body="${3-}"
    local out
    out="$(mktemp)"
    local -a args=(-sS -o "${out}" -w '%{http_code}' -X "${method}" --max-time 20)
    if [ -n "${AGENT_RELAY_API_TOKEN:-}" ]; then
        args+=(-H "Authorization: Bearer ${AGENT_RELAY_API_TOKEN}")
    fi
    if [ -n "${body}" ]; then
        args+=(-H 'Content-Type: application/json' -d "${body}")
    fi
    STATUS="$(curl "${args[@]}" "${BASE_URL}${path}" 2>/dev/null || echo 000)"
    BODY="$(cat "${out}")"
    rm -f "${out}"
}

# _expect <expected_status> <label> — check STATUS, echo PASS/FAIL.
_expect() {
    local want="$1" label="$2"
    if [ "${STATUS}" = "${want}" ]; then
        pass "${label} (HTTP ${STATUS})"
        return 0
    fi
    fail "${label} (want HTTP ${want}, got ${STATUS})" "${BODY:0:400}"
    return 1
}

# _assert <actual> <expected> <label>
_assert() {
    if [ "$1" = "$2" ]; then
        pass "$3"
    else
        fail "$3" "expected '$2', got '$1'"
    fi
}

# --- optionally start a relay ---------------------------------------------
if [ "${START_SERVER}" -eq 1 ]; then
    if ! command -v uv >/dev/null 2>&1; then
        echo "error: --start needs 'uv' on PATH" >&2
        exit 127
    fi
    TMPDIR_SMOKE="$(mktemp -d)"
    HOST="$(python3 -c 'import sys,urllib.parse as u; p=u.urlsplit(sys.argv[1]); print(p.hostname or "127.0.0.1")' "${BASE_URL}")"
    PORT="$(python3 -c 'import sys,urllib.parse as u; p=u.urlsplit(sys.argv[1]); print(p.port or 8077)' "${BASE_URL}")"
    info "starting relay on ${HOST}:${PORT} with db in ${TMPDIR_SMOKE}"
    # Run uv directly (not in a subshell) so ${SERVER_PID} is the process the
    # trap can actually signal; uv forwards SIGTERM to uvicorn.
    cd "${REPO_ROOT}"
    AGENT_RELAY_HOST="${HOST}" \
    AGENT_RELAY_PORT="${PORT}" \
    AGENT_RELAY_DB_URL="sqlite:///${TMPDIR_SMOKE}/smoke.db" \
    AGENT_RELAY_LOG_LEVEL=WARNING \
    SLACK_WEBHOOK_URL="" \
    uv run agent-relay serve >"${TMPDIR_SMOKE}/server.log" 2>&1 &
    SERVER_PID="$!"
fi

# --- wait for /health (poll, never a blind sleep) --------------------------
info "waiting for ${BASE_URL}/health (up to ${WAIT_SECONDS}s)"
deadline=$((SECONDS + WAIT_SECONDS))
ready=0
while [ "${SECONDS}" -lt "${deadline}" ]; do
    _req GET /health
    if [ "${STATUS}" = "200" ]; then
        ready=1
        break
    fi
    if [ -n "${SERVER_PID}" ] && ! kill -0 "${SERVER_PID}" 2>/dev/null; then
        echo "error: the relay exited while starting up" >&2
        [ -n "${TMPDIR_SMOKE}" ] && cat "${TMPDIR_SMOKE}/server.log" >&2 || true
        exit 1
    fi
    sleep 0.5
done

if [ "${ready}" -ne 1 ]; then
    echo "error: no healthy relay at ${BASE_URL} after ${WAIT_SECONDS}s" >&2
    if [ -n "${TMPDIR_SMOKE}" ] && [ -f "${TMPDIR_SMOKE}/server.log" ]; then
        tail -n 40 "${TMPDIR_SMOKE}/server.log" >&2
    fi
    exit 1
fi

printf '\n%sagent-relay smoke test%s  base=%s project=%s task=%s\n\n' \
    "${C_BOLD}" "${C_OFF}" "${BASE_URL}" "${PROJECT}" "${TASK}"

PROJECT_Q="$(_urlenc "${PROJECT}")"

# --- 1. health -------------------------------------------------------------
_req GET /health
_expect 200 "GET /health" || true
_assert "$(printf '%s' "${BODY}" | _json status)" "ok" "  health.status == ok"

# --- 2. post an UPDATE event ----------------------------------------------
_req POST /events "$(cat <<EOF
{"event_type":"UPDATE","agent":"${AGENT_A}","project":"${PROJECT}","task":"${TASK}",
 "summary":"smoke test: initial update","human_owner":"leonardo",
 "details":{"completed":["wrote the smoke test"]},"artifacts":["scripts/smoke_test.sh"]}
EOF
)"
if _expect 201 "POST /events (UPDATE)"; then :; else
    # Some deployments answer 200 rather than 201; accept it as a pass.
    if [ "${STATUS}" = "200" ]; then
        FAILURES=$((FAILURES - 1))
        info "  (server answered 200 instead of 201; accepted)"
    fi
fi
EVENT_REF="$(printf '%s' "${BODY}" | _json ref)"
if [ -n "${EVENT_REF}" ]; then
    pass "  event got a ref (${EVENT_REF})"
else
    fail "  event got a ref" "${BODY:0:400}"
fi

# --- 3. list events filtered by project -----------------------------------
_req GET "/events?project=${PROJECT_Q}"
_expect 200 "GET /events?project=${PROJECT}" || true
EVENT_COUNT="$(printf '%s' "${BODY}" | _count)"
if [ "${EVENT_COUNT}" -ge 1 ]; then
    pass "  project filter returned ${EVENT_COUNT} event(s)"
else
    fail "  project filter returned events" "got ${EVENT_COUNT}; body: ${BODY:0:400}"
fi

# --- 4. claim as leo-codex -------------------------------------------------
_req POST /claim "{\"agent\":\"${AGENT_A}\",\"project\":\"${PROJECT}\",\"task\":\"${TASK}\",\"human_owner\":\"leonardo\",\"branch\":\"smoke/branch\",\"note\":\"smoke test claim\"}"
_expect 200 "POST /claim as ${AGENT_A}" || true
_assert "$(printf '%s' "${BODY}" | _json agent)" "${AGENT_A}" "  claim owned by ${AGENT_A}"

# --- 5. claim collision: same project+task as niccolo-claude --------------
_req POST /claim "{\"agent\":\"${AGENT_B}\",\"project\":\"${PROJECT}\",\"task\":\"${TASK}\"}"
_expect 409 "POST /claim as ${AGENT_B} is rejected" || true
CONFLICT_OWNER="$(printf '%s' "${BODY}" | _json current_owner)"
if [ -z "${CONFLICT_OWNER}" ]; then
    # FastAPI HTTPException wraps the model under "detail".
    CONFLICT_OWNER="$(printf '%s' "${BODY}" | _json detail.current_owner)"
fi
_assert "${CONFLICT_OWNER}" "${AGENT_A}" "  409 body names ${AGENT_A} as current_owner"

# --- 6. handoff leo-codex -> andrea-agent ---------------------------------
_req POST /handoff "{\"agent\":\"${AGENT_A}\",\"target_agent\":\"${AGENT_C}\",\"project\":\"${PROJECT}\",\"task\":\"${TASK}\",\"summary\":\"smoke test handoff\",\"continue_from\":\"HEAD\",\"inputs\":[\"scripts/smoke_test.sh\"],\"warnings\":[],\"transfer_claim\":true}"
_expect 200 "POST /handoff ${AGENT_A} -> ${AGENT_C}" || true

# --- 7. tasks --------------------------------------------------------------
_req GET "/tasks?project=${PROJECT_Q}"
_expect 200 "GET /tasks" || true
TASK_OWNER="$(printf '%s' "${BODY}" | _json 0.owner)"
_assert "${TASK_OWNER}" "${AGENT_C}" "  task now owned by ${AGENT_C}"

# --- 8. context ------------------------------------------------------------
_req GET "/context?project=${PROJECT_Q}"
_expect 200 "GET /context?project=${PROJECT}" || true
_assert "$(printf '%s' "${BODY}" | _json project)" "${PROJECT}" "  context is for ${PROJECT}"

# --- 9. coordination summary ----------------------------------------------
_req GET "/coordination/summary?project=${PROJECT_Q}"
_expect 200 "GET /coordination/summary?project=${PROJECT}" || true
_assert "$(printf '%s' "${BODY}" | _json project)" "${PROJECT}" "  summary is for ${PROJECT}"

# --- 10. release -----------------------------------------------------------
_req POST /release "{\"agent\":\"${AGENT_C}\",\"project\":\"${PROJECT}\",\"task\":\"${TASK}\",\"summary\":\"smoke test done\"}"
_expect 200 "POST /release by ${AGENT_C}" || true

_req POST /claim "{\"agent\":\"${AGENT_B}\",\"project\":\"${PROJECT}\",\"task\":\"${TASK}\"}"
_expect 200 "POST /claim succeeds again after release" || true
_req POST /release "{\"agent\":\"${AGENT_B}\",\"project\":\"${PROJECT}\",\"task\":\"${TASK}\"}"

# ===========================================================================
# V2 surface. Everything below is opt-in in production, but the endpoints must
# answer sensibly even when the integration behind them is switched off.
# ===========================================================================

# --- 11. presence ----------------------------------------------------------
_req POST /heartbeat "{\"agent\":\"${AGENT_A}\",\"project\":\"${PROJECT}\",\"task\":\"${TASK}\",\"status_note\":\"smoke test\"}"
_expect 200 "POST /heartbeat" || true
_assert "$(printf '%s' "${BODY}" | _json status)" "online" "  agent reads as online right after a heartbeat"

_req GET "/agents?project=${PROJECT_Q}"
_expect 200 "GET /agents" || true

# --- 12. claim hygiene -----------------------------------------------------
_req GET "/claims/stale?project=${PROJECT_Q}"
_expect 200 "GET /claims/stale" || true

_req POST /claims/sweep "{}"
_expect 200 "POST /claims/sweep" || true

# --- 13. coordination brief (rule-based without an API key) ----------------
_req GET "/coordination/brief?project=${PROJECT_Q}"
_expect 200 "GET /coordination/brief" || true
_assert "$(printf '%s' "${BODY}" | _json source)" "deterministic" "  brief degrades to the rule-based path with no API key"

# --- 14. cross-project overview -------------------------------------------
_req GET "/coordination/overview"
_expect 200 "GET /coordination/overview" || true

# --- 15. experiments -------------------------------------------------------
_req GET "/experiments?project=${PROJECT_Q}"
_expect 200 "GET /experiments" || true

# --- 16. dashboard ---------------------------------------------------------
_req GET "/dashboard"
_expect 200 "GET /dashboard" || true

# --- 17. A2A ---------------------------------------------------------------
_req GET "/.well-known/agent.json"
_expect 200 "GET /.well-known/agent.json" || true
_assert "$(printf '%s' "${BODY}" | _json name)" "agent-relay" "  agent card identifies the relay"

_req POST /a2a '{"jsonrpc":"2.0","id":1,"method":"tasks/cancel","params":{}}'
_expect 200 "POST /a2a unsupported method" || true
_assert "$(printf '%s' "${BODY}" | _json error.code)" "-32601" "  JSON-RPC reports method-not-found, not HTTP 500"

# --- 18. webhooks are closed unless configured -----------------------------
# Both are unconfigured in a default relay, and must refuse rather than accept.
_req POST /webhooks/github '{}'
_expect 503 "POST /webhooks/github refuses when no secret is set" || true

_req POST /webhooks/slack/events '{}'
_expect 503 "POST /webhooks/slack/events refuses when the bot is off" || true

# --- summary ---------------------------------------------------------------
printf '\n'
if [ "${FAILURES}" -eq 0 ]; then
    printf '%s%s checks, 0 failures — the relay looks healthy.%s\n' "${C_GREEN}" "${STEPS}" "${C_OFF}"
    exit 0
fi
printf '%s%s checks, %s failure(s).%s\n' "${C_RED}" "${STEPS}" "${FAILURES}" "${C_OFF}"
exit 1
