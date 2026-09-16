"""The replenishment agent: its construction, its instructions, its tool registration.

The agent reads the shortfalls and drives the tools; the envelope is built from
what the tools say, not from what the model reports back about them. As with
sales, that matters more here than elsewhere, because a purchase cannot be
undone: both guards are evaluated by the output function, and the cash reading
they are evaluated against is taken in the same breath as the writes. The model
calls `cash_available` and `reorder_thresholds` too, one turn earlier, and those
calls are what the audit trail records; both go through the same functions, so
the reading the audit shows and the reading the money moved on can differ only
by what genuinely happened to the books in between.

So a model that misreads the floor cannot oversize an order, and a model that
invents a need cannot spend against it — an invented name fails
`CarriedItemName`, and an invented shortfall is still sized, dated and guarded
before anything is written.

**Replenishment decides everything, then writes.** Both guards run over the
whole batch before the first row reaches `transactions`, for the same reason
sales verifies every line before committing one: nothing in this database can
be rolled back, so a half-bought batch has no repair.

Nothing calls this agent yet. The orchestrator wiring — the intersection of
sales' pass-1 declines with inventory's restock needs, and the second sales
pass — is ticket 108.
"""

from pydantic_ai import Agent, RunContext
from pydantic_ai.toolsets import FunctionToolset

from beaver.audit import AgentDeps, AuditedToolset
from beaver.contract import AgentName, BlockerSignal
from beaver.replenishment.models import (
    ReplenishmentCustomerPayload,
    ReplenishmentInternalPayload,
    ReplenishmentResponse,
    RestockedItem,
    RestockRequest,
    RestockVerdict,
)
from beaver.replenishment.tools import (
    cash_available,
    decide_restocks,
    plan_restocks,
    record_restock,
    reorder_thresholds,
    restock_arrival_date,
)

INSTRUCTIONS = """
You are the buyer of Beaver's Choice Paper Company. You answer one question
about each shortfall you are given: should we buy it in?

Every shortfall you receive has already been matched to an exact product we
sell, priced by colleagues, and refused at the order desk for want of stock.
You do not resolve names, you do not price, you do not sell, and you never
change a shortfall you were given — the colleague who measured it is the only
one who may.

Work in exactly these steps, and never repeat one:

1. Call `reorder_thresholds` **once**, passing every item name you were given,
   to see the stock floor we restock each of them to.
2. Call `cash_available` **once**, with the date of the request, to see what we
   hold.
3. Optionally call `restock_arrival_date` to see when an order of a given size
   would reach us.
4. Return the request you were given, unchanged: the same date, the same
   deadline, the same shortfalls with the same line ids, item names and units.

Do not drop a shortfall because the cash looks short of it or the date looks
tight — what we buy is decided after you return, against readings taken at the
moment the money moves. Do not merge two shortfalls and do not invent one.

You write no prose for the customer, you promise no date and you name no
figure: the orchestrator turns what comes back into words.
""".strip()


async def build_replenishment_response(
    ctx: RunContext[AgentDeps], request: RestockRequest
) -> ReplenishmentResponse:
    """Buy what we can get in time and afford, and build the envelope.

    Args:
        request: The restock request, exactly as you were given it: the date,
            the deadline and every shortfall.

    Returns:
        The canonical envelope: the items now on their way to us and the date
        each can be promised from.
    """
    deps = ctx.deps

    thresholds = reorder_thresholds([need.item_name for need in request.needs])
    plans = plan_restocks(request.needs, thresholds, request.request_date)

    # The authoritative read: the same tool the model called, called again here,
    # because the reading that decides a purchase is the one taken in the same
    # breath as the writes.
    cash_before = cash_available(request.request_date)
    # The revenue this request has just brought in is not ours to spend: sales'
    # pass 1 has already written it to `transactions`, so the balance includes
    # money we are about to lay out against the same order.
    cash_on_hand = cash_before - request.committed_revenue

    decisions = decide_restocks(plans, request.deadline, cash_on_hand)
    bought = [
        decision.plan
        for decision in decisions
        if decision.verdict is RestockVerdict.RESTOCKED
    ]

    rowid_by_line = await record_restock(
        bought,
        run_id=deps.trail.run_id,
        request_id=deps.request_id,
        step_id=deps.current_step_id,
        bought_on=request.request_date,
    )
    cash_after = cash_available(request.request_date)

    # The purchase invariant, in one place: a need is bought iff the write
    # result gave it a rowid. Everything the orchestrator is told about this
    # batch is derived from this list alone.
    purchased = [
        plan
        for plan in bought
        if all(line_id in rowid_by_line for line_id in plan.line_ids)
    ]
    refused = [
        decision
        for decision in decisions
        if decision.verdict is not RestockVerdict.RESTOCKED
    ]

    return ReplenishmentResponse(
        agent=AgentName.REPLENISHMENT,
        step_id=deps.current_step_id,
        request_id=deps.request_id,
        customer=ReplenishmentCustomerPayload(
            # One item per line rather than per purchase: sales promises a
            # line, and two lines served by one purchase are still two lines to
            # promise — on the same date, which is the purchase's.
            restocked=[
                RestockedItem(
                    line_id=line_id,
                    item_name=plan.item_name,
                    # The supplier's date, which sales promises and never
                    # beats. The stock itself is already on our books.
                    available_from=plan.arrival_date,
                )
                for plan in purchased
                for line_id in plan.line_ids
            ]
        ),
        internal=ReplenishmentInternalPayload(
            cash_before=cash_before,
            cash_after=cash_after,
            cash_on_hand=cash_on_hand,
            units_ordered_by_item={plan.item_name: plan.order_qty for plan in purchased},
            unit_cost_by_item={plan.item_name: plan.unit_cost for plan in purchased},
            spend_by_item={plan.item_name: plan.spend for plan in purchased},
            total_spend=round(sum(plan.spend for plan in purchased), 2),
            transaction_rowid_by_line=rowid_by_line,
            # Per line, not per purchase: the orchestrator joins these to the
            # lines it declined, and a refusal of a shared purchase refuses
            # every line that was waiting on it.
            verdict_by_line={
                line_id: decision.verdict
                for decision in decisions
                for line_id in decision.plan.line_ids
            },
            signals=[
                BlockerSignal(
                    line_id=line_id,
                    code=decision.verdict.blocker_code,
                    detail=decision.detail,
                )
                for decision in refused
                for line_id in decision.plan.line_ids
            ],
        ),
    )


replenishment_agent = Agent(
    deps_type=AgentDeps,
    output_type=build_replenishment_response,
    instructions=INSTRUCTIONS,
    toolsets=[
        AuditedToolset(
            FunctionToolset(
                tools=[reorder_thresholds, cash_available, restock_arrival_date]
            ),
            agent=AgentName.REPLENISHMENT,
        )
    ],
    name="replenishment",
    # As the other three: a tool argument the model gets wrong is refused by the
    # validator and handed back to it, and an exact catalogue name needs more
    # than one attempt's room.
    retries=3,
)
