import importlib.util
import io
import json
import os
import socket
import threading
import time
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


SERVER_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "mpy_debug_server.py"
)
SPEC = importlib.util.spec_from_file_location("mpy_debug_server", SERVER_PATH)
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


class MonitorStateTests(unittest.TestCase):
    def test_runtime_requests_are_serialized(self):
        state = SERVER.MonitorState()
        first_entered = threading.Event()
        release_first = threading.Event()
        order = []

        def first_request():
            state.begin_runtime_request(1, "first", 10)
            order.append("first-entered")
            first_entered.set()
            release_first.wait(timeout=2)
            order.append("first-finished")
            state.end_runtime_request()

        def second_request():
            first_entered.wait(timeout=2)
            state.begin_runtime_request(2, "second", 10)
            order.append("second-entered")
            state.end_runtime_request()

        first = threading.Thread(target=first_request)
        second = threading.Thread(target=second_request)
        first.start()
        second.start()
        self.assertTrue(first_entered.wait(timeout=2))
        time.sleep(0.05)
        self.assertEqual(order, ["first-entered"])

        release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(
            order,
            ["first-entered", "first-finished", "second-entered"],
        )
        self.assertIsNone(state.snapshot()["active_runtime_request"])

    def test_monitor_error_wakes_response_waiter_immediately(self):
        state = SERVER.MonitorState()
        serial_fd, write_fd = os.pipe()
        try:
            with state._lock:
                state._serial_fd = serial_fd
                state._monitor_thread = threading.Thread(target=lambda: None)
                state._port = "/dev/test"

            state._record_monitor_error(
                serial_fd,
                OSError("device disconnected"),
            )
            serial_fd = None

            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "device disconnected"):
                state.wait_for_response(1, 10)
            self.assertLess(time.monotonic() - started, 0.1)
            self.assertEqual(
                state.snapshot()["monitor_error"],
                "device disconnected",
            )
        finally:
            if serial_fd is not None:
                os.close(serial_fd)
            os.close(write_fd)


class HTTPServerConfigurationTests(unittest.TestCase):
    def test_server_is_configured_for_concurrent_persistent_requests(self):
        self.assertTrue(SERVER.MPYDebugServer.allow_reuse_address)
        self.assertTrue(SERVER.MPYDebugServer.daemon_threads)
        self.assertFalse(SERVER.MPYDebugServer.block_on_close)
        self.assertEqual(SERVER.MPYDebugServer.request_queue_size, 64)


class ListenerLoopTests(unittest.TestCase):
    def test_repeated_listener_exceptions_are_logged_and_stop_the_loop(self):
        class FailingServer:
            shutdown_reason = None

            def __init__(self):
                self.calls = 0

            def serve_forever(self):
                self.calls += 1
                raise OSError("listener failed")

            def diagnostic_snapshot(self):
                return {"active_http_requests": [{"request_id": 7}]}

            def fileno(self):
                return 10

        server = FailingServer()
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            SERVER.serve_listener(server)

        self.assertEqual(server.calls, 3)
        self.assertEqual(
            server.shutdown_reason,
            "listener loop failed 3 consecutive times",
        )
        self.assertEqual(output.getvalue().count("LISTENER LOOP EXCEPTION:"), 3)
        self.assertIn('"request_id": 7', output.getvalue())


class LongRequestRegressionTests(unittest.TestCase):
    def test_long_call_and_disconnected_client_leave_listener_available(self):
        class SlowHandler(SERVER.Handler):
            def handle_state(self, body):
                _ = body
                time.sleep(0.25)
                return {"ok": True, "state": "complete"}

            def handle_install(self, body, monitor):
                _ = body
                return {"ok": True, "monitoring": monitor}

            def log_message(self, fmt, *args):
                _ = (fmt, args)

        server = SERVER.MPYDebugServer(
            ("127.0.0.1", 0),
            SlowHandler,
            "/dev/test",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address

        def fetch(path, body=None):
            url = "http://{}:{}{}".format(host, port, path)
            if body is None:
                request = url
            else:
                request = urllib.request.Request(
                    url,
                    data=json.dumps(body).encode("utf-8"),
                    method="POST",
                )
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read())

        try:
            self.assertEqual(fetch("/state", {})[0], 200)
            self.assertEqual(fetch("/health")[0], 200)
            self.assertEqual(fetch("/install-and-monitor", {})[0], 200)

            client = socket.create_connection((host, port), timeout=2)
            client.sendall(
                b"POST /state HTTP/1.1\r\n"
                + b"Host: 127.0.0.1\r\n"
                + b"Content-Type: application/json\r\n"
                + b"Content-Length: 2\r\n"
                + b"\r\n{}"
            )
            client.close()
            time.sleep(0.35)

            status, health = fetch("/health")
            self.assertEqual(status, 200)
            self.assertEqual(health["active_http_request_count"], 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
