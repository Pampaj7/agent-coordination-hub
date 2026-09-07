#!/usr/bin/env bash
# Developer entry point for agent-relay. Runnable from any directory.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

usage() {
    cat <<'EOF'
Usage: scripts/dev.sh <command>

Commands:
  setup    uv sync (dev deps included) and create .env from .env.example if missing
  serve    run the relay with auto-reload
  test     run the test suite
  lint     ruff check + ruff format --check
  fmt      ruff format + ruff check --fix
  types    mypy
  check    lint, then types, then test
  help     show this message
EOF
}

require_uv() {
    if ! command -v uv >/dev/null 2>&1; then
        echo "error: 'uv' is not installed. See https://docs.astral.sh/uv/" >&2
        exit 127
    fi
}

cmd_setup() {
    uv sync
    if [ ! -f "${REPO_ROOT}/.env" ]; then
        cp "${REPO_ROOT}/.env.example" "${REPO_ROOT}/.env"
        echo "created .env from .env.example"
    else
        echo ".env already exists, leaving it alone"
    fi
}

cmd_serve() { uv run agent-relay serve --reload; }
cmd_test()  { uv run pytest "$@"; }
cmd_lint()  { uv run ruff check . && uv run ruff format --check .; }
cmd_fmt()   { uv run ruff format . && uv run ruff check --fix .; }
cmd_types() { uv run mypy; }

cmd_check() {
    cmd_lint
    cmd_types
    cmd_test
}

main() {
    local command="${1-help}"
    if [ "$#" -gt 0 ]; then shift; fi

    case "${command}" in
        setup) require_uv; cmd_setup ;;
        serve) require_uv; cmd_serve ;;
        test)  require_uv; cmd_test "$@" ;;
        lint)  require_uv; cmd_lint ;;
        fmt|format) require_uv; cmd_fmt ;;
        types) require_uv; cmd_types ;;
        check) require_uv; cmd_check ;;
        help|-h|--help) usage ;;
        *)
            echo "error: unknown command '${command}'" >&2
            echo >&2
            usage >&2
            exit 2
            ;;
    esac
}

main "$@"
