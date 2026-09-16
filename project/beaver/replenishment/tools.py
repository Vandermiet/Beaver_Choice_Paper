"""The replenishment agent's tools.

Arithmetic lives in tools, judgement lives in the model — so every
deterministic rule this agent applies is ordinary function code here, testable
without a model call. Replenishment has no judgement at all: the shortfall
arrives from inventory, the deadline from the customer, the floor from the
`inventory` table and the lead time from the supplier. What it does with them is
addition, two comparisons in a fixed order, and a write.

Four of its functions exist for reasons beyond the arithmetic:

- `reorder_thresholds` is the **only** access to `min_stock_level` anywhere in
  the system, because no required helper exposes it. It reads that column and
  `unit_price`, and deliberately **never `current_stock`**: `init_database`
  writes the `inventory` table once and nothing updates it at runtime, so that
  column still reads 272 for A4 paper after we have sold every sheet. Live
  stock exists only as a sum over `transactions`, and the question "how much do
  we hold?" is inventory's in any case.
- `supplier_cost_ratio` draws `U(0.6, 0.8)` **per item, from the item name**.
  There is no supplier price anywhere in the provided data — `init_database`
  buys the seeded stock at the catalogue price, the same one quoting sells at —
  so this ratio is the business's own invention. Keying the draw on the name
  rather than taking it from a sequential generator is what makes a run
  reproducible: the system is model-driven, so the number and order of restocks
  varies between runs, and a sequential draw would hand out different ratios
  each time. Two graders running identical code would report different
  financials.
- `restock_arrival_date` wraps `get_supplier_delivery_date`, read as
  *availability to promise* rather than as when the stock appears. The helper is
  sized by quantity, which is why a late refusal cannot buy a smaller part of
  the order and keep the date that justified it.
- `record_restock` wraps `create_transaction` through `beaver.ledger` — the same
  lock and the same line-total convention as sales, because the two agents write
  through one door and a lock per agent would serialise neither against the
  other.

`record_restock` is deliberately **not** offered to the model, for the same
reason `record_sale` and `record_quote` are not: its arguments are money, and a
row that depends on a model remembering to write it is a row the books cannot
rely on. The output function calls it, with the plans both guards passed.

Tool docstring convention: only the leading description, the parameter
descriptions and the *first* returns entry reach the model. `Raises`, `Notes`
and `Examples` are dropped, so anything a tool contract depends on is stated in
the description or in the parameter docs.
"""

import asyncio
from datetime import date
from random import Random

from sqlalchemy import text

from beaver import ledger, starter
from beaver.contract import CarriedItemName
from beaver.inventory.models import RestockNeed
from beaver.replenishment.models import (
    ReorderThreshold,
    RestockDecision,
    RestockPlan,
    RestockVerdict,
)

#: The seed every fact in the locked design was established against, and the
#: same one `init_database` selects the carried catalogue at. The cost ratios
#: are keyed off it so that re-seeding the database and re-drawing the ratios
#: are one decision rather than two that can drift.
SEED = 137

#: The band the supplier's standing rate falls in, as a fraction of our shelf
#: price. Introduced to make the simulation's cash behave like a trading
#: business rather than one buying at the price it sells at.
COST_RATIO_BAND = (0.6, 0.8)


def reorder_thresholds(item_names: list[CarriedItemName]) -> list[ReorderThreshold]:
    """Look up the stock floor we restock to for each item, and its shelf price.

    The floor is a property of the item and never changes, so this is reference
    data rather than a reading of our position — it says nothing about how much
    we currently hold. Every name must be exactly as the carried catalogue
    spells it, and a name this does not find is refused rather than skipped, so
    only ever pass names a colleague resolved.

    Args:
        item_names: The exact carried-catalogue names to look up.

    Returns:
        One floor and shelf price per item, in the order the items were given.
    """
    with starter.engine().connect() as conn:
        rows = conn.execute(
            text(
                # `current_stock` is deliberately not selected: see the module
                # docstring. Asking for it here is the one way this agent could
                # start answering inventory's question.
                "SELECT item_name, min_stock_level, unit_price FROM inventory"
            )
        ).fetchall()
    by_name = {row.item_name: row for row in rows}
    # Not skipped: a name that passed `CarriedItemName` and is absent from the
    # `inventory` table means the catalogue and the table have diverged, which
    # is a bug rather than a business outcome — and there is no blocker code for
    # it. `audit` is explicit that the two must not be collapsed, so this
    # raises and lands in `agent_steps.error` rather than quietly buying less
    # than the batch asked for.
    missing = [name for name in item_names if name not in by_name]
    if missing:
        raise ValueError(f"no inventory row for {missing!r}")
    return [
        ReorderThreshold(
            item_name=item_name,
            min_stock_level=int(by_name[item_name].min_stock_level),
            unit_price=float(by_name[item_name].unit_price),
        )
        for item_name in item_names
    ]


