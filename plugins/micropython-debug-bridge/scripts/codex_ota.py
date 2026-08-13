"""Small polling OTA service for network-capable MicroPython boards."""

try:
    import ujson as json
except ImportError:
    import json

try:
    import uhashlib as hashlib
except ImportError:
    import hashlib

import machine
import network
import os
import socket
import time

try:
    import uselect as select
except ImportError:
    import select


DISCOVERY_MAGIC = b"MPY_OTA_DISCOVER_V1"
PROTOCOL = "mpy-ota-v1"
DISCOVERY_PORT = 8266
SERVICE_PORT = 8267
CONFIG_PATH = "/codex_ota.json"
HEADER_LIMIT = 16384
CHUNK_SIZE = 1024


def _hex(data):
    return "".join("{:02x}".format(value) for value in bytes(data))


def _read_config(path=CONFIG_PATH):
    with open(path, "r") as handle:
        value = json.loads(handle.read())
    if not isinstance(value, dict):
        raise ValueError("OTA config must contain an object")
    for key in ("ssid", "password", "token"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ValueError("OTA config requires {}".format(key))
    return value


def device_identity(name=None, port=SERVICE_PORT):
    uid = _hex(machine.unique_id())
    wlan = network.WLAN(network.STA_IF)
    try:
        mac = _hex(wlan.config("mac"))
    except Exception:
        mac = None
    try:
        ip = wlan.ifconfig()[0]
    except Exception:
        ip = None
    return {
        "protocol": PROTOCOL,
        "device_id": uid,
        "name": name or "micropython-{}".format(uid[-6:]),
        "mac": mac,
        "ip": ip,
        "port": port,
    }


class OTAService:
    """Poll from the application loop to provide discovery and file updates."""

    def __init__(self, config_path=CONFIG_PATH):
        self.config = _read_config(config_path)
        self.port = int(self.config.get("port", SERVICE_PORT))
        self.wlan = network.WLAN(network.STA_IF)
        self.discovery_socket = None
        self.service_socket = None
        self.poller = None
        self.identity = device_identity(self.config.get("name"), self.port)

    def connect(self, timeout_sec=20):
        self.wlan.active(True)
        if not self.wlan.isconnected():
            self.wlan.connect(self.config["ssid"], self.config["password"])
            deadline = time.ticks_add(time.ticks_ms(), int(timeout_sec * 1000))
            while not self.wlan.isconnected():
                if time.ticks_diff(deadline, time.ticks_ms()) <= 0:
                    raise RuntimeError("WiFi connection timed out")
                time.sleep_ms(100)
        self.identity = device_identity(self.config.get("name"), self.port)
        self._open_sockets()
        print(
            "OTA READY id={} name={} mac={} ip={}".format(
                self.identity["device_id"],
                self.identity["name"],
                self.identity["mac"],
                self.identity["ip"],
            )
        )
        return self.identity

    def close(self):
        for sock in (self.discovery_socket, self.service_socket):
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        self.discovery_socket = None
        self.service_socket = None
        self.poller = None

    def _open_sockets(self):
        self.close()
        discovery = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        discovery.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        discovery.bind(("0.0.0.0", DISCOVERY_PORT))
        discovery.setblocking(False)
        service = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        service.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        service.bind(("0.0.0.0", self.port))
        service.listen(1)
        service.setblocking(False)
        self.discovery_socket = discovery
        self.service_socket = service
        self.poller = select.poll()
        self.poller.register(discovery, select.POLLIN)
        self.poller.register(service, select.POLLIN)

    def poll(self, timeout_ms=0):
        if self.discovery_socket is None or self.service_socket is None:
            raise RuntimeError("call connect() before poll()")
        for descriptor, _ in self.poller.poll(timeout_ms):
            if (
                descriptor is self.discovery_socket
                or descriptor == self.discovery_socket.fileno()
            ):
                self._answer_discovery()
            else:
                client, _ = self.service_socket.accept()
                try:
                    client.settimeout(10)
                    self._receive_update(client)
                finally:
                    client.close()

    def _answer_discovery(self):
        payload, address = self.discovery_socket.recvfrom(256)
        if payload.strip() == DISCOVERY_MAGIC:
            self.identity = device_identity(self.config.get("name"), self.port)
            self.discovery_socket.sendto(json.dumps(self.identity).encode(), address)

    @staticmethod
    def _readline(sock):
        data = bytearray()
        while len(data) <= HEADER_LIMIT:
            chunk = sock.recv(1)
            if not chunk:
                raise OSError("connection closed while reading OTA header")
            if chunk == b"\n":
                return bytes(data)
            data.extend(chunk)
        raise ValueError("OTA header is too large")

    @staticmethod
    def _receive_file(sock, target, size, expected_hash):
        temporary = ".{}.ota".format(target)
        digest = hashlib.sha256()
        remaining = size
        try:
            with open(temporary, "wb") as handle:
                while remaining:
                    chunk = sock.recv(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        raise OSError("connection closed during {}".format(target))
                    handle.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
            actual_hash = _hex(digest.digest())
            if actual_hash != expected_hash:
                raise ValueError("SHA-256 mismatch for {}".format(target))
            try:
                os.remove(target)
            except OSError:
                pass
            os.rename(temporary, target)
        except Exception:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise

    def _receive_update(self, sock):
        restart = False
        try:
            request = json.loads(self._readline(sock).decode())
            if request.get("protocol") != PROTOCOL or request.get("op") != "install":
                raise ValueError("unsupported OTA request")
            if request.get("token") != self.config["token"]:
                raise ValueError("invalid OTA token")
            files = request.get("files")
            if not isinstance(files, list) or not files:
                raise ValueError("OTA request has no files")
            for item in files:
                name = item.get("name")
                size = item.get("size")
                digest = item.get("sha256")
                if (
                    not isinstance(name, str)
                    or not name
                    or name in (".", "..")
                    or "/" in name
                    or "\\" in name
                ):
                    raise ValueError("OTA file names must be root-level basenames")
                if not isinstance(size, int) or size < 0:
                    raise ValueError("invalid OTA file size")
                if not isinstance(digest, str) or len(digest) != 64:
                    raise ValueError("invalid OTA file hash")
                self._receive_file(sock, name, size, digest)
            restart = bool(request.get("restart", True))
            response = {"ok": True, "files": [item["name"] for item in files]}
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        sock.sendall((json.dumps(response) + "\n").encode())
        if restart and response["ok"]:
            time.sleep_ms(100)
            machine.reset()
