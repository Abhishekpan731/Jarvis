"""OpenAI provider. The only module that imports the OpenAI SDK."""

import asyncio
from collections.abc import AsyncIterator

from openai import APIError, AsyncOpenAI

from app.ai.base import ProviderError
from app.ai.schemas import Completion, Usage


class OpenAIModel:
    name = "openai"

    def __init__(self, api_key: str, model: str, base_url: str) -> None:
        self.model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=30, max_retries=2)
        self._usage = Usage()

    async def complete(self, messages: list[dict], *, schema: dict | None = None) -> Completion:
        kwargs: dict = {"model": self.model, "messages": messages}
        if schema is not None:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except APIError as exc:
            raise ProviderError(
                f"openai returned {getattr(exc, 'status_code', 'unknown')}"
            ) from None
        if not response.choices:
            raise ProviderError("openai returned no choices")
        choice = response.choices[0].message.content or ""
        usage = Usage(
            input_tokens=response.usage.prompt_tokens if response.usage else 0,
            output_tokens=response.usage.completion_tokens if response.usage else 0,
        )
        self._usage = usage
        return Completion(text=choice, model=self.model, provider=self.name, usage=usage)

    async def stream(self, messages: list[dict]) -> AsyncIterator[str]:
        try:
            stream = await self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=True,  # type: ignore[arg-type]
            )
            async for chunk in stream:  # type: ignore[union-attr]
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if delta:
                    yield delta
        except APIError as exc:
            raise ProviderError(
                f"openai returned {getattr(exc, 'status_code', 'unknown')}"
            ) from None

    def usage(self) -> Usage:
        return self._usage


async def with_retry(operation, attempts: int = 3):
    """Retry a provider call on transient failure. The last error propagates."""
    delay = 0.5
    for attempt in range(attempts):
        try:
            return await operation()
        except ProviderError:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(delay)
            delay *= 2
