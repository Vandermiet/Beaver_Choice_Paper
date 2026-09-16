"""The workflow diagram is a description, not an aspiration.

The diagram in `docs/workflow-diagram.md` is graded on whether it matches the
system it draws, and a drawing has no compiler. These tests are that compiler:
every tool the diagram names must be a real function in the agent's own module,
every tool the models can actually call must be named in the diagram and drawn
as callable, and each of the seven required helpers must be reached by an edge
from a tool that wraps it.

The diagram is parsed rather than read for keywords, so its node ids are the
contract: a tool node's id is the function's own name, a helper node's id is the
helper's own name, and an agent subgraph's id is the agent's name in caps.
"""

import importlib
import re
from pathlib import Path
from typing import NamedTuple

import pytest

from beaver import starter
from beaver.inventory.agent import inventory_agent
from beaver.quoting.agent import quoting_agent
from beaver.replenishment.agent import replenishment_agent
from beaver.sales.agent import sales_agent

DIAGRAM = Path(__file__).resolve().parents[2] / "docs" / "workflow-diagram.md"


class DrawnAgent(NamedTuple):
    """One agent as the diagram draws it: the module its tools live in, and it."""

    module: str
    agent: object


#: The four domain agents, by the subgraph id their tools are drawn in. The
#: orchestrator is absent because its tools are delegations rather than
#: functions in a tools module — it is checked for presence alone.
AGENTS = {
    "INVENTORY": DrawnAgent("beaver.inventory.tools", inventory_agent),
    "QUOTING": DrawnAgent("beaver.quoting.tools", quoting_agent),
    "SALES": DrawnAgent("beaver.sales.tools", sales_agent),
    "REPLENISHMENT": DrawnAgent("beaver.replenishment.tools", replenishment_agent),
}

#: The seven helpers the rubric requires the system to use.
REQUIRED_HELPERS = frozenset(
    {
        "create_transaction",
        "generate_financial_report",
        "get_all_inventory",
        "get_cash_balance",
        "get_stock_level",
        "get_supplier_delivery_date",
        "search_quote_history",
    }
)

#: The class the diagram draws a tool with when the model cannot call it, and
#: the one it draws a tool with when the model can call it *and* the output
#: function calls it again. Both are claims about the code, so both are checked.
UNEXPOSED, TWICE = "unexposed", "twice"

_FENCE = re.compile(r"^```mermaid\s*$")
_SUBGRAPH = re.compile(r"^subgraph\s+(\w+)")
_NODE = re.compile(r"^(\w+)[\[(]")
_EDGE = re.compile(r"^(\w+)\s*-[-.=]+>\s*(?:\|.*?\|\s*)?(\w+)", re.DOTALL)
_CLASS = re.compile(r"^class\s+([\w,]+)\s+(\w+)")


def _blocks() -> list[str]:
    """Every fenced Mermaid block in the diagram document, in order."""
    blocks, current = [], None
    for line in DIAGRAM.read_text().splitlines():
        if current is None:
            if _FENCE.match(line):
                current = []
        elif line.strip() == "```":
            blocks.append("\n".join(current))
            current = None
        else:
            current.append(line)
    assert current is None, "a mermaid block was never closed"
    return blocks


def _nodes_by_subgraph(block: str) -> dict[str, set[str]]:
    """The node ids declared inside each subgraph of one Mermaid block."""
    found: dict[str, set[str]] = {}
    stack: list[str] = []
    for raw in block.splitlines():
        line = raw.strip()
        if match := _SUBGRAPH.match(line):
            stack.append(match.group(1))
        elif line == "end":
            if stack:
                stack.pop()
        elif stack and (match := _NODE.match(line)):
            found.setdefault(stack[-1], set()).add(match.group(1))
    return found


def _edges(block: str) -> set[tuple[str, str]]:
    """Every `a --> b` edge in one Mermaid block, edge labels ignored."""
    return {
        (match.group(1), match.group(2))
        for line in block.splitlines()
        if (match := _EDGE.match(line.strip()))
    }


def _classed(block: str, class_name: str) -> set[str]:
    """The node ids one `class ... <name>` line applies that class to."""
    return {
        node
        for line in block.splitlines()
        if (match := _CLASS.match(line.strip())) and match.group(2) == class_name
        for node in match.group(1).split(",")
    }


def _callable_tools(agent) -> set[str]:
    """The tool names this agent's model may call, asked of the agent itself."""
    names: set[str] = set()
    for toolset in agent.toolsets:
        for holder in (toolset, getattr(toolset, "wrapped", None)):
            names |= set(getattr(holder, "tools", None) or {})
    assert names, f"{agent.name} exposes no tools: the check would pass vacuously"
    return names


