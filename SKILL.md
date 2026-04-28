# Micro Python Debug Bridge

Use this bridge when you need host-side install/monitor/debug access to the MicroPython runtime on the connected MCU.

## Start

A human starts the bridge with an explicit serial port:

```bash
python3 mpy_debug_server.py --serial-port "/dev/cu.usbmodem1101"
```

## Usage

Use the bridge CLI.

Use `"$MPY_DEBUG_BRIDGE"/mpy_bridge.sh help` for the authoritative command list. The most important commands are:

- `"$MPY_DEBUG_BRIDGE"/mpy_bridge.sh help`
- `"$MPY_DEBUG_BRIDGE"/mpy_bridge.sh health`
- `"$MPY_DEBUG_BRIDGE"/mpy_bridge.sh logs [tail]`
- `"$MPY_DEBUG_BRIDGE"/mpy_bridge.sh logs --tail [n]`
- `"$MPY_DEBUG_BRIDGE"/mpy_bridge.sh debug-threads`
- `"$MPY_DEBUG_BRIDGE"/mpy_bridge.sh install [--runtime]`
- `"$MPY_DEBUG_BRIDGE"/mpy_bridge.sh monitor`
- `"$MPY_DEBUG_BRIDGE"/mpy_bridge.sh reset`
- `"$MPY_DEBUG_BRIDGE"/mpy_bridge.sh install-and-monitor [--runtime]`
- `"$MPY_DEBUG_BRIDGE"/mpy_bridge.sh install-runtime`
- `"$MPY_DEBUG_BRIDGE"/mpy_bridge.sh install-runtime-and-monitor`
- `"$MPY_DEBUG_BRIDGE"/mpy_bridge.sh remove-runtime`
- `"$MPY_DEBUG_BRIDGE"/mpy_bridge.sh state [timeout_sec]`
- `"$MPY_DEBUG_BRIDGE"/mpy_bridge.sh call <function> [args_json] [kwargs_json] [timeout_sec]`
- `"$MPY_DEBUG_BRIDGE"/mpy_bridge.sh eval <expression> [timeout_sec]`
- `"$MPY_DEBUG_BRIDGE"/mpy_bridge.sh exec <statement_code> [timeout_sec]`
- `"$MPY_DEBUG_BRIDGE"/mpy_bridge.sh runtime '<request_json>' [timeout_sec]`

## Runtime model

The shared device runtime lives in `"$MPY_DEBUG_BRIDGE"/codex_debug_runtime.py` and is installed onto the MCU only when requested.

The device runtime uses a framed serial protocol:

- request/response header: `@@FRAME@@ <payload_length>`
- payload: JSON object
- runtime modes:
  - `get_state`
  - `call`
  - `eval`

Use `call` for exported app functions and `eval` for research/debug work that needs arbitrary on-device Python.

## Logs

- Machine responses travel over framed runtime messages.
- Human-readable device logs remain plain serial lines.
- For live logs, use:

```bash
"$MPY_DEBUG_BRIDGE"/mpy_bridge.sh logs --tail 50
```

## Guidance

- Ask for the bridge server to be restarted after editing host-side bridge code.
- Use `install-and-monitor --runtime` when you need the debug runtime installed and active immediately.
- Use `remove-runtime` when you want the app to run without the debug runtime on the MCU.
- Prefer `call app.get_state` over ad hoc parsing of logs when you need structured app state.
