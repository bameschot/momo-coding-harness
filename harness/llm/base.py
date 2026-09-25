from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

# Streaming callback: on_delta(kind, text) with kind 'content' or 'thinking'.
DeltaCallback = Callable[[str, str], None]


@dataclass
class ToolCall:
    """A single tool call, normalized across providers.  `arguments` is always a
    parsed dict (never a raw JSON string)."""
    name: str
    arguments: dict


@dataclass
class ChatResponse:
    """Provider-agnostic result of a chat() call.  Each adapter maps its own
    API response onto this shape so the harness never sees provider specifics."""
    content: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int | None = None
    eval_tokens: int | None = None
    done_reason: str | None = None   # "stop" | "length" | ... (provider-native)


class LLMClient(ABC):
    """Common interface the harness uses to talk to an LLM backend.

    Concrete adapters (Ollama, llama.cpp) translate the harness's canonical
    message list and this interface to/from their own wire format.
    """

    def __init__(self, host: str, model: str, auth_token: str | None = None):
        self.host = host
        self.model = model
        self._auth_token = auth_token

    @property
    def auth_token(self) -> str | None:
        """The bearer token this client sends (None: none, or the env default)."""
        return self._auth_token

    # Human-readable backend name, used in user-facing messages/errors.
    provider_name: str = "llm"

    # Whether the backend can switch the served model at runtime.  Ollama loads
    # models on demand by name; a single llama.cpp server serves one model for
    # its whole lifetime, so switching is not possible there.
    can_switch_model: bool = True

    @abstractmethod
    def chat(self, messages: list[dict], tools: list[dict],
             think: bool | None = None, num_ctx: int | None = None,
             on_delta: DeltaCallback | None = None) -> ChatResponse:
        """Run one completion. With `on_delta`, stream: call it with each piece of
        content/thinking as it arrives. Either way, return the complete response."""
        ...

    @abstractmethod
    def context_length(self) -> int | None:
        """Return the model's native context window size, or None if unavailable."""
        ...

    @abstractmethod
    def list_models(self) -> list[str]:
        """Return available model names, or an empty list if the host is
        unreachable.  Must never leak an error string into the list."""
        ...

    def loaded_models(self) -> list[str]:
        """Models the server holds in memory right now, most recent first; []
        when it can't say.  Used at startup when the saved model is gone."""
        return []

    @abstractmethod
    def abort(self):
        """Interrupt any in-flight request."""
        ...

    def set_model(self, model: str):
        self.model = model

    def set_host(self, host: str):
        self.host = host

    def set_auth_token(self, token: str | None):
        self._auth_token = token
