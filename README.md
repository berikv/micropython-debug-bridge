# MicroPython Debug Bridge

A Codex plugin that gives agents direct, structured access to a connected
MicroPython MCU through a local stdio MCP server.

The MCP server is launched by the Codex host, outside the agent command
sandbox. It opens the selected TTY itself, so it does not depend on localhost,
`curl`, a background HTTP server, or shell-script approvals.

## Features

- Lists currently connected macOS `/dev/cu.*` character devices.
- Reports whether each device is readable and writable by the MCP host.
- Lets the agent select an exact device returned by discovery.
- Copies MicroPython files to the MCU with `mpremote`.
- Resets the MCU and monitors serial output at 115200 baud.
- Buffers serial logs and supports cursor-based incremental reads.
- Installs an optional on-device runtime for structured state inspection,
  exported function calls, and focused expression or statement evaluation.
- Keeps serial access in one MCP process and serializes runtime requests.
- Has no network listener and no MCP shutdown tool.

## Requirements

- macOS with a USB serial device exposed as `/dev/cu.*`.
- Python 3 available as `python3`.
- [`mpremote`](https://docs.micropython.org/en/latest/reference/mpremote.html)
  on the Codex host `PATH` for file installation and device resets:

  ```bash
  pipx install mpremote
  ```

Device listing, selection, direct monitoring, and runtime calls use only the
Python standard library. `mpremote` is not needed merely to list devices.

## Install

Install this repository as a Codex plugin marketplace:

```bash
codex plugin marketplace add berikv/micropython-debug-bridge
codex plugin add micropython-debug-bridge@micropython-debug-bridge
```

For a local checkout, run this from anywhere:

```bash
codex plugin marketplace add /absolute/path/to/micropython-debug-bridge
codex plugin add micropython-debug-bridge@micropython-debug-bridge
```

Restart Codex after installation. The plugin starts the bundled stdio MCP
server automatically; do not run `mpy_mcp_server.py` by hand.

To update a Git-backed installation:

```bash
codex plugin marketplace upgrade micropython-debug-bridge
```

Then restart Codex so it launches the updated MCP server.

## TTY Access

The plugin MCP configuration launches `python3 scripts/mpy_mcp_server.py` as a
local host process. This is deliberately separate from the sandbox used for
agent shell commands. The MCP process discovers and opens `/dev/cu.*` directly
and requests exclusive ownership while monitoring.

Start a new Codex chat and ask:

> List the connected MicroPython serial devices.

The `list_serial_ports` result includes `readable` and `writable` for every
device. Select only an exact returned path:

> Select `/dev/cu.usbmodem11201` and show the bridge status.

If a device reports `writable: false`, the host OS denied the MCP process.
Close applications that may own the serial port, confirm the device node is
accessible to your macOS user, and restart Codex so the plugin process inherits
the corrected access. The agent cannot repair host permissions or launch a
replacement bridge from its shell.

Set `MPY_SERIAL_PORT` in the environment that launches Codex to provide an
initial selection. The device must still be present, readable, writable, and
returned by discovery:

```bash
MPY_SERIAL_PORT=/dev/cu.usbmodem11201 codex
```

Explicit `list_serial_ports` and `select_serial_port` calls remain the preferred
workflow because macOS device suffixes can change after reconnecting USB.

## MCP Tools

| Tool | Purpose |
| --- | --- |
| `list_serial_ports` | List supported connected devices and access status. |
| `select_serial_port` | Select an exact discovered `/dev/cu.*` path. |
| `get_bridge_status` | Show selection, monitor state, errors, and `mpremote`. |
| `install_files` | Copy absolute host paths and optionally install the runtime. |
| `install_debug_runtime` | Install the bundled on-device helper. |
| `remove_debug_runtime` | Remove the helper and reset the MCU. |
| `start_serial_monitor` | Open the selected TTY and collect output. |
| `read_serial_log` | Read buffered output by tail count or cursor. |
| `reset_device` | Reset the selected MCU with `mpremote`. |
| `get_runtime_state` | Call the exported `app.get_state`. |
| `call_runtime_function` | Call an application-exported function. |
| `evaluate_runtime` | Evaluate code in the application debug context. |

Tools that install, remove, reset, or evaluate code are marked as
side-effecting or destructive in their MCP annotations so Codex can apply its
normal approval policy.

## Typical Agent Workflow

1. Call `list_serial_ports`.
2. Call `select_serial_port` with an exact result.
3. Call `get_bridge_status`.
4. Call `install_files` with absolute project file paths and `monitor: true`.
5. Call `read_serial_log`, passing its returned `cursor` as `since` on later
   reads.
6. If structured debugging is needed, install the runtime and use
   `get_runtime_state` or `call_runtime_function`.

## On-Device Runtime

The plugin bundles `codex_debug_runtime.py`. Install it through
`install_debug_runtime` or set `include_debug_runtime: true` on `install_files`.
The MicroPython application must poll `RuntimeShell`:

```python
from codex_debug_runtime import RuntimeDispatcher, RuntimeShell

dispatcher = RuntimeDispatcher(
    functions={
        "app.get_state": get_state,
        "app.set_led": set_led,
    }
)
debug_shell = RuntimeShell(dispatcher)

while True:
    debug_shell.poll()
    run_one_application_iteration()
```

`app.get_state` enables `get_runtime_state`. Other entries in `functions`
become available to `call_runtime_function`. Runtime traffic uses length-framed
JSON on the same serial stream, while ordinary application logs remain plain
text.

## Repository Layout

```text
.agents/plugins/marketplace.json
plugins/micropython-debug-bridge/
  .codex-plugin/plugin.json
  .mcp.json
  skills/micropython-debug-bridge/SKILL.md
  scripts/mpy_mcp_server.py
  scripts/codex_debug_runtime.py
tests/test_mpy_mcp_server.py
```

Run the test suite without a physical MCU:

```bash
python3 -m unittest discover -s tests -v
```
