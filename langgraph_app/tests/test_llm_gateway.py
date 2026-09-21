"""Routing the triage model through the LangSmith LLM gateway.

The gateway is OpenAI-compatible (`POST /v1/chat/completions`), authenticates
with a LangSmith API key rather than a provider key, and addresses models by a
prefixed id such as `anthropic/claude-opus-5`.

`create_deep_agent(model=...)` accepts either a provider-prefixed string or a
built chat model. A bare string cannot carry a base URL, so the gateway path has
to build the model. This file pins which of the two happens, because getting it
wrong fails in a way that looks like it worked: a string falls back to calling
the provider directly, which *succeeds* while bypassing the gateway entirely --
no governance, no gateway tracing, and a provider key silently still in use.
"""

from __future__ import annotations

import pytest

from pagerduty_triage import agent as agent_mod
from pagerduty_triage.settings import load_settings


def test_no_gateway_configured_passes_the_string_through(monkeypatch):
    """Default behaviour is unchanged: init_chat_model resolves the string."""
    monkeypatch.delenv("LLM_GATEWAY_BASE_URL", raising=False)
    monkeypatch.setenv("TRIAGE_MODEL", "bedrock_converse:us.anthropic.claude-sonnet-5")
    s = load_settings()
    assert agent_mod.resolve_model(s) == "bedrock_converse:us.anthropic.claude-sonnet-5"


def test_gateway_builds_a_model_pointed_at_it(monkeypatch):
    monkeypatch.setenv("LLM_GATEWAY_BASE_URL", "https://gateway.smith.langchain.com/v1")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_test")
    monkeypatch.setenv("TRIAGE_MODEL", "anthropic/claude-opus-5")
    s = load_settings()

    model = agent_mod.resolve_model(s)

    assert not isinstance(model, str), "a string would bypass the gateway silently"
    # The two things that actually route the call.
    assert getattr(model, "model_name", None) == "anthropic/claude-opus-5"
    base = str(getattr(model, "openai_api_base", "") or "")
    assert base.rstrip("/") == "https://gateway.smith.langchain.com/v1"


def test_gateway_without_a_key_fails_loudly(monkeypatch):
    """A gateway URL with no key would fall back to a provider key from the
    environment and quietly not use the gateway. Refuse instead."""
    monkeypatch.setenv("LLM_GATEWAY_BASE_URL", "https://gateway.smith.langchain.com/v1")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LLM_GATEWAY_API_KEY", raising=False)
    monkeypatch.setenv("TRIAGE_MODEL", "anthropic/claude-opus-5")
    s = load_settings()

    with pytest.raises(RuntimeError, match="LANGSMITH_API_KEY"):
        agent_mod.resolve_model(s)


def test_explicit_gateway_key_overrides_the_langsmith_one(monkeypatch):
    """A deployment may want the gateway key separate from the tracing key."""
    monkeypatch.setenv("LLM_GATEWAY_BASE_URL", "https://gateway.smith.langchain.com/v1")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_tracing")
    monkeypatch.setenv("LLM_GATEWAY_API_KEY", "lsv2_gateway")
    monkeypatch.setenv("TRIAGE_MODEL", "anthropic/claude-opus-5")
    s = load_settings()

    model = agent_mod.resolve_model(s)
    key = getattr(model, "openai_api_key", None)
    assert key is not None
    assert key.get_secret_value() == "lsv2_gateway"
