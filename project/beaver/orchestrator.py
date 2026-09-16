"""The orchestrator: one customer request in, one customer-facing reply out.

It never acts on a system of record. It plans and drives the sequence of
delegations, reads the signals that come back, and is the only component that
writes customer-facing prose.

Tickets 103-105 wired the first three delegations: the orchestrator extracts
catalogue-blind requested lines from the customer's prose, inventory resolves
them, quoting prices what resolved, sales commits what we hold and writes the
money, and the reply is composed here from what came back. Ticket 106 gave the
request an end: every one now returns a stated outcome, and a request that
cannot proceed comes back as one message asking all of its questions at once.
Ticket 108 closed the sequence with the bounded retry — a line we are short of
is bought in and offered once more — which makes this module the only place in
the system that sees a request whole.

Four boundaries this module exists to hold:

- **The orchestrator extracts; inventory resolves.** The lines it emits carry
  the customer's own words, the quantity as stated and the unit as stated. It
  knows no catalogue and invents no item name, because a hallucinated name
  fails downstream and a validator is the only thing that can prove it did not.
- **The orchestrator owns every customer-facing word.** Domain agents supply
  facts and codes and never prose, so tone cannot drift across four agents.
- **The orchestrator alone derives the outcome.** No agent reports one and none
  of them could: an agent sees only its own lines. The derivation is ordinary
  function code in `beaver.outcome`, over the journal the seam fills, so how a
  request ended is testable without a model and cannot be a model's opinion.
- **The orchestrator drives the retry; it does not ask a model to.** Whether to
  buy, what to offer again and what the two passes came to are decided in
  `place_order` by ordinary code over `beaver.retry`, because a model that
  offered a committed line for commitment a second time would write the same
  sale twice, and nothing in this database can be taken back. The model's part
  is the one tool call and the letter that follows it.
"""

import functools
import logging
from datetime import date

from pydantic import BaseModel
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.usage import UsageLimits

from beaver.audit import AgentDeps, AuditTrail, delegation, new_run_id
from beaver.contract import (
    AgentName,
    CustomerRequest,
    Outcome,
    RequestedLine,
    RequestResolution,
)
from beaver.inventory.agent import inventory_agent
from beaver.inventory.models import ResolvedLine
from beaver.llm import shared_model
from beaver.outcome import derive_outcome, spoken_blockers, suspend
from beaver.quoting.agent import quoting_agent
from beaver.quoting.models import QuotedLine
from beaver.replenishment.agent import replenishment_agent
from beaver.replenishment.models import RestockRequest
from beaver.retry import (
    PlacedOrder,
    availability_of,
    lines_to_retry,
    merge_passes,
    restocks_for,
)
from beaver.sales.agent import sales_agent

