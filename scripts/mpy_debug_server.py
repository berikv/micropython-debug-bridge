#!/usr/bin/env python3

import argparse
import gzip
import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8765
SERIAL_BAUD = 115200
LOG_CAPACITY = 2000
LOG_DIR_NAME = "mpy-bridge-logs"
DAEMON_START_TIMEOUT_SEC = 10
COMMAND_TIMEOUT_SEC = 20
DEVICE_COMMAND_TIMEOUT_SEC = 60
SERIAL_WRITE_TIMEOUT_SEC = 5
FRAME_PREFIX = "@@FRAME@@ "
FRAME_PREFIX_BYTES = FRAME_PREFIX.encode("utf-8")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
DEBUG_RUNTIME_PATH = os.path.join(SCRIPT_DIR, "codex_debug_runtime.py")
DEBUG_RUNTIME_NAME = os.path.basename(DEBUG_RUNTIME_PATH)


class CommandRunner:
    def __init__(self):
        self._lock = threading.Lock()
        self._processes = set()

    def run(self, argv):
        process = subprocess.Popen(
            argv,
            cwd=REPO_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        with self._lock:
            self._processes.add(process)
        try:
            stdout, stderr = process.communicate(timeout=COMMAND_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            self.kill_active()
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

        if argv and argv[0] == "mpremote" and stderr:
            STATE.append_log_event("MPREMOTE STDERR:\n{}".format(stderr.rstrip()))

        if process.returncode != 0:
            raise RuntimeError(
                "command failed: {}\nstdout:\n{}\nstderr:\n{}".format(
                    " ".join(argv),
                    stdout.strip(),
                    stderr.strip(),
                )
            )
        return stdout

    def kill_active(self):
        with self._lock:
            processes = list(self._processes)
        for process in processes:
            if process.poll() is None:
                process.kill()


RUNNER = CommandRunner()


class MonitorState:
    def __init__(self):
        self._lock = threading.Lock()
        self._response_condition = threading.Condition(self._lock)
        self._runtime_request_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._lines = deque(maxlen=LOG_CAPACITY)
        self._responses = []
        self._cursor = 0
        self._serial_fd = None
        self._monitor_thread = None
        self._monitor_error = None
        self._stop_event = None
        self._port = None
        self._next_request_id = 1
        self._active_runtime_request = None
        self._install_log_path = None
        self._install_log_sequence = 0
        self._log_error = None

    def snapshot(self):
        with self._log_lock:
            log_error = self._log_error
        with self._lock:
            monitor_thread = self._monitor_thread
            active_runtime_request = self._active_runtime_request
            if active_runtime_request is not None:
                active_runtime_request = dict(active_runtime_request)
                active_runtime_request["elapsed_sec"] = round(
                    time.monotonic() - active_runtime_request.pop("started_monotonic"),
                    3,
                )
            return {
                "port": self._port,
                "monitoring": monitor_thread is not None and monitor_thread.is_alive(),
                "monitor_thread": (
                    {
                        "name": monitor_thread.name,
                        "ident": monitor_thread.ident,
                        "alive": monitor_thread.is_alive(),
                    }
                    if monitor_thread is not None
                    else None
                ),
                "monitor_error": self._monitor_error,
                "active_runtime_request": active_runtime_request,
                "log_error": log_error,
                "cursor": self._cursor,
            }

    def clear_logs(self):
        with self._lock:
            self._lines.clear()
            self._responses.clear()
            self._cursor = 0

    def append_line(self, line):
        with self._lock:
            self._cursor += 1
            self._lines.append((self._cursor, line))
        self.append_log_event("SERIAL LINE: " + line)

    def append_log_event(self, message):
        with self._log_lock:
            try:
                self._write_log_line_locked(message)
            except OSError as exc:
                self._log_error = str(exc)
                self._install_log_path = None

    def append_serial_bytes(self, direction, data):
        self.append_log_event("SERIAL {}: {}".format(direction, data.hex()))

    def _write_log_line_locked(self, message):
        if self._install_log_path is None:
            return
        encoded = (message + "\n").encode("utf-8")
        with open(self._install_log_path, "ab") as handle:
            handle.write(gzip.compress(encoded))

    def start_monitor_log(self):
        with self._log_lock:
            self._close_install_log_locked()
            self._install_log_sequence += 1
            log_dir = os.path.join(REPO_DIR, LOG_DIR_NAME)
            os.makedirs(log_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            filename = "monitor-{}-{:04d}.log.gz".format(stamp, self._install_log_sequence)
            path = os.path.join(log_dir, filename)
            print(f"Logging to {path}")
            open(path, "ab").close()
            self._install_log_path = path
            self._log_error = None
            self._write_log_line_locked("MONITOR LOG START: {}".format(stamp))
            return path

    def close_install_log(self):
        with self._log_lock:
            self._close_install_log_locked()

    def _close_install_log_locked(self):
        if self._install_log_path is not None:
            self._write_log_line_locked("MONITOR LOG END")
            self._install_log_path = None

    def append_response(self, payload):
        with self._response_condition:
            self._responses.append(payload)
            self._response_condition.notify_all()
        self.append_log_event("RUNTIME RESPONSE: {}".format(json.dumps(payload)))

    def get_lines(self, tail=None, since=None):
        with self._lock:
            items = list(self._lines)
            cursor = self._cursor
        if since is not None:
            items = [line for line in items if line[0] > since]
        if tail is not None:
            items = items[-tail:]
        return [line for _, line in items], cursor

    def stop_monitor(self, close_log=True):
        with self._lock:
            stop_event = self._stop_event
            serial_fd = self._serial_fd
            thread = self._monitor_thread
            self._stop_event = None
            self._serial_fd = None
            self._monitor_thread = None
            self._monitor_error = None
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
        if close_log:
            self.close_install_log()
        with self._response_condition:
            self._response_condition.notify_all()

    def start_monitor(self, port, new_log=True):
        self.stop_monitor(close_log=new_log)
        if new_log:
            self.start_monitor_log()
        configure_serial_port(port)
        serial_fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._monitor_loop,
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

    def write_bytes(self, data):
        with self._lock:
            serial_fd = self._serial_fd
        if serial_fd is None:
            raise RuntimeError("monitor is not running")
        deadline = time.monotonic() + SERIAL_WRITE_TIMEOUT_SEC
        total_written = 0
        while total_written < len(data):
            try:
                written = os.write(serial_fd, data[total_written:])
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("timed out writing to serial monitor")
                time.sleep(0.01)
                continue
            except OSError as exc:
                raise RuntimeError("failed writing to serial monitor: {}".format(exc)) from exc
            if written <= 0:
                raise RuntimeError("failed to write to serial monitor")
            self.append_serial_bytes("TX", data[total_written: total_written + written])
            total_written += written

    def next_request_id(self):
        with self._lock:
            request_id = self._next_request_id
            self._next_request_id += 1
            return request_id

    def begin_runtime_request(self, request_id, mode, timeout_sec):
        self._runtime_request_lock.acquire()
        with self._lock:
            self._active_runtime_request = {
                "request_id": request_id,
                "mode": mode,
                "timeout_sec": timeout_sec,
                "thread": threading.current_thread().name,
                "started_monotonic": time.monotonic(),
            }

    def end_runtime_request(self):
        with self._lock:
            self._active_runtime_request = None
        self._runtime_request_lock.release()

    def wait_for_response(self, request_id, timeout_sec):
        deadline = time.monotonic() + timeout_sec
        with self._response_condition:
            while True:
                for index, payload in enumerate(self._responses):
                    if payload.get("request_id") == request_id:
                        return self._responses.pop(index)
                if self._serial_fd is None:
                    if self._monitor_error:
                        raise RuntimeError("monitor stopped: {}".format(self._monitor_error))
                    raise RuntimeError("monitor is not running")
                if self._monitor_thread is None or not self._monitor_thread.is_alive():
                    message = self._monitor_error or "monitor thread stopped unexpectedly"
                    raise RuntimeError("monitor stopped: {}".format(message))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("timed out waiting for device response")
                self._response_condition.wait(timeout=remaining)

    def _record_monitor_error(self, serial_fd, exc):
        message = str(exc)
        with self._response_condition:
            if self._serial_fd == serial_fd:
                self._serial_fd = None
            self._monitor_error = message
            self._cursor += 1
            self._lines.append((self._cursor, "MONITOR ERROR: {}".format(message)))
            self._response_condition.notify_all()
        self.append_log_event("MONITOR ERROR: {}".format(message))
        try:
            os.close(serial_fd)
        except OSError:
            pass

    def _monitor_loop(self, serial_fd, stop_event):
        buffer = bytearray()
        expected_frame_length = None
        while not stop_event.is_set():
            try:
                ready, _, _ = select.select([serial_fd], [], [], 0.1)
                if not ready:
                    continue
                chunk = os.read(serial_fd, 256)
                self.append_serial_bytes("RX", chunk)
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
                                raw[len(FRAME_PREFIX_BYTES):].decode("utf-8", "replace").strip()
                            )
                        except ValueError:
                            self.append_line("MONITOR ERROR: invalid runtime frame length")
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
                except ValueError:
                    self.append_line("MONITOR ERROR: invalid runtime response payload")
                expected_frame_length = None


STATE = MonitorState()


def configure_serial_port(port):
    run_command(["stty", "-f", port, str(SERIAL_BAUD), "raw", "-echo"])


def run_command(argv):
    should_log = bool(argv) and argv[0] == "mpremote"
    if should_log:
        STATE.append_log_event("MPREMOTE RUN: {}".format(json.dumps(argv)))
    try:
        stdout = RUNNER.run(argv)
    except Exception as exc:
        if should_log:
            STATE.append_log_event("MPREMOTE ERROR: {}".format(str(exc)))
        raise
    if should_log:
        if stdout:
            STATE.append_log_event("MPREMOTE STDOUT:\n{}".format(stdout.rstrip()))
        STATE.append_log_event("MPREMOTE OK")
    return stdout


def install_files(files, port, reset=True):
    if not isinstance(files, list) or not files:
        raise ValueError("install requires a non-empty files list")
    for path in files:
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ValueError("install file paths must be absolute: {}".format(path))
        if not os.path.isfile(path):
            raise ValueError("install file does not exist: {}".format(path))
    run_command(["mpremote", "connect", port, "fs", "cp", *files, ":"])
    listing = run_command(["mpremote", "connect", port, "fs", "ls"])
    if reset:
        reset_board(port)
    return {"files": [os.path.basename(path) for path in files], "listing": listing}


def install_debug_runtime(port, reset=True):
    run_command(["mpremote", "connect", port, "fs", "cp", DEBUG_RUNTIME_PATH, ":"])
    listing = run_command(["mpremote", "connect", port, "fs", "ls"])
    if reset:
        reset_board(port)
    return {"files": [DEBUG_RUNTIME_NAME], "listing": listing}


def remove_debug_runtime(port, reset=True):
    run_command(
        [
            "mpremote",
            "connect",
            port,
            "exec",
            "import os\n"
            "try:\n"
            " os.remove('{}')\n"
            "except OSError:\n"
            " pass\n".format(DEBUG_RUNTIME_NAME),
        ],
    )
    listing = run_command(["mpremote", "connect", port, "fs", "ls"])
    if reset:
        reset_board(port)
    return {"files": [DEBUG_RUNTIME_NAME], "listing": listing}


def reset_board(port):
    run_command(["mpremote", "connect", port, "reset"])


class MPYDebugServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False
    request_queue_size = 64

    def __init__(
        self,
        server_address,
        handler_class,
        serial_port,
        daemon_mode=False,
        pid_path=None,
        server_log_path=None,
    ):
        super().__init__(server_address, handler_class)
        self.serial_port = serial_port
        self.daemon_mode = daemon_mode
        self.pid_path = pid_path
        self.server_log_path = server_log_path
        self.shutdown_reason = None
        self._request_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._active_requests = {}
        self._recent_requests = deque(maxlen=50)
        self._next_http_request_id = 1

    def begin_request(self, method, path):
        with self._request_lock:
            request_id = self._next_http_request_id
            self._next_http_request_id += 1
            self._active_requests[request_id] = {
                "request_id": request_id,
                "method": method,
                "path": path,
                "thread": threading.current_thread().name,
                "thread_ident": threading.get_ident(),
                "operation": "handling request",
                "started_monotonic": time.monotonic(),
            }
        STATE.append_log_event(
            "HTTP REQUEST START: id={} method={} path={} thread={}".format(
                request_id,
                method,
                path,
                threading.current_thread().name,
            )
        )
        return request_id

    def update_request(self, request_id, operation):
        with self._request_lock:
            request = self._active_requests.get(request_id)
            if request is not None:
                request["operation"] = operation

    def finish_request_tracking(self, request_id, status, error=None):
        with self._request_lock:
            request = self._active_requests.pop(request_id, None)
        if request is None:
            return
        elapsed_sec = time.monotonic() - request["started_monotonic"]
        completed = {
            key: value
            for key, value in request.items()
            if key != "started_monotonic"
        }
        completed["status"] = status
        completed["elapsed_sec"] = round(elapsed_sec, 3)
        completed["error"] = error
        with self._request_lock:
            self._recent_requests.append(completed)
        STATE.append_log_event(
            "HTTP REQUEST END: id={} status={} elapsed={:.3f}s{}".format(
                request_id,
                status if status is not None else "connection-closed",
                elapsed_sec,
                " error={!r}".format(error) if error else "",
            )
        )

    def request_snapshot(self, exclude_request_id=None):
        with self._request_lock:
            requests = []
            now = time.monotonic()
            for request_id, request in self._active_requests.items():
                if request_id == exclude_request_id:
                    continue
                item = dict(request)
                item["elapsed_sec"] = round(now - item.pop("started_monotonic"), 3)
                requests.append(item)
        return {
            "pid": os.getpid(),
            "daemon_mode": self.daemon_mode,
            "listener_address": "{}:{}".format(*self.server_address),
            "listener_fd": self.fileno(),
            "shutdown_reason": self.shutdown_reason,
            "active_http_request_count": len(requests),
            "active_http_requests": requests,
            "request_queue_size": self.request_queue_size,
        }

    def recent_request_snapshot(self):
        with self._request_lock:
            return list(self._recent_requests)

    def diagnostic_snapshot(self):
        return {
            "pid": os.getpid(),
            "daemon_mode": self.daemon_mode,
            "listener_address": "{}:{}".format(*self.server_address),
            "listener_fd": self.fileno(),
            "shutdown_reason": self.shutdown_reason,
            **STATE.snapshot(),
            **self.request_snapshot(),
            "recent_http_requests": self.recent_request_snapshot(),
        }

    def request_shutdown(self, reason):
        with self._shutdown_lock:
            if self.shutdown_reason is not None:
                return
            self.shutdown_reason = reason
        print("Shutdown requested: {}".format(reason), flush=True)
        threading.Thread(target=self.shutdown, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = "MPYDebugBridge/0.4"

    def do_GET(self):
        self._run_tracked_request(self._do_GET)

    def _do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            return self.send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    **STATE.snapshot(),
                    **self.server.request_snapshot(self._bridge_request_id),
                },
            )
        if parsed.path == "/logs":
            query = parse_qs(parsed.query)
            tail = parse_int(query.get("tail", [None])[0])
            since = parse_int(query.get("since", [None])[0])
            lines, cursor = STATE.get_lines(tail=tail, since=since)
            return self.send_json(
                HTTPStatus.OK,
                {"lines": lines, "cursor": cursor, **STATE.snapshot()},
            )
        if parsed.path == "/debug/threads":
            return self.send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "threads": collect_thread_stacks(),
                    "recent_http_requests": self.server.recent_request_snapshot(),
                    **STATE.snapshot(),
                    **self.server.request_snapshot(self._bridge_request_id),
                },
            )
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        self._run_tracked_request(self._do_POST)

    def _do_POST(self):
        parsed = urlparse(self.path)
        try:
            body = self.read_json()
            if parsed.path == "/install":
                return self.send_json(HTTPStatus.OK, self.handle_install(body, monitor=False))
            if parsed.path == "/monitor":
                return self.send_json(HTTPStatus.OK, self.handle_monitor())
            if parsed.path == "/reset":
                return self.send_json(HTTPStatus.OK, self.handle_reset())
            if parsed.path == "/install-and-monitor":
                return self.send_json(HTTPStatus.OK, self.handle_install(body, monitor=True))
            if parsed.path == "/install-runtime":
                return self.send_json(HTTPStatus.OK, self.handle_install_runtime(body, monitor=False))
            if parsed.path == "/install-runtime-and-monitor":
                return self.send_json(HTTPStatus.OK, self.handle_install_runtime(body, monitor=True))
            if parsed.path == "/remove-runtime":
                return self.send_json(HTTPStatus.OK, self.handle_remove_runtime(body))
            if parsed.path == "/runtime":
                return self.send_json(HTTPStatus.OK, self.handle_runtime(body))
            if parsed.path == "/call":
                return self.send_json(HTTPStatus.OK, self.handle_call(body))
            if parsed.path == "/eval":
                return self.send_json(HTTPStatus.OK, self.handle_eval(body))
            if parsed.path == "/state":
                return self.send_json(HTTPStatus.OK, self.handle_state(body))
            if parsed.path == "/shutdown":
                self.send_json(
                    HTTPStatus.OK,
                    {"ok": True, "message": "server shutdown requested"},
                )
                self.server.request_shutdown("HTTP /shutdown")
                return
        except (RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
            return self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        self.send_error(HTTPStatus.NOT_FOUND)

    def _run_tracked_request(self, callback):
        self._response_status = None
        self._bridge_request_id = self.server.begin_request(self.command, self.path)
        error = None
        try:
            callback()
        except (BrokenPipeError, ConnectionResetError) as exc:
            error = "{}: {}".format(type(exc).__name__, exc)
        except Exception as exc:
            error = "{}: {}".format(type(exc).__name__, exc)
            traceback.print_exc()
            try:
                self.send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"ok": False, "error": "internal server error"},
                )
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            self.server.finish_request_tracking(
                self._bridge_request_id,
                self._response_status,
                error,
            )

    def handle_install(self, body, monitor):
        port = self.server.serial_port
        STATE.stop_monitor()
        if monitor:
            STATE.clear_logs()
            STATE.start_monitor_log()
        include_runtime = bool(body.get("debug_runtime"))
        files = body.get("files")
        try:
            result = install_files(files, port, reset=(not monitor) and (not include_runtime))
            if include_runtime:
                runtime_result = install_debug_runtime(port, reset=not monitor)
                result["files"].extend(runtime_result["files"])
                result["listing"] = runtime_result["listing"]
        except Exception:
            STATE.close_install_log()
            raise
        if not monitor:
            result["monitoring"] = False
            result["port"] = port
            return {"ok": True, **result}
        STATE.start_monitor(port, new_log=False)
        time.sleep(0.2)
        STATE.write_bytes(b"\x03\x02\x04")
        time.sleep(0.5)
        lines, cursor = STATE.get_lines(tail=50)
        return {
            "ok": True,
            "port": port,
            "monitoring": True,
            "files": result["files"],
            "listing": result["listing"],
            "lines": lines,
            "cursor": cursor,
        }

    def handle_install_runtime(self, body, monitor):
        port = self.server.serial_port
        STATE.stop_monitor()
        if monitor:
            STATE.clear_logs()
            STATE.start_monitor_log()
        try:
            result = install_debug_runtime(port, reset=not monitor)
        except Exception:
            STATE.close_install_log()
            raise
        if not monitor:
            result["monitoring"] = False
            result["port"] = port
            return {"ok": True, **result}
        STATE.start_monitor(port, new_log=False)
        time.sleep(0.2)
        STATE.write_bytes(b"\x03\x02\x04")
        time.sleep(0.5)
        lines, cursor = STATE.get_lines(tail=50)
        return {
            "ok": True,
            "port": port,
            "monitoring": True,
            "files": result["files"],
            "listing": result["listing"],
            "lines": lines,
            "cursor": cursor,
        }

    def handle_remove_runtime(self, body):
        _ = body
        port = self.server.serial_port
        STATE.stop_monitor()
        result = remove_debug_runtime(port, reset=True)
        result["monitoring"] = False
        result["port"] = port
        return {"ok": True, **result}

    def handle_reset(self):
        port = self.server.serial_port
        STATE.stop_monitor()
        reset_board(port)
        return {"ok": True, "port": port}

    def handle_monitor(self):
        port = self.server.serial_port
        STATE.clear_logs()
        STATE.start_monitor(port)
        time.sleep(0.2)
        lines, cursor = STATE.get_lines(tail=50)
        return {
            "ok": True,
            "port": port,
            "monitoring": True,
            "lines": lines,
            "cursor": cursor,
        }

    def send_runtime_request(self, request, timeout_sec):
        request_id = STATE.next_request_id()
        payload = dict(request)
        payload["request_id"] = request_id
        self.server.update_request(
            self._bridge_request_id,
            "waiting for serial runtime request lock",
        )
        STATE.begin_runtime_request(request_id, payload.get("mode"), timeout_sec)
        try:
            snapshot = STATE.snapshot()
            if not snapshot["monitoring"]:
                message = "monitor is not running; call install-and-monitor first"
                if snapshot["monitor_error"]:
                    message = "monitor stopped: {}".format(snapshot["monitor_error"])
                raise RuntimeError(message)
            self.server.update_request(
                self._bridge_request_id,
                "waiting for device response {}".format(request_id),
            )
            encoded = json.dumps(payload).encode("utf-8")
            frame = (
                FRAME_PREFIX_BYTES
                + str(len(encoded)).encode("utf-8")
                + b"\n"
                + encoded
                + b"\n"
            )
            STATE.append_log_event("RUNTIME REQUEST: {}".format(json.dumps(payload)))
            STATE.write_bytes(frame)
            return STATE.wait_for_response(request_id, timeout_sec)
        finally:
            STATE.end_runtime_request()

    def handle_runtime(self, body):
        timeout_sec = parse_int(body.get("timeout_sec")) or DEVICE_COMMAND_TIMEOUT_SEC
        request = body.get("request")
        if request is None:
            request = dict(body)
            request.pop("timeout_sec", None)
        if not isinstance(request, dict):
            raise ValueError("runtime request must be an object")
        if "mode" not in request:
            raise ValueError("runtime request requires mode")
        return self.send_runtime_request(request, timeout_sec)

    def handle_call(self, body):
        function_name = body.get("function")
        if not function_name:
            raise ValueError("call requires function")
        args = body.get("args", [])
        kwargs = body.get("kwargs", {})
        if not isinstance(args, list):
            raise ValueError("args must be a list")
        if not isinstance(kwargs, dict):
            raise ValueError("kwargs must be an object")
        timeout_sec = parse_int(body.get("timeout_sec")) or DEVICE_COMMAND_TIMEOUT_SEC
        return self.send_runtime_request(
            {
                "mode": "call",
                "function": function_name,
                "args": args,
                "kwargs": kwargs,
            },
            timeout_sec,
        )

    def handle_eval(self, body):
        code = body.get("code")
        if not isinstance(code, str) or not code:
            raise ValueError("eval requires code")
        timeout_sec = parse_int(body.get("timeout_sec")) or DEVICE_COMMAND_TIMEOUT_SEC
        return self.send_runtime_request(
            {
                "mode": "eval",
                "code": code,
                "statement": bool(body.get("statement", False)),
            },
            timeout_sec,
        )

    def handle_state(self, body):
        timeout_sec = parse_int(body.get("timeout_sec")) or DEVICE_COMMAND_TIMEOUT_SEC
        return self.send_runtime_request({"mode": "get_state"}, timeout_sec)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def send_json(self, status, payload):
        self._response_status = int(status)
        body = json.dumps(payload).encode("utf-8") + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error(self, code, message=None, explain=None):
        self._response_status = int(code)
        return super().send_error(code, message, explain)

    def log_message(self, fmt, *args):
        message = fmt % args
        if '"GET /logs?' in message:
            return
        print(message)


