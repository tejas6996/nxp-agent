"""
OpenAI infrastructure client.

Wraps the openai SDK and exposes a minimal interface used by services.
All OpenAI-specific SDK logic is isolated here.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from app.exceptions import OpenAIClientError
from app.settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class TokenUsage:
    """Accumulated OpenAI usage for one run, with its cost in USD."""

    calls: int = 0
    input_tokens: int = 0  # includes cached input tokens
    cached_input_tokens: int = 0
    output_tokens: int = 0  # includes reasoning tokens (billed as output)
    reasoning_tokens: int = 0
    price_input_per_m: float = 0.0
    price_cached_input_per_m: float = 0.0
    price_output_per_m: float = 0.0

    @property
    def cost_usd(self) -> float:
        uncached = self.input_tokens - self.cached_input_tokens
        return (
            uncached * self.price_input_per_m
            + self.cached_input_tokens * self.price_cached_input_per_m
            + self.output_tokens * self.price_output_per_m
        ) / 1_000_000


class OpenAIClient:
    """Thin async wrapper around the OpenAI SDK for structured (JSON schema) completions.

    Token usage of every call is accumulated in `self.usage` so each run can report its
    exact API cost.
    """

    def __init__(
        self,
        settings: Settings,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        # The SDK retries 429/5xx/connection errors with exponential backoff.
        self._client = AsyncOpenAI(api_key=settings.openai_api_key, max_retries=5, timeout=300)
        self.model = model or settings.openai_model
        effort = settings.openai_reasoning_effort if reasoning_effort is None else reasoning_effort
        self._reasoning_effort = effort.strip().lower()
        self.usage = TokenUsage(
            price_input_per_m=settings.openai_price_input_per_m,
            price_cached_input_per_m=settings.openai_price_cached_input_per_m,
            price_output_per_m=settings.openai_price_output_per_m,
        )

    def _record_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        self.usage.calls += 1
        if usage is None:
            return
        self.usage.input_tokens += usage.prompt_tokens or 0
        self.usage.output_tokens += usage.completion_tokens or 0
        prompt_details = getattr(usage, "prompt_tokens_details", None)
        if prompt_details is not None:
            self.usage.cached_input_tokens += getattr(prompt_details, "cached_tokens", 0) or 0
        completion_details = getattr(usage, "completion_tokens_details", None)
        if completion_details is not None:
            self.usage.reasoning_tokens += getattr(completion_details, "reasoning_tokens", 0) or 0

    async def complete_json(
        self,
        system_prompt: str,
        user_content: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        """Request a completion that is guaranteed to match the given JSON schema.

        Args:
            system_prompt: Instruction prompt controlling output structure.
            user_content: User-facing content to process.
            schema_name: Identifier for the schema.
            schema: Strict JSON schema the response must follow.

        Returns:
            Parsed JSON dict from the model response.

        Raises:
            OpenAIClientError: If the API call fails or returns invalid JSON.
        """
        kwargs: dict[str, Any] = {}
        if self._reasoning_effort:
            kwargs["reasoning_effort"] = self._reasoning_effort
        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "strict": True, "schema": schema},
                },
                **kwargs,
            )
        except Exception as exc:
            logger.error("OpenAI API call failed: %s", exc)
            raise OpenAIClientError(f"OpenAI API call failed: {exc}") from exc

        self._record_usage(response)
        choice = response.choices[0]
        if getattr(choice.message, "refusal", None):
            raise OpenAIClientError(f"OpenAI refused the request: {choice.message.refusal}")
        if choice.finish_reason == "length":
            raise OpenAIClientError("OpenAI response was truncated (max tokens reached)")
        try:
            return json.loads(choice.message.content or "{}")
        except json.JSONDecodeError as exc:
            logger.error("OpenAI returned invalid JSON: %s", exc)
            raise OpenAIClientError(f"OpenAI returned invalid JSON: {exc}") from exc
