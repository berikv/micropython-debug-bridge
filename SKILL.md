# MicroPython Debug Bridge

Use this bridge when you need host-side install/monitor/debug access to the MicroPython runtime on the connected MCU.

## Entry Point

Use the bundled CLI. It owns both server startup and client commands:

```bash
"$HOME"/.codex/skills/micropython-debug-bridge/scripts/mpy_bridge.sh help
```

## Start Server

Start the server with the connected serial port. The server does not infer project files from its working directory.

```bash
"$HOME"/.codex/skills/micropython-debug-bridge/scripts/mpy_bridge.sh serve --serial-port /dev/cu.usbmodem1101
```

Use the `/dev/cu.*` serial device on macOS when available.

Non-interactive starts automatically detach so the bridge survives the command
runner's time limit. Use `--daemon` to request that explicitly, or
`--foreground` when a supervisor must own the attached process. Stop a detached
server with:

```bash
"$HOME"/.codex/skills/micropython-debug-bridge/scripts/mpy_bridge.sh stop
```

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

- Ask for the bridge server to be restarted after editing the skill's bridge scripts.
- Use `install-and-monitor --runtime` when you need the debug runtime installed and active immediately.
- When calling the HTTP API directly, send install files as absolute paths in the `files` array.
- Prefer `call app.get_state` over ad hoc parsing of logs when you need structured app state.
