import importlib.util
import io
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


class OTADeviceTests(unittest.TestCase):
    def setUp(self):
        self.controller = SERVER.DeviceController()

    def tearDown(self):
        self.controller.close()

    @mock.patch.object(SERVER, "discover_ota_devices")
    def test_discovers_and_selects_multiple_devices_by_stable_id(self, discover):
        discover.return_value = [
            {
                "device_id": "aabbcc01",
                "name": "reader-kitchen",
                "mac": "001122334455",
                "host": "192.0.2.10",
                "port": 8267,
                "protocol": SERVER.OTA_PROTOCOL,
            },
            {
                "device_id": "aabbcc02",
                "name": "reader-lab",
                "mac": "001122334466",
                "host": "192.0.2.11",
                "port": 8267,
                "protocol": SERVER.OTA_PROTOCOL,
            },
        ]

        result = self.controller.list_ota_devices({"timeout_sec": 0.1})
        selected = self.controller.select_ota_device({"device_id": "aabbcc02"})

        self.assertEqual(len(result["devices"]), 2)
        self.assertEqual(selected["selected_device"]["name"], "reader-lab")
        with self.assertRaisesRegex(ValueError, "latest list_ota_devices"):
            self.controller.select_ota_device({"device_id": "missing"})

    def test_serial_identity_program_is_valid_python(self):
        compile(self.controller._serial_identity_program(), "<identity>", "exec")

    @mock.patch.object(SERVER.socket, "create_connection")
    def test_ota_install_streams_header_and_file_with_hash(self, connect):
        class FakeConnection:
            def __init__(self):
                self.sent = bytearray()

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return None

            def settimeout(self, _):
                pass

            def sendall(self, data):
                self.sent.extend(data)

            def makefile(self, _):
                return io.BytesIO(b'{"ok":true}\n')

        connection = FakeConnection()
        connect.return_value = connection
        self.controller._ota_devices = {
            "aabbcc01": {
                "device_id": "aabbcc01",
                "name": "reader-kitchen",
                "mac": "001122334455",
                "host": "192.0.2.10",
                "port": 8267,
                "protocol": SERVER.OTA_PROTOCOL,
            }
        }
        self.controller._selected_ota_device = "aabbcc01"
        source = SERVER.DEBUG_RUNTIME_PATH
        result = self.controller.install_files_ota(
            {
                "files": [str(source)],
                "token": "0123456789abcdef",
                "restart": False,
            }
        )

        header, body = bytes(connection.sent).split(b"\n", 1)
        request = json.loads(header)
        self.assertEqual(body, source.read_bytes())
        self.assertEqual(
            request["files"][0]["sha256"],
            SERVER.hashlib.sha256(body).hexdigest(),
        )
        self.assertEqual(request["files"][0]["size"], len(body))
        self.assertEqual(result["transport"], "ota")


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


class DeviceTakeoverTests(unittest.TestCase):
    @mock.patch.object(SERVER.os, "open")
    def test_takeover_open_failure_preserves_port_ownership_evidence(
        self, open_device
    ):
        open_device.side_effect = PermissionError("device busy")

        with self.assertRaises(SERVER.DeviceControlError) as raised:
            SERVER.interrupt_running_program("/dev/cu.test")

        diagnostics = raised.exception.diagnostics
        self.assertFalse(diagnostics["ok"])
        self.assertEqual(diagnostics["failure_stage"], "open")
        self.assertEqual(diagnostics["port"], "/dev/cu.test")
        self.assertIn("device busy", diagnostics["error"])

    def test_repeated_interrupts_leave_evidence_of_friendly_repl(self):
        master_fd, slave_fd = pty.openpty()
        slave_path = os.ttyname(slave_fd)
        stop = threading.Event()

        def device():
            received = bytearray()
            responded = False
            while not stop.is_set():
                ready, _, _ = select.select([master_fd], [], [], 0.1)
                if not ready:
                    continue
                received.extend(os.read(master_fd, 128))
                if not responded and received.count(3) >= 2:
                    os.write(master_fd, b"KeyboardInterrupt\r\n>>> ")
                    responded = True

        device_thread = threading.Thread(target=device)
        device_thread.start()
        try:
            result = SERVER.interrupt_running_program(slave_path)
        finally:
            stop.set()
            device_thread.join(timeout=2)
            os.close(master_fd)
            os.close(slave_fd)

        self.assertTrue(result["friendly_prompt_seen"])
        self.assertTrue(result["keyboard_interrupt_seen"])
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(
            result["transmissions_hex"].count(b"\r\x03".hex()),
            SERVER.SERIAL_TAKEOVER_ATTEMPTS,
        )

    @mock.patch.object(SERVER, "interrupt_running_program")
    def test_mpremote_failure_retains_takeover_and_command_evidence(
        self, interrupt
    ):
        interrupt.return_value = {
            "port": "/dev/cu.test",
            "rx_hex": "3e3e3e20",
            "rx_text": ">>> ",
            "friendly_prompt_seen": True,
        }
        controller = SERVER.DeviceController()
        controller.runner.run = mock.Mock(
            side_effect=RuntimeError("could not enter raw repl")
        )
        self.addCleanup(controller.close)

        with self.assertRaises(SERVER.DeviceControlError) as raised:
            controller._run_mpremote(
                "/dev/cu.test",
                ["connect", "/dev/cu.test", "reset"],
            )

        diagnostics = raised.exception.diagnostics
        self.assertEqual(diagnostics["takeover"]["rx_text"], ">>> ")
        self.assertEqual(
            diagnostics["command"],
            ["mpremote", "connect", "/dev/cu.test", "reset"],
        )
        self.assertFalse(diagnostics["mpremote_ok"])
        self.assertIn("could not enter raw repl", diagnostics["mpremote_error"])
        self.assertEqual(
            controller.status({})["last_device_control"],
            diagnostics,
        )

    @mock.patch.object(SERVER, "interrupt_running_program")
    def test_takeover_failure_is_retained_without_starting_mpremote(
        self, interrupt
    ):
        interrupt.side_effect = SERVER.DeviceControlError(
            "serial takeover failed during open",
            {
                "ok": False,
                "port": "/dev/cu.test",
                "failure_stage": "open",
                "error": "device busy",
            },
        )
        controller = SERVER.DeviceController()
        controller.runner.run = mock.Mock()
        self.addCleanup(controller.close)

        with self.assertRaises(SERVER.DeviceControlError) as raised:
            controller._run_mpremote(
                "/dev/cu.test",
                ["connect", "/dev/cu.test", "reset"],
            )

        controller.runner.run.assert_not_called()
        self.assertEqual(
            raised.exception.diagnostics["takeover"]["failure_stage"],
            "open",
        )
        self.assertEqual(
            controller.status({})["last_device_control"],
            raised.exception.diagnostics,
        )

    def test_mcp_error_result_exposes_structured_diagnostics(self):
        error = SERVER.DeviceControlError(
            "control failed",
            {"takeover": {"rx_hex": "00ff"}},
        )

        result = SERVER._error_result(error)

        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["diagnostics"]["takeover"]["rx_hex"],
            "00ff",
        )


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
        self.assertIn("identify_serial_device", tools)
        self.assertIn("provision_ota", tools)
        self.assertIn("list_ota_devices", tools)
        self.assertIn("select_ota_device", tools)
        self.assertIn("install_files_ota", tools)
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
