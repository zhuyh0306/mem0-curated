#!/usr/bin/env bash
# One entry point for deploying curated/ with Docker Compose.
#
#   ./deploy.sh            env + build + up -d + wait for health + smoke test
#   ./deploy.sh env        only make sure .env exists with real secrets (needs no Docker)
#   ./deploy.sh up         build and start
#   ./deploy.sh pull       pull MEM0_CURATED_IMAGE and restart without building (CI/CD)
#   ./deploy.sh smoke      wait for health, then check that roles are enforced
#   ./deploy.sh down       stop (./data is left alone)
#   ./deploy.sh save FILE  bundle every image for an offline host
#   ./deploy.sh load FILE  unpack them there
#
# Set MEM0_CURATED_IMAGE to deploy a registry image, set HEALTH_WAIT_SECONDS to raise
# the startup budget when the embedder model is slow.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

IMAGE="${MEM0_CURATED_IMAGE:-mem0-curated:1.0.0}"
QDRANT_IMAGE="qdrant/qdrant:v1.12.4"
GATEWAY="http://127.0.0.1:8787"
DASHBOARD="http://127.0.0.1:8788"
HEALTH_WAIT_SECONDS="${HEALTH_WAIT_SECONDS:-300}"

die() {
  echo "deploy.sh: $*" >&2
  exit 1
}

COMPOSE=""

# Sets COMPOSE, or aborts. It must not be reached through $(...) : a `die` inside command
# substitution only exits the subshell, and the caller would then run an empty command.
require_compose() {
  [[ -n "$COMPOSE" ]] && return 0
  if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
  else
    die "neither 'docker compose' nor 'docker-compose' is available. Compose v2 ships as a plugin with Docker Desktop."
  fi
}

require_daemon() {
  command -v docker >/dev/null 2>&1 || die "the docker CLI is not on PATH."
  docker info >/dev/null 2>&1 || die "no reachable Docker daemon. Start Docker Desktop or a colima VM, then re-run.
  checked endpoint: $(docker context inspect -f '{{.Endpoints.docker.Host}}' 2>/dev/null || echo unknown)
  $(command -v colima >/dev/null 2>&1 && echo "  colima is installed; 'colima start' creates the VM." || true)"
  require_compose
}

# Secrets go to .env only, never to stdout, so they cannot leak into a terminal recording
# or an agent transcript.
ensure_env() {
  if [[ -f .env ]]; then
    echo ".env already present; leaving its secrets untouched."
  else
    [[ -f .env.example ]] || die ".env.example is missing, cannot seed .env."
    (
      umask 077
      sed -e "s|^WORKER_API_KEYS=.*|WORKER_API_KEYS=$(openssl rand -hex 32)|" \
        -e "s|^CURATOR_API_KEYS=.*|CURATOR_API_KEYS=$(openssl rand -hex 32)|" \
        -e "s|^INTERNAL_TOKEN=.*|INTERNAL_TOKEN=$(openssl rand -hex 32)|" \
        .env.example >.env
    )
    echo ".env generated with fresh secrets ($(stat -f '%Sp' .env 2>/dev/null || stat -c '%A' .env))."
  fi

  local leftover
  leftover="$(grep -c '=replace_me' .env || true)"
  [[ "$leftover" == "0" ]] || die ".env still holds ${leftover} placeholder value(s); replace them before deploying."

  local name
  for name in WORKER_API_KEYS CURATOR_API_KEYS INTERNAL_TOKEN; do
    grep -q "^${name}=" .env || die ".env is missing ${name}."
  done

  mkdir -p data/qdrant data/history data/fastembed
  echo "data directories ready: $(ls -d data/* | tr '\n' ' ')"
  echo "read the keys when you need them with: grep '^CURATOR_API_KEYS=' .env"
}

