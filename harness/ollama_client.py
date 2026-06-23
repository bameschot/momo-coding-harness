import ollama


class OllamaClient:
    def __init__(self, host: str, model: str):
        self.host = host
        self.model = model
        self._client = ollama.Client(host=host)

    def set_host(self, host: str):
        self.host = host
        self._client = ollama.Client(host=host)

    def set_model(self, model: str):
        self.model = model

    def chat(self, messages: list[dict], tools: list[dict],
             think: bool | None = None, num_ctx: int | None = None) -> dict:
        kwargs: dict = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        if num_ctx is not None:
            kwargs["options"] = {"num_ctx": num_ctx}
        if think is not None:
            kwargs["think"] = think
        response = self._client.chat(**kwargs)
        return response

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
        try:
            result = self._client.list()
            # result.models is a list of Model objects with a .model attribute
            return sorted(m.model for m in result.models)
        except Exception as e:
            return [f"ERROR: {e}"]