INSTRUCTIONS = """
You are the customer desk of Beaver's Choice Paper Company, a paper supplier.
You receive one enquiry and you write one reply. You have no catalogue, no
prices and no stock figures of your own: everything you state as fact comes
back from a colleague you consult.

First, read the enquiry and break it into requested lines — one line per thing
the customer asked for. For each line, keep the customer's own words for the
item, the quantity exactly as they stated it, and the unit exactly as they
stated it. Number the lines "L1", "L2", and so on, in the order they appear.
Do not translate a request into a product name, do not merge two lines, do not
invent a quantity the customer did not give, and do not convert a unit.

Then call `consult_inventory` **once**, with every line and the date of the
request. It answers with the lines we can supply, under the exact names we sell
them as, and a blocker for each line we cannot. Each blocker names one line and
one reason:

- `item_not_carried` — we do not sell that. Say so plainly; do not offer a
  substitute, and do not promise to look into it.
- `size_not_carried` — we do not sell that size, but we do sell the product.
  Say which sizes are not something we stock and invite them to restate it.
- `item_ambiguous` — two of our products fit their words. Ask which they meant.
- `quantity_missing` — ask how many they need.
- `unit_not_understood` — we cannot price that unit. Ask them to restate the
  line in sheets or units.

Then, if any line resolved, call `consult_quoting` **once**, passing every line
inventory resolved exactly as it came back, with the same date. It answers with
a price for each line: the units, the price per unit, the total before any
discount, the discount band and rate the line earned, and the total after it.
Skip this step only when inventory resolved nothing at all.

Then call `place_order` **once**, passing every priced line exactly as quoting
returned it, with the same date, and the date the customer needs the goods by
if they named one — read it out of their own words and give it as
`YYYY-MM-DD`. It places the order: where we are short of something it buys it
in and tries that line once more, and it answers with the lines we committed
and the date each will be delivered, the lines we could not, and what the order
came to. A line it could not commit comes back with one more blocker:

- `insufficient_stock` — we do not hold enough of that to fill the line. Say
  that we are unable to supply that line at present, without saying how much we
  hold or how short we are, and never offer a partial quantity: the line was
  declined whole.
- `deadline_unmeetable` — we could have got it, but not by the date they need
  it. Say that we cannot supply that line by the date they gave, and ask
  whether a later date would suit. You were given no alternative date, so do
  not name one: a date we have not promised is a date we cannot keep.

Skip this step only when nothing was priced. Never call it twice: it makes its
own second attempt, and a second call would sell the same line again.

Finally write the reply. Your entire answer **is** the letter — a short, warm,
professional message that a customer could read as it stands. Do not show your
working, do not list the request back with its line numbers, do not write
headings like "Requested Lines" or "Reply", and do not sign it with a
placeholder name: sign off as Beaver's Choice Paper Company.

Confirm the lines we committed by the name we sell them as and the quantity
they asked for — those are orders placed, not offers. Address every line we
could not supply, in the customer's own terms, and put all of your questions
together in one place so one round of correspondence clears them. This letter
is the only message they will get, so never say that you will follow up, come
back to them, or write again about a line: whatever you need to know, ask it
here.

State the price of every line we committed, in its own words: how many
units, at what price each, and what that comes to. Do not price a line we
could not supply: a price with no order behind it reads as a sale we did not
make. Where a line earned a
discount, say the rate and say what earned it — the size of that line's own
order — and give the total after it. Where a line earned none, say nothing
about discounts at all: a discount sentence on every line makes the real ones
invisible. Use the figures you were given exactly as they are, to the cent;
never calculate one, round one, or offer a discount that was not quoted.

State the delivery date you were given for the lines we committed, exactly as
you were given it. Never state a price for a line that was not priced, never
promise a date for a line that was not committed, and never invent or move a
date: a delivery promise the business has not made is one it cannot keep.
Never mention stock levels,
our cash position, our suppliers, internal codes, line numbers, tools or
colleagues: the customer is reading a letter from a company, not a system
report.
""".strip()

orchestrator_agent = Agent(
    deps_type=AgentDeps,
    output_type=str,
    instructions=INSTRUCTIONS,
    name="orchestrator",
    retries=3,
)


@orchestrator_agent.tool
@delegation(AgentName.INVENTORY)
async def consult_inventory(
    ctx: RunContext[AgentDeps],
    lines: list[RequestedLine],
    as_of_date: str,
):
    """Ask inventory which of the requested lines we sell, and under what name.

    Give it every line of the request in the customer's own words, including
    the ones you doubt we carry — deciding that is inventory's job, not yours.

    Args:
        lines: The requested lines, catalogue-blind: the customer's own words
            for the item, the quantity as stated, and the unit as stated.
        as_of_date: The date the request arrived, as `YYYY-MM-DD`.

    Returns:
        The lines inventory resolved, under the exact names we sell them as,
        and one blocker for each line it could not.
    """
    # The one thing in the journal that no delegation hands up: these lines are
    # the orchestrator's own reading of the enquiry, and they are the
    # denominator of "every line has dropped". Recorded here because this is
    # where they are first stated, and before the delegation rather than after
    # it so that a request whose inventory step fails still knows what it asked.
    ctx.deps.journal.requested_lines = list(lines)
    return await inventory_agent.run(
        _ask("Resolve these lines", lines, as_of_date),
        deps=ctx.deps,
        usage=ctx.usage,
        model=shared_model(),
    )