def parse_int(value):
    if value in (None, ""):
        return None
    return int(value)


def collect_thread_stacks():
    frames = sys._current_frames()
    threads = []
    for thread in threading.enumerate():
        frame = frames.get(thread.ident)
        if frame is None:
            stack = []
        else:
            stack = traceback.format_stack(frame)
        threads.append(
            {
                "name": thread.name,
                "ident": thread.ident,
                "daemon": thread.daemon,
                "alive": thread.is_alive(),
                "stack": stack,
            }
        )
    return threads


def parse_args():
    parser = argparse.ArgumentParser(description="Generic MicroPython debug bridge")
    parser.add_argument("--host", default=DEFAULT_HTTP_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT)
    parser.add_argument("--serial-port", type=str, default=None)
    process_mode = parser.add_mutually_exclusive_group()
    process_mode.add_argument(
        "--daemon",
        action="store_true",
        help="detach and keep running after the starting command exits",
    )
    process_mode.add_argument(
        "--foreground",
        action="store_true",
        help="stay attached even when standard input is not a terminal",
    )
    parser.add_argument(
        "--stop-on-sigterm",
        action="store_true",
        help="allow a process supervisor to stop the server with SIGTERM",
    )
    return parser.parse_args()


def daemon_paths(http_port):
    log_dir = os.path.join(REPO_DIR, LOG_DIR_NAME)
    return (
        os.path.join(log_dir, "server-{}.log".format(http_port)),
        os.path.join(log_dir, "server-{}.pid".format(http_port)),
    )


