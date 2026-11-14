"""The language-model boundary.

Business logic asks a provider to complete or stream. It never names a vendor
or a model. A missing credential is a configuration state, not an error to
paper over with a canned reply.
"""

from collections.abc import AsyncIterator
from typing import Protocol

from app.ai.schemas import Completion, Usage


class NotConfigured(Exception):
    """Raised when no provider credential is set. Never caught and replaced."""


class ProviderError(Exception):
    """A provider failed after its retries. Carries no credential material."""


class LanguageModel(Protocol):
    name: str
    model: str

    async def complete(self, messages: list[dict], *, schema: dict | None = None) -> Completion: ...

    def stream(self, messages: list[dict]) -> AsyncIterator[str]: ...

    def usage(self) -> Usage: ...
