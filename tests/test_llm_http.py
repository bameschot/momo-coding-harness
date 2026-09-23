"""The LLM adapters speak HTTP with the standard library (harness/llm/http.py).

A stub server emulates the parts of Ollama's and llama.cpp's APIs the adapters
use, so the wire format, streaming, errors, auth and abort are tested without a
model.  The last test guards the point of the change: importing the harness
must not pull in httpx, ollama or pydantic.
"""
import json
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from harness.llm import make_client
from harness.llm.http import LLMHTTPError, normalize_base_url

OLLAMA_STREAM = [
    {"message": {"role": "assistant", "thinking": "hmm "}, "done": False},
    {"message": {"role": "assistant", "content": "Hel"}, "done": False},
    {"message": {"role": "assistant", "content": "lo",
                 "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "a.py"}}},
                                {"function": {"name": "grep", "arguments": "{\"q\": \"x\"}"}}]},
     "done": False},
    {"message": {"role": "assistant", "content": ""}, "done": True,
     "done_reason": "stop", "prompt_eval_count": 12, "eval_count": 5},
]

LLAMA_SSE = [
    {"choices": [{"delta": {"reasoning_content": "think "}}]},
    {"choices": [{"delta": {"content": "Hi"}}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "read_file", "arguments": "{\"pa"}}]}}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "th\": \"b.py\"}"}}]}}]},
    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 7}},
]


class Stub(BaseHTTPRequestHandler):
    server_version = "stub"
    requests: list = []
    stall = threading.Event()

    def log_message(self, *a):
        pass

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n)) if n else None

    def _json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record(self, body):
        Stub.requests.append({"path": self.path, "method": self.command, "body": body,
                              "auth": self.headers.get("Authorization")})

    def do_GET(self):
        self._record(None)
        if self.path == "/api/tags":
            self._json({"models": [{"name": "qwen3.5:9b", "model": "qwen3.5:9b"},
                                   {"name": "llama3:8b", "model": "llama3:8b"}]})
        elif self.path == "/props":
            self._json({"default_generation_settings": {"n_ctx": 8192}})
        elif self.path == "/v1/models":
            self._json({"data": [{"id": "Qwen3.5-9B"}]})
        elif self.path == "/old":
            self.send_response(307)
            self.send_header("Location", "/api/tags")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        body = self._body()
        self._record(body)
        if self.path == "/api/show":
            if body.get("model") == "missing":
                self._json({"error": "model 'missing' not found"}, 404)
            else:
                self._json({"model_info": {"qwen35.context_length": 262144, "general.x": 1}})
        elif self.path == "/api/chat":
            if body["model"] == "broken":
                self._json({"error": "model runner crashed"}, 500)
                return
            if not body.get("stream"):
                final = dict(OLLAMA_STREAM[-1])
                final["message"] = {"role": "assistant", "content": "Hello", "thinking": "hmm",
                                    "tool_calls": OLLAMA_STREAM[2]["message"]["tool_calls"]}
                self._json(final)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            chunks = OLLAMA_STREAM
            if body["model"] == "midstream-error":
                chunks = OLLAMA_STREAM[:1] + [{"error": "out of memory"}]
            for c in chunks:
                self.wfile.write((json.dumps(c) + "\n").encode())
                self.wfile.flush()
            if body["model"] == "stall":
                Stub.stall.wait(10)
        elif self.path == "/v1/chat/completions":
            if body.get("model") == "broken":
                self._json({"error": {"code": 500, "message": "context overflow", "type": "server_error"}}, 500)
                return
            if not body.get("stream"):
                self._json({"choices": [{"message": {"content": "Hi", "reasoning_content": "think",
                                                     "tool_calls": [{"function": {"name": "read_file",
                                                                                  "arguments": "{\"path\": \"b.py\"}"}}]},
                                         "finish_reason": "tool_calls"}],
                            "usage": {"prompt_tokens": 20, "completion_tokens": 7}})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for c in LLAMA_SSE:
                self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
                self.wfile.flush()
            if body.get("model") == "stall":
                Stub.stall.wait(10)
            self.wfile.write(b"data: [DONE]\n\n")
        else:
            self._json({"error": "not found"}, 404)


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        cls.httpd.daemon_threads = True
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        Stub.stall.set()
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        Stub.requests.clear()
        Stub.stall.clear()

    def _abort_mid_stream(self, client):
        deltas = []
        result = {}

        def run():
            try:
                client.chat([{"role": "user", "content": "hi"}], [], on_delta=lambda k, t: deltas.append(t))
            except Exception as e:
                result["error"] = e
            result["done"] = time.monotonic()

        t = threading.Thread(target=run)
        t.start()
        deadline = time.monotonic() + 5
        while not deltas and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(deltas, "no delta arrived before the stall")
        start = time.monotonic()
        client.abort()
        t.join(5)
        self.assertFalse(t.is_alive(), "abort() did not end the stream")
        self.assertLess(result["done"] - start, 1.5)
        self.assertIn("error", result)


