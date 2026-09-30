from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

# Streaming callback: on_delta(kind, text) with kind 'content' or 'thinking'.
DeltaCallback = Callable[[str, str], None]

# Reasoning-effort words, least to most.  Which ones a model takes differs:
# gpt-oss takes low/medium/high, Qwen3.8 low/medium/xhigh (and rejects others).
EFFORT_LEVELS = ("minimal", "low", "medium", "high", "xhigh", "max")
# The levels assumed when a server says efforts work but not which ones.
DEFAULT_EFFORTS = ("low", "medium", "high")
# What /think accepts: plain on/off plus the effort words.
THINK_LEVELS = ("off", "on") + EFFORT_LEVELS
# How chat() is asked to think: True/False, an effort word, or None to send
# nothing (the model can't think).
Think = bool | str | None


def nearest_effort(level: str, levels: tuple[str, ...]) -> str:
    """The level in `levels` closest to `level` in EFFORT_LEVELS order; a tie
    goes to the higher one (high → xhigh on a low/medium/xhigh model)."""
    rank = EFFORT_LEVELS.index
    return min(levels, key=lambda lv: (abs(rank(lv) - rank(level)), -rank(lv)))


@dataclass(frozen=True)
class ThinkingCaps:
    """What the served model lets a request say about reasoning.

    `toggle`: thinking can be switched on/off per request.  `levels`: effort
    words it takes, least to most (empty: on/off only).  `can_disable`: it
    stops thinking when told to — gpt-oss always reasons, so off becomes its
    lowest level.  `known`: the server said; when it didn't, on/off is sent as
    before this was read."""
    toggle: bool = True
    levels: tuple[str, ...] = ()
    can_disable: bool = True
    known: bool = False

    def choices(self) -> list[str]:
        """The /think values that change what is sent to this model."""
        if not (self.toggle or self.levels):
            return []
        return (["off"] if self.can_disable else []) + ["on"] + list(self.levels)

    def resolve(self, level: str) -> Think:
        """Map a /think value onto what this model can be sent.  "on" means the
        model's own default effort, so it sends no level."""
        if not (self.toggle or self.levels):
            return None
        if level == "off":
            return False if self.can_disable or not self.levels else self.levels[0]
        if level == "on" or not self.levels:
            return True
        return level if level in self.levels else nearest_effort(level, self.levels)


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

    # chat_template_kwargs.preserve_thinking for templates that take it (Qwen3.8,
    # Gemma 4): whether past turns' reasoning is rendered.  None: not sent.  The
    # harness sets it per request from /think-history.
    preserve_thinking: bool | None = None

    def thinking_caps(self) -> ThinkingCaps:
        """What the served model accepts for `think`, read from the server once
        per model (an unanswered probe too — the status bar reads this, so it
        must not go to the network each time; forget_thinking_caps re-reads)."""
        cache = self.__dict__.setdefault("_think_caps", {})
        caps = cache.get(self.model)
        if caps is None:
            caps = cache[self.model] = self._probe_thinking()
        return caps

    def forget_thinking_caps(self):
        """Drop what was read, e.g. after the server may have been relaunched."""
        self.__dict__.get("_think_caps", {}).clear()

    def refresh_thinking_caps(self) -> ThinkingCaps:
        """Read them again now: the server may have been relaunched with another
        model, or was still loading the first time."""
        self.forget_thinking_caps()
        return self.thinking_caps()

    def _probe_thinking(self) -> ThinkingCaps:
        """Ask the server what the model accepts; unknown when it can't say."""
        return ThinkingCaps()

    def wire_think(self, think: Think) -> Think:
        """What to send for `think` given the model's caps: a word on an on/off
        model becomes True, False on a model that can't stop becomes its lowest
        level, and nothing is sent to a model that can't think.  With unknown
        caps, send on/off as before this was read."""
        if think is None:
            return None
        caps = self.thinking_caps()
        if not caps.known:
            return think if isinstance(think, bool) else True
        return caps.resolve("off" if think is False else "on" if think is True else think)

    @abstractmethod
    def chat(self, messages: list[dict], tools: list[dict],
             think: Think = None, num_ctx: int | None = None,
             on_delta: DeltaCallback | None = None) -> ChatResponse:
        """Run one completion. With `on_delta`, stream: call it with each piece of
        content/thinking as it arrives. Either way, return the complete response.
        `think`: see `Think`; False on a model that can't stop thinking is sent
        as its lowest effort level."""
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
