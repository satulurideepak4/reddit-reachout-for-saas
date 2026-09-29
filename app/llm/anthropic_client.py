import httpx

from app.config import Settings
from app.llm.base import LLMError, LLMOutputError, decision_json_schema

TOOL_NAME = "record_decision"


class AnthropicLLMClient:
    """Uses a forced tool call so the model must return schema-shaped JSON."""

    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.model = settings.llm_model
        self._key = settings.llm_api_key
        self._base = settings.llm_base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=90)

    def decide(self, system: str, user: str) -> dict:
        if not self._key:
            raise LLMError("LLM_API_KEY is not configured")
        payload = {
            "model": self.model,
            "max_tokens": 1500,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [{"name": TOOL_NAME, "description": "Record the engagement decision.", "input_schema": decision_json_schema()}],
            "tool_choice": {"type": "tool", "name": TOOL_NAME},
        }
        try:
            resp = self.http.post(
                f"{self._base}/v1/messages",
                json=payload,
                headers={"x-api-key": self._key, "anthropic-version": "2023-06-01"},
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM request failed: {type(exc).__name__}") from exc
        if resp.status_code != 200:
            raise LLMError(f"LLM returned HTTP {resp.status_code}")
        for block in resp.json().get("content", []):
            if block.get("type") == "tool_use" and block.get("name") == TOOL_NAME:
                return block["input"]
        raise LLMOutputError("LLM response contained no structured decision")
