#!/usr/bin/env python3
"""Dependency-free stdio MCP server for a connected MicroPython device."""

from __future__ import annotations

import fcntl
import glob
import hashlib
import json
import os
import select
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty
from collections import deque
from pathlib import Path
from typing import Any, Callable


SERVER_NAME = "micropython-debug-bridge"
SERVER_VERSION = "1.1.0"
PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    PROTOCOL_VERSION,
}
SERIAL_BAUD = 115200
LOG_CAPACITY = 2000
COMMAND_TIMEOUT_SEC = 20
DEVICE_COMMAND_TIMEOUT_SEC = 60
SERIAL_WRITE_TIMEOUT_SEC = 5
SERIAL_TAKEOVER_ATTEMPTS = 3
SERIAL_TAKEOVER_READ_SEC = 0.25
SERIAL_TAKEOVER_MAX_BYTES = 4096
FRAME_PREFIX = "@@FRAME@@ "
FRAME_PREFIX_BYTES = FRAME_PREFIX.encode("utf-8")
SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = SCRIPT_DIR.parent
DEBUG_RUNTIME_PATH = SCRIPT_DIR / "codex_debug_runtime.py"
DEBUG_RUNTIME_NAME = DEBUG_RUNTIME_PATH.name
OTA_RUNTIME_PATH = SCRIPT_DIR / "codex_ota.py"
OTA_RUNTIME_NAME = OTA_RUNTIME_PATH.name
OTA_CONFIG_NAME = "codex_ota.json"
OTA_DISCOVERY_MAGIC = b"MPY_OTA_DISCOVER_V1"
OTA_PROTOCOL = "mpy-ota-v1"
OTA_DISCOVERY_PORT = 8266
OTA_SERVICE_PORT = 8267
OTA_HEADER_LIMIT = 16384
OTA_DISCOVERY_TIMEOUT_SEC = 2.0
MACOS_SERIAL_GLOB = "/dev/cu.*"
LINUX_SERIAL_GLOBS = ("/dev/ttyACM*", "/dev/ttyUSB*")


def _debug(message: str) -> None:
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


def _json_text(value: Any) -> str:
    return json.dumps(value, indent=2, sort_keys=True, default=str)


def _result(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": _json_text(value)}],
        "structuredContent": value,
        "isError": False,
    }


def _error_result(exc: Exception) -> dict[str, Any]:
    value = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
    diagnostics = getattr(exc, "diagnostics", None)
    if diagnostics is not None:
        value["diagnostics"] = diagnostics
    return {
        "content": [{"type": "text", "text": _json_text(value)}],
        "structuredContent": value,
        "isError": True,
    }


def _object_schema(
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
    additional_properties: bool = False,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": additional_properties,
    }
    if required:
        schema["required"] = required
    return schema


def _tool(
    name: str,
    title: str,
    description: str,
    input_schema: dict[str, Any],
    handler: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    read_only: bool,
    destructive: bool = False,
    idempotent: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": input_schema,
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "idempotentHint": idempotent,
            "openWorldHint": False,
        },
        "_handler": handler,
    }


def discover_serial_ports() -> list[dict[str, Any]]:
    """Return supported, currently present character devices."""
    patterns = [MACOS_SERIAL_GLOB]
    if sys.platform.startswith("linux"):
        patterns.extend(LINUX_SERIAL_GLOBS)

    paths: set[str] = set()
    for pattern in patterns:
        paths.update(glob.glob(pattern))

    ports: list[dict[str, Any]] = []
    for path in sorted(paths):
        try:
            mode = os.stat(path).st_mode
        except OSError:
            continue
        if not stat.S_ISCHR(mode):
            continue
        ports.append(
            {
                "path": path,
                "name": os.path.basename(path),
                "readable": os.access(path, os.R_OK),
                "writable": os.access(path, os.W_OK),
            }
        )
    return ports


def validate_serial_port(path: str) -> str:
    if not isinstance(path, str) or not path:
        raise ValueError("port must be a non-empty string")
    available = {item["path"]: item for item in discover_serial_ports()}
    if path not in available:
        raise ValueError(
            f"{path!r} is not an available supported serial device; "
            f"call list_serial_ports and choose one of its exact paths"
        )
    entry = available[path]
    if not entry["readable"] or not entry["writable"]:
        raise PermissionError(
            f"Codex cannot read and write {path}; fix the host OS device permission "
            "and restart Codex so the MCP server inherits it"
        )
    return path


def _validate_timeout(value: Any, *, maximum: float = 30.0) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("timeout_sec must be a number")
    result = float(value)
    if result <= 0 or result > maximum:
        raise ValueError(f"timeout_sec must be greater than 0 and at most {maximum}")
    return result


def discover_ota_devices(
    *,
    timeout_sec: float = OTA_DISCOVERY_TIMEOUT_SEC,
    broadcast: str = "255.255.255.255",
) -> list[dict[str, Any]]:
    """Discover OTA agents on one broadcast domain."""
    timeout_sec = _validate_timeout(timeout_sec, maximum=10.0)
    if not isinstance(broadcast, str) or not broadcast:
        raise ValueError("broadcast must be a non-empty IPv4 address")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    devices: dict[str, dict[str, Any]] = {}
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 0))
        sock.sendto(OTA_DISCOVERY_MAGIC, (broadcast, OTA_DISCOVERY_PORT))
        deadline = time.monotonic() + timeout_sec
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                payload, source = sock.recvfrom(4096)
            except socket.timeout:
                break
            try:
                item = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(item, dict) or item.get("protocol") != OTA_PROTOCOL:
                continue
            device_id = item.get("device_id")
            port = item.get("port")
            if not isinstance(device_id, str) or not device_id:
                continue
            if (
                not isinstance(port, int)
                or isinstance(port, bool)
                or not 1 <= port <= 65535
            ):
                continue
            devices[device_id] = {
                "device_id": device_id,
                "name": item.get("name") or device_id,
                "mac": item.get("mac"),
                "host": source[0],
                "port": port,
                "protocol": OTA_PROTOCOL,
            }
    finally:
        sock.close()
    return sorted(
        devices.values(), key=lambda item: (item["name"], item["device_id"])
    )