def cash_available(as_of_date: date) -> float:
    """What the business holds in cash, read before any purchase is made.

    This is the cash guard's read. It is the whole balance, including money
    this very request has just brought in — excluding that is the caller's job,
    because only the caller knows what the request earned.

    Args:
        as_of_date: The date to read as of.

    Returns:
        The cash balance.
    """
    return float(starter.get_cash_balance(as_of_date.isoformat()))


def restock_arrival_date(from_date: date, quantity: int) -> date:
    """When an order of this size, placed on this date, reaches *us*.

    The supplier's lead time grows with the quantity, so this is a fact about
    the purchase and not about the item. It is the date we may promise the
    customer from, and never the date the stock appears on our books.

    Args:
        from_date: The date the order is placed.
        quantity: The units being bought.

    Returns:
        The date the goods reach us.
    """
    return date.fromisoformat(
        starter.get_supplier_delivery_date(from_date.isoformat(), quantity)
    )


def supplier_cost_ratio(item_name: str) -> float:
    """The fraction of our shelf price we pay the supplier for this item.

    Stable for a given item across every call and every run, and different
    between items: our supplier holds a standing rate per product, not one that
    changes by whim.

    Args:
        item_name: The exact carried-catalogue name.

    Returns:
        A ratio in `COST_RATIO_BAND`.
    """
    return Random(f"{SEED}:{item_name}").uniform(*COST_RATIO_BAND)


def order_quantity(shortfall_units: int, min_stock_level: int) -> int:
    """How much to buy: the order served, and the floor standing again after it.

    Measured **after** the sale. Measured before, the order we just served
    would immediately eat the floor we just bought and we would be below
    minimum the moment we ship.

    Args:
        shortfall_units: The units the line is short by, as inventory computed
            them. Never re-derived here.
        min_stock_level: The item's stock floor.

    Returns:
        The units to buy.
    """
    return shortfall_units + min_stock_level


def plan_restocks(
    needs: list[RestockNeed],
    thresholds: list[ReorderThreshold],
    request_date: date,
) -> list[RestockPlan]:
    """Size, cost and date one purchase per item, before either guard has ruled.

    Everything deterministic about a purchase is known here, which is what lets
    both guards be comparisons rather than attempts.

    **Needs naming one item become one purchase.** Two lines of a request can
    resolve to the same product, and each arrives as its own `RestockNeed`. One
    purchase per need would buy that item's floor once per line — two floors for
    one shelf — and a purchase is in any case a thing we do with a supplier
    about a product, not about a line. The shortfalls are summed and the floor
    is added once.

    A need naming an item the thresholds do not cover raises, for the reason
    `reorder_thresholds` raises: it is a divergence between the catalogue and
    the `inventory` table, and buying less than the batch asked for without
    saying so is the one answer that is worse than stopping.

    Args:
        needs: The shortfalls, as inventory emitted them.
        thresholds: The floors and shelf prices for the items named.
        request_date: The date the request arrived, which is also the date any
            purchase is booked on.

    Returns:
        One plan per distinct item, in the order the items were first named.
    """
    threshold_by_item = {threshold.item_name: threshold for threshold in thresholds}
    missing = [need.item_name for need in needs if need.item_name not in threshold_by_item]
    if missing:
        raise ValueError(f"no reorder threshold for {missing!r}")

    shortfall_by_item: dict[str, int] = {}
    lines_by_item: dict[str, list[str]] = {}
    for need in needs:
        shortfall_by_item[need.item_name] = (
            shortfall_by_item.get(need.item_name, 0) + need.shortfall_units
        )
        lines_by_item.setdefault(need.item_name, []).append(need.line_id)

    plans = []
    for item_name, shortfall_units in shortfall_by_item.items():
        threshold = threshold_by_item[item_name]
        order_qty = order_quantity(shortfall_units, threshold.min_stock_level)
        unit_cost = supplier_cost_ratio(item_name) * threshold.unit_price
        plans.append(
            RestockPlan(
                line_ids=lines_by_item[item_name],
                item_name=item_name,
                order_qty=order_qty,
                unit_cost=unit_cost,
                # Rounded here and nowhere else: this is the figure that leaves
                # the bank.
                spend=round(order_qty * unit_cost, 2),
                arrival_date=restock_arrival_date(request_date, order_qty),
            )
        )
    return plans


