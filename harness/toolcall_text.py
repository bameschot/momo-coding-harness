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
# Phi-3 / Phi-4 — no closing tag, JSON follows the token directly.  Without a
# closing tag a regex cannot tell where the object ends, so these two formats
# only find their start and _json_after reads the whole object.
_RX_PHI       = re.compile(r'<\|tool_call\|>\s*(?=\{)')
# DeepSeek-V2/V3/R1 — tool name precedes the args JSON, separated by a special token
_RX_DEEPSEEK  = re.compile(
    r'<｜tool▁call▁begin｜>(.*?)<｜tool▁sep｜>(.*?)<｜tool▁call▁end｜>', re.DOTALL
)
# Mistral / Mixtral — JSON array prefixed by a literal tag
_RX_MISTRAL   = re.compile(r'\[TOOL_CALL\]\s*(\[.*?\])',                       re.DOTALL)
# Command-R / Cohere — text-based action format
_RX_COMMAND_R = re.compile(r'Action:\s*(\S+)\s*\nAction\s+Input:\s*(?=\{)')

_JSON_DECODER = json.JSONDecoder()


def _json_after(rx: re.Pattern, text: str):
    """(match, object, end) for each match of rx that a JSON object follows
    directly; raw_decode reads the whole object, braces in strings included."""
    for m in rx.finditer(text):
        try:
            obj, end = _JSON_DECODER.raw_decode(text, m.end())
        except json.JSONDecodeError:
            continue
        yield m, obj, end


# ── <tool_call> payloads written as text ─────────────────────────────────────

# gemma also wrote <call>{"name": …, "arguments": …}</call> (run 3's planner,
# twice, create_plan among them).
_RX_TOOL_CALL_OPEN = re.compile(r'<(?:tool_call|call)>\s*(?=\{)')
# Where a payload ends: its closing tag, gemma's own closing token, or the next call.
_RX_TOOL_CALL_END = re.compile(r'</tool_call>|<tool_call\|>|</call>|<(?:tool_call|call)>')
# A backslash followed by a real line break inside a JSON string: an invalid
# escape that meant "\n".
_RX_BACKSLASH_NL = re.compile(r'(?<!\\)\\\r?\n')
_LENIENT_DECODER = json.JSONDecoder(strict=False)


def _tool_call_payloads(text: str):
    """(start, end, payload, closed) for each <tool_call>{…} in text: the span
    to cut (closing tag included), the JSON text between the tags, and whether
    a closing tag or the next call ends it (not the end of the reply)."""
    for m in _RX_TOOL_CALL_OPEN.finditer(text):
        close = _RX_TOOL_CALL_END.search(text, m.end())
        stop = close.start() if close else len(text)
        end = close.end() if close and close.group() not in ("<tool_call>", "<call>") else stop
        yield m.start(), end, text[m.end():stop], close is not None


def _lenient_json(payload: str, closed: bool = True) -> object | None:
    """The JSON object in payload, read with the slips models make: raw control
    characters in strings, a backslash before a line break, and (when a closing
    tag shows the model ended it) brackets left open.  A payload the reply ends
    in may be cut off by the output limit: closing it would run half a call.
    None when it still cannot be read."""
    payload = payload.strip()
    fixed = _RX_BACKSLASH_NL.sub(r"\\n", payload)
    for candidate in (payload, fixed) + ((_close_open(fixed),) if closed else ()):
        try:
            obj, end = _LENIENT_DECODER.raw_decode(candidate)
        except json.JSONDecodeError:
            continue
        if not candidate[end:].strip():
            return obj
    return None


def _close_open(s: str) -> str:
    """s with the strings and brackets it leaves open closed, in order."""
    stack, in_str, escaped = [], False, False
    for ch in s:
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()
    return s + ('"' if in_str else "") + "".join(reversed(stack))


def _nameless_call(obj: dict, tools: list[dict]) -> dict | None:
    """The call a bare arguments object stands for: a "content" payload is a
    file write (write_file, when offered); otherwise the one offered tool that
    has every key as a parameter and gets all its required ones.  None when no
    tool or more than one fits."""
    known = {t["function"]["name"] for t in tools}
    if "content" in obj and "write_file" in known:
        return {"name": "write_file",
                "arguments": {k: v for k, v in obj.items() if k in ("path", "content")}}
    if not obj:
        return None
    fits = []
    for t in tools:
        params = t["function"].get("parameters") or {}
        props = params.get("properties") or {}
        if set(obj) <= set(props) and set(params.get("required") or []) <= set(obj):
            fits.append(t["function"]["name"])
    return {"name": fits[0], "arguments": dict(obj)} if len(fits) == 1 else None


