"""The quoting agent: its construction, its instructions, its tool registration.

The agent reads the resolved lines and drives the tools; the envelope is built
from what the tools say, not from what the model reports back about them. Here
that argument is stronger than anywhere else in the system, because the model's
only unchecked contribution is *which words to search the history for* — and
the history cannot move a price. Every number the customer is quoted is
computed in `price_line` from the catalogue price and the units.

So a model that misreads a catalogue price cannot mis-bill a line, and a model
that invents a precedent cannot discount one.
"""

from datetime import date

from pydantic_ai import Agent, RunContext
from pydantic_ai.toolsets import FunctionToolset

from beaver.audit import AgentDeps, AuditedToolset
from beaver.contract import AgentName, BlockerCode, BlockerSignal
from beaver.inventory.models import ResolvedLine
from beaver.quoting.models import (
    PrecedentProbe,
    QuotedLine,
    QuotingCustomerPayload,
    QuotingInternalPayload,
    QuotingResponse,
)
from beaver.quoting.tools import (
    catalogue_price,
    check_against_precedent,
    find_precedent,
    price_line,
    price_of,
    quote_line_id,
    quote_total,
    record_quote,
)

INSTRUCTIONS = """
You are the pricing desk of Beaver's Choice Paper Company. You answer one
question about each line you are given: what does it cost?

Every line you receive has already been matched to an exact product we sell, by
a colleague whose verdict is final. You do not resolve names, you do not read
stock, you do not decide whether an order can be delivered, and you never
recompute a price yourself — the arithmetic is the tools'.

Work in exactly three steps, and never repeat one:

1. Call `catalogue_price` once for each line, to see what the item sells for.
2. Call `find_precedent` once for each line, to see how the business has priced
   similar work before. Pass **one or two words** from the item's name and no
   more: every term you pass must appear in the same past quote, so a long list
   finds nothing. Finding nothing is a perfectly good answer — say so and move
   on; never search a third time for a line.
3. Return every line you were given, unchanged: the same `line_id`, the same
   `item_name`, the same `category`, the same `quantity`, plus the search terms
   you used for each line and the `as_of_date` you were given.

Do not drop a line because we may be short of it — whether we hold the stock is
someone else's question, and a line we are short of is still a line with a
price. Do not merge two lines, do not invent one, and do not adjust a price to
match something you found in the history: precedent explains prices, it never
sets them.

You write no prose for the customer. The itemisation you return is facts; the
orchestrator turns it into words.
""".strip()


async def build_quoting_response(
    ctx: RunContext[AgentDeps],
    lines: list[ResolvedLine],
    probes: list[PrecedentProbe],
    as_of_date: date,
) -> QuotingResponse:
    """Price every resolved line and build the envelope quoting returns.

    Args:
        lines: The resolved lines, exactly as they were given to you.
        probes: The search terms you used to look for precedent, one entry per
            line.
        as_of_date: The date the request arrived, as `YYYY-MM-DD`.

    Returns:
        The canonical envelope: every priced line, what they come to, and the
        precedent behind each.
    """
    deps = ctx.deps
    run_id = deps.trail.run_id
    terms_by_line = {probe.line_id: probe.search_terms for probe in probes}

    quoted: list[QuotedLine] = []
    precedents = []
    signals: list[BlockerSignal] = []

    for line in _one_per_line(lines):
        unit_price = price_of(line.item_name)
        if unit_price is None:
            # Unreachable by construction: `item_name` is validated against the
            # carried catalogue and every carried item has a catalogue price.
            # The guard exists so that a divergence between the two is a
            # recorded refusal rather than an exception that ends the run.
            signals.append(
                BlockerSignal(
                    line_id=line.line_id,
                    code=BlockerCode.UNPRICEABLE,
                    detail=(
                        f"{line.item_name!r} is carried but `paper_supplies` has no "
                        f"price for it: the catalogue and the validator have diverged"
                    ),
                )
            )
            continue

        priced = price_line(
            line_id=line.line_id,
            quote_line_id=quote_line_id(run_id, deps.request_id, line.line_id),
            item_name=line.item_name,
            units=line.quantity,
            unit_price=unit_price,
        )
        quoted.append(priced)
        # Retrieved after the price is fixed, and compared to it only to write
        # a note. Precedent has no argument through which it could move one.
        precedents.append(
            check_against_precedent(
                find_precedent(
                    line.line_id, terms_by_line.get(line.line_id) or _terms_of(line)
                ),
                priced.line_total,
            )
        )

    return QuotingResponse(
        agent=AgentName.QUOTING,
        step_id=deps.current_step_id,
        request_id=deps.request_id,
        customer=QuotingCustomerPayload(
            quoted_lines=quoted,
            quote_total=quote_total(quoted),
        ),
        internal=QuotingInternalPayload(
            precedents=precedents,
            quote_rows_written=record_quote(
                quoted, run_id=run_id, request_id=deps.request_id, quoted_at=as_of_date
            ),
            signals=signals,
        ),
    )


def _terms_of(line: ResolvedLine) -> list[str]:
    """The search terms to fall back on when the model supplied none for a line.

    The item's own name, which is the one description of the line that is
    certainly accurate. It will usually miss on the full list and degrade to
    its longest word, which is the behaviour a missing probe should have.

    Args:
        line: The resolved line.

    Returns:
        The words of the item name.
    """
    return line.item_name.lower().split()


def _one_per_line(lines: list[ResolvedLine]) -> list[ResolvedLine]:
    """The first report of each line, in the order they arrived.

    A model that reports one line twice would otherwise be quoted twice, and
    the second registry row would collide with the first on a primary key that
    is derived from the line id.

    Args:
        lines: The resolved lines as the model reported them.

    Returns:
        One line per `line_id`, in the order they arrived.
    """
    seen: dict[str, ResolvedLine] = {}
    for line in lines:
        seen.setdefault(line.line_id, line)
    return list(seen.values())


quoting_agent = Agent(
    deps_type=AgentDeps,
    output_type=build_quoting_response,
    instructions=INSTRUCTIONS,
    toolsets=[
        AuditedToolset(
            FunctionToolset(tools=[catalogue_price, find_precedent]),
            agent=AgentName.QUOTING,
        )
    ],
    name="quoting",
    # As inventory: a tool argument the model gets wrong is refused by the
    # validator and handed back to it, and an exact catalogue name needs more
    # than one attempt's room. Every number in the envelope is the tools' own,
    # so a wasted call costs a token and nothing else.
    retries=3,
)
