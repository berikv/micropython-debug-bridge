import importlib.util
import json
import os
import pty
import select
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SERVER_PATH = (
    ROOT
    / "plugins"
    / "micropython-debug-bridge"
    / "scripts"
    / "mpy_mcp_server.py"
)
SPEC = importlib.util.spec_from_file_location("mpy_mcp_server", SERVER_PATH)
SERVER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SERVER)


class SerialDiscoveryTests(unittest.TestCase):
    @mock.patch.object(SERVER.glob, "glob")
    @mock.patch.object(SERVER.os, "stat")
    @mock.patch.object(SERVER.os, "access")
    def test_lists_macos_cu_character_devices(
        self, access, os_stat, glob_devices
    ):
        glob_devices.return_value = [
            "/dev/cu.usbmodem2",
            "/dev/cu.usbmodem1",
        ]
        os_stat.return_value.st_mode = 0o020666
        access.side_effect = lambda path, mode: (
            path == "/dev/cu.usbmodem1" or mode == os.R_OK
        )

        ports = SERVER.discover_serial_ports()

        self.assertEqual(
            [item["path"] for item in ports],
            ["/dev/cu.usbmodem1", "/dev/cu.usbmodem2"],
        )
        self.assertTrue(ports[0]["writable"])
        self.assertFalse(ports[1]["writable"])
        glob_devices.assert_called_with("/dev/cu.*")

    @mock.patch.object(SERVER, "discover_serial_ports")
    def test_selection_requires_an_available_writable_device(self, discover):
        discover.return_value = [
            {
                "path": "/dev/cu.usbmodem11201",
                "name": "cu.usbmodem11201",
                "readable": True,
                "writable": True,
            }
        ]
        controller = SERVER.DeviceController()
        self.addCleanup(controller.close)

        result = controller.select_port({"port": "/dev/cu.usbmodem11201"})

        self.assertEqual(result["selected_port"], "/dev/cu.usbmodem11201")
        with self.assertRaisesRegex(ValueError, "list_serial_ports"):
            controller.select_port({"port": "/dev/cu.not-present"})

    @mock.patch.object(SERVER, "discover_serial_ports")
    def test_selection_reports_host_permission_failure(self, discover):
        discover.return_value = [
            {
                "path": "/dev/cu.usbmodem11201",
                "name": "cu.usbmodem11201",
                "readable": True,
                "writable": False,
            }
        ]
        controller = SERVER.DeviceController()
        self.addCleanup(controller.close)

        with self.assertRaisesRegex(PermissionError, "restart Codex"):
            controller.select_port({"port": "/dev/cu.usbmodem11201"})


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.master_fd, self.slave_fd = pty.openpty()
        self.slave_path = os.ttyname(self.slave_fd)
        self.state = SERVER.MonitorState()

    def tearDown(self):
        self.state.stop_monitor()
        os.close(self.master_fd)
        os.close(self.slave_fd)

    def test_monitor_opens_and_reads_a_tty_directly(self):
        self.state.start_monitor(self.slave_path)
        os.write(self.master_fd, b"booted\r\n")

        deadline = time.monotonic() + 2
        lines = []
        while time.monotonic() < deadline:
            lines, _ = self.state.get_lines()
            if lines:
                break
            time.sleep(0.01)

        self.assertEqual(lines, ["booted"])
        self.assertTrue(self.state.snapshot()["monitoring"])

    def test_runtime_request_uses_framed_json_on_same_tty(self):
        self.state.start_monitor(self.slave_path)

        def device():
            buffer = bytearray()
            deadline = time.monotonic() + 2
            while b"\n" not in buffer and time.monotonic() < deadline:
                ready, _, _ = select.select([self.master_fd], [], [], 0.1)
                if ready:
                    buffer.extend(os.read(self.master_fd, 1024))
            header, payload_start = bytes(buffer).split(b"\n", 1)
            length = int(header[len(SERVER.FRAME_PREFIX_BYTES) :])
            while len(payload_start) < length:
                payload_start += os.read(self.master_fd, 1024)
            request = json.loads(payload_start[:length])
            response = json.dumps(
                {
                    "ok": True,
                    "request_id": request["request_id"],
                    "data": {"value": 42},
                },
                separators=(",", ":"),
            ).encode()
            os.write(
                self.master_fd,
                SERVER.FRAME_PREFIX_BYTES
                + str(len(response)).encode()
                + b"\n"
                + response
                + b"\n",
            )

        device_thread = threading.Thread(target=device)
        device_thread.start()
        result = self.state.runtime_request("get_state", {}, 2)
        device_thread.join(timeout=2)

        self.assertEqual(result["data"], {"value": 42})
        self.assertFalse(device_thread.is_alive())


class MCPProtocolTests(unittest.TestCase):
    def setUp(self):
        self.controller = SERVER.DeviceController()
        self.server = SERVER.MCPServer(self.controller)

    def tearDown(self):
        self.controller.close()

    def test_initialize_advertises_tools(self):
        response = self.server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25"},
            }
        )

        self.assertEqual(
            response["result"]["serverInfo"]["name"],
            "micropython-debug-bridge",
        )
        self.assertEqual(
            response["result"]["capabilities"],
            {"tools": {"listChanged": False}},
        )

    def test_tools_include_list_and_select_serial_port(self):
        response = self.server.dispatch(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        tools = {
            item["name"]: item for item in response["result"]["tools"]
        }

        self.assertIn("list_serial_ports", tools)
        self.assertIn("select_serial_port", tools)
        self.assertEqual(
            tools["select_serial_port"]["inputSchema"]["properties"]["port"][
                "pattern"
            ],
            r"^/dev/cu\..+",
        )

    def test_notifications_do_not_receive_responses(self):
        self.assertIsNone(
            self.server.dispatch(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                }
            )
        )

    def test_tool_failures_are_mcp_error_results_without_stopping_server(self):
        response = self.server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "select_serial_port",
                    "arguments": {"port": "/dev/cu.not-present"},
                },
            }
        )

        self.assertTrue(response["result"]["isError"])
        self.assertEqual(
            self.server.dispatch(
                {"jsonrpc": "2.0", "id": 4, "method": "ping"}
            )["result"],
            {},
        )


if __name__ == "__main__":
    unittest.main()
