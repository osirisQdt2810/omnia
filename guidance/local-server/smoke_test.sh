#!/usr/bin/env bash
# Smoke-test a self-hosted OpenAI-compatible endpoint the way Omnia uses it.
#
#   OMNIA_LLM_SSH=<your-host> guidance/local-server/smoke_test.sh [--cold] [--stop] [--no-image]
#
# Layers, in order, stopping at the first failure so it names where to look:
#   1. /health answers        -> the tunnel and the gateway are up (no key needed)
#   2. /status refuses no key -> auth is enforced
#   3. /status with the key   -> engine + GPU state
#   4. a chat completion      -> the text engine starts (if cold) and answers
#   5. an image, 1024x1024    -> the image engine, at the size Omnia requests
#
# --cold      POST /stop first, so step 4 measures a real cold start
# --stop      POST /stop at the end, handing the GPU back at once
# --no-image  skip step 5
#
# Environment (defaults in brackets):
#   OMNIA_LLM_SSH          SSH alias of the GPU host, used only to read the key
#   OMNIA_LLM_KEY          the key itself, instead of reading it over SSH
#   OMNIA_LLM_KEY_PATH     [~/workspaces/omnia-llm/api-key.txt] where the key lives on the host
#   OMNIA_LLM_BASE         [http://127.0.0.1:8721] the tunnel's local end
#   OMNIA_LLM_TEXT_MODEL   [omnia-local]
#   OMNIA_LLM_IMAGE_MODEL  [sdxl-turbo]
#
# The key is never printed and never put on a command line: it goes into a 0600 temp file that
# curl reads its header from, deleted on exit. Written for the bash 3.2 macOS ships.
set -euo pipefail

BASE="${OMNIA_LLM_BASE:-http://127.0.0.1:8721}"
KEY_PATH="${OMNIA_LLM_KEY_PATH:-~/workspaces/omnia-llm/api-key.txt}"
TEXT_MODEL="${OMNIA_LLM_TEXT_MODEL:-omnia-local}"
IMAGE_MODEL="${OMNIA_LLM_IMAGE_MODEL:-sdxl-turbo}"
COLD=0; STOP=0; IMAGE=1
for arg in "$@"; do
  case "$arg" in
    --cold) COLD=1 ;;
    --stop) STOP=1 ;;
    --no-image) IMAGE=0 ;;
    -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
HEADER="$WORK/auth-header"

pass() { printf '  \033[32mPASS\033[0m  %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n        -> %s\n' "$1" "$2"; exit 1; }

# curl METHOD PATH [DATA] -> sets CODE and TIME; the body lands in $WORK/body
call() {
  local method="$1" path="$2" data="${3:-}" auth=()
  [ -f "$HEADER" ] && auth=(-H "@$HEADER")
  local out
  if [ -n "$data" ]; then
    out="$(curl -s -m 900 -X "$method" "${auth[@]+"${auth[@]}"}" -H 'Content-Type: application/json' \
      -d "$data" -o "$WORK/body" -w '%{http_code} %{time_total}' "$BASE$path" || echo "000 0")"
  else
    out="$(curl -s -m 30 -X "$method" "${auth[@]+"${auth[@]}"}" \
      -o "$WORK/body" -w '%{http_code} %{time_total}' "$BASE$path" || echo "000 0")"
  fi
  CODE="${out%% *}"; TIME="${out##* }"
  # Two decimals, whatever curl prints (three before 7.73, six since). LC_ALL=C because awk formats
  # in the user's locale — a comma-decimal system would print "0,42" — while curl always writes ".".
  TIME="$(LC_ALL=C awk -v t="$TIME" 'BEGIN { printf "%.2f", t }')"
}

echo "Endpoint: $BASE"

# 1 ---------------------------------------------------------------------------------------------
call GET /health
[ "$CODE" = "200" ] || fail "health" "no answer on $BASE (HTTP $CODE). Is the tunnel up? launchctl list | grep omnia.llm"
pass "health — the tunnel and the gateway are up"

# 2 ---------------------------------------------------------------------------------------------
call GET /status
[ "$CODE" = "401" ] || fail "auth" "/status answered HTTP $CODE without a key; expected 401. Stop and check the gateway's auth."
pass "auth — a request without the key is refused (401)"

# The key, into a private file -------------------------------------------------------------------
umask 077
if [ -n "${OMNIA_LLM_KEY:-}" ]; then
  KEY="$OMNIA_LLM_KEY"
