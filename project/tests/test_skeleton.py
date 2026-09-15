"""The module boundaries the locked design names, and the stub loop.

These are structural assertions on purpose: ticket 101 builds the skeleton
every later ticket plugs into, so the boundaries themselves are the behaviour.
"""

import importlib
import inspect

import pytest

MODULES = [
    "beaver.contract",
    "beaver.audit",
    "beaver.llm",
    "beaver.orchestrator",
    "beaver.inventory.models",
    "beaver.inventory.tools",
    "beaver.inventory.agent",
    "beaver.quoting.models",
    "beaver.quoting.tools",
    "beaver.quoting.agent",
    "beaver.sales.models",
    "beaver.sales.tools",
    "beaver.sales.agent",
    "beaver.replenishment.models",
    "beaver.replenishment.tools",
    "beaver.replenishment.agent",
]


@pytest.mark.parametrize("name", MODULES)
def test_module_exists(name):
    assert importlib.import_module(name) is not None


def test_the_harness_call_site_does_not_move():
    """101 stubbed `handle_request` and 103 filled it in. The name and the
    three arguments the harness passes are the seam, so they are pinned here;
    what the seam now does is covered in `test_inventory.py`, under a scripted
    model rather than against the proxy."""
    from beaver.orchestrator import handle_request

    assert inspect.iscoroutinefunction(handle_request)
    assert list(inspect.signature(handle_request).parameters) == [
        "request_with_date",
        "request_date",
        "request_id",
    ]


def test_build_model_constructs_a_chat_completions_model(monkeypatch):
    """The Vocareum proxy speaks Chat Completions, so the model must be
    `OpenAIChatModel` — the bare `openai:` prefix would reach `/v1/responses`."""
    from pydantic_ai.models.openai import OpenAIChatModel

    from beaver.llm import build_model

    monkeypatch.setenv("UDACITY_OPENAI_API_KEY", "test-key")
    model = build_model()
    assert isinstance(model, OpenAIChatModel)


def test_starter_helpers_are_importable_without_closing_the_cycle():
    """`beaver.starter` imports `project_starter`, which imports `beaver` from
    inside `run_test_scenarios()`. If that import ever moves to module level,
    this is what breaks."""
    from beaver import starter

    for name in starter.__all__:
        assert getattr(starter, name) is not None
