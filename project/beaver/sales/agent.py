"""The sales agent: its construction, its instructions, its tool registration.

The agent reads the priced lines and drives the tool; the envelope is built
from what the tools say, not from what the model reports back about them. Here
that matters more than anywhere else in the system, because this is the only
agent that cannot be undone: the stock reading every line is judged against is
taken by the output function itself, in the same breath as the writes, and the
model's own call to `snapshot_financials` is what the trail records rather than
what the order is decided on.

So a model that misreads the shelf cannot oversell it, and a model that invents
a line cannot bill for it — an invented name fails `CarriedItemName` and an
invented quantity is still verified against stock before anything is written.

**Sales is stateless.** It never learns that a retry happened, which is what
makes it safely callable twice: the orchestrator re-calls it with only the
lines that were short, so a committed line is never offered for commitment
again. `SalesOrderRequest` from #10's model set is not built here — its
`request_id`, `run_id` and `step_id` are already on `AgentDeps`, and its
`deadline` died with #15, which left sales owning no deadline at all. What
remains of it is this function's arguments.
"""

from datetime import date

from pydantic_ai import Agent, RunContext
from pydantic_ai.toolsets import FunctionToolset

from beaver.audit import AgentDeps, AuditedToolset
from beaver.contract import AgentName, BlockerCode, BlockerSignal
from beaver.quoting.models import QuotedLine
from beaver.sales.models import (
    CommittedLine,
    DeclinedLine,
    LineVerdict,
    SalesCustomerPayload,
    SalesInternalPayload,
    SalesResponse,
)
from beaver.sales.tools import (
    order_total,
    promised_date,
    read_cash,
    read_financials,
    record_sale,
    snapshot_financials,
    verify_lines,
)

INSTRUCTIONS = """
You are the order desk of Beaver's Choice Paper Company. You answer one
question about each line you are given: can we commit it?

Every line you receive has already been matched to an exact product we sell and
priced by colleagues whose verdicts are final. You do not resolve names, you do
not price, you do not restock, and you never change a quantity or a total.

Work in exactly two steps, and never repeat one:

1. Call `snapshot_financials` **once**, passing every item name on the order
   and the date of the request, to see what we hold at this moment.
2. Return every line you were given, unchanged: the same `line_id`, the same
   `quote_line_id`, the same `item_name`, the same `units`, the same prices and
   the same band, plus the `as_of_date` you were given and the availability
   dates you were given, if any.

Do not drop a line because the snapshot looks short of it — whether a line
commits is decided after you return, against a reading taken at the moment the
money moves. Do not merge two lines and do not invent one.

You write no prose for the customer, you state no delivery date and you name no
figure: the orchestrator turns what comes back into words.
""".strip()


async def build_sales_response(
    ctx: RunContext[AgentDeps],
    lines: list[QuotedLine],
    as_of_date: date,
    earliest_availability: dict[str, date] | None = None,
) -> SalesResponse:
    """Commit every line we can fill, write the money, and build the envelope.

    Args:
        lines: The priced lines, exactly as they were given to you.
        as_of_date: The date the request arrived, as `YYYY-MM-DD`.
        earliest_availability: The arrival date of any stock bought in for this
            request, by item name, exactly as you were given it. Omit it when
            you were given none.

    Returns:
        The canonical envelope: what we committed and when we will deliver it,
        what we declined, and what the order came to.
    """
    deps = ctx.deps
    ordered = _one_per_line(lines)
    availability = earliest_availability or {}
    as_of = as_of_date.isoformat()

    # The authoritative read: taken here rather than trusted from the model's
    # own call, and taken now rather than trusted from inventory's survey,
    # which ran before quoting and before any restock landed.
    snapshot = read_financials([line.item_name for line in ordered], as_of)

    # Every line is decided before any line is written. Nothing in this
    # database can be rolled back, so an order that half-commits has no repair.
    decisions = verify_lines(ordered, snapshot.stock_by_item)
    committed_lines = [
        decision.line for decision in decisions if decision.verdict is LineVerdict.COMMITTED
    ]

    rowid_by_line = await record_sale(
        committed_lines,
        run_id=deps.trail.run_id,
        request_id=deps.request_id,
        step_id=deps.current_step_id,
        sold_on=as_of_date,
    )
    cash_after = read_cash(as_of)

    # Built from the write result rather than from the verdicts, so a line can
    # appear here only if money actually moved for it.
    committed = [
        CommittedLine(
            line_id=line.line_id,
            item_name=line.item_name,
            units=line.units,
            line_total=line.line_total,
            promised_delivery_date=promised_date(line.item_name, as_of_date, availability),
        )
        for line in committed_lines
        if line.line_id in rowid_by_line
    ]
    declined = [
        DeclinedLine(
            line_id=decision.line.line_id,
            item_name=decision.line.item_name,
            units=decision.line.units,
        )
        for decision in decisions
        if decision.verdict is LineVerdict.DECLINED
    ]
    signals = [
        BlockerSignal(
            line_id=decision.line.line_id,
            code=BlockerCode.INSUFFICIENT_STOCK,
            detail=decision.detail,
        )
        for decision in decisions
        if decision.verdict is LineVerdict.DECLINED
    ]

    return SalesResponse(
        agent=AgentName.SALES,
        step_id=deps.current_step_id,
        request_id=deps.request_id,
        customer=SalesCustomerPayload(
            committed=committed,
            declined=declined,
            order_total=order_total([line for line in committed_lines if line.line_id in rowid_by_line]),
            # An order is delivered when its last item arrives.
            promised_delivery_date=max(
                (line.promised_delivery_date for line in committed), default=None
            ),
        ),
        internal=SalesInternalPayload(
            cash_before=snapshot.cash_balance,
            cash_after=cash_after,
            stock_read_by_item=snapshot.stock_by_item,
            transaction_rowid_by_line=rowid_by_line,
            verdict_by_line={
                decision.line.line_id: decision.verdict for decision in decisions
            },
            signals=signals,
        ),
    )


def _one_per_line(lines: list[QuotedLine]) -> list[QuotedLine]:
    """The first report of each line, in the order they arrived.

    A model that reports one line twice would otherwise bill it twice, and a
    sale is the one thing here that cannot be taken back.

    Args:
        lines: The priced lines as the model reported them.

    Returns:
        One line per `line_id`, in the order they arrived.
    """
    seen: dict[str, QuotedLine] = {}
    for line in lines:
        seen.setdefault(line.line_id, line)
    return list(seen.values())


sales_agent = Agent(
    deps_type=AgentDeps,
    output_type=build_sales_response,
    instructions=INSTRUCTIONS,
    toolsets=[
        AuditedToolset(
            FunctionToolset(tools=[snapshot_financials]),
            agent=AgentName.SALES,
        )
    ],
    name="sales",
    # As inventory and quoting: a tool argument the model gets wrong is refused
    # by the validator and handed back to it, and an exact catalogue name needs
    # more than one attempt's room.
    retries=3,
)
