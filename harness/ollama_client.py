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
             options: dict | None = None, think: bool | None = None) -> dict:
        kwargs: dict = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        if options:
            kwargs["options"] = options
        if think is not None:
            kwargs["think"] = think
        response = self._client.chat(**kwargs)
        return response

    def list_models(self) -> list[str]:
        try:
            result = self._client.list()
            # result.models is a list of Model objects with a .model attribute
            return sorted(m.model for m in result.models)
        except Exception as e:
            return [f"ERROR: {e}"]
