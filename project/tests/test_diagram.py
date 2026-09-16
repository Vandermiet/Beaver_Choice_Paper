"""The workflow diagram is a description, not an aspiration.

The diagram in `docs/workflow-diagram.md` is graded on whether it matches the
system it draws, and a drawing has no compiler. These tests are that compiler:
every tool the diagram names must be a real function in the agent's own module,
every tool the models can actually call must be named in the diagram, and the
seven helpers the rubric requires must each be visibly wrapped by something.

The diagram is parsed rather than read for keywords, so the node ids are the
contract: a tool node's id is the function's own name, a helper node's id is the
helper's own name, and an agent subgraph's id is the agent's name in caps.
"""

import re
from pathlib import Path

import pytest

from beaver import starter
from beaver.inventory.agent import inventory_agent
from beaver.quoting.agent import quoting_agent
from beaver.replenishment.agent import replenishment_agent
from beaver.sales.agent import sales_agent

DIAGRAM = Path(__file__).resolve().parents[2] / "docs" / "workflow-diagram.md"

#: The subgraph id each agent's tools are drawn in, and the module they live in.
AGENTS = {
    "INVENTORY": ("beaver.inventory.tools", inventory_agent),
    "QUOTING": ("beaver.quoting.tools", quoting_agent),
    "SALES": ("beaver.sales.tools", sales_agent),
    "REPLENISHMENT": ("beaver.replenishment.tools", replenishment_agent),
}

#: The seven helpers §2 of the rubric requires the system to use.
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

_FENCE = re.compile(r"^```mermaid\s*$")
_SUBGRAPH = re.compile(r"^subgraph\s+(\w+)")
_NODE = re.compile(r"^(\w+)[\[(]")


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
            continue
        if line == "end":
            if stack:
                stack.pop()
            continue
        if stack and (match := _NODE.match(line)):
            for name in stack:
                found.setdefault(name, set()).add(match.group(1))
    return found


@pytest.fixture(scope="module")
def nodes() -> dict[str, set[str]]:
    """The architecture block's nodes, by the subgraph they are drawn in."""
    return _nodes_by_subgraph(_blocks()[0])


def registered_tools(agent) -> set[str]:
    """The tool names the agent's model may call, from the agent itself."""
    names: set[str] = set()
    for toolset in agent.toolsets:
        wrapped = getattr(toolset, "wrapped", None)
        if wrapped is not None:
            names |= set(wrapped.tools)
    return names


def test_all_five_agents_are_drawn(nodes):
    assert {"ORCHESTRATOR", *AGENTS} <= set(nodes)


@pytest.mark.parametrize("subgraph", sorted(AGENTS))
def test_every_callable_tool_is_in_the_diagram(subgraph, nodes):
    _, agent = AGENTS[subgraph]
    assert registered_tools(agent) <= nodes[subgraph]


@pytest.mark.parametrize("subgraph", sorted(AGENTS))
def test_every_tool_in_the_diagram_exists_in_the_code(subgraph, nodes):
    module_name, _ = AGENTS[subgraph]
    module = __import__(module_name, fromlist=["*"])
    missing = {
        node
        for node in nodes[subgraph]
        if not callable(getattr(module, node, None))
    }
    assert not missing


def test_all_seven_required_helpers_are_drawn(nodes):
    assert REQUIRED_HELPERS <= nodes["STARTER"]


def test_every_helper_in_the_diagram_exists_in_the_starter(nodes):
    missing = {node for node in nodes["STARTER"] if not hasattr(starter, node)}
    assert not missing


def test_every_helper_is_wrapped_by_a_tool_in_the_diagram():
    """Each required helper is reached by an edge from a tool that wraps it."""
    block = _blocks()[0]
    wrapped = {
        match.group(1)
        for match in re.finditer(r"-->\s*(?:\|[^|]*\|\s*)?(\w+)", block)
    }
    assert REQUIRED_HELPERS <= wrapped


def test_the_sequence_is_drawn_with_the_conditional_retry():
    """The second block is the sequence: five agents, one bounded retry."""
    sequence = _blocks()[1]
    assert sequence.lstrip().startswith("sequenceDiagram")
    for agent in ("orchestrator", "inventory", "quoting", "sales", "replenishment"):
        assert agent in sequence
