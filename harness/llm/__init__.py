from __future__ import annotations

from .base import ChatResponse, LLMClient, ToolCall


def make_client(provider: str, host: str, model: str,
                auth_token: str | None = None) -> LLMClient:
    """Build the LLM client for the named provider.

    Imports are deferred so a backend's optional dependency (e.g. the `ollama`
    library) is only required when that provider is actually selected."""
    provider = (provider or "ollama").lower()
    if provider == "ollama":
        from .ollama_client import OllamaClient
        return OllamaClient(host=host, model=model, auth_token=auth_token)
    if provider in ("llamacpp", "llama.cpp", "llama_cpp"):
        from .llamacpp_client import LlamaCppClient
        return LlamaCppClient(host=host, model=model, auth_token=auth_token)
    raise ValueError(f"unknown provider: {provider!r} (expected 'ollama' or 'llamacpp')")


__all__ = ["ChatResponse", "LLMClient", "ToolCall", "make_client"]
