---
name: micropython-debug-bridge
description: Use the plugin-provided MCP tools for direct host-side serial and OTA installation, device discovery, serial monitoring, and runtime debugging of MicroPython MCUs. Use when an agent needs to discover or identify devices, select a macOS /dev/cu.* device or OTA target, copy MicroPython files, read serial output, reset an MCU, provision WiFi OTA, or call the optional on-device debug runtime.
---

# MicroPython Debug Bridge

Use the plugin-provided MCP tools. The Codex host launches the stdio MCP server
and gives that process direct access to host TTY devices; the agent shell does
not need localhost or serial-device access.

## MCP Lifecycle

- Never run `mpy_mcp_server.py` yourself and never create an HTTP, shell, or
  localhost proxy for it.
- Codex starts and stops the MCP process as part of the installed plugin.
- If the tools are absent or the MCP server is disconnected, ask the human to
  install or enable the plugin and restart Codex. Do not start the process from
  a shell.
- There is no agent-callable MCP shutdown tool.

## Select the TTY First

Before any MCU operation:

1. Call `list_serial_ports`.
2. Choose an exact path from that call's `ports` result, even if a device was
   preselected through the environment. Prefer `/dev/cu.usbmodem*` on macOS for
   USB CDC devices. If several devices are plausible and project context does
   not identify one, ask the human which one to use.
3. Call `select_serial_port` with that exact `/dev/cu.*` path.
4. Call `get_bridge_status` before a destructive operation.

Never invent a device path. A path must be returned by `list_serial_ports`;
selection verifies the host's read/write permissions. `start_serial_monitor`
then verifies that the MCP process can actually open and exclusively own it.

Serial paths are temporary locations. To identify the physical MCU, call
`identify_serial_device` after selecting it. Record the returned stable
`device_id` (`machine.unique_id()`), friendly `name`, and station `mac`.

## OTA Devices

- OTA must first be provisioned over a selected serial port with
  `provision_ota`. Explain that this writes WiFi credentials, device name, and
  token to the MCU and installs `codex_ota.py`.
- The application must instantiate `OTAService`, call `connect()`, and call
  `poll()` frequently. Provisioning does not modify `main.py`; install an
  OTA-aware application over serial during bootstrap. If the application does
  not poll, the MCU will not appear in discovery.
- Before every OTA operation, call `list_ota_devices`. If necessary, use the
  subnet broadcast address instead of `255.255.255.255`.
- Select only an exact `device_id` returned by that discovery with
  `select_ota_device`. Never select by friendly name alone; names can collide.
- Compare `device_id` from `identify_serial_device` and `list_ota_devices` to
  correlate the serial and network views. MAC and name are secondary checks;
  serial paths and DHCP addresses can change.
- Use `install_files_ota` for frequently changing application files. Supply
  the provisioned token and normally leave `restart: true`.
- After a resetting update, call `list_ota_devices` again and confirm the same
  `device_id` returns. This is the basic post-deployment health check.
- Discovery supports multiple MCUs. Refresh it immediately before deployment
  so the selected registry contains the current network endpoint.
- OTA is a trusted-LAN transport with a pre-shared token and SHA-256 integrity,
  not encrypted transport. Never expose its UDP/TCP ports to the internet.
- If an application stops polling OTA or an update prevents boot, OTA is no
  longer reachable. Do not repeatedly retry OTA. Return to USB, rediscover and
  select an exact serial port, call `identify_serial_device`, require its
  `device_id` to match the intended MCU, then repair `main.py` with
  `install_files` or rerun `provision_ota` if configuration is wrong.

Application integration:

```python
from codex_ota import OTAService

ota = OTAService()
ota.connect()

while True:
    ota.poll()
    run_one_application_iteration()
```

## Typical Workflow

- Use `install_files` with absolute host paths. Set
  `include_debug_runtime: true` only when runtime calls are needed.
- Leave `monitor: true` to begin collecting output after installation.
- Use `start_serial_monitor` when the application is already installed.
- Use `read_serial_log`; retain the returned `cursor` and pass it as `since`
  to fetch only newer lines.
- Prefer `get_runtime_state` or `call_runtime_function` over parsing log text
  when the application exports structured functions.
- Use `evaluate_runtime` only for focused debugging. It can mutate MCU state.
- Before any runtime tool, confirm `monitoring: true` in `get_bridge_status`.
  If monitoring is false, call `start_serial_monitor` first.
- On `install_files`, `install_debug_runtime`, `remove_debug_runtime`, or
  `reset_device` raw-REPL failure, check that `get_bridge_status.server.version`
  is at least 1.0.1, then inspect the returned `diagnostics` and retained
  `last_device_control`. Report `takeover.transmissions_hex`, `rx_hex`,
  `rx_text`, `friendly_prompt_seen`, `keyboard_interrupt_seen`, `command`,
  `mpremote_ok`, and `mpremote_error`.
- When `mpremote_ok` is false, do not claim the requested MCU operation ran and
  do not automatically retry, reset, or restart monitoring. If no friendly
  prompt was seen, ask the human for an external hardware reset. Otherwise,
  report the `mpremote` handoff failure and preserve the evidence for diagnosis.

`install_files`, `install_debug_runtime`, `remove_debug_runtime`,
`reset_device`, and `evaluate_runtime` modify device state. Explain the intended
operation when requesting approval.

`identify_serial_device` interrupts and resets the selected MCU.
`provision_ota` and `install_files_ota` modify device files and reset by
default; explain the exact target identity and files before requesting approval.

## Runtime Integration

The shared MCU helper is bundled as `scripts/codex_debug_runtime.py`. The
application must import it, register exported functions, and poll
`RuntimeShell`. Runtime requests use framed JSON over the same serial stream and
are serialized by the MCP server.

Example application integration:

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
