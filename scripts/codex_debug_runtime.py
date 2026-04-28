import sys

try:
    import ujson as json
except ImportError:
    import json

try:
    import uselect
except ImportError:
    uselect = None


FRAME_PREFIX = "@@FRAME@@ "
FRAME_PREFIX_BYTES = FRAME_PREFIX.encode("utf-8")


class RuntimeShell:
    def __init__(self, handler, trace=None):
        self.handler = handler
        self.trace = trace
        self.buffer = bytearray()
        self.expected_payload_length = None
        self.poller = None
        if uselect is not None:
            try:
                self.poller = uselect.poll()
                self.poller.register(sys.stdin, uselect.POLLIN)
            except Exception:
                self.poller = None

    def _trace(self, message):
        if self.trace is not None:
            self.trace(message)

    def poll(self):
        if self.poller is None:
            return
        while self.poller.poll(0):
            chunk = self._read_chunk()
            if not chunk:
                break
            self.buffer.extend(chunk)
            self._process_buffer()

    def _read_chunk(self):
        try:
            chunk = sys.stdin.read(128)
        except Exception:
            return b""
        if chunk is None:
            return b""
        if isinstance(chunk, str):
            return chunk.encode("utf-8")
        return bytes(chunk)

    def _process_buffer(self):
        while True:
            if self.expected_payload_length is None:
                newline_index = self.buffer.find(b"\n")
                if newline_index < 0:
                    return
                line = bytes(self.buffer[:newline_index]).rstrip(b"\r")
                del self.buffer[:newline_index + 1]
                if not line:
                    continue
                if not line.startswith(FRAME_PREFIX_BYTES):
                    self._trace("RUNTIME input ignored: {}".format(line.decode("utf-8", "replace")))
                    continue
                try:
                    self.expected_payload_length = int(
                        line[len(FRAME_PREFIX_BYTES):].decode("utf-8", "replace").strip()
                    )
                except ValueError:
                    self._trace("RUNTIME frame error: invalid length header")
                    self.expected_payload_length = None
                    continue

            if self.expected_payload_length is None:
                continue
            if len(self.buffer) < self.expected_payload_length:
                return

            payload_bytes = bytes(self.buffer[:self.expected_payload_length])
            del self.buffer[:self.expected_payload_length]
            if self.buffer[:1] == b"\n":
                del self.buffer[:1]
            self.expected_payload_length = None

            try:
                request = json.loads(payload_bytes.decode("utf-8"))
                response = self.handler(request)
            except Exception as exc:
                request_id = None
                try:
                    request_id = request.get("request_id")
                except Exception:
                    pass
                response = {"ok": False, "request_id": request_id, "error": str(exc)}
            self.send_frame(response)

    def send_frame(self, payload):
        body = json.dumps(payload)
        frame = "{}{}\n{}\n".format(FRAME_PREFIX, len(body), body)
        sys.stdout.write(frame)
        try:
            sys.stdout.flush()
        except Exception:
            pass


def default_serialize(value):
    if isinstance(value, dict):
        result = {}
        for key in value:
            result[key] = default_serialize(value[key])
        return result
    if isinstance(value, list):
        return [default_serialize(item) for item in value]
    if isinstance(value, tuple):
        return [default_serialize(item) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "".join("{:02X}".format(byte) for byte in bytes(value))
    return value


class RuntimeDispatcher:
    def __init__(self, functions=None, context_factory=None, serializer=None):
        self.functions = functions or {}
        self.context_factory = context_factory
        self.serializer = serializer or default_serialize

    def __call__(self, request):
        request_id = request.get("request_id")
        try:
            mode = request.get("mode")
            if mode == "get_state":
                target = self.functions.get("app.get_state")
                if target is None:
                    raise ValueError("get_state requires exported function app.get_state")
                result = target()
            elif mode == "call":
                function_name = request.get("function")
                if not function_name:
                    raise ValueError("call mode requires function")
                target = self.functions.get(function_name)
                if target is None:
                    raise ValueError("unknown exported function: {}".format(function_name))
                args = request.get("args", [])
                kwargs = request.get("kwargs", {})
                if not isinstance(args, list):
                    raise ValueError("args must be a list")
                if not isinstance(kwargs, dict):
                    raise ValueError("kwargs must be an object")
                result = target(*args, **kwargs)
            elif mode == "eval":
                code = request.get("code")
                if not isinstance(code, str) or not code:
                    raise ValueError("eval mode requires code")
                statement = bool(request.get("statement", False))
                env = {}
                if self.context_factory is not None:
                    env.update(self.context_factory())
                if statement:
                    exec(code, env, env)
                    result = env.get("result")
                else:
                    result = eval(code, env, env)
            else:
                raise ValueError("unsupported runtime mode: {}".format(mode))
            return {"ok": True, "request_id": request_id, "data": self.serializer(result)}
        except Exception as exc:
            return {"ok": False, "request_id": request_id, "error": str(exc)}
