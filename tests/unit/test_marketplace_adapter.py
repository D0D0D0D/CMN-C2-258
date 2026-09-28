"""Marketplace entry adapter and Azure LLM boundary tests."""

from __future__ import annotations

import sys
import types

import pytest
from framework.secrets.base import MissingSecret, NullProvider
from framework.secrets.context import bound_secrets

import cli
from src.graph.graph import CompetitiveAnalysisWinRateEstimationGraph
from src.services.azure_llm import AzureLLM


def _set_required_environment(monkeypatch, suffix: str = "one") -> dict[str, str]:
    values = {key: f"test-{suffix}-{key.lower()}" for key in cli._REQUIRED_SECRET_KEYS}
    # The startup gate requires a bare Azure resource endpoint, so
    # this value cannot be a placeholder like the other secrets.
    values["AZURE_OPENAI_ENDPOINT"] = f"https://{suffix}.services.ai.azure.example"
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def test_marketplace_context_is_rebuilt_without_runner_owned_fields(monkeypatch):
    captured = {}

    def capture_invoke(self, user_input, session_id="", ctx=None, input_context=None):
        captured.update(
            user_input=user_input,
            session_id=session_id,
            ctx=ctx,
            input_context=input_context,
        )
        return {"status": "success"}

    monkeypatch.setattr(CompetitiveAnalysisWinRateEstimationGraph, "invoke", capture_invoke)
    context = object()
    result = cli.MarketplaceGraph().invoke(
        "CompX",
        session_id="marketplace-session",
        ctx=context,
        input_context={
            "conversation_history": [{"role": "user", "content": "runner-free-text"}],
            "competitor_urls": {"CompX": ["https://runner.invalid/"]},
            "future_runner_key": "unvalidated",
        },
    )

    assert captured == {
        "user_input": "CompX",
        "session_id": "marketplace-session",
        "ctx": context,
        "input_context": {"competitor_urls": {}},
    }
    assert result == {"status": "success"}


def test_marketplace_context_contains_only_consumed_optional_key():
    assert cli._marketplace_input_context() == {"competitor_urls": {}}


def test_secret_provider_resolves_only_allowlisted_process_environment(monkeypatch):
    values = _set_required_environment(monkeypatch)
    monkeypatch.setenv("UNRELATED_PLATFORM_SECRET", "must-not-be-visible")
    provider = cli._AllowlistedEnvironmentProvider()

    assert {key: provider.require(key) for key in cli._REQUIRED_SECRET_KEYS} == values
    assert provider.get("UNRELATED_PLATFORM_SECRET") is None
    assert "must-not-be-visible" not in repr(vars(provider))


@pytest.mark.parametrize("invalid_value", [None, "", "   "])
def test_missing_or_blank_azure_secret_fails_during_startup(monkeypatch, invalid_value):
    _set_required_environment(monkeypatch)
    if invalid_value is None:
        monkeypatch.delenv("AZURE_OPENAI_ENDPOINT")
    else:
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", invalid_value)

    with pytest.raises(MissingSecret, match="AZURE_OPENAI_ENDPOINT"):
        cli.MarketplaceGraph().provision_secrets(NullProvider())


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(monkeypatch, endpoint):
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    _set_required_environment(monkeypatch)
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)

    with pytest.raises(ValueError, match="bare"):
        cli.MarketplaceGraph().provision_secrets(NullProvider())


def test_provision_after_compile_injects_same_llm_into_synthesis(monkeypatch):
    _set_required_environment(monkeypatch)
    agent = cli.MarketplaceGraph(config={"max_retry": 3})
    agent.compile()
    main_node = agent._nodes["main"]

    assert agent.config is main_node._injected
    agent.provision_secrets(NullProvider())
    injected_llm = agent.config["llm"]

    workflow = main_node.get_subgraph()
    workflow.compile()

    assert isinstance(injected_llm, AzureLLM)
    assert workflow._nodes["synthesis"]._llm is injected_llm
    assert agent._secrets_provider is not None


def test_azure_llm_resolves_fresh_values_without_caching_client(monkeypatch):
    configs = []

    class FakeAzureOpenAIClient:
        def __init__(self, config):
            configs.append(dict(config))

        def complete(self, messages):
            return {"content": "ok", "messages": messages}

        def stream(self, messages):
            yield "ok"

        def bind_tools(self, tools):
            return self

    azure_module = types.ModuleType("shared.services.llm.azure_openai_client")
    azure_module.AzureOpenAIClient = FakeAzureOpenAIClient
    monkeypatch.setitem(sys.modules, azure_module.__name__, azure_module)

    provider = cli._AllowlistedEnvironmentProvider()
    llm = AzureLLM()
    assert vars(llm) == {}

    with bound_secrets(provider):
        first = _set_required_environment(monkeypatch, "first")
        llm.complete([{"role": "user", "content": "first"}])
        assert vars(llm) == {}

        second = _set_required_environment(monkeypatch, "second")
        llm.complete([{"role": "user", "content": "second"}])
        assert vars(llm) == {}

    assert configs == [
        {
            "api_key": first["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": first["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": first["AZURE_OPENAI_DEPLOYMENT"],
        },
        {
            "api_key": second["AZURE_OPENAI_API_KEY"],
            "azure_endpoint": second["AZURE_OPENAI_ENDPOINT"],
            "azure_deployment": second["AZURE_OPENAI_DEPLOYMENT"],
        },
    ]


def test_azure_llm_bind_tools_returns_new_instance():
    llm = AzureLLM()
    bound = llm.bind_tools([{"name": "lookup"}])

    assert bound is not llm
    assert vars(llm) == {}
    assert vars(bound) == {"_tools": ({"name": "lookup"},)}


def test_main_loads_repo_config_for_marketplace_runner(monkeypatch):
    called = {}

    def fake_runner(agent_cls, **kwargs):
        called["agent_cls"] = agent_cls
        called.update(kwargs)

    bootstrap = types.ModuleType("shared.bootstrap")
    bootstrap.run_agent_marketplace = fake_runner
    monkeypatch.setitem(sys.modules, bootstrap.__name__, bootstrap)

    cli.main()

    assert called["agent_cls"] is cli.MarketplaceGraph
    assert called["agent_name"] == "CMN-C2-258"
    assert called["namespace"] == "cmn"
    assert called["config"]["max_retry"] == 3
    assert called["config"]["win_loss_table"] == "win_loss"