def _cut_spans(text: str, spans: list[tuple[int, int]]) -> str:
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + text[end:]
    return text


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
    text = _RX_DEEPSEEK.sub("", text)
    for rx in (_RX_PHI, _RX_COMMAND_R):
        text = _cut_spans(text, [(m.start(), end) for m, _obj, end in _json_after(rx, text)])
    return text.strip()


# What may follow a call written as text when it ends the reply: closing
# parens (print(...)), semicolons, backticks and code fences, and leaked turn
# markers.  A call with prose after it is an example in an answer, not a call.
_RX_FENCE = re.compile(r"```[\w-]*")
_RX_PRINT_OPEN = re.compile(r"\bprint\(\s*$")


def _only_closing(tail: str) -> bool:
    tail = _RX_TURN_MARKERS.sub("", _RX_FENCE.sub("", tail))
    return not tail.strip(" \t\r\n);`")


def _trailing(cands: list[tuple[dict, tuple[int, int]]], text: str) -> list[tuple[dict, tuple[int, int]]]:
    """The candidates that end the reply: walking back from its end, each one
    followed only by closing syntax or a later accepted call.  A model that
    writes a call as text stops after it; an answer that mentions one
    (`write_file('out.txt', 'hi')` in a summary, read_file(...) in a code
    example) goes on, and recovering that ran the call and dropped the answer."""
    kept, cursor = [], len(text)
    for hit, (start, end) in sorted(cands, key=lambda c: c[1][1], reverse=True):
        if end > cursor:
            continue                    # inside a call already kept (a string argument)
        if not _only_closing(text[end:cursor]):
            break
        kept.append((hit, (start, end)))
        cursor = start
    return kept[::-1]


def _extract_text_tool_calls(text: str, tools: list[dict]) -> list[dict]:
    """Tool calls embedded in plain text when the model bypassed the tool API
    (recover_text_calls without the remaining text).
    Returns a list of {"name": str, "arguments": dict}."""
    return recover_text_calls(text, tools)[0]


