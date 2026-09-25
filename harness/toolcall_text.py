"""Reading a model reply as text: tool calls written into the content instead
of the tool API (many local-model formats), <think> blocks, and the write-intent
heuristics the harness's recovery nudges use."""
from __future__ import annotations

import ast
import json
import re


# ── text tool-call recovery ───────────────────────────────────────────────────
# Static regexes for known tagged formats.  The tier-3 bare-JSON pattern is
# built dynamically inside _extract_text_tool_calls from the live tool set.

# Qwen3, Hermes 2/3, NousResearch — most common Ollama chat models
_RX_QWEN      = re.compile(r'<tool_call>\s*(\{.*?\})\s*</tool_call>',         re.DOTALL)
# Functionary / older Hermes variants
_RX_FUNC      = re.compile(r'<functioncall>\s*(\{.*?\})\s*</functioncall>',   re.DOTALL | re.IGNORECASE)
_RX_FUNC2     = re.compile(r'<function_call>\s*(\{.*?\})\s*</function_call>', re.DOTALL | re.IGNORECASE)
# Phi-3 / Phi-4 — no closing tag, JSON follows the token directly
_RX_PHI       = re.compile(r'<\|tool_call\|>\s*(\{.*?\})',                    re.DOTALL)
# DeepSeek-V2/V3/R1 — tool name precedes the args JSON, separated by a special token
_RX_DEEPSEEK  = re.compile(
    r'<｜tool▁call▁begin｜>(.*?)<｜tool▁sep｜>(.*?)<｜tool▁call▁end｜>', re.DOTALL
)
# Mistral / Mixtral — JSON array prefixed by a literal tag
_RX_MISTRAL   = re.compile(r'\[TOOL_CALL\]\s*(\[.*?\])',                       re.DOTALL)
# Command-R / Cohere — text-based action format
_RX_COMMAND_R = re.compile(r'Action:\s*(\S+)\s*\nAction\s+Input:\s*(\{.*?\})', re.DOTALL)

_JSON_DECODER = json.JSONDecoder()


def _match_paren(s: str, i: int) -> int:
    """Given s[i] == '(', return the index of the matching ')', skipping over
    Python string literals (so parens/commas inside quotes are not counted).
    Returns -1 if unbalanced.  Used to carve a `name(...)` call out of free text."""
    depth = 0
    n = len(s)
    quote: str | None = None
    triple = False
    escaped = False
    while i < n:
        ch = s[i]
        if quote is not None:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif triple:
                if s[i:i + 3] == quote * 3:
                    i += 2
                    quote = None
            elif ch == quote:
                quote = None
        elif ch in ("'", '"'):
            if s[i:i + 3] == ch * 3:
                quote, triple = ch, True
                i += 2
            else:
                quote, triple = ch, False
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _strip_text_tool_calls(text: str) -> str:
    """Remove text-based tool-call markup from an assistant message content string.

    When a model embeds its tool call in plain text (instead of via the native
    tool_calls API field), the harness extracts the call but the raw XML/tagged
    markup is still sitting in `content`.  Re-sending that markup to Ollama causes
    Qwen3's XML template engine to embed it verbatim inside its own XML output,
    producing malformed nesting and a 500 "XML syntax error: element <function>
    closed by </parameter>" on the next request.  Stripping before storage fixes this.
    """
    for rx in (_RX_QWEN, _RX_FUNC, _RX_FUNC2, _RX_MISTRAL):
        text = rx.sub("", text)
    text = _RX_PHI.sub("", text)
    text = _RX_DEEPSEEK.sub("", text)
    text = _RX_COMMAND_R.sub("", text)
    return text.strip()


