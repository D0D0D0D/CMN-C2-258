"""AGENTIC STAR Marketplace entry adapter for CMN-C2-258."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from framework.secrets.base import SecretProvider

try:
    from framework.utils.config_loader import load_agent_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


from src.graph.graph import CompetitiveAnalysisWinRateEstimationGraph
from src.services.azure_llm import AzureLLM

_NAMESPACE = "cmn"
_AGENT_NAME = "CMN-C2-258"
_PROVIDER_AGENT_NAME = "cmn_c2_258"

_REQUIRED_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_ALLOWED_SECRET_KEYS = frozenset(_REQUIRED_SECRET_KEYS)


class _AllowlistedEnvironmentProvider(SecretProvider):
    """Read only this agent's declared secrets from the process environment."""

    _namespace = _NAMESPACE
    _agent_name = _PROVIDER_AGENT_NAME

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _ALLOWED_SECRET_KEYS:
            return default
        value = os.environ.get(key)
        return value if value is not None and value.strip() else default


def _marketplace_input_context() -> dict[str, Any]:
    """Build the complete context accepted at the Marketplace boundary."""

    # Optional URL hints are the only input_context field consumed by this
    # agent. Do not accept runner-owned context additions.
    return {"competitor_urls": {}}


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = provider.require("AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


class MarketplaceGraph(CompetitiveAnalysisWinRateEstimationGraph):
    """Marketplace-only input hardening and Azure LLM wiring."""

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: Any | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Marketplace conversation history is untrusted free text and is not
        # covered by this agent's S-2 input gate. Rebuild adapter-owned context
        # so present and future runner keys cannot enter State.
        del input_context
        return cast(
            dict[str, Any],
            super().invoke(
                user_input,
                session_id=session_id,
                ctx=ctx,
                input_context=_marketplace_input_context(),
            ),
        )

    def provision_secrets(self, provider: SecretProvider) -> None:
        # The Marketplace runner may supply a broader provider. This single-
        # agent adapter deliberately exposes only its declared Azure secrets.
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _REQUIRED_SECRET_KEYS:
            environment_provider.require(key)

        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)
        self.config["llm"] = AzureLLM()


def main() -> None:
    # shared.bootstrap ships only with the marketplace extra. Keep the import
    # lazy so local imports and the offline test suite remain supported.
    from shared.bootstrap import run_agent_marketplace

    config = load_agent_config(Path(__file__).resolve().parent)
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=config,
    )


if __name__ == "__main__":
    main()