elif [ -n "${OMNIA_LLM_SSH:-}" ]; then
  KEY="$(ssh -o ConnectTimeout=20 "$OMNIA_LLM_SSH" "cat $KEY_PATH")" \
    || fail "key" "could not read $KEY_PATH over ssh $OMNIA_LLM_SSH"
else
  fail "key" "set OMNIA_LLM_SSH=<your-host> (or OMNIA_LLM_KEY) so the script can authenticate"
fi
[ -n "$KEY" ] || fail "key" "the key came back empty"
printf 'Authorization: Bearer %s\n' "$KEY" > "$HEADER"
unset KEY

# 3 ---------------------------------------------------------------------------------------------
engines() {
  python3 - "$WORK/body" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
for role, e in (("text", d["engine"]), ("image", d["images"])):
    state = f"running on GPU {e['gpu']}, idle {e['idle_seconds']:.0f}s" if e["running"] else "stopped"
    print(f"        {role:5} {e['model']}: {state}")
free = [g["index"] for g in d["gpus"] if g["free"]]
print(f"        free GPUs: {free if free else 'none'}")
PY
}
call GET /status
[ "$CODE" = "200" ] || fail "status" "HTTP $CODE with the key — is it the current one? $(head -c 200 "$WORK/body")"
pass "status — the key is accepted"
engines

if [ "$COLD" = "1" ]; then
  call POST /stop
  [ "$CODE" = "200" ] || fail "stop" "HTTP $CODE from /stop"
  pass "stopped both engines, so the next request is a cold start"
fi

# 4 ---------------------------------------------------------------------------------------------
call POST /v1/chat/completions "$(printf '{"model":"%s","messages":[{"role":"user","content":"Write a one-sentence definition of the word ephemeral for a flashcard. Output only the definition."}],"max_tokens":80,"temperature":0.3}' "$TEXT_MODEL")"
case "$CODE" in
  200) ;;
  503) fail "text" "no free GPU right now (503, Retry-After 120). Nothing is broken — try later." ;;
  502) fail "text" "the engine failed to start: $(head -c 300 "$WORK/body"). Check disk space (df -h /home) and the host's journal." ;;
  *) fail "text" "HTTP $CODE: $(head -c 300 "$WORK/body")" ;;
esac
REPLY="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["choices"][0]["message"]["content"].strip()[:120])' "$WORK/body")"
[ -n "$REPLY" ] || fail "text" "HTTP 200 but an empty reply"
pass "text in ${TIME}s — \"$REPLY\""

# 5 ---------------------------------------------------------------------------------------------
if [ "$IMAGE" = "1" ]; then
  call POST /v1/images/generations "$(printf '{"model":"%s","prompt":"a lighthouse at dusk, flat illustration","size":"1024x1024","response_format":"b64_json"}' "$IMAGE_MODEL")"
  [ "$CODE" = "200" ] || fail "image" "HTTP $CODE: $(head -c 300 "$WORK/body")"
  OUT="${TMPDIR:-/tmp}"; OUT="${OUT%/}/omnia-smoke-$(date +%Y%m%d-%H%M%S).png"
  DIMS="$(python3 - "$WORK/body" "$OUT" <<'PY'
import base64, json, struct, sys
png = base64.b64decode(json.load(open(sys.argv[1]))["data"][0]["b64_json"])
if png[:8] != b"\x89PNG\r\n\x1a\n":
    sys.exit("not a PNG")
open(sys.argv[2], "wb").write(png)
w, h = struct.unpack(">II", png[16:24])
print(f"{w}x{h}, {len(png) // 1024} KB")
PY
)" || fail "image" "the response was not a valid PNG"
  pass "image in ${TIME}s — $DIMS, saved to $OUT"
fi

call GET /status
if [ "$CODE" = "200" ]; then
  engines
else
  echo "        (could not read /status afterwards: HTTP $CODE — the layers above still passed)"
fi

if [ "$STOP" = "1" ]; then
  # Loud, not best-effort: --stop is how a colleague gets the card back, and a silent failure
  # here would report "passed" while the GPU stayed taken.
  call POST /stop
  [ "$CODE" = "200" ] || fail "stop" "HTTP $CODE from /stop — the GPU may still be held; check /status"
  pass "handed the GPU back (/stop)"
fi
echo "All layers passed."