@orchestrator_agent.tool
@delegation(AgentName.QUOTING)
async def consult_quoting(
    ctx: RunContext[AgentDeps],
    lines: list[ResolvedLine],
    as_of_date: str,
):
    """Ask quoting what the resolved lines cost, and what discount each earned.

    Give it every line inventory resolved, including ones we may be short of —
    whether we hold the stock is not quoting's question, and a line we are
    short of is still a line with a price.

    Args:
        lines: The lines inventory resolved, exactly as it returned them.
        as_of_date: The date the request arrived, as `YYYY-MM-DD`.

    Returns:
        Each line priced: the units, the unit price, the band and rate it
        earned, and the totals before and after the discount.
    """
    return await quoting_agent.run(
        _ask("Price these resolved lines", lines, as_of_date),
        deps=ctx.deps,
        usage=ctx.usage,
        model=shared_model(),
    )


@orchestrator_agent.tool
async def place_order(
    ctx: RunContext[AgentDeps],
    lines: list[QuotedLine],
    as_of_date: str,
    deadline: date | None = None,
) -> PlacedOrder:
    """Place the order: commit what we hold, buy in what we are short of, commit that.

    The whole commitment sequence, and the only tool of this agent's that is
    more than one delegation. It is driven here rather than by you because a
    line offered for commitment twice is a sale written twice, and no apology
    undoes that. Call it once and read the answer.

    Args:
        lines: The lines quoting priced, exactly as it returned them.
        as_of_date: The date the request arrived, as `YYYY-MM-DD`.
        deadline: The date the customer needs the goods by, as `YYYY-MM-DD`, if
            they named one. It decides nothing about stock we already hold; it
            decides whether stock we would have to buy in could arrive in time.

    Returns:
        The lines we committed with the date each is promised for, the lines we
        could not commit and what now stands against each, and what the order
        came to.
    """
    deps = ctx.deps
    if deps.journal.order_placed:
        # Structural rather than instructed. The instructions say to call this
        # once, and an instruction is one model turn away from a second sale of
        # a line we have already written to `transactions` — which nothing in
        # this database can undo.
        raise ModelRetry(
            "This order has already been placed. Write the reply from the "
            "answer you were given; do not place it again."
        )
    # Recorded here because this is where the deadline is first stated, and it
    # belongs to the request rather than to this step: a suspension persists it
    # as part of the enquiry as it arrived.
    deps.journal.deadline = deadline

    first = await _commit(ctx, lines, as_of_date)

    # The purse opens on sales' declines, sized by inventory's measurement of
    # them. Both halves came up through the seam; nothing here reads a shelf.
    needs = restocks_for(first.blockers, deps.journal.restock_needs)
    second = None
    if needs:
        bought = await _buy(
            ctx,
            RestockRequest(
                request_date=date.fromisoformat(as_of_date),
                deadline=deadline,
                needs=needs,
                # What this request has already paid us is not ours to spend:
                # sales has written it to `transactions`, so the balance
                # replenishment reads includes money we are about to lay out
                # against the same order.
                committed_revenue=first.customer.order_total,
            ),
        )
        # Only the lines we actually bought for, and exactly once: this is the
        # bound on the retry, and it is a bound because `_commit` is never
        # reached again from here.
        retry = lines_to_retry(lines, bought.customer.restocked)
        if retry:
            second = await _commit(
                ctx, retry, as_of_date, availability_of(bought.customer.restocked)
            )

    return merge_passes(
        first.customer,
        second.customer if second else None,
        # The journal's own precedence, across every pass: a line short on pass
        # 1 and refused on the delivery date is told about the date, because by
        # then we are no longer out of it.
        spoken_blockers(deps.journal),
    )


