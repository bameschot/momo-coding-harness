from __future__ import annotations

import json

import httpx

from .base import ChatResponse, LLMClient, ToolCall


class LlamaCppClient(LLMClient):
    """Adapter for a running llama.cpp server via its OpenAI-compatible API.

    Talks to `{host}/v1/chat/completions` for chat, `{host}/props` for the
    server's context size, and `{host}/v1/models` for the loaded model list.

    Note: the llama.cpp server must be started with `--jinja` for tool calling
    to work, and with `--reasoning-format ...` for `reasoning_content` to be
    populated (thinking is otherwise recovered from <think> tags downstream)."""

    provider_name = "llama.cpp"
    # A llama.cpp server serves the single model it was launched with; the
    # request's `model` field is ignored, so runtime switching is unsupported.
    can_switch_model = False

    def __init__(self, host: str, model: str, auth_token: str | None = None):
        super().__init__(host=host, model=model, auth_token=auth_token)
        self._client = self._make_client()

    def _make_client(self) -> httpx.Client:
        # No global read timeout: generation can take a long time; abort() closes
        # the client to interrupt an in-flight request instead.
        return httpx.Client(base_url=self.host.rstrip("/"),
                            timeout=httpx.Timeout(30.0, read=None))

    def _headers(self) -> dict:
        if self._auth_token:
            return {"Authorization": f"Bearer {self._auth_token}"}
        return {}

    def set_host(self, host: str):
        self.host = host
        try:
            self._client.close()
        except Exception:
            pass
        self._client = self._make_client()

    def set_auth_token(self, token: str | None):
        self._auth_token = token

    def abort(self):
        """Close the underlying HTTP client to interrupt any in-flight request."""
        try:
            self._client.close()
        except Exception:
            pass
        self._client = self._make_client()

    @staticmethod
    def _to_openai_messages(messages: list[dict]) -> list[dict]:
        """Translate the harness's canonical (Ollama-minimal) history into the
        strict OpenAI shape llama.cpp's endpoint requires:

        - assistant tool calls need an `id` and `type: "function"`, and their
          `arguments` must be a JSON *string* (not a dict);
        - each following `tool` result needs a `tool_call_id` linking it back to
          the call it answers.

        The harness emits an assistant turn's tool calls followed immediately by
        their results in order, so a FIFO of generated ids pairs them up."""
        out: list[dict] = []
        pending_ids: list[str] = []   # call ids awaiting their tool results
        counter = 0
        for m in messages:
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                new_calls = []
                for tc in m["tool_calls"]:
                    fn = tc.get("function", {})
                    args = fn.get("arguments", {})
                    if not isinstance(args, str):
                        args = json.dumps(args)
                    cid = f"call_{counter}"
                    counter += 1
                    pending_ids.append(cid)
                    new_calls.append({
                        "id": cid,
                        "type": "function",
                        "function": {"name": fn.get("name", ""), "arguments": args},
                    })
                out.append({"role": "assistant",
                            "content": m.get("content"),
                            "tool_calls": new_calls})
            elif role == "tool":
                if pending_ids:
                    cid = pending_ids.pop(0)
                else:
                    cid = f"call_{counter}"
                    counter += 1
                out.append({"role": "tool",
                            "tool_call_id": cid,
                            "content": m.get("content", "")})
            else:
                out.append(m)
        return out

    def chat(self, messages: list[dict], tools: list[dict],
             think: bool | None = None, num_ctx: int | None = None,
             on_delta=None) -> ChatResponse:
        payload: dict = {
            "model": self.model,
            "messages": self._to_openai_messages(messages),
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        # llama.cpp fixes the context window at server launch, so num_ctx is not
        # applicable and is intentionally ignored here.
        if think is False:
            # Qwen3-style templates honor this to suppress reasoning output.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        elif think is True:
            payload["chat_template_kwargs"] = {"enable_thinking": True}

        if on_delta is None:
            response = self._client.post("/v1/chat/completions",
                                         json=payload, headers=self._headers())
            response.raise_for_status()
            return self._normalize(response.json())

        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        with self._client.stream("POST", "/v1/chat/completions",
                                 json=payload, headers=self._headers()) as response:
            if response.status_code >= 400:
                response.read()
                response.raise_for_status()
            return self._consume_stream(response.iter_lines(), on_delta)

    @staticmethod
    def _consume_stream(lines, on_delta) -> ChatResponse:
        """Parse an OpenAI-style SSE stream: content / reasoning deltas, tool calls
        whose `arguments` arrive in fragments (merged by index), and final usage."""
        content: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict] = {}
        finish = None
        usage: dict = {}
        for line in lines:
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except ValueError:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if (t := delta.get("reasoning_content")):
                    reasoning.append(t)
                    on_delta("thinking", t)
                if (t := delta.get("content")):
                    content.append(t)
                    on_delta("content", t)
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", len(calls)), {"name": "", "args": ""})
                    fn = tc.get("function") or {}
                    if fn.get("name") and not slot["name"]:
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["args"] += fn["arguments"]
                if choice.get("finish_reason"):
                    finish = choice["finish_reason"]
        tool_calls = []
        for _, slot in sorted(calls.items()):
            try:
                args = json.loads(slot["args"]) if slot["args"].strip() else {}
            except ValueError:
                args = {}
            tool_calls.append(ToolCall(name=slot["name"], arguments=args if isinstance(args, dict) else {}))
        return ChatResponse(
            content="".join(content),
            thinking="".join(reasoning),
            tool_calls=tool_calls,
            prompt_tokens=usage.get("prompt_tokens"),
            eval_tokens=usage.get("completion_tokens"),
            done_reason=finish,
        )

    @staticmethod
    def _normalize(data: dict) -> ChatResponse:
        choices = data.get("choices") or [{}]
        choice = choices[0] or {}
        msg = choice.get("message") or {}

        calls: list[ToolCall] = []
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments")
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args)
                except Exception:
                    args = {}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}
            calls.append(ToolCall(name=fn.get("name", ""), arguments=args))

        usage = data.get("usage") or {}
        return ChatResponse(
            content=msg.get("content") or "",
            thinking=msg.get("reasoning_content") or "",
            tool_calls=calls,
            prompt_tokens=usage.get("prompt_tokens"),
            eval_tokens=usage.get("completion_tokens"),
            done_reason=choice.get("finish_reason"),
        )

    def context_length(self) -> int | None:
        """Read the server's context window from /props."""
        try:
            r = self._client.get("/props", headers=self._headers())
            r.raise_for_status()
            props = r.json()
            gen = props.get("default_generation_settings") or {}
            for source in (gen, props):
                n = source.get("n_ctx")
                if isinstance(n, int) and n > 0:
                    return n
        except Exception:
            pass
        return None

    def list_models(self) -> list[str]:
        """Return the loaded model id(s), or an empty list if unreachable."""
        try:
            r = self._client.get("/v1/models", headers=self._headers())
            r.raise_for_status()
            data = r.json().get("data") or []
            return sorted(m["id"] for m in data if isinstance(m, dict) and m.get("id"))
        except Exception:
            return []