def configure_serial_fd(serial_fd: int) -> None:
    tty.setraw(serial_fd, termios.TCSANOW)
    attributes = termios.tcgetattr(serial_fd)
    attributes[4] = termios.B115200
    attributes[5] = termios.B115200
    termios.tcsetattr(serial_fd, termios.TCSANOW, attributes)


class DeviceControlError(RuntimeError):
    def __init__(self, message: str, diagnostics: dict[str, Any]) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


def _read_serial_evidence(serial_fd: int, duration_sec: float) -> bytes:
    deadline = time.monotonic() + duration_sec
    evidence = bytearray()
    while time.monotonic() < deadline and len(evidence) < SERIAL_TAKEOVER_MAX_BYTES:
        remaining = max(0.0, deadline - time.monotonic())
        ready, _, _ = select.select([serial_fd], [], [], remaining)
        if not ready:
            break
        try:
            chunk = os.read(
                serial_fd,
                min(512, SERIAL_TAKEOVER_MAX_BYTES - len(evidence)),
            )
        except BlockingIOError:
            continue
        if not chunk:
            break
        evidence.extend(chunk)
    return bytes(evidence)


def _write_serial_bytes(serial_fd: int, data: bytes) -> None:
    deadline = time.monotonic() + SERIAL_WRITE_TIMEOUT_SEC
    offset = 0
    while offset < len(data):
        try:
            written = os.write(serial_fd, data[offset:])
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise RuntimeError("timed out writing serial takeover sequence")
            time.sleep(0.01)
            continue
        if written <= 0:
            raise RuntimeError("failed to write serial takeover sequence")
        offset += written


def interrupt_running_program(port: str) -> dict[str, Any]:
    """Interrupt a running app and retain evidence before mpremote opens the TTY."""
    started = time.monotonic()
    serial_fd: int | None = None
    evidence = bytearray()
    transmissions: list[str] = []
    stage = "open"
    try:
        serial_fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        stage = "configure"
        configure_serial_fd(serial_fd)
        stage = "drain"
        evidence.extend(_read_serial_evidence(serial_fd, 0.05))

        # mpremote sends only one Ctrl-C before entering raw REPL. Some busy
        # applications need repeated interrupts before the friendly prompt is
        # available, so leave the MCU there before handing over the device.
        stage = "interrupt"
        for _ in range(SERIAL_TAKEOVER_ATTEMPTS):
            payload = b"\r\x03"
            _write_serial_bytes(serial_fd, payload)
            transmissions.append(payload.hex())
            evidence.extend(
                _read_serial_evidence(serial_fd, SERIAL_TAKEOVER_READ_SEC)
            )

        # If a previous client left raw REPL active, Ctrl-B returns to friendly
        # REPL. A final Ctrl-C makes the handoff state deterministic.
        stage = "normalize"
        for payload in (b"\r\x02", b"\r\x03"):
            _write_serial_bytes(serial_fd, payload)
            transmissions.append(payload.hex())
            evidence.extend(
                _read_serial_evidence(serial_fd, SERIAL_TAKEOVER_READ_SEC)
            )
    except Exception as exc:
        captured = bytes(evidence[-SERIAL_TAKEOVER_MAX_BYTES:])
        raise DeviceControlError(
            f"serial takeover failed during {stage}: {exc}",
            {
                "ok": False,
                "port": port,
                "failure_stage": stage,
                "error": str(exc),
                "elapsed_sec": round(time.monotonic() - started, 3),
                "transmissions_hex": transmissions,
                "rx_hex": captured.hex(),
                "rx_text": captured.decode("utf-8", errors="replace"),
            },
        ) from exc
    finally:
        if serial_fd is not None:
            os.close(serial_fd)

    captured = bytes(evidence[-SERIAL_TAKEOVER_MAX_BYTES:])
    text = captured.decode("utf-8", errors="replace")
    return {
        "ok": True,
        "port": port,
        "elapsed_sec": round(time.monotonic() - started, 3),
        "transmissions_hex": transmissions,
        "rx_hex": captured.hex(),
        "rx_text": text,
        "friendly_prompt_seen": ">>>" in text,
        "keyboard_interrupt_seen": "KeyboardInterrupt" in text,
    }