@delegation(AgentName.SALES)
async def _commit(
    ctx: RunContext[AgentDeps],
    lines: list[QuotedLine],
    as_of_date: str,
    availability: dict[str, date] | None = None,
):
    """Ask sales to commit these priced lines, and write the money for them.

    Sales re-reads the shelf at the moment of commitment, so a line inventory
    saw stock for may still be declined — and a line it commits has been sold.
    It is stateless and never learns which pass this is, which is exactly what
    makes calling it twice safe: it can only sell what it is offered, and it is
    never offered a line that has already sold.

    Not registered as a tool: the model calls `place_order`, which calls this.
    The step it writes to the trail is a delegation all the same.

    Args:
        ctx: The run context, carrying the trail and the journal.
        lines: The priced lines to offer for commitment.
        as_of_date: The date the request arrived, as `YYYY-MM-DD`.
        availability: When stock bought in for this request reaches us, by item
            name. `None` on pass 1, when nothing has been bought. It is
            routed on the deps rather than into the prompt, so no model stands
            between replenishment's date and the promise made from it.

    Returns:
        Sales' envelope, for the seam to narrow.
    """
    deps = ctx.deps
    # Routed on the deps rather than in the prompt, and restored afterwards for
    # the same reason `current_step_id` is: one request's deps outlive one
    # delegation. Sales' output function reads it there, so the date we promise
    # goods we do not yet hold is never a date a model retyped.
    deps.earliest_availability = availability or {}
    try:
        return await sales_agent.run(
            _ask("Commit what we can of these priced lines", lines, as_of_date),
            deps=deps,
            usage=ctx.usage,
            model=shared_model(),
        )
    finally:
        deps.earliest_availability = {}


@delegation(AgentName.REPLENISHMENT)
async def _buy(ctx: RunContext[AgentDeps], request: RestockRequest):
    """Ask replenishment to buy in the shortfalls sales refused for want of stock.

    Every purchase this business makes arrives here, and every one of them is
    traceable to a customer who asked for something we did not have. What comes
    back is the items now on their way to us and the date each can be promised
    from; a need it refused is absent, and its reason comes up as a blocker.

    Not registered as a tool, for the reason `_commit` is not: the purse opens
    on a decline, not on a model deciding to open it.

    Args:
        ctx: The run context, carrying the trail and the journal.
        request: The shortfalls, the date, the deadline and what this request
            has already earned us.

    Returns:
        Replenishment's envelope, for the seam to narrow.
    """
    return await replenishment_agent.run(
        f"The request arrived on {request.request_date.isoformat()}. "
        "Buy what we were refused for want of stock:\n"
        + request.model_dump_json(),
        deps=ctx.deps,
        usage=ctx.usage,
        model=shared_model(),
    )


def _ask(instruction: str, lines: list[BaseModel], as_of_date: str) -> str:
    """The prompt a delegation sends: what to do, the date, and the lines as JSON.

    Every delegation asks the same shape of question about the same list of
    lines, and the lines travel as their own JSON rather than as prose, so the
    delegate parses a model it already knows instead of re-reading English.

    Args:
        instruction: What the delegate is being asked to do with the lines.
        lines: The lines, whichever line model this delegation carries.
        as_of_date: The date the request arrived, as `YYYY-MM-DD`.

    Returns:
        The prompt.
    """
    return (
        f"The request arrived on {as_of_date}. {instruction}:\n"
        + "\n".join(line.model_dump_json() for line in lines)
    )


#: How many model requests one customer request may spend, across the
#: orchestrator and everything it delegates to. A model that loops instead of
#: answering is the failure this bounds, and it is a real one: an early run
#: watched one request call `check_stock` eighty-two times and take the other
#: nineteen requests down with it. Thirty is about twice a healthy request's
#: cost at its longest — the orchestrator's own turns plus a handful each for
#: inventory, quoting and sales, and, on a request that goes short,
#: replenishment and sales again.
REQUEST_BUDGET = UsageLimits(request_limit=30)

