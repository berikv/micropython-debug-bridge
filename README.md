# MicroPython Debug Bridge

Generic host-side bridge for MicroPython projects. A human starts the server with access to a specific serial port; agents and scripts then use the HTTP/CLI bridge to install files, reset the MCU, monitor serial output, and call the optional on-device debug runtime.

## Features

This bridge and skill makes it possible for an LLM to:

1. Install **micropython** files to an **MCU** (ESP32, RP2040, etc)
2. Monitor program output
3. Install a runtime which adds support for:
  - Inspect threads variables
  - Run functions

## Install

Clone the repository into your personal Codex skills directory:

```bash
mkdir -p "$HOME/.codex/skills"
git clone https://github.com/berikv/micropython-debug-bridge.git \
  "$HOME/.codex/skills/micropython-debug-bridge"
```

Confirm that the bridge CLI is available:

```bash
"$HOME/.codex/skills/micropython-debug-bridge/scripts/mpy_bridge.sh" help
```

To update an existing installation:

```bash
git -C "$HOME/.codex/skills/micropython-debug-bridge" pull
```

## Start

Start the server with the connected serial port:

```bash
"$HOME/.codex/skills/micropython-debug-bridge/scripts/mpy_bridge.sh" \
  serve --serial-port "/dev/cu.usbmodem1101"
```

When started from a non-interactive command runner, the server automatically
detaches into a new process session so the runner cannot kill it when a command
time budget expires. Interactive terminal starts stay in the foreground and
stop with Ctrl-C. Use `--daemon` or `--foreground` to select the mode explicitly.
Stop a detached server through the local HTTP endpoint:

```bash
"$HOME/.codex/skills/micropython-debug-bridge/scripts/mpy_bridge.sh" stop
```

Server listener-loop exceptions and the final listener/health snapshot are
written to `mpy-bridge-logs/server-8765.log`; detached server output is written
there as well. The detached process ID is stored in
`mpy-bridge-logs/server-8765.pid` while it is running.

The server accepts:

- `--serial-port`
- `--host`, defaulting to `127.0.0.1`
- `--port`, defaulting to `8765`
- `--daemon`, to detach explicitly
- `--foreground`, to stay attached explicitly
- `--stop-on-sigterm`, for process supervisors that intentionally stop services
  with `SIGTERM`

The server ignores incidental `SIGTERM` and `SIGHUP` by default. Foreground
servers stop with Ctrl-C; detached servers stop with `mpy_bridge.sh stop`.

The HTTP server accepts requests on independent daemon threads with a backlog of
64 connections. Runtime requests are serialized because they share one framed
serial stream; `/health`, `/logs`, and `/debug/threads` remain available while a
device call is active or waiting. `/health` reports active HTTP and runtime
requests, monitor-thread state, and the last monitor error. `/debug/threads`
also includes the 50 most recently completed HTTP requests.

## Skill

Use the micropython-debug-bridge skill to communicate with the server.

## Install Files

The server does not discover project files. Clients must choose the files to install and send absolute paths:

```json
{"files":["/absolute/path/main.py","/absolute/path/lib.py"]}
```

The bundled CLI accepts absolute paths:

```bash
"$MPY_DEBUG_BRIDGE"/mpy_bridge.sh install /absolute/path/main.py /absolute/path/lib.py
"$MPY_DEBUG_BRIDGE"/mpy_bridge.sh install-and-monitor --runtime /absolute/path/main.py
```

If no paths are passed to the CLI install commands, the CLI expands `*.py` in its current directory and sends those absolute paths.

## Runtime

The shared device runtime is `codex_debug_runtime.py`. It is installed only on request:

```bash
"$MPY_DEBUG_BRIDGE"/mpy_bridge.sh install-runtime
"$MPY_DEBUG_BRIDGE"/mpy_bridge.sh remove-runtime
```

Apps can conditionally import it:

```python
try:
    from codex_debug_runtime import RuntimeShell
except ImportError:
    RuntimeShell = None
```

If installed and polled by the app, the runtime accepts framed JSON requests and returns framed JSON responses. Human-readable logs remain plain serial output.
