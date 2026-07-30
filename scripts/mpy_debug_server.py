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
        self._lines = deque(maxlen=LOG_CAPACITY)
        self._responses = []
        self._cursor = 0
        self._serial_fd = None
        self._monitor_thread = None
        self._stop_event = None
        self._port = None
        self._next_request_id = 1
        self._install_log_path = None
        self._install_log_sequence = 0

    def snapshot(self):
        with self._lock:
            return {
                "port": self._port,
                "monitoring": self._monitor_thread is not None and self._monitor_thread.is_alive(),
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
            self._write_log_line_locked("SERIAL LINE: " + line)

    def append_log_event(self, message):
        with self._lock:
            self._write_log_line_locked(message)

    def append_serial_bytes(self, direction, data):
        self.append_log_event("SERIAL {}: {}".format(direction, data.hex()))

    def _write_log_line_locked(self, message):
        if self._install_log_path is None:
            return
        encoded = (message + "\n").encode("utf-8")
        with open(self._install_log_path, "ab") as handle:
            handle.write(gzip.compress(encoded))

    def start_monitor_log(self):
        with self._lock:
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
            self._write_log_line_locked("MONITOR LOG START: {}".format(stamp))
            return path

    def close_install_log(self):
        with self._lock:
            self._close_install_log_locked()

    def _close_install_log_locked(self):
        if self._install_log_path is not None:
            self._write_log_line_locked("MONITOR LOG END")
            self._install_log_path = None

    def append_response(self, payload):
        with self._response_condition:
            self._write_log_line_locked("RUNTIME RESPONSE: {}".format(json.dumps(payload)))
            self._responses.append(payload)
            self._response_condition.notify_all()

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
            if written <= 0:
                raise RuntimeError("failed to write to serial monitor")
            self.append_serial_bytes("TX", data[total_written: total_written + written])
            total_written += written

    def next_request_id(self):
        with self._lock:
            request_id = self._next_request_id
            self._next_request_id += 1
            return request_id

    def wait_for_response(self, request_id, timeout_sec):
        deadline = time.monotonic() + timeout_sec
        with self._response_condition:
            while True:
                for index, payload in enumerate(self._responses):
                    if payload.get("request_id") == request_id:
                        return self._responses.pop(index)
                if self._serial_fd is None:
                    raise RuntimeError("monitor is not running")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("timed out waiting for device response")
                self._response_condition.wait(timeout=remaining)

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
                self.append_line("MONITOR ERROR: {}".format(exc))
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
    def __init__(self, server_address, handler_class, serial_port):
        super().__init__(server_address, handler_class)
        self.serial_port = serial_port


class Handler(BaseHTTPRequestHandler):
    server_version = "MPYDebugBridge/0.2"

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            return self.send_json(
                HTTPStatus.OK,
                {"ok": True, **STATE.snapshot()},
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
                {"ok": True, "threads": collect_thread_stacks(), **STATE.snapshot()},
            )
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        parsed = urlparse(self.path)
        body = self.read_json()
        try:
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
        except (RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
            return self.send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
        self.send_error(HTTPStatus.NOT_FOUND)

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
        snapshot = STATE.snapshot()
        if not snapshot["monitoring"]:
            raise RuntimeError("monitor is not running; call install-and-monitor first")
        request_id = STATE.next_request_id()
        payload = dict(request)
        payload["request_id"] = request_id
        encoded = json.dumps(payload).encode("utf-8")
        frame = FRAME_PREFIX_BYTES + str(len(encoded)).encode("utf-8") + b"\n" + encoded + b"\n"
        STATE.append_log_event("RUNTIME REQUEST: {}".format(json.dumps(payload)))
        STATE.write_bytes(frame)
        return STATE.wait_for_response(request_id, timeout_sec)

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
        body = json.dumps(payload).encode("utf-8") + b"\n"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
    parser.add_argument(
        "--stop-on-sigterm",
        action="store_true",
        help="allow a process supervisor to stop the server with SIGTERM",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    serial_port = args.serial_port or os.getenv("SERIAL_PORT")
    if serial_port is None:
        print("Usage: python3 mpy_debug_server.py --serial-port /dev/cu.usbmodem...")
        return
    server = MPYDebugServer((args.host, args.port), Handler, serial_port)
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
        STATE.stop_monitor()
        threading.Thread(target=server.shutdown, daemon=True).start()

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
    try:
        server.serve_forever()
    finally:
        STATE.stop_monitor()
        server.server_close()


if __name__ == "__main__":
    main()