_log = logging.getLogger(__name__)

APOLOGY = (
    "Thank you for your enquiry. We are sorry — we were unable to process your "
    "request automatically, and a member of our team will follow it up with you "
    "directly."
)


@functools.cache
def trail() -> AuditTrail:
    """The audit trail this process writes to, minted once per run.

    The harness has no notion of a run, so the first request to be handled
    names it and every later one joins it.

    Returns:
        The trail, stamped with this run's id.
    """
    return AuditTrail(run_id=new_run_id())


async def handle_request(
    request_with_date: str,
    request_date: str,
    request_id: int,
) -> RequestResolution:
    """Handle one customer request and state how it ended.

    Args:
        request_with_date: The customer's prose with the request date appended,
            exactly as the harness composes it. The `job` and `event` columns
            are deliberately not passed — they sharpen tone but carry no
            decision value.
        request_date: The ISO date the request arrived, which is also the date
            any resulting transaction is booked on.
        request_id: The harness's 1-based index for the request.

    Returns:
        The outcome, the one message the customer reads, and a resume token if
        the request paused for a revision.
    """
    run_trail = trail()
    deps = AgentDeps(trail=run_trail, request_id=str(request_id))
    try:
        result = await orchestrator_agent.run(
            f"{request_with_date}\n\nThe date of this request is {request_date}.",
            deps=deps,
            model=shared_model(),
            usage_limits=REQUEST_BUDGET,
        )
        outcome = derive_outcome(deps.journal)
        token = None
        if outcome is Outcome.PENDING_CUSTOMER_REVISION:
            flow = suspend(
                deps.journal,
                _as_request(deps, request_with_date, request_date),
            )
            run_trail.write_suspension(flow)
            token = flow.resume_token
    except Exception:
        # A crash is a bug, and a bug must not look like a business decision:
        # nothing here raises a blocker signal, and the trail already holds the
        # exception in `agent_steps.error`, which is where the two are told
        # apart. What it must also not do is end the evaluation — the harness
        # has no error handling of its own, so an escaping exception would take
        # the remaining requests with it.
        _log.exception("request %s failed", request_id)
        return RequestResolution(
            request_id=str(request_id),
            outcome=Outcome.REJECTED,
            customer_message=APOLOGY,
            resume_token=None,
        )
    finally:
        # In a `finally` because money that moved before a crash moved all the
        # same: a request that sold a line and then fell over is exactly the
        # one an auditor needs the row for, and it is the one path on which a
        # write at the end of the happy branch would never run.
        run_trail.write_cash(deps.request_id, deps.cash)
    return RequestResolution(
        request_id=str(request_id),
        outcome=outcome,
        # The whole of the leak guarantee, at the point it matters: the message
        # is the orchestrator's own letter, written from customer payloads and
        # code-only blockers. It cannot name a cash balance, a margin, a stock
        # count or an internal `detail` string, because none of them was ever
        # in its context to name.
        customer_message=result.output,
        resume_token=token,
    )


def _as_request(
    deps: AgentDeps, request_with_date: str, request_date: str
) -> CustomerRequest:
    """The request as it arrived, for a suspension to be resumed from.

    `deadline` comes from the journal, where `place_order` recorded what the
    orchestrator read out of the customer's prose. A request that suspends
    never reaches that step, so it carries none — and `None` is the honest
    answer there rather than a guessed one.

    Args:
        deps: This request's deps, carrying the trail and the journal.
        request_with_date: The customer's prose, exactly as it arrived.
        request_date: The ISO date it arrived on.

    Returns:
        The request, with the lines the orchestrator read out of it.
    """
    return CustomerRequest(
        request_id=deps.request_id,
        run_id=deps.trail.run_id,
        request_date=date.fromisoformat(request_date),
        raw_text=request_with_date,
        deadline=deps.journal.deadline,
        lines=deps.journal.requested_lines,
    )
