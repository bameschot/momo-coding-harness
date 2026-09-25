from __future__ import annotations

import json
import os

from .base import ChatResponse, LLMClient, ToolCall
from .http import HTTPClient, LLMHTTPError, normalize_base_url

# Message fields Ollama's /api/chat understands (the ollama SDK kept exactly these).
_MESSAGE_FIELDS = ("role", "content", "thinking", "images", "tool_name", "tool_calls")


def _is_qwen(model: str) -> bool:
    return "qwen" in model.lower()


def _xml_escape_str(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _xml_escape_args(obj: object) -> object:
    """Recursively escape XML special chars in string values within a tool-call argument structure."""
    if isinstance(obj, dict):
        return {k: _xml_escape_args(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_xml_escape_args(v) for v in obj]
    if isinstance(obj, str):
        return _xml_escape_str(obj)
    return obj


def _xml_escape_for_ollama(messages: list[dict]) -> list[dict]:
    """Return a shallow copy of messages with XML-special characters escaped for Qwen3's template.

    Qwen3's Ollama template wraps tool results in <tool_response>…</tool_response> XML and
    embeds past tool-call arguments in XML-like structure.  Unescaped < > & in either location
    break the XML parser and produce a 500.  We escape only the copy sent to the API —
    the caller's stored messages keep the correct, unescaped content so history is accurate."""
    result = []
    for m in messages:
        role = m.get("role")
        if role == "tool" and isinstance(m.get("content"), str):
            m = dict(m)
            m["content"] = _xml_escape_str(m["content"])
        elif role == "assistant" and m.get("tool_calls"):
            m = dict(m)
            m["tool_calls"] = [
                {"function": {
                    "name": tc["function"]["name"],
                    "arguments": _xml_escape_args(tc["function"]["arguments"]),
                }}
                for tc in m["tool_calls"]
            ]
        result.append(m)
    return result


class OllamaClient(LLMClient):
    """Adapter for Ollama's native REST API: /api/chat, /api/show, /api/tags."""

    provider_name = "ollama"

    def __init__(self, host: str, model: str, auth_token: str | None = None):
        super().__init__(host=host, model=model, auth_token=auth_token)
        self._client = self._make_client()

    def _headers(self) -> dict:
        # OLLAMA_API_KEY is the ollama SDK's fallback for a key that was not set.
        token = self._auth_token or os.environ.get("OLLAMA_API_KEY")
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _make_client(self) -> HTTPClient:
        return HTTPClient(normalize_base_url(self.host, 11434), self._headers())

    def set_host(self, host: str):
        self.host = host
        self._client.close()
        self._client = self._make_client()

    def set_auth_token(self, token: str | None):
        self._auth_token = token
        self._client.headers = self._headers()

    def abort(self):
        """Close the underlying HTTP client to interrupt any in-flight request."""
        self._client.close()
        self._client = self._make_client()

    @staticmethod
    def _wire_messages(messages: list[dict]) -> list[dict]:
        """Keep only the fields /api/chat knows, and drop empty ones — what the
        ollama SDK did before sending."""
        out = []
        for m in messages:
            w = {k: m[k] for k in _MESSAGE_FIELDS if m.get(k)}
            if "tool_calls" in w:
                w["tool_calls"] = [{"function": {"name": (tc.get("function") or {}).get("name", ""),
                                                 "arguments": (tc.get("function") or {}).get("arguments", {})}}
                                   for tc in w["tool_calls"]]
            out.append(w)
        return out

    def chat(self, messages: list[dict], tools: list[dict],
             think: bool | None = None, num_ctx: int | None = None,
             on_delta=None) -> ChatResponse:
        # Qwen3's Ollama chat template embeds message content into XML; escape the
        # copy we send so tool results/args with < > & don't break its parser.
        if _is_qwen(self.model):
            messages = _xml_escape_for_ollama(messages)
        body: dict = {"model": self.model, "messages": self._wire_messages(messages),
                      "stream": on_delta is not None}
        if tools:
            body["tools"] = tools
        if num_ctx is not None:
            body["options"] = {"num_ctx": num_ctx}
        if think is not None:
            body["think"] = think
        client = self._client   # abort() swaps in a fresh one; keep reading this one
        if on_delta is None:
            return self._normalize(client.request_json("POST", "/api/chat", body, timeout=None))
        return self._consume_stream(self._json_lines(client.stream_lines("POST", "/api/chat", body)),
                                    on_delta)

    @staticmethod
    def _json_lines(lines):
        """One JSON object per line; an {"error": ...} line ends the stream."""
        for line in lines:
            if not line.strip():
                continue
            try:
                chunk = json.loads(line)
            except ValueError:
                continue
            if isinstance(chunk, dict) and chunk.get("error"):
                raise LLMHTTPError(str(chunk["error"]))
            yield chunk

    @classmethod
    def _consume_stream(cls, chunks, on_delta) -> ChatResponse:
        """Accumulate streamed chunks; the final chunk (done=True) carries the stats."""
        content: list[str] = []
        thinking: list[str] = []
        raw_calls: list = []
        last: dict = {}
        for chunk in chunks:
            msg = chunk.get("message") or {}
            if (t := msg.get("thinking")):
                thinking.append(t)
                on_delta("thinking", t)
            if (c := msg.get("content")):
                content.append(c)
                on_delta("content", c)
            raw_calls.extend(msg.get("tool_calls") or [])
            last = chunk
        return ChatResponse(
            content="".join(content),
            thinking="".join(thinking),
            tool_calls=cls._convert_calls(raw_calls),
            prompt_tokens=last.get("prompt_eval_count"),
            eval_tokens=last.get("eval_count"),
            done_reason=last.get("done_reason"),
        )

    @staticmethod
    def _convert_calls(raw_calls) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for tc in raw_calls:
            fn = (tc or {}).get("function") or {}
            args = fn.get("arguments") or {}
            # Older Ollama versions return arguments as a raw JSON string.
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            calls.append(ToolCall(name=fn.get("name", ""), arguments=args if isinstance(args, dict) else {}))
        return calls

    @classmethod
    def _normalize(cls, response: dict) -> ChatResponse:
        msg = response.get("message") or {}
        return ChatResponse(
            content=msg.get("content") or "",
            thinking=msg.get("thinking") or "",
            tool_calls=cls._convert_calls(msg.get("tool_calls") or []),
            prompt_tokens=response.get("prompt_eval_count"),
            eval_tokens=response.get("eval_count"),
            done_reason=response.get("done_reason"),
        )

    def context_length(self) -> int | None:
        """Return the model's native context window size, or None if unavailable."""
        try:
            info = self._client.request_json("POST", "/api/show", {"model": self.model})
            for key, value in (info.get("model_info") or {}).items():
                if "context_length" in key and isinstance(value, int):
                    return value
        except Exception:
            pass
        return None

    def loaded_models(self) -> list[str]:
        """The models Ollama holds in memory (/api/ps)."""
        try:
            models = self._client.request_json("GET", "/api/ps").get("models") or []
            return [m.get("model") or m.get("name") for m in models
                    if isinstance(m, dict) and (m.get("model") or m.get("name"))]
        except Exception:
            return []

    def list_models(self) -> list[str]:
        """Return the available model names, or an empty list if the host is
        unreachable.  The caller (/model) reports the failure — an error string
        must never leak into the list and be shown as a selectable model."""
        try:
            models = self._client.request_json("GET", "/api/tags").get("models") or []
            return sorted(m.get("model") or m.get("name") for m in models
                          if isinstance(m, dict) and (m.get("model") or m.get("name")))
        except Exception:
            return []
