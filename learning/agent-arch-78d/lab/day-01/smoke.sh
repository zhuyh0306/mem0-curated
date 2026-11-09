#!/usr/bin/env bash
# Day 1 smoke test: one completion through LiteLLM, then prove the Langfuse
# trace landed. Ingestion is async, so the trace check polls for a while.
set -u

cd "$(dirname "$0")"

env_value() {
  sed -n "s/^$1=//p" .env | tail -1
}

MODEL="${SMOKE_MODEL:-fast}"
MASTER_KEY=$(env_value LITELLM_MASTER_KEY)
PUBLIC_KEY=$(env_value LANGFUSE_PUBLIC_KEY)
SECRET_KEY=$(env_value LANGFUSE_SECRET_KEY)
WAIT_SECONDS="${SMOKE_WAIT:-60}"

if [ -z "$MASTER_KEY" ] || [ -z "$PUBLIC_KEY" ] || [ -z "$SECRET_KEY" ]; then
  echo "missing keys in .env" >&2
  exit 1
fi

echo "1) completion via gateway"
response=$(curl -sS -m 120 http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $MASTER_KEY" \
  -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"用一句话说明什么是可复现环境。\"}],\"max_tokens\":80}" 2>&1)
if ! printf '%s' "$response" | jq -e '.choices[0].message.content' >/dev/null 2>&1; then
  echo "completion failed: $response" >&2
  exit 1
fi
printf '   model=%s\n   content=%s\n' \
  "$(printf '%s' "$response" | jq -r '.model')" \
  "$(printf '%s' "$response" | jq -r '.choices[0].message.content' | tr '\n' ' ')"

echo
echo "2) langfuse trace (up to ${WAIT_SECONDS}s)"
deadline=$(( $(date +%s) + WAIT_SECONDS ))
trace_id=""
while [ "$(date +%s)" -lt "$deadline" ]; do
  body=$(curl -sS -m 5 -u "$PUBLIC_KEY:$SECRET_KEY" \
    'http://127.0.0.1:3000/api/public/traces?limit=1' 2>/dev/null)
  trace_id=$(printf '%s' "$body" | jq -r '.data[0].id // empty' 2>/dev/null)
  [ -n "$trace_id" ] && break
  sleep 2
done

if [ -z "$trace_id" ]; then
  echo "no trace found in Langfuse; check langfuse-worker logs" >&2
  exit 1
fi

printf '   trace=%s\n   name=%s\n   url=http://localhost:3000/trace/%s\n' \
  "$trace_id" \
  "$(printf '%s' "$body" | jq -r '.data[0].name // ""')" \
  "$trace_id"
echo "smoke ok"
