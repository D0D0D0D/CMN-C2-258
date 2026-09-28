"""Marketplace Azure OpenAI adapter with per-call secret resolution."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

from framework.secrets.context import current_secrets
from shared.services.llm.base_llm import BaseLLM


class AzureLLM(BaseLLM):  # type: ignore[misc]
    """Create an Azure client from the currently bound secrets for each call."""

    def __init__(self, *, tools: tuple[Any, ...] = ()) -> None:
        if tools:
            self._tools = tools

    def _client(self) -> BaseLLM:
        # The Azure client ships only with the platform-storage-azure extra, so
        # import it lazily — a build without that extra must still load this
        # module and fail only if an Azure call is actually made.
        from shared.services.llm.azure_openai_client import AzureOpenAIClient

        secrets = current_secrets()
        client = AzureOpenAIClient(
            {
                "api_key": secrets.require("AZURE_OPENAI_API_KEY"),
                "azure_endpoint": secrets.require("AZURE_OPENAI_ENDPOINT"),
                "azure_deployment": secrets.require("AZURE_OPENAI_DEPLOYMENT"),
            }
        )
        tools = getattr(self, "_tools", ())
        return client.bind_tools(list(tools)) if tools else client

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> "AzureLLM":
        return type(self)(tools=tuple(tools))