def detach_process(log_path):
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    read_fd, write_fd = os.pipe()
    child_pid = os.fork()
    if child_pid:
        os.close(write_fd)
        ready, _, _ = select.select([read_fd], [], [], DAEMON_START_TIMEOUT_SEC)
        if not ready:
            os.close(read_fd)
            try:
                os.kill(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            raise RuntimeError("timed out waiting for detached server to start")
        message = os.read(read_fd, 4096).decode("utf-8", "replace").strip()
        os.close(read_fd)
        if not message.startswith("READY "):
            raise RuntimeError(message or "detached server exited during startup")
        print(message, flush=True)
        return True, None

    os.close(read_fd)
    os.setsid()
    null_fd = os.open(os.devnull, os.O_RDONLY)
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.dup2(null_fd, sys.stdin.fileno())
    os.dup2(log_fd, sys.stdout.fileno())
    os.dup2(log_fd, sys.stderr.fileno())
    os.close(null_fd)
    os.close(log_fd)
    return False, write_fd


def notify_daemon_parent(ready_fd, message):
    if ready_fd is None:
        return
    try:
        os.write(ready_fd, message.encode("utf-8", "replace"))
    finally:
        os.close(ready_fd)


def write_pid_file(path):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{}\n".format(os.getpid()))


def remove_own_pid_file(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            recorded_pid = int(handle.read().strip())
        if recorded_pid == os.getpid():
            os.unlink(path)
    except (FileNotFoundError, OSError, ValueError):
        pass


def emit_server_event(server, message):
    print(message, flush=True)
    server_log_path = getattr(server, "server_log_path", None)
    if getattr(server, "daemon_mode", False) or not server_log_path:
        return
    os.makedirs(os.path.dirname(server_log_path), exist_ok=True)
    with open(server_log_path, "a", encoding="utf-8") as handle:
        handle.write(message)
        handle.write("\n")


def serve_listener(server):
    listener_failures = 0
    while server.shutdown_reason is None:
        try:
            server.serve_forever()
        except Exception as exc:
            listener_failures += 1
            emit_server_event(
                server,
                "LISTENER LOOP EXCEPTION: {}: {}".format(
                    type(exc).__name__,
                    exc,
                ),
            )
            emit_server_event(server, traceback.format_exc().rstrip())
            emit_server_event(
                server,
                "LISTENER SNAPSHOT: {}".format(
                    json.dumps(server.diagnostic_snapshot(), default=str)
                ),
            )
            if server.fileno() < 0 or listener_failures >= 3:
                server.shutdown_reason = (
                    "listener loop failed {} consecutive times".format(
                        listener_failures
                    )
                )
                break
            time.sleep(0.1)
            continue
        if server.shutdown_reason is None:
            listener_failures += 1
            emit_server_event(
                server,
                "LISTENER LOOP RETURNED UNEXPECTEDLY: {}".format(
                    json.dumps(server.diagnostic_snapshot(), default=str)
                ),
            )
            if listener_failures >= 3:
                server.shutdown_reason = (
                    "listener loop returned unexpectedly {} consecutive times".format(
                        listener_failures
                    )
                )
                break
            time.sleep(0.1)


def main():
    args = parse_args()
    serial_port = args.serial_port or os.getenv("SERIAL_PORT")
    if serial_port is None:
        print("Usage: python3 mpy_debug_server.py --serial-port /dev/cu.usbmodem...")
        return 2

    daemon_mode = args.daemon or (not args.foreground and not sys.stdin.isatty())
    server_log_path, pid_path = daemon_paths(args.port)
    daemon_parent = False
    ready_fd = None
    if daemon_mode:
        daemon_parent, ready_fd = detach_process(server_log_path)
        if daemon_parent:
            return 0

    try:
        server = MPYDebugServer(
            (args.host, args.port),
            Handler,
            serial_port,
            daemon_mode=daemon_mode,
            pid_path=pid_path if daemon_mode else None,
            server_log_path=server_log_path,
        )
        if daemon_mode:
            write_pid_file(pid_path)
    except Exception as exc:
        notify_daemon_parent(ready_fd, "ERROR: {}".format(exc))
        raise

    stop_requested = {"value": False}

    def stop_server(signum, frame):
        _ = frame
        if stop_requested["value"]:
            return
        stop_requested["value"] = True
        print(
            "Stopping server after {}".format(signal.Signals(signum).name),
            flush=True,
        )
        RUNNER.kill_active()
        server.request_shutdown(signal.Signals(signum).name)

    def ignore_termination_signal(signum, frame):
        _ = frame
        print(
            "Ignoring {}; press Ctrl-C to stop the server".format(
                signal.Signals(signum).name
            ),
            flush=True,
        )

    signal.signal(signal.SIGINT, stop_server)
    if args.stop_on_sigterm:
        signal.signal(signal.SIGTERM, stop_server)
    else:
        signal.signal(signal.SIGTERM, ignore_termination_signal)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, ignore_termination_signal)
    notify_daemon_parent(
        ready_fd,
        "READY pid={} url=http://{}:{} log={}".format(
            os.getpid(),
            args.host,
            args.port,
            server_log_path,
        ),
    )
    try:
        serve_listener(server)
    finally:
        emit_server_event(
            server,
            "LISTENER FINAL SNAPSHOT: {}".format(
                json.dumps(server.diagnostic_snapshot(), default=str)
            ),
        )
        STATE.stop_monitor()
        server.server_close()
        if daemon_mode:
            remove_own_pid_file(pid_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