def decide_restocks(
    plans: list[RestockPlan], deadline: date | None, cash_on_hand: float
) -> list[RestockDecision]:
    """Rule on every plan — deadline first, then cash — and write nothing.

    The order of the two guards is the whole of the precedence rule: **if we
    are not buying, there is nothing to afford.** It also keeps the cash
    guard's expected-zero honest, so that a `CASH_INSUFFICIENT` row means what
    it says — a purchase we actually wanted to make and could not fund.

    The cash guard is applied per item, **cheapest first**: refusing A4 paper
    because banner rolls were unaffordable would decline a line we could have
    served. Within an item it is all-or-nothing, because half of what a line
    needs is money out with the line still declined.

    Evaluation runs cheapest-first; the decisions come back in the order the
    plans were given, because that is the order the trail reads in.

    Args:
        plans: The sized, costed, dated plans.
        deadline: The date the customer needs the goods by, or `None`.
        cash_on_hand: What the guard may spend — the balance less any revenue
            this request itself has just brought in.

    Returns:
        One decision per plan, in the order the plans were given.
    """
    decision_by_item: dict[str, RestockDecision] = {}
    in_time = []
    for plan in plans:
        if deadline is not None and plan.arrival_date > deadline:
            decision_by_item[plan.item_name] = RestockDecision(
                plan=plan,
                verdict=RestockVerdict.REFUSED_LATE,
                detail=(
                    f"{plan.order_qty} units of {plan.item_name!r} would arrive "
                    f"{plan.arrival_date.isoformat()}, after the {deadline.isoformat()} "
                    f"deadline; nothing bought, floor included"
                ),
            )
        else:
            in_time.append(plan)

    remaining = cash_on_hand
    for plan in sorted(in_time, key=lambda plan: plan.spend):
        if plan.spend <= remaining:
            remaining -= plan.spend
            decision_by_item[plan.item_name] = RestockDecision(
                plan=plan,
                verdict=RestockVerdict.RESTOCKED,
                detail=(
                    f"bought {plan.order_qty} units of {plan.item_name!r} for "
                    f"{plan.spend:.2f}, arriving {plan.arrival_date.isoformat()}"
                ),
            )
        else:
            # Not a break: the guard is per item, and continuing is what makes
            # that provable rather than incidental to the sort order.
            decision_by_item[plan.item_name] = RestockDecision(
                plan=plan,
                verdict=RestockVerdict.REFUSED_CASH,
                detail=(
                    f"{plan.order_qty} units of {plan.item_name!r} would cost "
                    f"{plan.spend:.2f} against {remaining:.2f} left to spend"
                ),
            )

    return [decision_by_item[plan.item_name] for plan in plans]


async def record_restock(
    plans: list[RestockPlan],
    run_id: str,
    request_id: str,
    step_id: str,
    bought_on: date,
) -> dict[str, int]:
    """Buy the stock: one `stock_orders` row per purchase, under the shared lock.

    Money out and stock in, in one row and on one date. The rows are written
    while the ledger's lock is held — the one every writer shares — so no other
    writer can come between a row and the rowid it reports, and each row is
    threaded back to the request that caused it before the next is written.
    Every purchase this business makes is traceable to a customer who asked for
    something we did not have.

    Args:
        plans: The plans both guards passed, and only those.
        run_id: The run the purchase belongs to.
        request_id: The request the purchase belongs to.
        step_id: The delegation that bought it.
        bought_on: The request date. The stock lands the day we pay for it, so
            a commit-time re-check on that date can see it; the supplier's lead
            time is carried forward into the delivery promise instead.

    Returns:
        The `transactions` rowid of every purchase written, by `line_id`. Two
        lines served by one purchase share its rowid.
    """
    if not plans:
        return {}

    rowid_by_line: dict[str, int] = {}
    async with ledger.write_lock:
        for plan in plans:
            rowid = await ledger.write_row(
                plan.item_name,
                "stock_orders",
                plan.order_qty,
                # The total spend, never the unit cost: `get_cash_balance`
                # subtracts this column directly.
                plan.spend,
                bought_on,
            )
            for line_id in plan.line_ids:
                rowid_by_line[line_id] = rowid
            # Off the event loop, as sales does: the lock is what serialises
            # the writers, so a blocking call under it must not also block the
            # loop.
            await asyncio.to_thread(
                ledger.link_to_request, rowid, run_id, request_id, step_id
            )
    return rowid_by_line
