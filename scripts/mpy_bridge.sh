#!/bin/bash

set -euo pipefail

BASE_URL="${MPY_BRIDGE_URL:-http://127.0.0.1:8765}"
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )

usage() {
  cat <<'EOF'
Usage:
  mpy_bridge.sh help
  mpy_bridge.sh serve --serial-port [--daemon|--foreground]
  mpy_bridge.sh stop
  mpy_bridge.sh health
  mpy_bridge.sh logs [tail]
  mpy_bridge.sh logs --tail [n]
  mpy_bridge.sh debug-threads
  mpy_bridge.sh install [--runtime] [absolute_file ...]
  mpy_bridge.sh monitor
  mpy_bridge.sh reset
  mpy_bridge.sh install-and-monitor [--runtime] [absolute_file ...]
  mpy_bridge.sh install-runtime
  mpy_bridge.sh install-runtime-and-monitor
  mpy_bridge.sh remove-runtime
  mpy_bridge.sh state [timeout_sec]
  mpy_bridge.sh call <function> [args_json] [kwargs_json] [timeout_sec]
  mpy_bridge.sh eval <expression> [timeout_sec]
  mpy_bridge.sh exec <statement_code> [timeout_sec]
  mpy_bridge.sh runtime '<request_json>' [timeout_sec]
EOF
}

json_post() {
  local path="$1"
  local body="$2"
  curl -sS -X POST "${BASE_URL}${path}" \
    -H 'Content-Type: application/json' \
    -d "$body"
}

python_json() {
  python3 - "$@"
}

install_body() {
  local include_runtime="$1"
  shift
  python_json "$include_runtime" "$@" <<'PY'
import glob
import json
import os
import sys

include_runtime = sys.argv[1] == "true"
files = sys.argv[2:]
if not files:
    files = sorted(os.path.abspath(path) for path in glob.glob("*.py"))
if not files:
    raise SystemExit("no Python files specified and none found in current directory")
for path in files:
    if not os.path.isabs(path):
        raise SystemExit("install file path must be absolute: {}".format(path))
    if not os.path.isfile(path):
        raise SystemExit("install file does not exist: {}".format(path))
payload = {"files": files}
if include_runtime:
    payload["debug_runtime"] = True
print(json.dumps(payload))
PY
}

action="${1:-}"

case "$action" in
  help)
    usage
    ;;
  serve)
    exec python3 "$SCRIPT_DIR"/mpy_debug_server.py "${@:2}"
    ;;
  stop)
    json_post "/shutdown" "{}"
    ;;
  health)
    exec curl -sS "${BASE_URL}/health"
    ;;
  logs)
    if [[ "${2:-}" == "--tail" ]]; then
      tail_lines="${3:-50}"
      exec python3 - "${BASE_URL}" "${tail_lines}" <<'PY'
import json
import sys
import time
import urllib.request

base_url = sys.argv[1]
tail = int(sys.argv[2])


def fetch(path):
    with urllib.request.urlopen(base_url + path, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


payload = fetch("/logs?tail={}".format(tail))
for line in payload.get("lines", []):
    print(line, flush=True)
cursor = payload.get("cursor", 0)

while True:
    try:
        payload = fetch("/logs?since={}".format(cursor))
        for line in payload.get("lines", []):
            print(line, flush=True)
        cursor = payload.get("cursor", cursor)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        print("log stream error: {}".format(exc), file=sys.stderr, flush=True)
        time.sleep(1)
    time.sleep(0.25)
PY
    fi
    tail_lines="${2:-80}"
    exec curl -sS "${BASE_URL}/logs?tail=${tail_lines}"
    ;;
  debug-threads)
    exec curl -sS "${BASE_URL}/debug/threads"
    ;;
  install)
    if [[ "${2:-}" == "--runtime" ]]; then
      shift 2
      body="$(install_body true "$@")"
    else
      shift
      body="$(install_body false "$@")"
    fi
    json_post "/install" "${body}"
    ;;
  monitor)
    json_post "/monitor" "{}"
    ;;
  reset)
    json_post "/reset" "{}"
    ;;
  install-and-monitor)
    if [[ "${2:-}" == "--runtime" ]]; then
      shift 2
      body="$(install_body true "$@")"
    else
      shift
      body="$(install_body false "$@")"
    fi
    json_post "/install-and-monitor" "${body}"
    ;;
  install-runtime)
    json_post "/install-runtime" "{}"
    ;;
  install-runtime-and-monitor)
    json_post "/install-runtime-and-monitor" "{}"
    ;;
  remove-runtime)
    json_post "/remove-runtime" "{}"
    ;;
  state)
    timeout_sec="${2:-60}"
    json_post "/state" "{\"timeout_sec\":${timeout_sec}}"
    ;;
  call)
    if [[ $# -lt 2 ]]; then
      usage >&2
      exit 1
    fi
    function_name="$2"
    args_json="${3:-[]}"
    kwargs_json="${4:-{}}"
    timeout_sec="${5:-60}"
    body="$(python_json "$function_name" "$args_json" "$kwargs_json" "$timeout_sec" <<'PY'
import json
import sys

function_name, args_json, kwargs_json, timeout_sec = sys.argv[1:5]
payload = {
    "function": function_name,
    "args": json.loads(args_json),
    "kwargs": json.loads(kwargs_json),
    "timeout_sec": int(timeout_sec),
}
print(json.dumps(payload))
PY
)"
    json_post "/call" "${body}"
    ;;
  eval)
    if [[ $# -lt 2 ]]; then
      usage >&2
      exit 1
    fi
    code="$2"
    timeout_sec="${3:-60}"
    body="$(python_json "$code" "$timeout_sec" <<'PY'
import json
import sys

print(json.dumps({"code": sys.argv[1], "timeout_sec": int(sys.argv[2])}))
PY
)"
    json_post "/eval" "${body}"
    ;;
  exec)
    if [[ $# -lt 2 ]]; then
      usage >&2
      exit 1
    fi
    code="$2"
    timeout_sec="${3:-60}"
    body="$(python_json "$code" "$timeout_sec" <<'PY'
import json
import sys

print(json.dumps({"code": sys.argv[1], "statement": True, "timeout_sec": int(sys.argv[2])}))
PY
)"
    json_post "/eval" "${body}"
    ;;
  runtime)
    if [[ $# -lt 2 ]]; then
      usage >&2
      exit 1
    fi
    request_json="$2"
    timeout_sec="${3:-60}"
    body="$(python_json "$request_json" "$timeout_sec" <<'PY'
import json
import sys

print(json.dumps({"request": json.loads(sys.argv[1]), "timeout_sec": int(sys.argv[2])}))
PY
)"
    json_post "/runtime" "${body}"
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