class Ollama(_Base):
    def client(self, model="qwen3.5:9b", token=None):
        return make_client("ollama", self.url, model, token)

    def test_stream(self):
        deltas = []
        r = self.client().chat([{"role": "user", "content": "hi"}], [{"type": "function"}],
                               think=True, num_ctx=4096, on_delta=lambda k, t: deltas.append((k, t)))
        self.assertEqual(r.content, "Hello")
        self.assertEqual(r.thinking, "hmm ")
        self.assertEqual([(c.name, c.arguments) for c in r.tool_calls],
                         [("read_file", {"path": "a.py"}), ("grep", {"q": "x"})])
        self.assertEqual((r.prompt_tokens, r.eval_tokens, r.done_reason), (12, 5, "stop"))
        self.assertEqual(deltas, [("thinking", "hmm "), ("content", "Hel"), ("content", "lo")])
        body = Stub.requests[-1]["body"]
        self.assertEqual(body["options"], {"num_ctx": 4096})
        self.assertIs(body["think"], True)
        self.assertIs(body["stream"], True)
        self.assertEqual(body["tools"], [{"type": "function"}])

    def test_non_stream(self):
        r = self.client().chat([{"role": "user", "content": "hi"}], [])
        self.assertEqual((r.content, r.thinking), ("Hello", "hmm"))
        self.assertEqual(len(r.tool_calls), 2)
        self.assertIs(Stub.requests[-1]["body"]["stream"], False)
        self.assertNotIn("tools", Stub.requests[-1]["body"])

    def test_wire_messages_like_the_sdk(self):
        self.client("llama3").chat([
            {"role": "user", "content": "hi", "_internal": 1},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "x", "type": "function", "function": {"name": "f", "arguments": {"a": 1}}}]},
            {"role": "tool", "content": "<ok>", "tool_name": "f"},
        ], [])
        msgs = Stub.requests[-1]["body"]["messages"]
        self.assertEqual(msgs[0], {"role": "user", "content": "hi"})
        self.assertEqual(msgs[1], {"role": "assistant",
                                   "tool_calls": [{"function": {"name": "f", "arguments": {"a": 1}}}]})
        self.assertEqual(msgs[2]["content"], "<ok>")       # not a qwen model: no escaping

    def test_qwen_escaping_still_applies(self):
        self.client("qwen3.5:9b").chat([{"role": "tool", "content": "a<b"}], [])
        self.assertEqual(Stub.requests[-1]["body"]["messages"][0]["content"], "a&lt;b")

    def test_context_length_and_models(self):
        c = self.client()
        self.assertEqual(c.context_length(), 262144)
        self.assertEqual(c.list_models(), ["llama3:8b", "qwen3.5:9b"])
        self.assertIsNone(self.client("missing").context_length())

    def test_errors_carry_server_message(self):
        with self.assertRaises(LLMHTTPError) as cm:
            self.client("broken").chat([{"role": "user", "content": "x"}], [])
        self.assertIn("model runner crashed", str(cm.exception))
        self.assertEqual(cm.exception.status, 500)
        with self.assertRaises(LLMHTTPError) as cm:
            self.client("midstream-error").chat([{"role": "user", "content": "x"}], [],
                                                on_delta=lambda k, t: None)
        self.assertIn("out of memory", str(cm.exception))

    def test_auth_header(self):
        c = self.client(token="sk-1")
        c.list_models()
        self.assertEqual(Stub.requests[-1]["auth"], "Bearer sk-1")
        c.set_auth_token(None)
        c.list_models()
        self.assertIsNone(Stub.requests[-1]["auth"])

    def test_unreachable_host(self):
        c = make_client("ollama", "http://127.0.0.1:9", "m")
        self.assertEqual(c.list_models(), [])
        self.assertIsNone(c.context_length())
        with self.assertRaises(LLMHTTPError) as cm:
            c.chat([{"role": "user", "content": "x"}], [])
        self.assertIn("cannot connect", str(cm.exception))

    def test_abort_mid_stream(self):
        self._abort_mid_stream(self.client("stall"))

    def test_redirect_followed(self):
        from harness.llm.http import HTTPClient
        self.assertIn("models", HTTPClient(self.url).request_json("GET", "/old"))


