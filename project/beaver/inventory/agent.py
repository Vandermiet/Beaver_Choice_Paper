"""The inventory agent: its construction, its instructions, its tool registration.

The agent reads the requested lines and drives the tools; the envelope is built
from what the tools say, not from what the model reports back about them. That
is deliberate and it is the whole of this module's safety argument: the model's
only unchecked contribution is *which words belong to which line*, and every
consequence of those words — the item, the category, the count, the stock, the
shortfall, the blocker — is derived again here in ordinary code.

So a model that mis-reads a stock reading cannot mis-size a restock, and a
model that names an item we do not carry cannot get it past `CarriedItemName`.
"""

from datetime import date

from pydantic_ai import Agent, RunContext
from pydantic_ai.toolsets import FunctionToolset

from beaver.audit import AgentDeps, AuditedToolset
from beaver.contract import AgentName, BlockerSignal, RequestedLine
from beaver.inventory.models import (
    InventoryCustomerPayload,
    InventoryInternalPayload,
    InventoryResponse,
    LineStockFact,
    ResolutionTrace,
    ResolvedLine,
    RestockNeed,
)
from beaver.inventory.tools import (
    check_stock,
    list_carried_catalogue,
    read_stock,
    resolve_requested_line,
    shortlist_candidates,
)

INSTRUCTIONS = """
You are the inventory desk of Beaver's Choice Paper Company. You answer one
question about each line of a customer's request: what did they actually ask
for, do we sell it, and how short of it are we.

You are given the requested lines in the customer's own words, and the date the
request arrived. Work in exactly four steps, and never repeat one:

1. Call `list_carried_catalogue` **once**, as of the request date, so you know
   what the business sells at all.
2. Call `shortlist_candidates` **once**, passing the customer's words for every
   line, with the quantities and units left out. It applies every resolution
   rule and returns one verdict per line.
3. Call `check_stock` **once**, passing every item name the verdicts resolved
   to, as of the request date. Skip this step if no line resolved. Never pass a
   name a verdict did not resolve to.
4. Return every line you were given, unchanged: the same `line_id`, the same
   words, the same quantity, the same unit, plus the `as_of_date` you were
   given. Do not correct a line, do not merge two lines, do not invent one, and
   do not drop a line because it was refused — a refused line is still an
   answer, and returning it is how it gets reported.

A verdict is final. Do not ask for one again in different words, do not hunt
for a substitute for a line that was refused, and do not call a tool twice with
the same arguments. When every line has a verdict, you are finished: return the
result.

Never invent an item name. Never convert a unit into another unit. You resolve
and you count; you do not price, you do not promise a date, and you write no
prose for the customer — the orchestrator owns every customer-facing word.
""".strip()


async def build_inventory_response(
    ctx: RunContext[AgentDeps],
    lines: list[RequestedLine],
    as_of_date: date,
) -> InventoryResponse:
    """Resolve every requested line and build the envelope inventory returns.

    Args:
        lines: The requested lines, exactly as they were given to you.
        as_of_date: The date the request arrived, as `YYYY-MM-DD`.

    Returns:
        The canonical envelope: what we can sell, what we are short of, and the
        reason for every line we refused.
    """
    resolved: list[ResolvedLine] = []
    needs: list[RestockNeed] = []
    facts: list[LineStockFact] = []
    traces: list[ResolutionTrace] = []
    signals: list[BlockerSignal] = []

    for line in _one_per_line(lines):
        verdict = resolve_requested_line(line)
        name_verdict = verdict.name_verdict
        traces.append(
            ResolutionTrace(
                line_id=line.line_id,
                item_as_stated=line.item_as_stated,
                carried_candidates=name_verdict.carried_candidates,
                universe_best_match=name_verdict.universe_best_match,
                universe_best_category=name_verdict.universe_best_category,
                decision=verdict.decision,
            )
        )

        code = verdict.decision.blocker_code
        if code is not None:
            signals.append(
                BlockerSignal(line_id=line.line_id, code=code, detail=verdict.detail)
            )
            continue

        item_name, quantity = verdict.resolved_item, verdict.quantity
        stock_on_hand = read_stock(item_name, as_of_date.isoformat())
        resolved.append(
            ResolvedLine(
                line_id=line.line_id,
                item_name=item_name,
                category=verdict.category,
                quantity=quantity,
            )
        )
        facts.append(
            LineStockFact(
                line_id=line.line_id,
                item_name=item_name,
                quantity_requested=quantity,
                stock_on_hand=stock_on_hand,
                as_of=as_of_date,
            )
        )
        shortfall = quantity - stock_on_hand
        if shortfall > 0:
            needs.append(
                RestockNeed(
                    line_id=line.line_id,
                    item_name=item_name,
                    shortfall_units=shortfall,
                )
            )

    return InventoryResponse(
        agent=AgentName.INVENTORY,
        step_id=ctx.deps.current_step_id,
        request_id=ctx.deps.request_id,
        customer=InventoryCustomerPayload(resolved_lines=resolved, restock_needs=needs),
        internal=InventoryInternalPayload(
            as_of_date=as_of_date,
            stock_facts=facts,
            traces=traces,
            signals=signals,
        ),
    )


def _one_per_line(lines: list[RequestedLine]) -> list[RequestedLine]:
    """The first report of each line, in the order they arrived.

    A model that reports one line twice would otherwise resolve it twice, and
    a line that raised a blocker would be counted as two refusals in a trail
    whose rejection gate is a `GROUP BY`.
    """
    seen: dict[str, RequestedLine] = {}
    for line in lines:
        seen.setdefault(line.line_id, line)
    return list(seen.values())


inventory_agent = Agent(
    deps_type=AgentDeps,
    output_type=build_inventory_response,
    instructions=INSTRUCTIONS,
    toolsets=[
        AuditedToolset(
            FunctionToolset(tools=[list_carried_catalogue, check_stock, shortlist_candidates]),
            agent=AgentName.INVENTORY,
        )
    ],
    name="inventory",
    # A tool argument the model gets wrong is refused by the validator and
    # handed back to it. One retry is not enough room for an exact catalogue
    # name; three is, and the envelope is built from the tools' answers rather
    # than the model's, so a wasted call costs a token and nothing else.
    retries=3,
)