def _not_in_the_code(names: set[str], module_name: str) -> set[str]:
    """Of these node ids, the ones that are not a function in that module."""
    module = importlib.import_module(module_name)
    return {name for name in names if not callable(getattr(module, name, None))}


@pytest.fixture(scope="module")
def architecture() -> str:
    """The first Mermaid block: the agents, their tools and the helpers."""
    return _blocks()[0]


@pytest.fixture(scope="module")
def nodes(architecture) -> dict[str, set[str]]:
    """The architecture block's nodes, by the subgraph they are drawn in."""
    return _nodes_by_subgraph(architecture)


def test_all_five_agents_are_drawn(nodes):
    assert {"ORCHESTRATOR", *AGENTS} <= set(nodes)


@pytest.mark.parametrize("subgraph", sorted(AGENTS))
def test_every_callable_tool_is_in_the_diagram(subgraph, nodes):
    undrawn = _callable_tools(AGENTS[subgraph].agent) - nodes[subgraph]
    assert not undrawn, f"{subgraph} tools the model can call but nothing draws: {undrawn}"


@pytest.mark.parametrize("subgraph", sorted(AGENTS))
def test_every_tool_in_the_diagram_exists_in_the_code(subgraph, nodes):
    invented = _not_in_the_code(nodes[subgraph], AGENTS[subgraph].module)
    assert not invented, f"{subgraph} draws tools that do not exist: {invented}"


@pytest.mark.parametrize("subgraph", sorted(AGENTS))
def test_a_tool_drawn_as_unexposed_is_one_the_model_cannot_call(subgraph, architecture, nodes):
    """The dashed border is a claim about the code, so it is checked like one."""
    drawn_unexposed = _classed(architecture, UNEXPOSED) & nodes[subgraph]
    wrong = drawn_unexposed & _callable_tools(AGENTS[subgraph].agent)
    assert not wrong, f"{subgraph} draws these as unexposed, but the model can call them: {wrong}"


@pytest.mark.parametrize("subgraph", sorted(AGENTS))
def test_a_tool_drawn_as_called_twice_is_one_the_model_can_call(subgraph, architecture, nodes):
    """The thick border claims the model calls it *and* the output function does."""
    drawn_twice = _classed(architecture, TWICE) & nodes[subgraph]
    wrong = drawn_twice - _callable_tools(AGENTS[subgraph].agent)
    assert not wrong, f"{subgraph} draws these as called twice, but the model cannot call them: {wrong}"


def test_all_seven_required_helpers_are_drawn(nodes):
    undrawn = REQUIRED_HELPERS - nodes["STARTER"]
    assert not undrawn, f"required helpers nothing draws: {undrawn}"


def test_every_helper_in_the_diagram_exists_in_the_starter(nodes):
    invented = {node for node in nodes["STARTER"] if not hasattr(starter, node)}
    assert not invented, f"the starter provides no such helpers: {invented}"


def test_every_helper_is_wrapped_by_a_tool_in_the_diagram(architecture, nodes):
    """Each required helper is the target of an edge from a drawn tool.

    The source is checked, not just the arrival: an edge into a helper from a
    database table would otherwise pass for coverage.
    """
    # `ledger_write` counts as a wrapping source because `create_transaction`
    # is reached through `beaver.ledger`: both writers share its lock, so the
    # two `record_*` tools point at the door rather than at the helper.
    sources = set().union(*(nodes[subgraph] for subgraph in AGENTS)) | {"ledger_write"}
    wrapped = {
        target for source, target in _edges(architecture) if source in sources
    }
    unwrapped = REQUIRED_HELPERS - wrapped
    assert not unwrapped, f"helpers no tool is drawn as wrapping: {unwrapped}"


def test_the_sequence_shows_one_bounded_retry(nodes):
    """The second block is the sequence: the gate, the retry, and no third pass."""
    sequence = _blocks()[1]
    assert sequence.lstrip().startswith("sequenceDiagram")
    for participant in ("inventory", "quoting", "sales", "replenishment"):
        assert f"participant {participant}" in sequence, f"{participant} is not in the sequence"
    assert sequence.count("participant") + sequence.count("actor ") == 8
    # The retry is conditional on sales' stock declines and happens once.
    assert "opt pass-1 insufficient_stock declines" in sequence
    assert sequence.count("_commit, pass 1") == 1
    assert sequence.count("_commit, pass 2") == 1
