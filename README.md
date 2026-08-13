# MicroPython Debug Bridge

A Codex plugin that gives agents direct, structured access to MicroPython MCUs
over USB serial and local-network OTA through a stdio MCP server.

The MCP server is launched by the Codex host, outside the agent command
sandbox. It opens the selected TTY itself, so it does not depend on localhost,
`curl`, a background HTTP server, or shell-script approvals.

## Features

- Lists currently connected macOS `/dev/cu.*` character devices.
- Reports whether each device is readable and writable by the MCP host.
- Lets the agent select an exact device returned by discovery.
- Copies MicroPython files to the MCU with `mpremote`.
- Sends repeated direct serial interrupts before each `mpremote` handoff.
- Resets the MCU and monitors serial output at 115200 baud.
- Buffers serial logs and supports cursor-based incremental reads.
- Installs an optional on-device runtime for structured state inspection,
  exported function calls, and focused expression or statement evaluation.
- Keeps serial access in one MCP process and serializes runtime requests.
- Retains takeover RX/TX evidence when raw REPL cannot be entered.
- Provisions a small polling OTA service and WiFi settings over serial.
- Discovers multiple OTA-capable MCUs by stable hardware ID, friendly name,
  station MAC address, and current IP endpoint.
- Streams application files over WiFi, verifies SHA-256 on the MCU, and resets
  into the new application.
- Has no network listener and no MCP shutdown tool.

## Requirements

- macOS with a USB serial device exposed as `/dev/cu.*`.
- Python 3 available as `python3`.
- A network-capable MicroPython board for OTA, such as a Pico W, with the Codex
  OTA service polled by its application.
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
| `identify_serial_device` | Read the selected MCU's stable ID, friendly name, and WiFi MAC. |
| `provision_ota` | Install the OTA helper and WiFi/device configuration over serial. |
| `list_ota_devices` | Discover all responding OTA MCUs on the local network. |
| `select_ota_device` | Select one exact stable device ID from OTA discovery. |
| `install_files_ota` | Upload files to the selected MCU, verify them, and optionally reset. |
| `get_bridge_status` | Show selection, monitor state, `mpremote`, and the last device-control evidence. |
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

Before each `mpremote` command, the MCP process releases its monitor and sends
repeated Ctrl-C interrupts directly to the selected TTY. If `mpremote` still
cannot enter raw REPL, the tool error and `get_bridge_status` retain the
transmitted bytes, received bytes and text, prompt detection, command, and
`mpremote` error. This distinguishes a responsive REPL from an application
stuck in native code or hardware that requires an external reset.

## Typical Agent Workflow

1. Call `list_serial_ports`.
2. Call `select_serial_port` with an exact result.
3. Call `get_bridge_status`.
4. Call `install_files` with absolute project file paths and `monitor: true`.
5. Call `read_serial_log`, passing its returned `cursor` as `since` on later
   reads.
6. If structured debugging is needed, install the runtime and use
   `get_runtime_state` or `call_runtime_function`.

This is also the serial application-programming workflow: `install_files`
copies the selected absolute host files to the MCU root, resets the device, and
normally starts serial monitoring. Always rediscover and identify the physical
serial device before writing when more than one MCU is connected.

## Device Identity

Serial device paths and IP addresses are temporary locations, not identities.
macOS can assign a different `/dev/cu.usbmodem*` suffix after a reconnect, and
DHCP can change an IP address.

The plugin uses these fields:

- `device_id`: lowercase hexadecimal `machine.unique_id()`. This is the stable,
  authoritative identifier used by `select_ota_device`.
- `name`: an adjustable, human-friendly label such as `reader-kitchen`. Names
  need not be unique and must not be used alone to select a target.
- `mac`: the station-interface MAC address, useful as a second physical check.
- `port` or `host`: the current serial or network location.

To correlate USB and OTA, select a serial port and call
`identify_serial_device`. After provisioning and starting the application,
call `list_ota_devices`. Match the identical `device_id`; the MAC and friendly
name provide additional confirmation.

## Enable OTA Over Serial

Serial is the initial provisioning and recovery transport. In a new Codex task:

1. Call `list_serial_ports` and select an exact returned path.
2. Call `identify_serial_device` and record its `device_id` and `mac`.
3. Call `provision_ota` with the WiFi SSID, password, a unique friendly name,
   and a random pre-shared token of at least 16 characters.
4. Integrate and install an application that starts and polls `OTAService`.

The provisioning tool writes `/codex_ota.py` and `/codex_ota.json`, then resets
the board. The JSON file resembles this, but normally let the tool create it:

```json
{
  "ssid": "workshop-wifi",
  "password": "replace-me",
  "name": "reader-kitchen",
  "token": "replace-with-a-long-random-token",
  "port": 8267
}
```

The application must service OTA regularly. A typical `main.py` is:

```python
from codex_ota import OTAService

ota = OTAService()
ota.connect()

while True:
    ota.poll()
    run_one_application_iteration()
```

Keep each application iteration short enough to call `ota.poll()` regularly.
The upload itself is synchronous and pauses the application until its files
have arrived. `OTA READY ...` on serial reports the same ID, name, MAC, and IP
returned by network discovery.

To change WiFi credentials, the friendly name, token, or OTA port, run
`provision_ota` over serial again. The hardware `device_id` does not change.

## Install Application Files Over OTA

Once the provisioned application is running:

1. Call `list_ota_devices`. Discovery uses UDP port 8266 on the local broadcast
   domain. If the all-hosts address does not work, pass the subnet broadcast,
   for example `192.168.1.255`.
2. Compare `device_id`, name, MAC, and IP, then call `select_ota_device` with the
   exact `device_id` returned by discovery.
3. Call `install_files_ota` with absolute host paths and the device's token.
   Basenames become root-level MCU filenames.
4. Leave `restart: true` for application updates. The MCU verifies every file's
   SHA-256 before replacing it and resets after reporting success.
5. Call `list_ota_devices` again and confirm that the same `device_id` returns.
   This verifies that the updated application booted, connected, and resumed
   polling OTA.

Repeat discovery immediately before each deployment: it refreshes DHCP
addresses and proves that the target currently responds. Multiple MCUs may
share the same WiFi and discovery ports; each responds with its own stable ID.

### OTA Security and Recovery

The compact OTA protocol is intended for a trusted local network. Its token
prevents accidental or unauthenticated writes, and SHA-256 detects corrupted
transfers, but traffic and credentials are not encrypted. Use an isolated or
trusted WiFi network; do not expose ports 8266/8267 to the internet.

Files are downloaded to temporary names and verified before replacement. This
simple implementation does not provide a transactional multi-file rollback:
power loss while replacing several files can leave a mixed version. Upload
supporting modules before `main.py`, keep serial recovery available, and use
USB serial if the application no longer starts or polls OTA.

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
  scripts/codex_ota.py
tests/test_mpy_mcp_server.py
```

Run the test suite without a physical MCU:

```bash
python3 -m unittest discover -s tests -v
```
