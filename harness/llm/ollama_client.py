from __future__ import annotations

import json

import ollama

from .base import ChatResponse, LLMClient, ToolCall


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
    provider_name = "ollama"

    def __init__(self, host: str, model: str, auth_token: str | None = None):
        super().__init__(host=host, model=model, auth_token=auth_token)
        self._client = self._make_client()

    def _make_client(self) -> ollama.Client:
        headers = {"Authorization": f"Bearer {self._auth_token}"} if self._auth_token else None
        return ollama.Client(host=self.host, headers=headers)

    def set_host(self, host: str):
        self.host = host
        self._client = self._make_client()

    def set_auth_token(self, token: str | None):
        self._auth_token = token
        self._client = self._make_client()

    def abort(self):
        """Close the underlying HTTP client to interrupt any in-flight request."""
        try:
            self._client.close()
        except Exception:
            pass
        self._client = self._make_client()

    def chat(self, messages: list[dict], tools: list[dict],
             think: bool | None = None, num_ctx: int | None = None) -> ChatResponse:
        # Qwen3's Ollama chat template embeds message content into XML; escape the
        # copy we send so tool results/args with < > & don't break its parser.
        if _is_qwen(self.model):
            messages = _xml_escape_for_ollama(messages)
        kwargs: dict = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        if num_ctx is not None:
            kwargs["options"] = {"num_ctx": num_ctx}
        if think is not None:
            kwargs["think"] = think
        response = self._client.chat(**kwargs)
        return self._normalize(response)

    @staticmethod
    def _normalize(response) -> ChatResponse:
        msg = response.message
        calls: list[ToolCall] = []
        for tc in (getattr(msg, "tool_calls", None) or []):
            args = tc.function.arguments or {}
            # Older Ollama versions return arguments as a raw JSON string.
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            calls.append(ToolCall(name=tc.function.name, arguments=args))
        return ChatResponse(
            content=getattr(msg, "content", "") or "",
            thinking=getattr(msg, "thinking", "") or "",
            tool_calls=calls,
            prompt_tokens=getattr(response, "prompt_eval_count", None),
            eval_tokens=getattr(response, "eval_count", None),
            done_reason=getattr(response, "done_reason", None),
        )

    def context_length(self) -> int | None:
        """Return the model's native context window size, or None if unavailable."""
        try:
            info = self._client.show(self.model)
            # SDK exposes the field as 'modelinfo' (no underscore)
            model_info: dict = getattr(info, "modelinfo", None) or {}
            for key, value in model_info.items():
                if "context_length" in key and isinstance(value, int):
                    return value
        except Exception:
            pass
        return None

    def list_models(self) -> list[str]:
        """Return the available model names, or an empty list if the host is
        unreachable.  The caller (/model) reports the failure — an error string
        must never leak into the list and be shown as a selectable model."""
        try:
            result = self._client.list()
            # result.models is a list of Model objects with a .model attribute
            return sorted(m.model for m in result.models)
        except Exception:
            return []