def _extract_text_tool_calls(text: str, tools: list[dict]) -> list[dict]:
    """
    Recover tool calls embedded in plain text when the model bypassed the tool
    API.  Tries all known tagged/structured formats first, then falls back to a
    bare-JSON scan anchored to tool-name occurrences in the text.

    The bare-JSON pattern is built dynamically from `tools`, so adding a tool
    to tools.py automatically extends coverage without touching this function.

    Returns a list of {"name": str, "arguments": dict}.
    """
    known = {t["function"]["name"] for t in tools}
    results: list[dict] = []
    seen: set[str] = set()

    def _append(hit: dict) -> bool:
        """Dedup-check and append.  Returns True if the hit was new."""
        key = hit["name"] + json.dumps(hit["arguments"], sort_keys=True)
        if key in seen:
            return False
        seen.add(key)
        results.append(hit)
        return True

    def _accept_full(obj: object) -> dict | None:
        """
        Validate a {"name": ..., "arguments"|"parameters": ...} object.
        Requires the arguments/parameters key to be explicitly present so that
        an arbitrary JSON blob with a matching "name" field is not mistaken for
        a tool call.  Does not touch `seen` — dedup is the caller's job.
        """
        if not isinstance(obj, dict):
            return None
        name = obj.get("name")
        if name not in known:
            return None
        if "arguments" in obj:
            args = obj["arguments"]
        elif "parameters" in obj:
            args = obj["parameters"]
        else:
            return None  # no explicit args key → not a tool-call structure
        if not isinstance(args, dict):
            return None
        return {"name": name, "arguments": args}

    def _accept_args(name: str, obj: object) -> dict | None:
        """
        Validate a plain args dict paired with a tool name supplied externally
        (e.g. DeepSeek / Command-R formats where the name precedes the JSON).
        Does not touch `seen`.
        """
        if name not in known or not isinstance(obj, dict):
            return None
        return {"name": name, "arguments": obj}

    # ── Tagged / structured formats ───────────────────────────────────────────

    for rx in (_RX_QWEN, _RX_FUNC, _RX_FUNC2, _RX_PHI):
        for m in rx.finditer(text):
            try:
                hit = _accept_full(json.loads(m.group(1)))
                if hit:
                    _append(hit)
            except json.JSONDecodeError:
                pass

    # DeepSeek: name before separator, captured group 2 is the raw args dict
    for m in _RX_DEEPSEEK.finditer(text):
        name = m.group(1).strip()
        try:
            hit = _accept_args(name, json.loads(m.group(2).strip()))
            if hit:
                _append(hit)
        except json.JSONDecodeError:
            pass

    # Mistral wraps multiple calls in a JSON array
    for m in _RX_MISTRAL.finditer(text):
        try:
            for obj in json.loads(m.group(1)):
                hit = _accept_full(obj)
                if hit:
                    _append(hit)
        except (json.JSONDecodeError, TypeError):
            pass

    for m in _RX_COMMAND_R.finditer(text):
        name = m.group(1).strip()
        try:
            hit = _accept_args(name, json.loads(m.group(2).strip()))
            if hit:
                _append(hit)
        except json.JSONDecodeError:
            pass

    # Nameless <tool_call> payloads: some models (notably gemma) emit the argument
    # object directly inside the tag with no {"name":..., "arguments":...} wrapper.
    # These carry no tool name, but a payload with a "content" key is a file write —
    # attribute it to write_file when that tool is available.  raw_decode reads the
    # full object, so braces inside the content value do not truncate parsing.
    if "write_file" in known:
        for m in re.finditer(r'<tool_call>\s*(\{)', text):
            try:
                obj, _ = _JSON_DECODER.raw_decode(text, m.start(1))
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or obj.get("name") in known:
                continue  # a named structure — already handled by the tiers above
            if "content" in obj:
                args = {k: v for k, v in obj.items() if k in ("path", "content")}
                _append({"name": "write_file", "arguments": args})

    # Python-call syntax: some models (notably gemma) emit tool calls as
    # name(key='value', ...) — Python source, not JSON — sometimes wrapped in a
    # print(...) call.  Anchor on each known tool name, carve out the balanced
    # call with _match_paren, and parse it with `ast` so quoting/escaping in the
    # argument values is handled correctly.
    if not results:
        # Declared parameter order per tool, so positional args like
        # edit_file("app.py", "foo", "bar") can be mapped to their names.
        param_order = {
            t["function"]["name"]: list(t["function"]["parameters"].get("properties", {}).keys())
            for t in tools
        }
        _call_pat = re.compile(
            r'\b(' + '|'.join(re.escape(n) for n in sorted(known, key=len, reverse=True)) + r')\s*\('
        )
        for m in _call_pat.finditer(text):
            fn = m.group(1)
            open_idx = m.end() - 1            # position of the '(' the regex consumed
            close_idx = _match_paren(text, open_idx)
            if close_idx < 0:
                continue
            expr = fn + text[open_idx:close_idx + 1]
            try:
                node = ast.parse(expr, mode="eval").body
            except SyntaxError:
                continue
            if (not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name)
                    or node.func.id != fn):
                continue
            call_args: dict = {}
            # Positional args → parameter names in declared order.
            names = param_order.get(fn, [])
            for i, an in enumerate(node.args):
                if i < len(names):
                    try:
                        call_args[names[i]] = ast.literal_eval(an)
                    except Exception:
                        pass
            # Keyword args (override/extend positionals).
            for kw in node.keywords:
                if kw.arg is None:
                    continue
                try:
                    call_args[kw.arg] = ast.literal_eval(kw.value)
                except Exception:
                    pass  # non-literal value (f-string, expression) — skip that arg
            # Require at least one resolved arg so a bare mention like read_file(x)
            # in prose does not become an empty, argument-less call.
            if call_args:
                hit = _accept_args(fn, call_args)
                if hit:
                    _append(hit)

    if results:
        return results

    # ── Tier 3: bare JSON scan anchored to tool-name occurrences ─────────────
    # Build the name pattern dynamically.  Longer names listed first so that
    # "grep_files" cannot be shadowed by the shorter prefix "grep_file".
    name_pat = re.compile(
        r'\b(' + '|'.join(re.escape(n) for n in sorted(known, key=len, reverse=True)) + r')\b'
    )
    for nm in name_pat.finditer(text):
        found = nm.group(1)
        # Start the window up to 300 chars before the match: the name may appear
        # inside the JSON ({"name": "write_file", ...}) so the opening brace can
        # precede the name.  Extend to end-of-text because content args can be large.
        window_start = max(0, nm.start() - 300)
        for i in range(window_start, len(text)):
            if text[i] != '{':
                continue
            try:
                obj, _ = _JSON_DECODER.raw_decode(text, i)
                # Case A: {"name": "write_file", "arguments": {...}}
                hit = _accept_full(obj)
                if hit and hit["name"] == found:
                    if _append(hit):
                        break  # new result added — move on to the next name match
                # Case B: write_file( {...} ) — JSON follows "(" after the tool
                # name, with optional whitespace between "(" and "{".
                pre = text[max(window_start, i - 10):i].rstrip()
                if pre.endswith('('):
                    hit = _accept_args(found, obj)
                    if hit and _append(hit):
                        break
            except json.JSONDecodeError:
                continue

    return results