first_key_of() {
  local value
  value="$(grep "^$1=" .env | head -1 | cut -d= -f2- | cut -d, -f1 | tr -d '[:space:]')"
  [[ -n "$value" ]] || die "could not read $1 from .env."
  printf '%s' "$value"
}

status_of() {
  curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$@"
}

wait_healthy() {
  local deadline=$((SECONDS + HEALTH_WAIT_SECONDS))
  echo "waiting for ${GATEWAY}/health (a first start downloads the embedder model, so allow minutes)..."
  while ((SECONDS < deadline)); do
    if curl -fsS --max-time 5 "${GATEWAY}/health" >/dev/null 2>&1; then
      echo "gateway is up."
      return 0
    fi
    sleep 3
  done

  require_compose
  echo "--- status ---" >&2
  $COMPOSE ps >&2 || true
  echo "--- backend logs ---" >&2
  $COMPOSE logs --tail 40 mem0-curated >&2 || true
  die "gateway never became healthy within ${HEALTH_WAIT_SECONDS}s. Raise HEALTH_WAIT_SECONDS if model download is slow on this host."
}

smoke_test() {
  [[ -f .env ]] || die "no .env; run ./deploy.sh env first."
  command -v curl >/dev/null 2>&1 || die "curl is required for the smoke test."

  local wk ck body failures=0
  wk="$(first_key_of WORKER_API_KEYS)"
  ck="$(first_key_of CURATOR_API_KEYS)"
  body='{"project_id":"deploy-smoke","record":{"memory_type":"experience","summary":"deploy self-test record","task_goal":"prove the gateway enforces roles","constraints":["docker compose"],"successful_solution":{"steps":["run deploy.sh smoke"],"verification":"http codes match the role table"},"failed_attempts":[],"applicable_scope":"deployment self-test only","exclude_scope":"must not be reused as a real finding","tags":["deploy-selftest"],"source_agent":"deploy.sh","confidence":0.3}}'

  check() {
    if [[ "$1" == "$2" ]]; then
      echo "  ok   $3 (got $2)"
    else
      echo "  FAIL $3 (expected $1, got $2)" >&2
      failures=$((failures + 1))
    fi
  }

  echo "smoke test:"
  check 200 "$(status_of "${GATEWAY}/health")" "gateway /health needs no key"
  check 200 "$(status_of "${DASHBOARD}/api/config")" "dashboard is up"
  check 200 "$(status_of "${DASHBOARD}/api/records?project_id=deploy-smoke")" "dashboard reads through the gateway"
  check 401 "$(status_of -X POST -H 'X-API-Key: deliberately-wrong' -H 'Content-Type: application/json' -d '{"query":"x","project_id":"deploy-smoke"}' "${GATEWAY}/v1/experiences/search")" "unknown key refused"
  check 403 "$(status_of -X POST -H "X-API-Key: ${wk}" -H 'Content-Type: application/json' -d "$body" "${GATEWAY}/v1/experiences")" "worker cannot write"
  check 200 "$(status_of -X POST -H "X-API-Key: ${wk}" -H 'Content-Type: application/json' -d '{"query":"roles","project_id":"deploy-smoke"}' "${GATEWAY}/v1/experiences/search")" "worker can search"

  local created
  if ! created="$(curl -fsS --max-time 90 -X POST -H "X-API-Key: ${ck}" -H 'Content-Type: application/json' -d "$body" "${GATEWAY}/v1/experiences")"; then
    die "curator write failed. First searches/writes load the embedder model; if this host is slow, re-run ./deploy.sh smoke."
  fi
  if ! printf '%s' "$created" | grep -q '"id"'; then
    echo "  FAIL curator write returned no id: ${created}" >&2
    failures=$((failures + 1))
  else
    echo "  ok   curator wrote a record"
    local id
    id="$(printf '%s' "$created" | sed -e 's/.*"id":"\([^"]*\)".*/\1/')"
    # There is no DELETE by design, so retiring the self-test record is the only way to keep
    # smoke noise out of real search results.
    if curl -fsS --max-time 30 -X POST -H "X-API-Key: ${ck}" -H 'Content-Type: application/json' \
      -d '{"project_id":"deploy-smoke","reason":"deployment self-test, retired"}' \
      "${GATEWAY}/v1/experiences/${id}/deprecate" | grep -q '"deprecated":true'; then
      echo "  ok   self-test record retired"
    else
      echo "  FAIL could not retire the self-test record; it is still in project deploy-smoke" >&2
      failures=$((failures + 1))
    fi
    check 200 "$(status_of -X POST -H "X-API-Key: ${wk}" -H 'Content-Type: application/json' -d '{"query":"roles","project_id":"deploy-smoke-other"}' "${GATEWAY}/v1/experiences/search")" "an unrelated project reads its own empty scope"
  fi

  ((failures == 0)) || die "${failures} smoke check(s) failed."
  echo "smoke test passed: read/write isolation holds end to end through the gateway."
}

run_up() {
  require_compose
  # Skipping the build when the image is already loaded is what lets an air-gapped host
  # deploy after `load`: building needs PyPI and the Qdrant registry, which it will not have.
  if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "image ${IMAGE} is already present; skipping the build. Delete it to force a rebuild."
  else
    $COMPOSE build
  fi
  $COMPOSE up -d
  echo "containers: $($COMPOSE ps --format '{{.Name}} {{.Status}}' 2>/dev/null | tr '\n' ';' || true)"
}

# Used by CI: the image was built and pushed by a runner, so this host only pulls.
# Pulling first means a failed pull (registry down, bad tag) leaves the old containers running.
run_pull() {
  require_daemon
  ensure_env
  [[ -n "${MEM0_CURATED_IMAGE:-}" ]] || die "MEM0_CURATED_IMAGE is required, e.g. ghcr.io/owner/mem0-curated:<sha>."
  docker pull "$IMAGE"
  $COMPOSE up -d --no-build
  echo "containers: $($COMPOSE ps --format '{{.Name}} {{.Status}}' 2>/dev/null | tr '\n' ';' || true)"
  wait_healthy
}

case "${1:-all}" in
  all)
    ensure_env
    require_daemon
    run_up
    wait_healthy
    smoke_test
    ;;
  env)
    ensure_env
    ;;
  up)
    # A registry image cannot be built from the deploy directory (no source tree there),
    # so `up` follows the same pull-then-start path as CI when one is requested.
    if [[ -n "${MEM0_CURATED_IMAGE:-}" ]]; then
      run_pull
    else
      require_daemon
      ensure_env
      run_up
    fi
    ;;
  pull)
    run_pull
    ;;
  smoke)
    wait_healthy
    smoke_test
    ;;
  down)
    require_daemon
    ensure_env
    $COMPOSE down
    echo "stopped. ./data is untouched, so the memory survives a restart."
    ;;
  save)
    [[ -n "${2:-}" ]] || die "usage: ./deploy.sh save <file.tar>"
    require_daemon
    docker image inspect "$IMAGE" >/dev/null 2>&1 || die "image ${IMAGE} is not built here. Run ./deploy.sh up first."
    docker image inspect "$QDRANT_IMAGE" >/dev/null 2>&1 || die "image ${QDRANT_IMAGE} is not present here. Run ./deploy.sh up once with network."
    # Both images: an air-gapped host cannot pull qdrant either.
    docker save -o "$2" "$IMAGE" "$QDRANT_IMAGE"
    echo "wrote $2 ($(du -h "$2" | cut -f1)). With ./data and .env that is the entire deployment."
    ;;
  load)
    [[ -n "${2:-}" ]] || die "usage: ./deploy.sh load <file.tar>"
    require_daemon
    docker load -i "$2"
    echo "loaded. ./deploy.sh up will find the images already present."
    ;;
  *)
    die "unknown command '${1}'. See the header comment in this file."
    ;;
esac
