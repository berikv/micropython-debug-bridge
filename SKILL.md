---
name: micropython-debug-bridge
description: Use a human-started local bridge for host-side install, monitor, and debug access to a connected MicroPython MCU. Agents may use bridge client commands after a human starts it, but must never start or restart the bridge process themselves.
---

# MicroPython Debug Bridge

Use this bridge when you need host-side install/monitor/debug access to the MicroPython runtime on the connected MCU.

## Human-Owned Server Lifecycle

- Never start or restart `mpy_debug_server.py`, `mpy_bridge.sh serve`, or an
  equivalent bridge server process yourself.
- If the bridge is unavailable, tell the human how to start it and wait for
  them to confirm that it is running. Do not execute the start command.
- Use bridge client commands only after the human has started the server.
- Never call `mpy_bridge.sh stop` or `POST /shutdown` unless the human
  explicitly asks you to shut down the bridge.
- Before an explicitly requested shutdown, warn the human that only they may
  start the bridge again and that subsequent bridge work will remain blocked
  until they do.
- After changing bridge server scripts, tell the human that they must manually
  stop and restart the bridge for the changes to take effect.

## Entry Point

Use the bundled CLI for client commands:

```bash
"$HOME"/.codex/skills/micropython-debug-bridge/scripts/mpy_bridge.sh help
```

## Human Start Instructions

When the bridge is unavailable, give the human this command. Do not run it:

```bash
"$HOME"/.codex/skills/micropython-debug-bridge/scripts/mpy_bridge.sh serve --serial-port /dev/cu.usbmodem1101
```

Ask the human to replace the example port with the connected device. Recommend
the `/dev/cu.*` serial device on macOS when available. The server does not infer
project files from its working directory.

Listener-loop errors and final diagnostic snapshots are stored in
`mpy-bridge-logs/server-8765.log`; detached server output is written there as
well. Incidental `SIGTERM` and `SIGHUP` remain ignored unless
`--stop-on-sigterm` is supplied.

Runtime calls share one serial stream and are serialized by the server. Health
and debugging endpoints remain responsive during a long runtime call. Inspect
`health` for active request, runtime request, monitor-thread, and monitor-error
details; use `debug-threads` for thread stacks and recently completed requests.

## Runtime Model

The shared MCU runtime is bundled at `scripts/codex_debug_runtime.py` and is installed onto the MCU only when requested, for example with `install-and-monitor --runtime`.

Install requests must provide the application file paths on the client side. HTTP clients should send absolute paths in `files`, for example `{"files":["/abs/path/main.py"]}`. The bundled CLI accepts absolute paths explicitly; if none are provided, it expands `*.py` in the current directory and sends those absolute paths.

The device protocol is framed JSON over serial. Use `call` for exported app functions and `eval` / `exec` for research work.

## Usage

Use the CLI help as the source of truth:

```bash
"$HOME"/.codex/skills/micropython-debug-bridge/scripts/mpy_bridge.sh help
```

Common commands:

```bash
"$HOME"/.codex/skills/micropython-debug-bridge/scripts/mpy_bridge.sh install-and-monitor --runtime
"$HOME"/.codex/skills/micropython-debug-bridge/scripts/mpy_bridge.sh install-and-monitor --runtime /abs/path/main.py /abs/path/lib.py
"$HOME"/.codex/skills/micropython-debug-bridge/scripts/mpy_bridge.sh state
"$HOME"/.codex/skills/micropython-debug-bridge/scripts/mpy_bridge.sh call app.get_state
"$HOME"/.codex/skills/micropython-debug-bridge/scripts/mpy_bridge.sh logs --tail 50
```

## Guidance

- Use `install-and-monitor --runtime` when you need the debug runtime installed and active immediately.
- When calling the HTTP API directly, send install files as absolute paths in the `files` array.
- Prefer `call app.get_state` over ad hoc parsing of logs when you need structured app state.
