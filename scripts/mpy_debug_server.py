#!/usr/bin/env python3

import argparse
import glob
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
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8765
SERIAL_BAUD = 115200
LOG_CAPACITY = 2000
COMMAND_TIMEOUT_SEC = 20
DEVICE_COMMAND_TIMEOUT_SEC = 60
FRAME_PREFIX = "@@FRAME@@ "
FRAME_PREFIX_BYTES = FRAME_PREFIX.encode("utf-8")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEBUG_RUNTIME_PATH = os.path.join(SCRIPT_DIR, "codex_debug_runtime.py")
DEBUG_RUNTIME_NAME = os.path.basename(DEBUG_RUNTIME_PATH)


class CommandRunner:
    def __init__(self):
        self._lock = threading.Lock()
        self._processes = set()

    def run(self, argv, cwd):
        process = subprocess.Popen(
            argv,
            cwd=cwd,
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

    def append_response(self, payload):
        with self._response_condition:
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

    def stop_monitor(self):
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

    def start_monitor(self, port, repo_root):
        self.stop_monitor()
        configure_serial_port(port, repo_root)
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
        total_written = 0
        while total_written < len(data):
            try:
                written = os.write(serial_fd, data[total_written:])
            except BlockingIOError:
                time.sleep(0.01)
                continue
            if written <= 0:
                raise RuntimeError("failed to write to serial monitor")
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


def configure_serial_port(port, repo_root):
    run_command(["stty", "-f", port, str(SERIAL_BAUD), "raw", "-echo"], cwd=repo_root)


def run_command(argv, cwd):
    return RUNNER.run(argv, cwd)


def micropython_files(repo_root):
    files = sorted(glob.glob(os.path.join(repo_root, "*.py")))
    if not files:
        raise ValueError("no Python files found in {}".format(repo_root))
    return files


def install_files(repo_root, port, reset=True):
    files = micropython_files(repo_root)
    run_command(["mpremote", "connect", port, "fs", "cp", *files, ":"], cwd=repo_root)
    listing = run_command(["mpremote", "connect", port, "fs", "ls"], cwd=repo_root)
    if reset:
        reset_board(port, repo_root)
    return {"files": [os.path.basename(path) for path in files], "listing": listing}


def install_debug_runtime(port, repo_root, reset=True):
    run_command(["mpremote", "connect", port, "fs", "cp", DEBUG_RUNTIME_PATH, ":"], cwd=repo_root)
    listing = run_command(["mpremote", "connect", port, "fs", "ls"], cwd=repo_root)
    if reset:
        reset_board(port, repo_root)
    return {"files": [DEBUG_RUNTIME_NAME], "listing": listing}


def remove_debug_runtime(port, repo_root, reset=True):
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
        cwd=repo_root,
    )
    listing = run_command(["mpremote", "connect", port, "fs", "ls"], cwd=repo_root)
    if reset:
        reset_board(port, repo_root)
    return {"files": [DEBUG_RUNTIME_NAME], "listing": listing}


def reset_board(port, repo_root):
    run_command(["mpremote", "connect", port, "reset"], cwd=repo_root)


class MPYDebugServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_class, serial_port, repo_root):
        super().__init__(server_address, handler_class)
        self.serial_port = serial_port
        self.repo_root = repo_root


class Handler(BaseHTTPRequestHandler):
    server_version = "MPYDebugBridge/0.2"

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            return self.send_json(
                HTTPStatus.OK,
                {"ok": True, "repo_root": self.server.repo_root, **STATE.snapshot()},
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
        repo_root = self.server.repo_root
        port = self.server.serial_port
        STATE.stop_monitor()
        if monitor:
            STATE.clear_logs()
        include_runtime = bool(body.get("debug_runtime"))
        result = install_files(repo_root, port, reset=(not monitor) and (not include_runtime))
        if include_runtime:
            runtime_result = install_debug_runtime(port, repo_root, reset=not monitor)
            result["files"].extend(runtime_result["files"])
            result["listing"] = runtime_result["listing"]
        if not monitor:
            result["monitoring"] = False
            result["port"] = port
            return {"ok": True, **result}
        STATE.start_monitor(port, repo_root)
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
        repo_root = self.server.repo_root
        port = self.server.serial_port
        STATE.stop_monitor()
        if monitor:
            STATE.clear_logs()
        result = install_debug_runtime(port, repo_root, reset=not monitor)
        if not monitor:
            result["monitoring"] = False
            result["port"] = port
            return {"ok": True, **result}
        STATE.start_monitor(port, repo_root)
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
        repo_root = self.server.repo_root
        port = self.server.serial_port
        STATE.stop_monitor()
        result = remove_debug_runtime(port, repo_root, reset=True)
        result["monitoring"] = False
        result["port"] = port
        return {"ok": True, **result}

    def handle_reset(self):
        repo_root = self.server.repo_root
        port = self.server.serial_port
        STATE.stop_monitor()
        reset_board(port, repo_root)
        return {"ok": True, "port": port}

    def handle_monitor(self):
        port = self.server.serial_port
        STATE.clear_logs()
        STATE.start_monitor(port, self.server.repo_root)
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
        print(fmt % args)


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
    parser.add_argument("--repo-root", type=str, default=None)
    return parser.parse_args()


def main():
    args = parse_args()
    serial_port = args.serial_port or os.getenv("SERIAL_PORT")
    if serial_port is None:
        print("Usage: python3 debug_bridge/mpy_debug_server.py --serial-port /dev/cu.usbmodem...")
        return
    repo_root = os.path.abspath(args.repo_root or os.getenv("MPY_PROJECT_ROOT") or os.getcwd())
    if not os.path.isdir(repo_root):
        print("repo root does not exist: {}".format(repo_root))
        return

    server = MPYDebugServer((args.host, args.port), Handler, serial_port, repo_root)
    stop_requested = {"value": False}

    def handle_signal(signum, frame):
        _ = frame
        if stop_requested["value"]:
            return
        stop_requested["value"] = True
        RUNNER.kill_active()
        STATE.stop_monitor()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    try:
        server.serve_forever()
    finally:
        STATE.stop_monitor()
        server.server_close()


if __name__ == "__main__":
    main()
