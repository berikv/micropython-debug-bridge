import importlib.util
import os
import threading
import time
import unittest
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


if __name__ == "__main__":
    unittest.main()
