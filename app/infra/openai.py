"""
OpenAI infrastructure client.

Wraps the openai SDK and exposes a minimal interface used by services.
All OpenAI-specific SDK logic is isolated here.
"""

import json
import logging

from openai import OpenAI

from app.exceptions import OpenAIClientError
from app.settings import Settings

logger = logging.getLogger(__name__)


class OpenAIClient:
    """Thin wrapper around the OpenAI SDK for chat completions."""

    def __init__(self, settings: Settings) -> None:
        self._client = OpenAI(api_key=settings.openai_api_key)
        self._model = settings.openai_model

    def complete_json(self, system_prompt: str, user_content: str) -> dict:
        """Request a JSON-structured completion from the model.

        The API is called with response_format=json_object, guaranteeing
        valid JSON output that is parsed before returning.

        Args:
            system_prompt: Instruction prompt controlling output structure.
            user_content: User-facing content to process.

        Returns:
            Parsed JSON dict from the model response.

        Raises:
            OpenAIClientError: If the API call fails or returns invalid JSON.
        """
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
            raw = response.choices[0].message.content or "{}"
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error("OpenAI returned invalid JSON: %s", exc)
            raise OpenAIClientError(f"OpenAI returned invalid JSON: {exc}") from exc
        except Exception as exc:
            logger.error("OpenAI API call failed: %s", exc)
            raise OpenAIClientError(f"OpenAI API call failed: {exc}") from exc