def recover_text_calls(text: str, tools: list[dict]) -> tuple[list[dict], str]:
    """
    Recover tool calls embedded in plain text when the model bypassed the tool
    API, and the text that is left around them (what the model said alongside,
    kept as the call's preamble).  Tries all known tagged/structured formats
    first, then calls written as Python source, then a bare-JSON scan anchored
    to tool-name occurrences.  The last two count only when they end the reply.

    The bare-JSON pattern is built dynamically from `tools`, so adding a tool
    (a file in harness/tools/builtin/, or a custom one) automatically extends
    coverage without touching this function.

    Returns ([{"name": str, "arguments": dict}], remaining text).
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

    for _m, obj, _end in _json_after(_RX_PHI, text):
        if (hit := _accept_full(obj)):
            _append(hit)

    for rx in (_RX_FUNC, _RX_FUNC2):
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

    for m, obj, _end in _json_after(_RX_COMMAND_R, text):
        if (hit := _accept_args(m.group(1).strip(), obj)):
            _append(hit)

    # <tool_call> payloads (Qwen3, Hermes, and gemma writing them as text), in
    # the order written.  gemma's JSON often has slips a strict parser refuses —
    # raw newlines in strings, a dropped closing brace (story-mud run 2 lost a
    # story to four such write_file calls) — so _lenient_json reads them.  Some
    # payloads are the arguments alone, with no {"name":..., "arguments":...}
    # wrapper: _nameless_call attributes them to the one tool they fit.
    spans: list[tuple[int, int]] = []
    for start, end, payload, closed in _tool_call_payloads(text):
        obj = _lenient_json(payload, closed)
        hit = _accept_full(obj)
        if hit is None and isinstance(obj, dict) and obj.get("name") not in known:
            hit = _nameless_call(obj, tools)
        if hit is not None:
            _append(hit)
            spans.append((start, end))
    if spans:
        text = _cut_spans(text, spans)

    if results:
        return results, _strip_text_tool_calls(text)

    # Python-call syntax: some models (notably gemma) emit tool calls as
    # name(key='value', ...) — Python source, not JSON — sometimes wrapped in a
    # print(...) call.  Anchor on each known tool name, carve out the balanced
    # call with _match_paren, and parse it with `ast` so quoting/escaping in the
    # argument values is handled correctly.
    cands: list[tuple[dict, tuple[int, int]]] = []
    # Declared parameter order per tool, so positional args like
    # edit_file("app.py", "foo", "bar") can be mapped to their names.
    param_order = {
        t["function"]["name"]: list(t["function"]["parameters"].get("properties", {}).keys())
        for t in tools
    }
    _call_pat = re.compile(
        r'\b(' + '|'.join(re.escape(n) for n in sorted(known, key=len, reverse=True)) + r')\s*\('
    ) if known else None
    for m in (_call_pat.finditer(text) if _call_pat else ()):
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
        # write_file({"path": ..., "content": ...}): the one argument is the
        # arguments object, written as a dict literal.
        if len(node.args) == 1 and not node.keywords and isinstance(node.args[0], ast.Dict):
            try:
                node.args, call_args = [], dict(ast.literal_eval(node.args[0]))
            except Exception:
                pass
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
        if call_args and (hit := _accept_args(fn, call_args)):
            start, end = m.start(), close_idx + 1
            # print(write_file(...)): the wrapper is part of the call's markup.
            if (wrap := _RX_PRINT_OPEN.search(text, 0, start)) and \
                    (close := re.compile(r"\s*\)").match(text, end)):
                start, end = wrap.start(), close.end()
            cands.append((hit, (start, end)))

    if not cands and known:
        # ── Tier 3: bare JSON scan anchored to tool-name occurrences ─────────
        # Build the name pattern dynamically.  Longer names listed first so that
        # "grep_files" cannot be shadowed by the shorter prefix "grep_file".
        # (Quadratic in the worst case: every "{" after each name mention is
        # tried; it only runs when nothing above matched.)
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
                    obj, end = _JSON_DECODER.raw_decode(text, i)
                except json.JSONDecodeError:
                    continue
                # Case A: {"name": "write_file", "arguments": {...}}
                hit = _accept_full(obj)
                if hit and hit["name"] == found:
                    cands.append((hit, (i, end)))
                    break  # move on to the next name match
                # Case B: write_file( {...} ) — JSON follows "(" after the tool
                # name, with optional whitespace between "(" and "{".
                pre = text[max(window_start, i - 10):i].rstrip()
                if pre.endswith('(') and (hit := _accept_args(found, obj)):
                    cands.append((hit, (nm.start(), end)))
                    break

    kept = _trailing(cands, text)
    for hit, _span in kept:
        _append(hit)
    rest = _cut_spans(text, [span for _hit, span in kept])
    # A fence the call sat in, now empty (```tool_code\n```), goes too.
    rest = re.sub(r"```[\w-]*\s*```", "", rest)
    return results, rest.strip() if kept else text


def text_call_error(text: str, tools: list[dict]) -> str:
    """Why a call to a known tool written as text (Python-call syntax, the form
    _extract_text_tool_calls reads with `ast`) could not be read: the parse error
    and where, for the nudge that asks the model to call the tool itself.  "" when
    the text names no tool as a call, or the call parses."""
    known = {t["function"]["name"] for t in tools}
    if not known:
        return ""
    pat = re.compile(r'\b(' + '|'.join(re.escape(n) for n in sorted(known, key=len, reverse=True))
                     + r')\s*\(')
    for m in pat.finditer(text):
        fn, open_idx = m.group(1), m.end() - 1
        close = _match_paren(text, open_idx)
        if close >= 0:
            end = close + 1
        else:                       # unbalanced (a stray quote): up to the end of its code block
            fence = re.compile(r"^[ \t]*```[ \t]*$", re.M).search(text, open_idx)
            end = fence.start() if fence else len(text)
        try:
            ast.parse(fn + text[open_idx:end], mode="eval")
        except SyntaxError as e:
            where = f" at line {e.lineno} of the call" if e.lineno else ""
            line = (e.text or "").strip()
            shown = f": {line[:100]}" if line else ""
            return f"your {fn}(…) written as text could not be read ({e.msg}{where}{shown})"
        return ""
    # A call written as JSON, {"name": "<tool>", "arguments": {...}}, that tier 3
    # of _extract_text_tool_calls could not read (gemma once put a stray `1,`
    # inside a story object of a write_stories call).
    pat = re.compile(r'\{\s*"name"\s*:\s*"(' + '|'.join(re.escape(n) for n in known) + r')"')
    for m in pat.finditer(text):
        try:
            _JSON_DECODER.raw_decode(text, m.start())
        except json.JSONDecodeError as e:
            line_no = text.count("\n", m.start(), e.pos) + 1
            line_start = text.rfind("\n", 0, e.pos) + 1
            line_end = text.find("\n", e.pos)
            line = text[line_start:line_end if line_end >= 0 else len(text)].strip()
            shown = f": {line[:100]}" if line else ""
            return (f"your {m.group(1)} call written as JSON could not be read "
                    f"({e.msg} at line {line_no} of the call{shown})")
        return ""
    return ""


# Where a call written in one of the tagged formats starts.
_RX_CALL_START = re.compile(
    r'<tool_call>|<\|tool_call\|>|<function_?call>|\[TOOL_CALL\]|<｜tool▁call▁begin｜>', re.IGNORECASE)


def partial_text_call(text: str, tools: list[dict]) -> tuple[str, int] | None:
    """(tool, start) for a call a cut-off reply was writing as text when it
    stopped: the tool ("a tool" when the call names none yet) and where the call
    starts, so the unfinished markup can be cut off before the reply is stored.
    None when the text starts no call.  For text _extract_text_tool_calls found
    no call in: an opening tag, a {"name": "<tool>" object that does not parse,
    or an unbalanced <tool>(."""
    known = {t["function"]["name"] for t in tools}
    if not text or not known:
        return None
    names = '|'.join(re.escape(n) for n in sorted(known, key=len, reverse=True))
    if (start := _RX_CALL_START.search(text)):
        found = re.compile(r'\b(' + names + r')\b').search(text, start.end())
        return (found.group(1) if found else "a tool"), start.start()
    for m in re.finditer(r'\{\s*"name"\s*:\s*"(' + names + r')"', text):
        try:
            _JSON_DECODER.raw_decode(text, m.start())
        except json.JSONDecodeError:
            return m.group(1), m.start()
    for m in re.finditer(r'\b(' + names + r')\s*\(', text):
        if _match_paren(text, m.end() - 1) < 0:
            return m.group(1), m.start()
    return None


# Chat-template turn markers a model sometimes writes as text, e.g. gemma ending
# its reply with "<|im_end|>\n<|im_start|>assistant" and carrying on as if in a
# new turn.  Markup only: they must not be shown or sent back as content.
_RX_TURN_MARKERS = re.compile(
    r'<\|im_start\|>(?:system|user|assistant|tool)?\n?'
    r'|<\|im_end\|>'
    r'|<start_of_turn>(?:system|user|model)?\n?'
    r'|<end_of_turn>'
    r'|<\|start_header_id\|>\w*<\|end_header_id\|>\n?'
    r'|<\|eot_id\|>'
    r'|<\|endoftext\|>'
)


# gemma's tool-call and channel tokens.  When its call goes wrong the server's
# parser can end a string argument only at the next quote, so the argument
# carries the rest of the turn, tokens and all (story-mud run 2 listed the
# directory '.}<tool_call|><tool_call|>ep<|channel>thought…').
_RX_TEMPLATE_TOKENS = re.compile(r"<\|tool_call>|<tool_call\|>|<\|channel>|<channel\|>"
                                 r"|<\|tool_response>|<tool_response\|>")
# Arguments that are file text or prose: these may quote the tokens on purpose
# (a file about chat templates), so only the others are checked.
_TEXT_ARGS = frozenset({"content", "old_string", "new_string", "edits", "text", "summary",
                        "description", "body", "message", "note"})


def template_leak(name: str, args: dict) -> str:
    """An error for a call whose path, command or other short argument ran into
    chat-template tokens, so it is not run on garbage; "" when it is clean."""
    for key, value in args.items():
        if key in _TEXT_ARGS or not isinstance(value, str):
            continue
        if _RX_TEMPLATE_TOKENS.search(value):
            return (f"ERROR: {name}'s '{key}' argument ran into chat-template tokens "
                    f"({value[:60]!r}…), so the call was not run — send the call again "
                    "with only the argument's value.")
    return ""

def _strip_turn_markers(text: str) -> str:
    """Remove leaked chat-template turn markers from a reply's text."""
    return _RX_TURN_MARKERS.sub("", text).strip() if text else text


def _derive_write_path(content: str, mode: str) -> str | None:
    """Infer a filename for a write_file/append_to_file call that arrived with
    'content' but no 'path'.  Some models (notably gemma) emit the large content
    argument first and drop the trailing 'path', which would otherwise fail the
    required-argument check.  A Markdown document is named after its first H1,
    else design.md in design mode.  None otherwise: code written to
    "untitled.md" was a junk file, and the required-argument error tells the
    model what is missing."""
    m = re.search(r'^\s{0,3}#\s+(.+?)\s*$', content, re.MULTILINE)
    if m and not re.match(r'\s*#!', content):       # a shebang is no heading
        slug = re.sub(r'[^a-z0-9]+', '-', m.group(1).lower()).strip('-')
        if slug and (mode == "design" or not _looks_like_code(content)):
            return f"{slug[:60]}.md"
    return "design.md" if mode == "design" else None


def _looks_like_code(content: str) -> bool:
    """A Python or shell comment can look like an H1 ("# Helpers"): code lines
    elsewhere in the text say it is not a Markdown document."""
    return bool(re.search(r'^\s*(?:def |class |import |from \S+ import |function |const |'
                          r'let |var |#include|package |fn |pub |return\b)', content, re.M))


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


# A final reply that only says what comes next ("My next step is to implement the
# changes in shop/pricing.py.", "I will begin with the first task", or a plan list
# ending "4. Verify all changes by running the tests."): 10 of 12 failed coding
# runs of a 26B ended like this (evals/multipart_bench.py), since a reply without a
# tool call ends the turn.  A finished summary is in the past tense ("Updated …").
_RX_NEXT_INTENT = re.compile(
    r"\b(?:I will|I'll|I am going to|I'm going to|let me(?! know)|let's|my next steps?(?: is| are)?"
    r"|next,? I|now I|my plan is)\b", re.IGNORECASE)
_NEXT_VERBS = ("update|add|modify|implement|fix|create|run|verify|write|change|refactor|remove|"
               "restore|test|edit|make|rename|replace|delete|move|check")
# A list item, then optionally a bold or code label ("**`shop/util.py`**:"), then the verb.
_RX_PLAN_STEP = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+(?:\*\*)?`?(?:[^\s:*`]+`?(?:\*\*)?:\s*(?:\*\*)?)?"
                           r"(?:" + _NEXT_VERBS + r")\b", re.IGNORECASE)
_NEXT_TAIL = 400       # characters at the end of the reply that count
# What makes an intent sentence an announcement: an action verb within a few
# words of it ("I will now proceed to modify", "My next step is to locate and
# run").  "I'll leave the docs as they are" is no step.
_RX_ACTION = re.compile(r"\b(?:" + _NEXT_VERBS + r"|read|look|inspect|open|search|find|locate|"
                        r"start|begin|continue|proceed|investigate|examine|review)\w*\b",
                        re.IGNORECASE)
_ACTION_WORDS = 6      # words after the intent that may hold the verb
# A sentence that offers or defers instead: "Let me know …, and I'll add them",
# "If you'd like, I'll also add a test", "Next time, let's add a check".  A
# finished summary that ends this way is done; the note made the model go on.
_RX_OFFER = re.compile(r"\b(?:if you|let me know|would you like|want me to|should you|"
                       r"happy to|feel free|next time|in the future|later on|in a follow-up)\b",
                       re.IGNORECASE)
_RX_SENTENCE = re.compile(r"(?<=[.!])\s+|\n+")


def announces_next_step(text: str) -> bool:
    """Whether a reply without a tool call ends by announcing work it has not
    done yet (and is not a question to the user, or an offer)."""
    t = (text or "").strip()
    if not t or t.endswith("?"):
        return False
    for sentence in _RX_SENTENCE.split(t[-_NEXT_TAIL:]):
        m = _RX_NEXT_INTENT.search(sentence)
        if not m or _RX_OFFER.search(sentence):
            continue
        if _RX_ACTION.search(" ".join(sentence[m.end():].split()[:_ACTION_WORDS])):
            return True
    last = [ln for ln in t.splitlines() if ln.strip()][-1]
    return bool(_RX_PLAN_STEP.match(last))


def _extract_and_strip_thinking(raw_content: str) -> tuple[str, str]:
    """Return (content_without_think_tags, thinking_text).

    Handles complete <think>…</think> blocks (all of them), an incomplete
    <think>… block (generation cut off mid-thinking), and a closing </think>
    with no opening tag: templates that open the block in the prompt (Qwen3
    thinking models, llama.cpp with --reasoning-format none) make the reply
    start inside it, and the reasoning was shown as the answer, tag and all.
    Any part may be absent.
    """
    if not raw_content:
        return "", ""
    parts: list[str] = []
    opened, closed = raw_content.find("<think>"), raw_content.find("</think>")
    if closed >= 0 and (opened < 0 or closed < opened):
        parts.append(raw_content[:closed].strip())
        raw_content = raw_content[closed + len("</think>"):]
    parts += [m.strip() for m in re.findall(r"<think>(.*?)</think>", raw_content, flags=re.DOTALL)]
    content = re.sub(r"<think>.*?</think>", "", raw_content, flags=re.DOTALL).strip()
    incomplete = re.search(r"<think>(.*?)$", content, flags=re.DOTALL)
    if incomplete:
        parts.append(incomplete.group(1).strip())
        content = content[:incomplete.start()].strip()
    return content, "\n\n".join(p for p in parts if p)
