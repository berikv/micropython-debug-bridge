import hashlib
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
OTA_PATH = (
    ROOT
    / "plugins"
    / "micropython-debug-bridge"
    / "scripts"
    / "codex_ota.py"
)

machine = types.ModuleType("machine")
machine.unique_id = lambda: b"\xaa\xbb\xcc\xdd"
machine.reset = mock.Mock()
network = types.ModuleType("network")
network.STA_IF = 0
network.WLAN = mock.Mock()

with mock.patch.dict(sys.modules, {"machine": machine, "network": network}):
    spec = importlib.util.spec_from_file_location("codex_ota", OTA_PATH)
    OTA = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(OTA)


class FakeClient:
    def __init__(self, data):
        self.data = bytearray(data)
        self.sent = bytearray()

    def recv(self, size):
        chunk = bytes(self.data[:size])
        del self.data[:size]
        return chunk

    def sendall(self, data):
        self.sent.extend(data)


class OTAServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = OTA.OTAService.__new__(OTA.OTAService)
        self.service.config = {"token": "0123456789abcdef"}

    @mock.patch.object(OTA.os, "rename")
    @mock.patch.object(OTA.os, "remove")
    def test_receives_verified_file_before_replacing_target(self, remove, rename):
        payload = b"print('new application')\n"
        header = {
            "protocol": OTA.PROTOCOL,
            "op": "install",
            "token": "0123456789abcdef",
            "restart": False,
            "files": [
                {
                    "name": "main.py",
                    "size": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            ],
        }
        client = FakeClient(json.dumps(header).encode() + b"\n" + payload)
        file_handle = mock.mock_open()

        with mock.patch("builtins.open", file_handle):
            self.service._receive_update(client)

        response = json.loads(client.sent.decode())
        self.assertTrue(response["ok"])
        file_handle.assert_called_once_with(".main.py.ota", "wb")
        file_handle().write.assert_called_once_with(payload)
        remove.assert_called_once_with("main.py")
        rename.assert_called_once_with(".main.py.ota", "main.py")

    def test_rejects_wrong_token_without_writing(self):
        header = {
            "protocol": OTA.PROTOCOL,
            "op": "install",
            "token": "wrong-wrong-wrong",
            "restart": False,
            "files": [{"name": "main.py", "size": 0, "sha256": "0" * 64}],
        }
        client = FakeClient(json.dumps(header).encode() + b"\n")

        with mock.patch("builtins.open") as open_file:
            self.service._receive_update(client)

        self.assertFalse(json.loads(client.sent.decode())["ok"])
        open_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
