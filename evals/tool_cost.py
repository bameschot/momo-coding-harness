"""What the tools cost in the first request, per role: the tool schemas the API
sends, the tool reference and the rules in the system prompt, and the rest.

With a llama.cpp server the counts are real: the request is rendered through
the server's own chat template (/apply-template) and tokenized (/tokenize), so
they include how the template prints the schemas.  Without one (or --offline)
they are the harness's estimate, 4 characters per token.

    python evals/tool_cost.py --host http://127.0.0.1:9931
    python evals/tool_cost.py --offline --roles coding --top 10
    python evals/tool_cost.py --tool-ref minimal --json after.json
"""
import argparse
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
# The harness writes prefs and a session under ~/.momo-harness: point HOME
# somewhere disposable before importing it (as run_evals.py does).
os.environ["HOME"] = tempfile.mkdtemp(prefix="momo_toolcost_home_")

import harness.harness as hmod  # noqa: E402
from harness.compaction import text_tokens  # noqa: E402
from harness.llm.base import ChatResponse, LLMClient  # noqa: E402
from harness.tools import TOOL_REF_STYLES  # noqa: E402

# The system prompt sections spent on tools (Harness.system_prompt_sections keys).
_TOOL_SECTIONS = ("tools", "discover", "nav", "net", "index")


class _Offline(LLMClient):
    """No model is asked anything: only the prompt is built."""
    provider_name = "fake"

    def chat(self, *a, **k):
        return ChatResponse(content="", done_reason="stop")

    def context_length(self):
        return 32768

    def list_models(self):
        return ["fake"]

    def abort(self):
        pass


class _Counter:
    """Tokens of a text, and of a whole rendered request: the server's when it
    answers, else the estimate."""

    def __init__(self, host: str | None):
        self.host = host.rstrip("/") if host else None
        self.real = False
        if self.host:
            try:
                self._post("/tokenize", {"content": "probe"})
                self.real = True
            except Exception as e:      # noqa: BLE001 — any failure means: estimate
                print(f"({self.host} not usable: {e}; using the estimate)", file=sys.stderr)

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(self.host + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    def text(self, s: str) -> int:
        if not self.real:
            return text_tokens(s)
        return len(self._post("/tokenize", {"content": s})["tokens"])

    def schemas(self, tools: list[dict]) -> int:
        """What the tool list adds to a rendered request."""
        if not tools:
            return 0
        if not self.real:
            return text_tokens(json.dumps(tools, separators=(",", ":")))
        msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}]
        with_tools = self._post("/apply-template", {"messages": msgs, "tools": tools})["prompt"]
        without = self._post("/apply-template", {"messages": msgs})["prompt"]
        return self.text(with_tools) - self.text(without)

    def one_schema(self, tool: dict) -> int:
        # The templates print each tool as json.dumps does (", " and ": ").
        return self.text(json.dumps(tool)) if self.real else text_tokens(json.dumps(tool))


def measure(role: str, index: bool, net: bool, counter: _Counter, tool_ref: str) -> dict:
    h = hmod.Harness(host="http://offline", model="offline", workdir=REPO)
    try:
        h.mode = role
        h.tool_ref = tool_ref
        if net:
            h.net_access = "on"
        if index:
            h.index = object()          # the prompt only asks whether there is one
        sections = h.system_prompt_sections()
        tools = h._current_tools()
        parts = {s["key"]: counter.text(s["text"]) for s in sections}
        prompt = counter.text("".join(s["sep"] + s["text"] for s in sections))
        schemas = counter.schemas(tools)
        per_tool = sorted(((counter.one_schema(t), t["function"]["name"]) for t in tools),
                          reverse=True)
    finally:
        h.index = None
        h.logger.close()
    tool_prompt = sum(n for k, n in parts.items() if k in _TOOL_SECTIONS)
    return {"role": role, "index": index, "net": net, "tool_ref": tool_ref, "tools": len(tools),
            "schemas": schemas, "prompt": prompt, "prompt_tool_sections": tool_prompt,
            "sections": parts, "first_request": prompt + schemas,
            "tool_share": round((schemas + tool_prompt) / max(prompt + schemas, 1), 3),
            "per_tool": [{"name": n, "tokens": t} for t, n in per_tool]}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="http://127.0.0.1:8080",
                    help="llama.cpp server for real counts (default %(default)s)")
    ap.add_argument("--offline", action="store_true", help="estimate only; ask no server")
    ap.add_argument("--roles", default="design,chat,plan,coding",
                    help="comma list (default %(default)s)")
    ap.add_argument("--tool-ref", choices=TOOL_REF_STYLES, default="minimal")
    ap.add_argument("--top", type=int, default=0, help="list the N most costly tools per row")
    ap.add_argument("--json", metavar="PATH", help="write every row here")
    args = ap.parse_args()

    counter = _Counter(None if args.offline else args.host)
    hmod.make_client = lambda *a, **k: _Offline("http://offline", "offline")
    hmod.Harness._start_index = lambda self: None
    rows = [measure(role, index, net, counter, args.tool_ref)
            for role in args.roles.split(",")
            for index, net in ((False, False), (True, False), (True, True))]

    kind = "real tokens (server template)" if counter.real else "estimated tokens (4 chars each)"
    print(f"Tool cost of the first request — {kind}, tool reference: {args.tool_ref}\n")
    hdr = (f"{'role':8s} {'index':5s} {'net':4s} {'tools':>5s} {'schemas':>8s} {'reference':>9s} "
           f"{'rules':>6s} {'prompt':>7s} {'first req':>9s} {'tools %':>7s}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        s = r["sections"]
        ref = s.get("tools", 0) + s.get("discover", 0)
        rules = r["prompt_tool_sections"] - ref
        print(f"{r['role']:8s} {'on' if r['index'] else 'off':5s} {'on' if r['net'] else 'off':4s} "
              f"{r['tools']:5d} {r['schemas']:8,d} {ref:9,d} {rules:6,d} {r['prompt']:7,d} "
              f"{r['first_request']:9,d} {r['tool_share'] * 100:6.0f}%")
        for t in r["per_tool"][:args.top]:
            print(f"      {t['tokens']:5d}  {t['name']}")
    if args.json:
        Path(args.json).write_text(json.dumps({"real": counter.real, "rows": rows}, indent=1))
        print(f"\nwritten to {args.json}")


if __name__ == "__main__":
    main()
