#!/usr/bin/env bash
# Day 1 health check: TCP reachability for datastores, HTTP probes for services.
# Exit 0 only when every check passes, so it can gate the smoke test.
set -u

cd "$(dirname "$0")"

TIMEOUT="${CHECK_TIMEOUT:-3}"
PASS=0
FAIL=0
FAILED=()

env_value() {
  [ -f .env ] || return 0
  sed -n "s/^$1=//p" .env | tail -1
}

report() {
  local status="$1" name="$2" detail="$3"
  if [ "$status" = "ok" ]; then
    PASS=$((PASS + 1))
    printf '  \033[32m✓\033[0m %-26s %s\n' "$name" "$detail"
  else
    FAIL=$((FAIL + 1))
    FAILED+=("$name")
    printf '  \033[31m✗\033[0m %-26s %s\n' "$name" "$detail"
  fi
}

check_tcp() {
  local name="$1" host="$2" port="$3"
  if nc -z -w "$TIMEOUT" "$host" "$port" >/dev/null 2>&1; then
    report ok "$name" "$host:$port  reachable"
  else
    report fail "$name" "$host:$port  unreachable"
  fi
}

check_http() {
  local name="$1" url="$2"
  local out code time
  out=$(curl -s -m "$TIMEOUT" -o /dev/null -w '%{http_code} %{time_total}' "$url" 2>/dev/null)
  code=${out%% *}
  time=${out##* }
  if [ "$code" = "200" ]; then
    report ok "$name" "$url  ${time}s"
  else
    report fail "$name" "$url  HTTP ${code:-000}"
  fi
}

check_litellm_auth() {
  local key url out code
  key=$(env_value LITELLM_MASTER_KEY)
  url="http://127.0.0.1:4000/v1/models"
  if [ -z "$key" ]; then
    report fail "litellm /v1/models" "LITELLM_MASTER_KEY missing from .env"
    return
  fi
  code=$(curl -s -m "$TIMEOUT" -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $key" "$url")
  if [ "$code" = "200" ]; then
    report ok "litellm /v1/models" "authenticated 200"
  else
    report fail "litellm /v1/models" "HTTP ${code:-000} (check master key)"
  fi
}

echo "containers"
if docker compose ps --status running --format '{{.Name}}' 2>/dev/null | grep -q .; then
  docker compose ps --status running --format '  {{.Name}}  {{.Status}}' 2>/dev/null
else
  echo "  (none running)"
fi

echo
echo "ports"
check_tcp "postgres" 127.0.0.1 5432
check_tcp "redis" 127.0.0.1 6379
check_tcp "qdrant http" 127.0.0.1 6333
check_tcp "minio" 127.0.0.1 19000

echo
echo "http health"
check_http "qdrant" http://127.0.0.1:6333/healthz
check_http "clickhouse" http://127.0.0.1:8123/ping
check_http "minio" http://127.0.0.1:19000/minio/health/live
check_http "langfuse" http://127.0.0.1:3000/api/public/health
check_http "litellm" http://127.0.0.1:4000/health/liveliness
check_litellm_auth

echo
if [ "$FAIL" -eq 0 ]; then
  printf '\033[32m%d checks passed\033[0m\n' "$PASS"
  exit 0
fi
printf '\033[31m%d passed, %d failed:\033[0m %s\n' "$PASS" "$FAIL" "${FAILED[*]}"
exit 1
