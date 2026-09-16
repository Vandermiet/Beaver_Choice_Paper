"""The orchestrator: one customer request in, one customer-facing reply out.

It never acts on a system of record. It plans and drives the sequence of
delegations, reads the signals that come back, and is the only component that
writes customer-facing prose.

Tickets 103-105 wired the first three delegations: the orchestrator extracts
catalogue-blind requested lines from the customer's prose, inventory resolves
them, quoting prices what resolved, sales commits what we hold and writes the
money, and the reply is composed here from what came back. Replenishment, the
bounded retry and the outcome derivation land in tickets 106-108.

Two boundaries this module exists to hold:

- **The orchestrator extracts; inventory resolves.** The lines it emits carry
  the customer's own words, the quantity as stated and the unit as stated. It
  knows no catalogue and invents no item name, because a hallucinated name
  fails downstream and a validator is the only thing that can prove it did not.
- **The orchestrator owns every customer-facing word.** Domain agents supply
  facts and codes and never prose, so tone cannot drift across four agents.
"""

import functools
import logging

from pydantic import BaseModel
from pydantic_ai import Agent, RunContext
from pydantic_ai.usage import UsageLimits

from beaver.audit import AgentDeps, AuditTrail, delegation, new_run_id
from beaver.contract import AgentName, RequestedLine
from beaver.inventory.agent import inventory_agent
from beaver.inventory.models import ResolvedLine
from beaver.llm import shared_model
from beaver.quoting.agent import quoting_agent
from beaver.quoting.models import QuotedLine
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

Then call `consult_sales` **once**, passing every priced line exactly as
quoting returned it, with the same date. It places the order: it answers with
the lines we committed and the date each will be delivered, the lines we could
not, and what the order came to. A line it could not commit comes back with one
more blocker:

- `insufficient_stock` — we do not hold enough of that to fill the line. Say
  that we are unable to supply that line at present, without saying how much we
  hold or how short we are, and never offer a partial quantity: the line was
  declined whole.

Skip this step only when nothing was priced.

Finally write the reply. Your entire answer **is** the letter — a short, warm,
professional message that a customer could read as it stands. Do not show your
working, do not list the request back with its line numbers, do not write
headings like "Requested Lines" or "Reply", and do not sign it with a
placeholder name: sign off as Beaver's Choice Paper Company.

Confirm the lines we can supply by the name we sell them as and the quantity
they asked for. Address every line we could not, in the customer's own terms,
and put all of your questions together in one place so one round of
correspondence clears them.

State the price of every line quoting priced, in its own words: how many
units, at what price each, and what that comes to. Where a line earned a
discount, say the rate and say what earned it — the size of that line's own
order — and give the total after it. Where a line earned none, say nothing
about discounts at all: a discount sentence on every line makes the real ones
invisible. Use the figures you were given exactly as they are, to the cent;
never calculate one, round one, or offer a discount that was not quoted.

State the delivery date sales gave you for the lines it committed, exactly as
it gave it to you. Never state a price for a line that was not priced, never
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
@delegation(AgentName.SALES)
async def consult_sales(
    ctx: RunContext[AgentDeps],
    lines: list[QuotedLine],
    as_of_date: str,
):
    """Ask sales to place the order: which priced lines can we commit today?

    Give it every line quoting priced, exactly as quoting returned it. Sales
    re-reads the shelf at the moment of commitment, so a line inventory saw
    stock for may still be declined — and a line it commits has been sold.

    Args:
        lines: The lines quoting priced, exactly as it returned them.
        as_of_date: The date the request arrived, as `YYYY-MM-DD`.

    Returns:
        The lines we committed with the date each is promised for, the lines we
        could not commit, and what the order came to.
    """
    return await sales_agent.run(
        _ask("Commit what we can of these priced lines", lines, as_of_date),
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
#: nineteen requests down with it. Twenty is about twice a healthy request's
#: cost with three delegations in the sequence — the orchestrator's own turns
#: plus a handful each for inventory, quoting and sales.
REQUEST_BUDGET = UsageLimits(request_limit=20)

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
) -> str:
    """Handle one customer request and return the reply the customer reads.

    Args:
        request_with_date: The customer's prose with the request date appended,
            exactly as the harness composes it. The `job` and `event` columns
            are deliberately not passed — they sharpen tone but carry no
            decision value.
        request_date: The ISO date the request arrived, which is also the date
            any resulting transaction is booked on.
        request_id: The harness's 1-based index for the request.

    Returns:
        The customer-facing reply.
    """
    deps = AgentDeps(trail=trail(), request_id=str(request_id))
    try:
        result = await orchestrator_agent.run(
            f"{request_with_date}\n\nThe date of this request is {request_date}.",
            deps=deps,
            model=shared_model(),
            usage_limits=REQUEST_BUDGET,
        )
    except Exception:
        # A crash is a bug, and a bug must not look like a business decision:
        # nothing here raises a blocker signal, and the trail already holds the
        # exception in `agent_steps.error`. What it must also not do is end the
        # evaluation — the harness has no error handling of its own, so an
        # escaping exception would take the remaining requests with it.
        _log.exception("request %s failed", request_id)
        return APOLOGY
    return result.output
