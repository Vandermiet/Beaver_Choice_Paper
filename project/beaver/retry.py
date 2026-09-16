"""The bounded retry: what to buy, what to offer again, and how the two passes merge.

Filled by ticket 108. Everything here is the orchestrator's own reasoning about
a request that was short of stock, and it lives outside `orchestrator.py` for
the reason `beaver.outcome` does: none of it needs a model. Which lines to buy
for, which to offer a second time, and what the order finally came to are
arithmetic over the views the delegations handed up, so they are ordinary
functions a test can drive without spending a model call.

Three rules this module exists to make structural rather than hoped for:

- **The purse opens on sales' declines, not on inventory's survey.** The
  restock is sized by intersecting sales' pass-1 `INSUFFICIENT_STOCK` declines
  with inventory's `RestockNeed`s. The two sets are identical by construction —
  sales snapshots the books once and nothing writes to `transactions` between
  inventory's survey and that snapshot — so the intersection is provably
  lossless, and where they could diverge sales' commit-time read is the right
  one.
- **Pass 2 carries only lines we actually bought stock for.** A committed line
  is never offered for commitment twice, so the same sale can never be written
  twice; and a line whose restock was refused never reaches sales again, so it
  keeps the true reason it was refused instead of collecting a second
  `INSUFFICIENT_STOCK` for stock we deliberately did not buy.
- **The merge happens here and nowhere else.** The orchestrator is the only
  component that sees both passes, and `PlacedOrder` is the one shape its model
  writes the letter from — so there is no second definition of what the order
  came to.
"""

from datetime import date

from pydantic import BaseModel

from beaver.contract import BlockerCode, CustomerBlocker
from beaver.inventory.models import RestockNeed
from beaver.quoting.models import QuotedLine
from beaver.replenishment.models import RestockedItem
from beaver.sales.models import CommittedLine, DeclinedLine, SalesCustomerPayload


class PlacedOrder(BaseModel):
    """The whole order as the orchestrator finally saw it, across both passes.

    The same four facts a single sales pass carries, plus the blockers — because
    after a retry the code holding a line is no longer necessarily the one the
    pass that declined it raised, and the model writing the letter must be told
    the one that stands rather than the one that did.
    """

    committed: list[CommittedLine]
    declined: list[DeclinedLine]
    order_total: float
    #: The latest of the committed lines': an order is delivered when its last
    #: item arrives. `None` when nothing committed.
    promised_delivery_date: date | None
    blockers: list[CustomerBlocker]


def restocks_for(
    blockers: list[CustomerBlocker], needs: list[RestockNeed]
) -> list[RestockNeed]:
    """The shortfalls to buy in: sales' stock declines, as inventory measured them.

    An intersection rather than either set alone. Sales decides what we refused
    and inventory decides how short we were, and neither agent answers the
    other's question — so the orchestrator, which was handed both, is the only
    thing that can put them together.

    Args:
        blockers: What sales handed up from pass 1.
        needs: The shortfalls inventory measured, in the order it measured them.

    Returns:
        The needs whose lines sales declined for want of stock, in inventory's
        own order.
    """
    short = {
        blocker.line_id
        for blocker in blockers
        if blocker.code is BlockerCode.INSUFFICIENT_STOCK
    }
    return [need for need in needs if need.line_id in short]


def lines_to_retry(
    lines: list[QuotedLine], restocked: list[RestockedItem]
) -> list[QuotedLine]:
    """The priced lines to offer sales a second time: the ones we bought for.

    Not simply "the declined lines": a need replenishment refused as late or as
    unaffordable has no new stock behind it, and offering it again would take
    its true refusal away and replace it with a second `INSUFFICIENT_STOCK` for
    stock we chose not to buy.

    Args:
        lines: The priced lines pass 1 was given.
        restocked: The items now on their way to us, one per line.

    Returns:
        The lines to carry into pass 2, in their original order.
    """
    bought = {item.line_id for item in restocked}
    return [line for line in lines if line.line_id in bought]


def availability_of(restocked: list[RestockedItem]) -> dict[str, date]:
    """When each restocked item reaches us, keyed the way sales promises from.

    Args:
        restocked: The items now on their way to us.

    Returns:
        The arrival date by item name. Two lines served by one purchase share
        a date, so collapsing them to one key loses nothing.
    """
    return {item.item_name: item.available_from for item in restocked}


def merge_passes(
    first: SalesCustomerPayload,
    second: SalesCustomerPayload | None,
    spoken: dict[str, BlockerCode],
) -> PlacedOrder:
    """Put the two commitment passes together into the one order the customer reads.

    A line committed on pass 2 stops being declined; every other pass-1 decline
    stands. Nothing is counted twice because the two passes are disjoint by
    construction — pass 2 was only ever given lines pass 1 declined.

    Args:
        first: What sales committed and declined on pass 1.
        second: The same for pass 2, or `None` if there was no retry.
        spoken: The code each line is told about, by line id, as the journal's
            precedence resolved it across both passes.

    Returns:
        The merged order, with the blocker now standing on each declined line.
    """
    committed = [*first.committed, *(second.committed if second else [])]
    filled = {line.line_id for line in (second.committed if second else [])}

    declined = [line for line in first.declined if line.line_id not in filled]
    already = {line.line_id for line in declined}
    declined += [
        line
        for line in (second.declined if second else [])
        if line.line_id not in already
    ]

    return PlacedOrder(
        committed=committed,
        declined=declined,
        # Rounded because two passes' totals are two sums of cents, and the
        # letter states this figure to the cent.
        order_total=round(first.order_total + (second.order_total if second else 0.0), 2),
        promised_delivery_date=max(
            (line.promised_delivery_date for line in committed), default=None
        ),
        blockers=[
            CustomerBlocker(line_id=line.line_id, code=spoken[line.line_id])
            for line in declined
            if line.line_id in spoken
        ],
    )