class LlamaCpp(_Base):
    def client(self, model="Qwen3.5-9B", token=None):
        return make_client("llamacpp", self.url, model, token)

    def test_stream(self):
        deltas = []
        r = self.client().chat([{"role": "user", "content": "hi"}], [], think=False,
                               on_delta=lambda k, t: deltas.append((k, t)))
        self.assertEqual((r.content, r.thinking), ("Hi", "think "))
        self.assertEqual([(c.name, c.arguments) for c in r.tool_calls], [("read_file", {"path": "b.py"})])
        self.assertEqual((r.prompt_tokens, r.eval_tokens, r.done_reason), (20, 7, "tool_calls"))
        body = Stub.requests[-1]["body"]
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(body["stream_options"], {"include_usage": True})

    def test_non_stream(self):
        r = self.client().chat([{"role": "user", "content": "hi"}], [])
        self.assertEqual((r.content, r.thinking), ("Hi", "think"))
        self.assertEqual(r.tool_calls[0].arguments, {"path": "b.py"})

    def test_context_length_and_models(self):
        c = self.client()
        self.assertEqual(c.context_length(), 8192)
        self.assertEqual(c.list_models(), ["Qwen3.5-9B"])

    def test_openai_style_error(self):
        with self.assertRaises(LLMHTTPError) as cm:
            self.client("broken").chat([{"role": "user", "content": "x"}], [])
        self.assertIn("context overflow", str(cm.exception))

    def test_auth_header(self):
        c = self.client(token="sk-2")
        c.context_length()
        self.assertEqual(Stub.requests[-1]["auth"], "Bearer sk-2")

    def test_abort_mid_stream(self):
        self._abort_mid_stream(self.client("stall"))


class BaseURL(unittest.TestCase):
    def test_normalize(self):
        cases = {
            ("localhost:11434", 11434): "http://localhost:11434",
            ("localhost", 11434): "http://localhost:11434",
            ("192.168.1.10", 8080): "http://192.168.1.10:8080",
            ("http://localhost:8080/", 8080): "http://localhost:8080",
            ("http://example.com", 11434): "http://example.com:80",
            ("https://example.com", 11434): "https://example.com:443",
            ("https://example.com:8443/ollama/", 11434): "https://example.com:8443/ollama",
            ("[::1]:11434", 11434): "http://[::1]:11434",
        }
        for (host, port), want in cases.items():
            with self.subTest(host=host):
                self.assertEqual(normalize_base_url(host, port), want)
        with self.assertRaises(ValueError):
            normalize_base_url("ftp://x", 1)


class NoThirdPartyHTTP(unittest.TestCase):
    def test_harness_imports_without_http_libraries(self):
        code = ("import sys, harness.harness, harness.llm.ollama_client, harness.llm.llamacpp_client, "
                "harness.web.server; "
                "print(sorted(m for m in ('httpx', 'ollama', 'pydantic', 'httpcore') if m in sys.modules))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             cwd=Path(__file__).resolve().parent.parent, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