class CommandRunner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: set[subprocess.Popen[str]] = set()

    def run(self, argv: list[str]) -> str:
        executable = shutil.which(argv[0])
        if executable is None:
            raise RuntimeError(
                f"{argv[0]} is not installed or is not on the Codex host PATH"
            )
        command = [executable, *argv[1:]]
        process = subprocess.Popen(
            command,
            cwd=PLUGIN_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with self._lock:
            self._processes.add(process)
        try:
            stdout, stderr = process.communicate(timeout=COMMAND_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            raise RuntimeError(
                "command timed out after {}s: {}\nstdout:\n{}\nstderr:\n{}".format(
                    COMMAND_TIMEOUT_SEC,
                    " ".join(argv),
                    stdout.strip(),
                    stderr.strip(),
                )
            )
        finally:
            with self._lock:
                self._processes.discard(process)

        if process.returncode != 0:
            raise RuntimeError(
                "command failed: {}\nstdout:\n{}\nstderr:\n{}".format(
                    " ".join(argv),
                    stdout.strip(),
                    stderr.strip(),
                )
            )
        return stdout

    def kill_active(self) -> None:
        with self._lock:
            processes = list(self._processes)
        for process in processes:
            if process.poll() is None:
                process.kill()


class MonitorState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._response_condition = threading.Condition(self._lock)
        self._runtime_request_lock = threading.Lock()
        self._lines: deque[tuple[int, str]] = deque(maxlen=LOG_CAPACITY)
        self._responses: list[dict[str, Any]] = []
        self._cursor = 0
        self._serial_fd: int | None = None
        self._monitor_thread: threading.Thread | None = None
        self._monitor_error: str | None = None
        self._stop_event: threading.Event | None = None
        self._port: str | None = None
        self._next_request_id = 1
        self._active_runtime_request: dict[str, Any] | None = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            monitor_thread = self._monitor_thread
            active = self._active_runtime_request
            if active is not None:
                active = dict(active)
                active["elapsed_sec"] = round(
                    time.monotonic() - active.pop("started_monotonic"), 3
                )
            return {
                "port": self._port,
                "monitoring": monitor_thread is not None and monitor_thread.is_alive(),
                "monitor_error": self._monitor_error,
                "active_runtime_request": active,
                "cursor": self._cursor,
            }

    def clear_logs(self) -> None:
        with self._lock:
            self._lines.clear()
            self._responses.clear()
            self._cursor = 0

    def append_line(self, line: str) -> None:
        with self._lock:
            self._cursor += 1
            self._lines.append((self._cursor, line))

    def append_response(self, payload: dict[str, Any]) -> None:
        with self._response_condition:
            self._responses.append(payload)
            self._response_condition.notify_all()

    def get_lines(
        self, *, tail: int | None = None, since: int | None = None
    ) -> tuple[list[str], int]:
        with self._lock:
            items = list(self._lines)
            cursor = self._cursor
        if since is not None:
            items = [line for line in items if line[0] > since]
        if tail is not None:
            items = items[-tail:]
        return [line for _, line in items], cursor

    def stop_monitor(self) -> None:
        with self._lock:
            stop_event = self._stop_event
            serial_fd = self._serial_fd
            thread = self._monitor_thread
            self._stop_event = None
            self._serial_fd = None
            self._monitor_thread = None
            self._port = None
        if stop_event is not None:
            stop_event.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        if serial_fd is not None:
            try:
                os.close(serial_fd)
            except OSError:
                pass
        with self._response_condition:
            self._response_condition.notify_all()

    def start_monitor(self, port: str) -> None:
        self.stop_monitor()
        serial_fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            if hasattr(termios, "TIOCEXCL"):
                fcntl.ioctl(serial_fd, termios.TIOCEXCL)
            configure_serial_fd(serial_fd)
        except Exception:
            os.close(serial_fd)
            raise
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._monitor_loop,
            name="micropython-serial-monitor",
            args=(serial_fd, stop_event),
            daemon=True,
        )
        with self._lock:
            self._serial_fd = serial_fd
            self._stop_event = stop_event
            self._monitor_thread = thread
            self._monitor_error = None
            self._port = port
        thread.start()

    def write_bytes(self, data: bytes) -> None:
        with self._lock:
            serial_fd = self._serial_fd
        if serial_fd is None:
            raise RuntimeError("serial monitor is not running")
        deadline = time.monotonic() + SERIAL_WRITE_TIMEOUT_SEC
        written_total = 0
        while written_total < len(data):
            try:
                written = os.write(serial_fd, data[written_total:])
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("timed out writing to the serial device")
                time.sleep(0.01)
                continue
            if written <= 0:
                raise RuntimeError("failed to write to the serial device")
            written_total += written

    def runtime_request(
        self, mode: str, payload: dict[str, Any], timeout_sec: float
    ) -> dict[str, Any]:
        if timeout_sec <= 0 or timeout_sec > DEVICE_COMMAND_TIMEOUT_SEC:
            raise ValueError(
                f"timeout_sec must be greater than 0 and at most "
                f"{DEVICE_COMMAND_TIMEOUT_SEC}"
            )
        with self._runtime_request_lock:
            with self._lock:
                request_id = self._next_request_id
                self._next_request_id += 1
                self._active_runtime_request = {
                    "request_id": request_id,
                    "mode": mode,
                    "timeout_sec": timeout_sec,
                    "started_monotonic": time.monotonic(),
                }
            try:
                request = {"request_id": request_id, "mode": mode, **payload}
                body = json.dumps(request, separators=(",", ":")).encode("utf-8")
                frame = (
                    FRAME_PREFIX_BYTES
                    + str(len(body)).encode("ascii")
                    + b"\n"
                    + body
                    + b"\n"
                )
                self.write_bytes(frame)
                return self.wait_for_response(request_id, timeout_sec)
            finally:
                with self._lock:
                    self._active_runtime_request = None

    def wait_for_response(
        self, request_id: int, timeout_sec: float
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_sec
        with self._response_condition:
            while True:
                for index, payload in enumerate(self._responses):
                    if payload.get("request_id") == request_id:
                        return self._responses.pop(index)
                if self._serial_fd is None:
                    if self._monitor_error:
                        raise RuntimeError(
                            f"serial monitor stopped: {self._monitor_error}"
                        )
                    raise RuntimeError("serial monitor is not running")
                if self._monitor_thread is None or not self._monitor_thread.is_alive():
                    raise RuntimeError(
                        "serial monitor stopped: "
                        + (self._monitor_error or "monitor thread exited")
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("timed out waiting for the MCU response")
                self._response_condition.wait(timeout=remaining)

    def _record_monitor_error(self, serial_fd: int, exc: OSError) -> None:
        message = str(exc)
        with self._response_condition:
            if self._serial_fd == serial_fd:
                self._serial_fd = None
            self._monitor_error = message
            self._cursor += 1
            self._lines.append((self._cursor, f"MONITOR ERROR: {message}"))
            self._response_condition.notify_all()
        try:
            os.close(serial_fd)
        except OSError:
            pass

    def _monitor_loop(
        self, serial_fd: int, stop_event: threading.Event
    ) -> None:
        buffer = bytearray()
        expected_frame_length: int | None = None
        while not stop_event.is_set():
            try:
                ready, _, _ = select.select([serial_fd], [], [], 0.1)
                if not ready:
                    continue
                chunk = os.read(serial_fd, 256)
            except BlockingIOError:
                continue
            except OSError as exc:
                if stop_event.is_set():
                    return
                self._record_monitor_error(serial_fd, exc)
                return
            if not chunk:
                continue
            buffer.extend(chunk)

            while True:
                if expected_frame_length is None:
                    newline = buffer.find(b"\n")
                    if newline < 0:
                        break
                    raw = bytes(buffer[:newline]).rstrip(b"\r")
                    del buffer[: newline + 1]
                    if not raw:
                        continue
                    if raw.startswith(FRAME_PREFIX_BYTES):
                        try:
                            expected_frame_length = int(
                                raw[len(FRAME_PREFIX_BYTES) :]
                                .decode("utf-8", "replace")
                                .strip()
                            )
                        except ValueError:
                            self.append_line(
                                "MONITOR ERROR: invalid runtime frame length"
                            )
                            expected_frame_length = None
                        continue
                    self.append_line(raw.decode("utf-8", errors="replace"))
                    continue

                if len(buffer) < expected_frame_length:
                    break
                payload_bytes = bytes(buffer[:expected_frame_length])
                del buffer[:expected_frame_length]
                if buffer[:1] == b"\n":
                    del buffer[:1]
                try:
                    payload = json.loads(payload_bytes.decode("utf-8"))
                    self.append_response(payload)
                except (UnicodeDecodeError, ValueError):
                    self.append_line(
                        "MONITOR ERROR: invalid runtime response payload"
                    )
                expected_frame_length = None


class DeviceController:
    def __init__(self) -> None:
        self.monitor = MonitorState()
        self.runner = CommandRunner()
        self._operation_lock = threading.RLock()
        self._selected_port = os.environ.get("MPY_SERIAL_PORT")
        self._ota_devices: dict[str, dict[str, Any]] = {}
        self._selected_ota_device: str | None = None
        self._last_device_control: dict[str, Any] | None = None

    def close(self) -> None:
        self.runner.kill_active()
        self.monitor.stop_monitor()

    def list_ports(self, _: dict[str, Any]) -> dict[str, Any]:
        ports = discover_serial_ports()
        available_paths = {item["path"] for item in ports}
        selected = (
            self._selected_port if self._selected_port in available_paths else None
        )
        for item in ports:
            item["selected"] = item["path"] == selected
        return {
            "ok": True,
            "platform": sys.platform,
            "pattern": MACOS_SERIAL_GLOB,
            "ports": ports,
            "selected_port": selected,
        }

    def select_port(self, arguments: dict[str, Any]) -> dict[str, Any]:
        path = validate_serial_port(arguments.get("port"))
        with self._operation_lock:
            current_monitor_port = self.monitor.snapshot()["port"]
            if current_monitor_port is not None and current_monitor_port != path:
                self.monitor.stop_monitor()
            self._selected_port = path
        return {
            "ok": True,
            "selected_port": path,
            "message": "Serial device selected for this MCP server session.",
        }

    def _port(self) -> str:
        if self._selected_port is None:
            raise RuntimeError(
                "no serial device is selected; call list_serial_ports, then "
                "select_serial_port with an exact /dev/cu.* path"
            )
        return validate_serial_port(self._selected_port)

    def status(self, _: dict[str, Any]) -> dict[str, Any]:
        ports = discover_serial_ports()
        available_paths = {item["path"] for item in ports}
        selected_available = self._selected_port in available_paths
        return {
            "ok": True,
            "server": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "selected_port": self._selected_port,
            "selected_port_available": selected_available,
            "selected_ota_device": self._selected_ota_device,
            "selected_ota_endpoint": self._ota_devices.get(
                self._selected_ota_device or ""
            ),
            "mpremote": shutil.which("mpremote"),
            "last_device_control": self._last_device_control,
            **self.monitor.snapshot(),
        }

    def _run_mpremote(self, port: str, argv: list[str]) -> str:
        command = ["mpremote", *argv]
        try:
            takeover = interrupt_running_program(port)
        except DeviceControlError as exc:
            diagnostics = {
                "takeover": exc.diagnostics,
                "command": command,
                "mpremote_ok": False,
                "mpremote_error": "not started because serial takeover failed",
            }
            self._last_device_control = diagnostics
            raise DeviceControlError(
                "serial takeover failed before mpremote could start; inspect "
                "diagnostics.takeover for port and handshake evidence",
                diagnostics,
            ) from exc
        diagnostics = {
            "takeover": takeover,
            "command": command,
        }
        self._last_device_control = diagnostics
        try:
            stdout = self.runner.run(["mpremote", *argv])
        except Exception as exc:
            diagnostics["mpremote_ok"] = False
            diagnostics["mpremote_error"] = str(exc)
            raise DeviceControlError(
                "mpremote failed after the bridge sent repeated interrupts; "
                "inspect diagnostics.takeover for the serial handshake evidence",
                diagnostics,
            ) from exc
        diagnostics["mpremote_ok"] = True
        diagnostics["mpremote_stdout"] = stdout
        return stdout

    @staticmethod
    def _checked_files(files: Any) -> list[Path]:
        if not isinstance(files, list) or not files:
            raise ValueError("files must be a non-empty array of absolute paths")
        checked: list[Path] = []
        names: set[str] = set()
        for item in files:
            if not isinstance(item, str) or not os.path.isabs(item):
                raise ValueError(f"install path must be absolute: {item!r}")
            path = Path(item)
            if not path.is_file():
                raise ValueError(f"install file does not exist: {item}")
            if path.name in names:
                raise ValueError(f"duplicate destination basename: {path.name}")
            names.add(path.name)
            checked.append(path)
        return checked

    def _reset(self, port: str) -> None:
        self._run_mpremote(port, ["connect", port, "reset"])

    def _install_paths(self, files: Any, port: str) -> dict[str, Any]:
        checked = self._checked_files(files)
        self._run_mpremote(
            port, ["connect", port, "fs", "cp", *map(str, checked), ":"]
        )
        listing = self._run_mpremote(port, ["connect", port, "fs", "ls"])
        return {
            "files": [item.name for item in checked],
            "listing": listing,
        }

    @staticmethod
    def _serial_identity_program() -> str:
        return (
            "import machine\n"
            "try:\n import ujson as json\nexcept ImportError:\n import json\n"
            "uid=''.join('{:02x}'.format(x) for x in machine.unique_id())\n"
            "name=None\nmac=None\n"
            "try:\n"
            f" c=json.loads(open({OTA_CONFIG_NAME!r}).read())\n"
            " name=c.get('name')\n"
            "except Exception:\n pass\n"
            "try:\n"
            " import network\n"
            " w=network.WLAN(network.STA_IF)\n"
            " mac=''.join('{:02x}'.format(x) for x in w.config('mac'))\n"
            "except Exception:\n pass\n"
            "print('@@MPY_ID@@'+json.dumps({'device_id':uid,'name':name or "
            "'micropython-'+uid[-6:],'mac':mac}))\n"
        )

    def _identify_serial(self, port: str) -> dict[str, Any]:
        output = self._run_mpremote(
            port, ["connect", port, "exec", self._serial_identity_program()]
        )
        marker = "@@MPY_ID@@"
        for line in reversed(output.splitlines()):
            if marker in line:
                value = json.loads(line.split(marker, 1)[1])
                if isinstance(value, dict) and value.get("device_id"):
                    return value
        raise RuntimeError("MCU identity marker was not returned by mpremote")

    def identify_serial(self, _: dict[str, Any]) -> dict[str, Any]:
        port = self._port()
        with self._operation_lock:
            self.monitor.stop_monitor()
            identity = self._identify_serial(port)
            self._reset(port)
        return {"ok": True, "transport": "serial", "port": port, **identity}

    def provision_ota(self, arguments: dict[str, Any]) -> dict[str, Any]:
        port = self._port()
        values: dict[str, str] = {}
        for key in ("ssid", "password", "name", "token"):
            value = arguments.get(key)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{key} must be a non-empty string")
            values[key] = value
        if len(values["token"]) < 16:
            raise ValueError("token must contain at least 16 characters")
        ota_port = arguments.get("port", OTA_SERVICE_PORT)
        if (
            not isinstance(ota_port, int)
            or isinstance(ota_port, bool)
            or not 1 <= ota_port <= 65535
        ):
            raise ValueError("port must be an integer from 1 through 65535")
        config = {**values, "port": ota_port}
        encoded = json.dumps(config, separators=(",", ":"))
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix="codex-ota-",
                suffix=".json",
                delete=False,
            ) as temporary:
                temporary.write(encoded)
                temporary_path = temporary.name
            with self._operation_lock:
                self.monitor.stop_monitor()
                self._run_mpremote(
                    port,
                    ["connect", port, "fs", "cp", str(OTA_RUNTIME_PATH), ":"],
                )
                self._run_mpremote(
                    port,
                    [
                        "connect",
                        port,
                        "fs",
                        "cp",
                        temporary_path,
                        f":{OTA_CONFIG_NAME}",
                    ],
                )
                identity = self._identify_serial(port)
                self._reset(port)
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass
        return {
            "ok": True,
            "transport": "serial",
            "port": port,
            "installed": [OTA_RUNTIME_NAME, OTA_CONFIG_NAME],
            "ota_port": ota_port,
            **identity,
        }

    def list_ota_devices(self, arguments: dict[str, Any]) -> dict[str, Any]:
        timeout_sec = _validate_timeout(
            arguments.get("timeout_sec", OTA_DISCOVERY_TIMEOUT_SEC), maximum=10.0
        )
        broadcast = arguments.get("broadcast", "255.255.255.255")
        devices = discover_ota_devices(
            timeout_sec=timeout_sec, broadcast=broadcast
        )
        self._ota_devices = {item["device_id"]: item for item in devices}
        if self._selected_ota_device not in self._ota_devices:
            self._selected_ota_device = None
        for item in devices:
            item["selected"] = item["device_id"] == self._selected_ota_device
        return {
            "ok": True,
            "devices": devices,
            "selected_device_id": self._selected_ota_device,
            "broadcast": broadcast,
        }

    def select_ota_device(self, arguments: dict[str, Any]) -> dict[str, Any]:
        device_id = arguments.get("device_id")
        if not isinstance(device_id, str) or not device_id:
            raise ValueError("device_id must be a non-empty string")
        device = self._ota_devices.get(device_id)
        if device is None:
            raise ValueError(
                "device_id was not returned by the latest list_ota_devices call"
            )
        self._selected_ota_device = device_id
        return {"ok": True, "selected_device": device}

    def install_files_ota(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._selected_ota_device is None:
            raise RuntimeError(
                "no OTA device selected; call list_ota_devices, then "
                "select_ota_device with an exact device_id"
            )
        device = self._ota_devices.get(self._selected_ota_device)
        if device is None:
            raise RuntimeError("selected OTA device is no longer in the registry")
        token = arguments.get("token")
        if not isinstance(token, str) or len(token) < 16:
            raise ValueError("token must contain at least 16 characters")
        checked = self._checked_files(arguments.get("files"))
        timeout_sec = _validate_timeout(
            arguments.get("timeout_sec", 30), maximum=120.0
        )
        metadata = []
        for path in checked:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(65536), b""):
                    digest.update(chunk)
            metadata.append(
                {
                    "name": path.name,
                    "size": path.stat().st_size,
                    "sha256": digest.hexdigest(),
                }
            )
        request = {
            "protocol": OTA_PROTOCOL,
            "op": "install",
            "token": token,
            "restart": bool(arguments.get("restart", True)),
            "files": metadata,
        }
        with self._operation_lock:
            with socket.create_connection(
                (device["host"], device["port"]), timeout=timeout_sec
            ) as connection:
                connection.settimeout(timeout_sec)
                connection.sendall(
                    json.dumps(request, separators=(",", ":")).encode("utf-8")
                    + b"\n"
                )
                for path in checked:
                    with path.open("rb") as handle:
                        for chunk in iter(lambda: handle.read(65536), b""):
                            connection.sendall(chunk)
                with connection.makefile("rb") as response_file:
                    response_line = response_file.readline(OTA_HEADER_LIMIT + 1)
                if not response_line or len(response_line) > OTA_HEADER_LIMIT:
                    raise RuntimeError("invalid or missing OTA response")
                response = json.loads(response_line.decode("utf-8"))
        if not response.get("ok"):
            raise RuntimeError(
                f"OTA install failed: {response.get('error', 'unknown error')}"
            )
        return {
            "ok": True,
            "transport": "ota",
            "device": device,
            "files": [item["name"] for item in metadata],
            "restart": request["restart"],
        }

    def _install_runtime(self, port: str) -> dict[str, Any]:
        self._run_mpremote(
            port,
            ["connect", port, "fs", "cp", str(DEBUG_RUNTIME_PATH), ":"]
        )
        listing = self._run_mpremote(port, ["connect", port, "fs", "ls"])
        return {"files": [DEBUG_RUNTIME_NAME], "listing": listing}

    def _begin_monitor(self, port: str) -> tuple[list[str], int]:
        self.monitor.start_monitor(port)
        time.sleep(0.2)
        self.monitor.write_bytes(b"\x03\x02\x04")
        time.sleep(0.5)
        return self.monitor.get_lines(tail=50)

    def install_files(self, arguments: dict[str, Any]) -> dict[str, Any]:
        port = self._port()
        include_runtime = bool(arguments.get("include_debug_runtime", False))
        monitor = bool(arguments.get("monitor", True))
        with self._operation_lock:
            self.monitor.stop_monitor()
            self.monitor.clear_logs()
            installed = self._install_paths(arguments.get("files"), port)
            if include_runtime:
                runtime = self._install_runtime(port)
                installed["files"].extend(runtime["files"])
                installed["listing"] = runtime["listing"]
            if monitor:
                lines, cursor = self._begin_monitor(port)
            else:
                self._reset(port)
                lines, cursor = [], 0
        return {
            "ok": True,
            "port": port,
            "monitoring": monitor,
            "lines": lines,
            "cursor": cursor,
            **installed,
        }

    def install_runtime(self, arguments: dict[str, Any]) -> dict[str, Any]:
        port = self._port()
        monitor = bool(arguments.get("monitor", True))
        with self._operation_lock:
            self.monitor.stop_monitor()
            self.monitor.clear_logs()
            installed = self._install_runtime(port)
            if monitor:
                lines, cursor = self._begin_monitor(port)
            else:
                self._reset(port)
                lines, cursor = [], 0
        return {
            "ok": True,
            "port": port,
            "monitoring": monitor,
            "lines": lines,
            "cursor": cursor,
            **installed,
        }

    def remove_runtime(self, _: dict[str, Any]) -> dict[str, Any]:
        port = self._port()
        with self._operation_lock:
            self.monitor.stop_monitor()
            self._run_mpremote(
                port,
                [
                    "connect",
                    port,
                    "exec",
                    "import os\n"
                    "try:\n"
                    f" os.remove({DEBUG_RUNTIME_NAME!r})\n"
                    "except OSError:\n"
                    " pass\n",
                ]
            )
            listing = self._run_mpremote(port, ["connect", port, "fs", "ls"])
            self._reset(port)
        return {
            "ok": True,
            "port": port,
            "removed": DEBUG_RUNTIME_NAME,
            "listing": listing,
        }

    def start_monitor(self, _: dict[str, Any]) -> dict[str, Any]:
        port = self._port()
        with self._operation_lock:
            self.monitor.clear_logs()
            lines, cursor = self._begin_monitor(port)
        return {
            "ok": True,
            "port": port,
            "monitoring": True,
            "lines": lines,
            "cursor": cursor,
        }

    def read_logs(self, arguments: dict[str, Any]) -> dict[str, Any]:
        tail = arguments.get("tail", 100)
        since = arguments.get("since")
        if not isinstance(tail, int) or isinstance(tail, bool) or not 1 <= tail <= 2000:
            raise ValueError("tail must be an integer from 1 through 2000")
        if since is not None and (
            not isinstance(since, int) or isinstance(since, bool) or since < 0
        ):
            raise ValueError("since must be a non-negative integer")
        lines, cursor = self.monitor.get_lines(tail=tail, since=since)
        return {
            "ok": True,
            "lines": lines,
            "cursor": cursor,
            **self.monitor.snapshot(),
        }

    def reset_device(self, _: dict[str, Any]) -> dict[str, Any]:
        port = self._port()
        with self._operation_lock:
            self.monitor.stop_monitor()
            self._reset(port)
        return {"ok": True, "port": port, "reset": True, "monitoring": False}

    @staticmethod
    def _timeout(arguments: dict[str, Any]) -> float:
        value = arguments.get("timeout_sec", 10)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("timeout_sec must be a number")
        return float(value)

    def runtime_state(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.monitor.runtime_request(
            "get_state", {}, self._timeout(arguments)
        )

    def runtime_call(self, arguments: dict[str, Any]) -> dict[str, Any]:
        function = arguments.get("function")
        if not isinstance(function, str) or not function:
            raise ValueError("function must be a non-empty string")
        args = arguments.get("args", [])
        kwargs = arguments.get("kwargs", {})
        if not isinstance(args, list):
            raise ValueError("args must be an array")
        if not isinstance(kwargs, dict):
            raise ValueError("kwargs must be an object")
        return self.monitor.runtime_request(
            "call",
            {"function": function, "args": args, "kwargs": kwargs},
            self._timeout(arguments),
        )

    def runtime_evaluate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        code = arguments.get("code")
        if not isinstance(code, str) or not code:
            raise ValueError("code must be a non-empty string")
        return self.monitor.runtime_request(
            "eval",
            {"code": code, "statement": bool(arguments.get("statement", False))},
            self._timeout(arguments),
        )


class MCPServer:
    def __init__(self, controller: DeviceController | None = None) -> None:
        self.controller = controller or DeviceController()
        timeout_property = {
            "type": "number",
            "minimum": 0.001,
            "maximum": DEVICE_COMMAND_TIMEOUT_SEC,
            "default": 10,
            "description": "Maximum seconds to wait for the MCU response.",
        }
        self._tools = [
            _tool(
                "list_serial_ports",
                "List serial devices",
                "List currently present macOS /dev/cu.* devices that this MCP "
                "server can open. Returns readability, writability, and selection.",
                _object_schema(),
                self.controller.list_ports,
                read_only=True,
                idempotent=True,
            ),
            _tool(
                "select_serial_port",
                "Select serial device",
                "Select one exact path returned by list_serial_ports for this MCP "
                "server session. Switching devices releases the current monitor.",
                _object_schema(
                    {
                        "port": {
                            "type": "string",
                            "pattern": "^/dev/cu\\..+",
                            "description": "Exact /dev/cu.* device path.",
                        }
                    },
                    ["port"],
                ),
                self.controller.select_port,
                read_only=False,
                idempotent=True,
            ),
            _tool(
                "identify_serial_device",
                "Identify serial device",
                "Read the selected MCU's stable machine unique ID, configured "
                "friendly name, and WiFi MAC address. This temporarily enters "
                "the REPL, resets the MCU, and leaves monitoring stopped.",
                _object_schema(),
                self.controller.identify_serial,
                read_only=False,
                idempotent=True,
            ),
            _tool(
                "provision_ota",
                "Provision OTA over serial",
                "Install the OTA helper and WiFi configuration on the selected "
                "serial MCU. The application must instantiate OTAService, call "
                "connect(), and poll it. Resets the MCU when complete.",
                _object_schema(
                    {
                        "ssid": {"type": "string", "minLength": 1},
                        "password": {"type": "string", "minLength": 1},
                        "name": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Adjustable human-friendly device name.",
                        },
                        "token": {
                            "type": "string",
                            "minLength": 16,
                            "description": (
                                "Pre-shared OTA token (at least 16 characters)."
                            ),
                        },
                        "port": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 65535,
                            "default": OTA_SERVICE_PORT,
                        },
                    },
                    ["ssid", "password", "name", "token"],
                ),
                self.controller.provision_ota,
                read_only=False,
                destructive=True,
            ),
            _tool(
                "list_ota_devices",
                "List OTA devices",
                "Broadcast on the local network and list responding MicroPython "
                "OTA devices by stable device ID, friendly name, MAC, and endpoint.",
                _object_schema(
                    {
                        "timeout_sec": {
                            "type": "number",
                            "minimum": 0.001,
                            "maximum": 10,
                            "default": OTA_DISCOVERY_TIMEOUT_SEC,
                        },
                        "broadcast": {
                            "type": "string",
                            "default": "255.255.255.255",
                            "description": (
                                "IPv4 broadcast address; use the subnet "
                                "broadcast if needed."
                            ),
                        },
                    }
                ),
                self.controller.list_ota_devices,
                read_only=True,
                idempotent=True,
            ),
            _tool(
                "select_ota_device",
                "Select OTA device",
                "Select an exact stable device_id returned by the latest "
                "list_ota_devices call.",
                _object_schema(
                    {"device_id": {"type": "string", "minLength": 1}},
                    ["device_id"],
                ),
                self.controller.select_ota_device,
                read_only=False,
                idempotent=True,
            ),
            _tool(
                "install_files_ota",
                "Install files over OTA",
                "Stream absolute host file paths to the selected OTA MCU, verify "
                "SHA-256 on-device, replace root-level files, and optionally reset.",
                _object_schema(
                    {
                        "files": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                            "description": (
                                "Absolute host paths; basenames become MCU filenames."
                            ),
                        },
                        "token": {"type": "string", "minLength": 16},
                        "restart": {"type": "boolean", "default": True},
                        "timeout_sec": {
                            "type": "number",
                            "minimum": 0.001,
                            "maximum": 120,
                            "default": 30,
                        },
                    },
                    ["files", "token"],
                ),
                self.controller.install_files_ota,
                read_only=False,
                destructive=True,
            ),
            _tool(
                "get_bridge_status",
                "Get bridge status",
                "Report the selected device, monitor state, last serial error, "
                "whether mpremote is installed, and retained evidence from the "
                "most recent device-control handoff.",
                _object_schema(),
                self.controller.status,
                read_only=True,
                idempotent=True,
            ),
            _tool(
                "install_files",
                "Install MicroPython files",
                "Copy absolute host file paths to the selected MCU with mpremote. "
                "Optionally install the debug runtime and begin monitoring.",
                _object_schema(
                    {
                        "files": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                            "description": "Absolute host paths to copy to the MCU.",
                        },
                        "include_debug_runtime": {
                            "type": "boolean",
                            "default": False,
                        },
                        "monitor": {"type": "boolean", "default": True},
                    },
                    ["files"],
                ),
                self.controller.install_files,
                read_only=False,
                destructive=True,
            ),
            _tool(
                "install_debug_runtime",
                "Install debug runtime",
                "Install codex_debug_runtime.py on the selected MCU and optionally "
                "begin serial monitoring.",
                _object_schema(
                    {"monitor": {"type": "boolean", "default": True}}
                ),
                self.controller.install_runtime,
                read_only=False,
                destructive=True,
            ),
            _tool(
                "remove_debug_runtime",
                "Remove debug runtime",
                "Delete codex_debug_runtime.py from the selected MCU and reset it.",
                _object_schema(),
                self.controller.remove_runtime,
                read_only=False,
                destructive=True,
                idempotent=True,
            ),
            _tool(
                "start_serial_monitor",
                "Start serial monitor",
                "Open the selected TTY directly in the MCP host process, reset the "
                "MicroPython runtime, and collect serial output.",
                _object_schema(),
                self.controller.start_monitor,
                read_only=False,
            ),
            _tool(
                "read_serial_log",
                "Read serial output",
                "Read buffered serial output. Use cursor as since on the next call "
                "to retrieve only newer lines.",
                _object_schema(
                    {
                        "tail": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 2000,
                            "default": 100,
                        },
                        "since": {"type": "integer", "minimum": 0},
                    }
                ),
                self.controller.read_logs,
                read_only=True,
                idempotent=True,
            ),
            _tool(
                "reset_device",
                "Reset MicroPython device",
                "Release the monitor, reset the selected MCU with mpremote, and "
                "leave monitoring stopped.",
                _object_schema(),
                self.controller.reset_device,
                read_only=False,
                destructive=True,
            ),
            _tool(
                "get_runtime_state",
                "Get application state",
                "Call exported app.get_state through the installed on-device debug "
                "runtime. The application must poll RuntimeShell.",
                _object_schema({"timeout_sec": timeout_property}),
                self.controller.runtime_state,
                read_only=True,
            ),
            _tool(
                "call_runtime_function",
                "Call exported function",
                "Call a function explicitly exported by the application through "
                "the installed on-device debug runtime.",
                _object_schema(
                    {
                        "function": {"type": "string", "minLength": 1},
                        "args": {"type": "array", "default": []},
                        "kwargs": {"type": "object", "default": {}},
                        "timeout_sec": timeout_property,
                    },
                    ["function"],
                ),
                self.controller.runtime_call,
                read_only=False,
            ),
            _tool(
                "evaluate_runtime",
                "Evaluate code on MCU",
                "Evaluate an expression or execute statements in the application "
                "debug context. Statements may mutate device state.",
                _object_schema(
                    {
                        "code": {"type": "string", "minLength": 1},
                        "statement": {"type": "boolean", "default": False},
                        "timeout_sec": timeout_property,
                    },
                    ["code"],
                ),
                self.controller.runtime_evaluate,
                read_only=False,
                destructive=True,
            ),
        ]
        self._tools_by_name = {item["name"]: item for item in self._tools}
        self._write_lock = threading.Lock()

    def tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {key: value for key, value in item.items() if key != "_handler"}
            for item in self._tools
        ]

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any] | None:
        method = request.get("method")
        request_id = request.get("id")
        if request_id is None:
            return None
        try:
            if method == "initialize":
                client_version = request.get("params", {}).get("protocolVersion")
                protocol_version = (
                    client_version
                    if client_version in SUPPORTED_PROTOCOL_VERSIONS
                    else PROTOCOL_VERSION
                )
                result = {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": SERVER_NAME,
                        "version": SERVER_VERSION,
                    },
                    "instructions": (
                        "For USB, first call list_serial_ports and select_serial_port "
                        "with an exact /dev/cu.* path. For OTA, first call "
                        "list_ota_devices and select_ota_device with an exact stable "
                        "device_id. This MCP process has direct host TTY and LAN "
                        "access; do not use shell scripts, curl, or localhost HTTP."
                    ),
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": self.tool_definitions()}
            elif method == "tools/call":
                params = request.get("params", {})
                name = params.get("name")
                arguments = params.get("arguments", {})
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments must be an object")
                tool = self._tools_by_name.get(name)
                if tool is None:
                    raise ValueError(f"unknown tool: {name!r}")
                try:
                    value = tool["_handler"](arguments)
                    result = _result(value)
                except Exception as exc:
                    _debug(f"{name} failed: {exc}")
                    result = _error_result(exc)
            else:
                return self._rpc_error(request_id, -32601, f"method not found: {method}")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            return self._rpc_error(request_id, -32603, str(exc))

    @staticmethod
    def _rpc_error(
        request_id: Any, code: int, message: str
    ) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    def write(self, message: dict[str, Any]) -> None:
        encoded = json.dumps(message, separators=(",", ":"))
        with self._write_lock:
            sys.stdout.write(encoded + "\n")
            sys.stdout.flush()

    def serve(self) -> None:
        _debug("stdio MCP server started")
        try:
            for line in sys.stdin:
                try:
                    request = json.loads(line)
                    if not isinstance(request, dict):
                        raise ValueError("JSON-RPC message must be an object")
                    response = self.dispatch(request)
                except (UnicodeDecodeError, ValueError) as exc:
                    response = self._rpc_error(None, -32700, str(exc))
                if response is not None:
                    self.write(response)
        finally:
            self.controller.close()
            _debug("stdio MCP server stopped")


def main() -> int:
    MCPServer().serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