def _derive_write_path(content: str, mode: str) -> str:
    """Infer a filename for a write_file/append_to_file call that arrived with
    'content' but no 'path'.  Some models (notably gemma) emit the large content
    argument first and drop the trailing 'path', which would otherwise fail the
    required-argument check.  Prefer the document's first Markdown H1 as the name,
    else fall back to a mode-appropriate default."""
    m = re.search(r'^\s{0,3}#\s+(.+?)\s*$', content, re.MULTILINE)
    if m:
        slug = re.sub(r'[^a-z0-9]+', '-', m.group(1).lower()).strip('-')
        if slug:
            return f"{slug[:60]}.md"
    return "design.md" if mode == "design" else "untitled.md"


_WRITE_INTENT = (
    "let me write", "i will write", "i'll write", "i'm going to write",
    "writing the design", "writing the spec", "writing it now",
    "write the complete", "write the design", "write the specification",
    "write the spec", "write it now", "now write", "will now write",
    "let me create", "i'll draft", "i'll compose", "i'm going to create",
    "going to write", "going to draft", "composing the", "drafting the",
    "creating the design", "creating the spec", "i'm writing", "i'm creating",
)

def _has_write_intent(text: str) -> bool:
    t = text.lower()
    return any(p in t for p in _WRITE_INTENT)


def _extract_and_strip_thinking(raw_content: str) -> tuple[str, str]:
    """Return (content_without_think_tags, thinking_text).

    Handles complete <think>…</think> blocks and incomplete <think>… blocks
    (generation cut off mid-thinking).  Either or both may be absent.
    """
    thinking = ""
    complete = re.search(r"<think>(.*?)</think>", raw_content, flags=re.DOTALL)
    if complete:
        thinking = complete.group(1).strip()
    content = re.sub(r"<think>.*?</think>", "", raw_content, flags=re.DOTALL).strip()
    incomplete = re.search(r"<think>(.*?)$", content, flags=re.DOTALL)
    if incomplete:
        if not thinking:
            thinking = incomplete.group(1).strip()
        content = content[:incomplete.start()].strip()
    return content, thinking
