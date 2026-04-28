# MicroPython Debug Bridge

Use this bridge when you need host-side install/monitor/debug access to the MicroPython runtime on the connected MCU.

## Entry Point

Use the bundled CLI. It owns both server startup and client commands:

```bash
/Users/berik/.codex/skills/micropython-debug-bridge/scripts/mpy_bridge.sh help
```

## Start Server

Start the server from the MicroPython project directory. The current working directory is the project root.

```bash
/Users/berik/.codex/skills/micropython-debug-bridge/scripts/mpy_bridge.sh serve --serial-port /dev/cu.usbmodem1101
```

Use the `/dev/cu.*` serial device on macOS when available. Use `MPY_PROJECT_ROOT=/path/to/project` only when starting from a different directory.

## Runtime Model

The shared MCU runtime is bundled at `scripts/codex_debug_runtime.py` and is installed onto the MCU only when requested, for example with `install-and-monitor --runtime`.

The device protocol is framed JSON over serial. Use `call` for exported app functions and `eval` / `exec` for research work.

## Usage

Use the CLI help as the source of truth:

```bash
/Users/berik/.codex/skills/micropython-debug-bridge/scripts/mpy_bridge.sh help
```

Common commands:

```bash
/Users/berik/.codex/skills/micropython-debug-bridge/scripts/mpy_bridge.sh install-and-monitor --runtime
/Users/berik/.codex/skills/micropython-debug-bridge/scripts/mpy_bridge.sh state
/Users/berik/.codex/skills/micropython-debug-bridge/scripts/mpy_bridge.sh call app.get_state
/Users/berik/.codex/skills/micropython-debug-bridge/scripts/mpy_bridge.sh logs --tail 50
```

## Guidance

- Ask for the bridge server to be restarted after editing the skill's bridge scripts.
- Use `install-and-monitor --runtime` when you need the debug runtime installed and active immediately.
- Prefer `call app.get_state` over ad hoc parsing of logs when you need structured app state.
