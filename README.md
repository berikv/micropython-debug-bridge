# Micro Python Debug Bridge

Generic host-side bridge for MicroPython projects. A human starts the server with access to a specific serial port; agents and scripts then use the HTTP/CLI bridge to install files, reset the MCU, monitor serial output, and call the optional on-device debug runtime.

## Start

From an app repository:

```bash
python3 mpy_debug_server.py --serial-port "/dev/cu.usbmodem1101"
MPY_DEBUG_BRIDGE="/path/to/debug_bridge"
```

The server accepts:

- `--serial-port`
- `--host`, defaulting to `127.0.0.1`
- `--port`, defaulting to `8765`

## Skill

Use the micropython-debug-bridge skill to communicate with the server.

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
