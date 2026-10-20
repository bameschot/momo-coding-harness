"""A stdlib MCP server for the tests: stdio (default) or streamable HTTP (--http PORT).

Tools: echo (read-only), add, write_note (destructive), slow (sleeps; for
cancel/timeout tests), fail (isError), ansi (control characters in the result),
crash (the process exits), change_tools (adds a tool and sends list_changed).
tools/list is paged two tools at a time.  Resources: notes://a, notes://b.
--http replies to requests as JSON, or as SSE with --sse.
"""
import json
import os
import sys
import threading
import time

TOOLS = [
    {"name": "echo", "description": "Echo the text back.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "add", "description": "Add two numbers.",
     "inputSchema": {"type": "object", "$defs": {"num": {"type": "number", "description": "A number"}},
                     "properties": {"a": {"$ref": "#/$defs/num"}, "b": {"$ref": "#/$defs/num"}},
                     "required": ["a", "b"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "write_note", "description": "Write a note.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
     "annotations": {"destructiveHint": True}},
    {"name": "slow", "description": "Sleep for a while.",
     "inputSchema": {"type": "object", "properties": {"secs": {"type": "number"}}}},
    {"name": "fail", "description": "Always fails.", "inputSchema": {"type": "object"}},
    {"name": "ansi", "description": "Returns control characters.", "inputSchema": {"type": "object"}},
    {"name": "crash", "description": "Exits the server.", "inputSchema": {"type": "object"}},
    {"name": "change_tools", "description": "Adds a tool.", "inputSchema": {"type": "object"}},
]
EXTRA = {"name": "late", "description": "Added later.", "inputSchema": {"type": "object"},
         "annotations": {"readOnlyHint": True}}
RESOURCES = {"notes://a": "Note A text", "notes://b": "Note B text"}

lock = threading.Lock()
cancelled: set = set()


def handle(msg, send):
    """Returns the response dict (or None for notifications)."""
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method is None:
        return None
    if rid is None:
        if method == "notifications/cancelled":
            cancelled.add(params.get("requestId"))
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    if method == "initialize":
        return ok({"protocolVersion": os.environ.get("STUB_VERSION", params.get("protocolVersion")),
                   "capabilities": {"tools": {"listChanged": True}, "resources": {}},
                   "serverInfo": {"name": "stub", "version": "0.1"},
                   "instructions": os.environ.get("STUB_INSTRUCTIONS", "Use echo to test.")})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        start = int(params.get("cursor") or 0)
        page = TOOLS[start:start + 2]
        res = {"tools": page}
        if start + 2 < len(TOOLS):
            res["nextCursor"] = str(start + 2)
        return ok(res)
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "echo":
            return ok({"content": [{"type": "text", "text": f"echo: {args.get('text')}"}]})
        if name == "add":
            return ok({"content": [{"type": "text", "text": str(args["a"] + args["b"])}]})
        if name == "write_note":
            return ok({"content": [{"type": "text", "text": "saved"}]})
        if name == "slow":
            end = time.time() + float(args.get("secs", 5))
            while time.time() < end and rid not in cancelled:
                time.sleep(0.02)
            return ok({"content": [{"type": "text", "text": "done"}]})
        if name == "fail":
            return ok({"isError": True, "content": [{"type": "text", "text": "it broke"}]})
        if name == "ansi":
            return ok({"content": [{"type": "text", "text": "red\x1b[31mtext\x07\u009b2J end"}]})
        if name == "crash":
            sys.stderr.write("crashing now\n")
            sys.stderr.flush()
            os._exit(3)
        if name == "change_tools":
            if EXTRA not in TOOLS:
                TOOLS.append(EXTRA)
            send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
            return ok({"content": [{"type": "text", "text": "changed"}]})
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
    if method == "resources/list":
        return ok({"resources": [{"uri": u, "name": u.split("//")[1], "mimeType": "text/plain"}
                                 for u in RESOURCES]})
    if method == "resources/read":
        uri = params.get("uri")
        if uri not in RESOURCES:
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32002, "message": "not found"}}
        return ok({"contents": [{"uri": uri, "mimeType": "text/plain", "text": RESOURCES[uri]}]})
    if method == "roots/list":
        return None
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "no such method"}}


def run_stdio():
    out_lock = threading.Lock()

    def send(m):
        with out_lock:
            sys.stdout.write(json.dumps(m) + "\n")
            sys.stdout.flush()

    sys.stderr.write("stub started\n")
    sys.stderr.flush()
    for line in sys.stdin:
        if not line.strip():
            continue
        msg = json.loads(line)
        # Calls run on threads so a slow one doesn't block cancellation.
        threading.Thread(target=lambda m=msg: (r := handle(m, send)) and send(r), daemon=True).start()


def run_http(port: int, sse: bool):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if msg.get("method") != "initialize" and self.headers.get("Mcp-Session-Id") != "sess-1":
                self.send_response(400)
                self.end_headers()
                return
            notes = []
            resp = handle(msg, notes.append)
            if resp is None:
                self.send_response(202)
                self.end_headers()
                return
            self.send_response(200)
            if msg.get("method") == "initialize":
                self.send_header("Mcp-Session-Id", "sess-1")
            if sse:
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for m in notes + [resp]:
                    self.wfile.write(f"event: message\ndata: {json.dumps(m)}\n\n".encode())
            else:
                body = json.dumps(resp).encode()
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def do_DELETE(self):
            self.send_response(200)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    print(srv.server_address[1], flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    if "--http" in sys.argv:
        run_http(int(sys.argv[sys.argv.index("--http") + 1]), "--sse" in sys.argv)
    else:
        run_stdio()
