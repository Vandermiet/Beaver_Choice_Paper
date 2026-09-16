"""The sales agent's tools.

Arithmetic lives in tools, judgement lives in the model — so every
deterministic rule this agent applies is ordinary function code here, testable
without a model call. Sales has less judgement in it than any other agent: it
prices nothing, resolves nothing, and promises no date it was not handed. What
it does is read the shelf once, decide every line against that reading, and
move money.

Three of its four functions exist to work around what the provided helpers do
rather than to add a rule of our own:

- `snapshot_financials` wraps `generate_financial_report` because its
  `inventory_summary` carries the stock of every item we sell in a single call.
  That is the commit-time re-check — the point at which we stop trusting
  inventory's earlier survey, which was taken before quoting ran and before any
  restock landed.
- `record_sale` wraps `create_transaction`, whose `price` argument is the
  **line total** and not a unit price: `get_cash_balance` sums that column
  directly, so a unit price there would understate cash by 100-10,000x. It also
  runs `INSERT` and `SELECT last_insert_rowid()` as two statements through a
  pooled engine, and `last_insert_rowid()` is per-connection — hence the lock,
  which belongs here and never in the helper.
- `read_cash` wraps `get_cash_balance` as the post-write read. The delta
  against the snapshot's `cash_balance` is the rubric's evidence that a request
  moved money.

`record_sale` is deliberately **not** offered to the model, for the same reason
`record_quote` is not: its arguments are money, and a row that depends on a
model remembering to write it is a row the books cannot rely on. The output
function calls it, with the lines the verification passed.

Tool docstring convention: only the leading description, the parameter
descriptions and the *first* returns entry reach the model. `Raises`, `Notes`
and `Examples` are dropped, so anything a tool contract depends on is stated in
the description or in the parameter docs.
"""

import asyncio
from datetime import date

from sqlalchemy import text

from beaver import starter
from beaver.contract import CarriedItemName
from beaver.quoting.models import QuotedLine
from beaver.sales.models import FinancialSnapshot, LineDecision, LineVerdict

#: Serialises `create_transaction` across the whole process. The helper writes
#: the row and then asks the connection for `last_insert_rowid()`; between those
#: two statements a second writer on another pooled connection would leave the
#: first holding a rowid that belongs to someone else's sale. Nothing calls
#: sales concurrently today, and the lock is what makes that a choice rather
#: than a load-bearing assumption.
_write_lock = asyncio.Lock()


def snapshot_financials(
    item_names: list[CarriedItemName], as_of_date: str
) -> FinancialSnapshot:
    """Read the books at commit time: what we hold of each item, and our cash.

    This is the reading every line is judged against, and it is taken **now**
    rather than trusted from an earlier survey — stock may have been sold to a
    request handled minutes ago, or bought in since. Every name must be exactly
    as the carried catalogue spells it, so only ever pass names a colleague
    resolved.

    Args:
        item_names: The exact carried-catalogue names on this order.
        as_of_date: The date the request arrived, as `YYYY-MM-DD`.

    Returns:
        Stock for each item named, and the cash balance as of that date.
    """
    return read_financials(item_names, as_of_date)


def read_financials(item_names: list[str], as_of_date: str) -> FinancialSnapshot:
    """The snapshot itself, for code that must not take it from the model.

    `snapshot_financials` is what the model calls and what the trail records;
    this is what the envelope is built from, immediately before the writes. The
    two are separate reads on purpose: the authoritative one is the one taken
    in the same breath as the commitment, not the one taken a model turn
    earlier.

    Args:
        item_names: The exact carried-catalogue names on this order.
        as_of_date: The date to read as of, as `YYYY-MM-DD`.

    Returns:
        The snapshot, filtered to the items on this order.
    """
    report = starter.generate_financial_report(as_of_date)
    wanted = set(item_names)
    stock_by_item = {
        row["item_name"]: int(row["stock"])
        for row in report["inventory_summary"]
        if row["item_name"] in wanted
    }
    return FinancialSnapshot(
        as_of=date.fromisoformat(as_of_date),
        cash_balance=float(report["cash_balance"]),
        stock_by_item=stock_by_item,
    )


def read_cash(as_of_date: str) -> float:
    """What the business holds in cash, read after the writes.

    Args:
        as_of_date: The date to read as of, as `YYYY-MM-DD`.

    Returns:
        The cash balance.
    """
    return float(starter.get_cash_balance(as_of_date))


def verify_lines(
    lines: list[QuotedLine], stock_by_item: dict[str, int]
) -> list[LineDecision]:
    """Decide every line against one stock reading, before anything is written.

    The whole of the *verify all, then write* rule: this returns the fate of
    the entire order and touches nothing. Two lines naming the same item draw
    on the same shelf, in the order they were given, because one reading is
    what the commit-time re-check means — and a declined line takes nothing,
    because it ships nothing.

    A part-short line is declined whole. Splitting it would give one line two
    verdicts and two delivery dates, and the line is the indivisible unit of
    decision everywhere else in this design.

    Args:
        lines: The priced lines, exactly as quoting returned them.
        stock_by_item: What the commit-time snapshot says we hold, by item.

    Returns:
        One decision per line, in the order the lines were given.
    """
    remaining = dict(stock_by_item)
    decisions = []
    for line in lines:
        on_hand = remaining.get(line.item_name, 0)
        committed = line.units <= on_hand
        if committed:
            remaining[line.item_name] = on_hand - line.units
        decisions.append(
            LineDecision(
                line=line,
                verdict=LineVerdict.COMMITTED if committed else LineVerdict.DECLINED,
                stock_on_hand=on_hand,
                detail=(
                    f"{line.units} units of {line.item_name!r} committed against "
                    f"{on_hand} on hand at commit time"
                    if committed
                    else f"requested {line.units}, stock {on_hand}"
                ),
            )
        )
    return decisions


def promised_date(
    item_name: str, request_date: date, earliest_availability: dict[str, date]
) -> date:
    """When we will put this line in the customer's hands.

    A property of where the goods are, and of nothing else: what sits on our
    shelf is promised the day the request arrived, and what we had to buy in is
    promised the day it reaches us. The quantity does not enter — a supplier's
    lead time describes a purchase, and applying it to stock we already hold
    refuses orders we could fill today.

    Args:
        item_name: The exact carried-catalogue name.
        request_date: The date the request arrived.
        earliest_availability: Arrival dates for items bought in for this
            request, by item. Empty on the first pass, when nothing was bought.

    Returns:
        The date promised for this line.
    """
    return earliest_availability.get(item_name, request_date)


def order_total(lines: list[QuotedLine]) -> float:
    """What the committed lines come to, at quoting's prices.

    Args:
        lines: The committed lines.

    Returns:
        The sum of their line totals, to the cent.
    """
    return round(sum(line.line_total for line in lines), 2)


async def record_sale(
    lines: list[QuotedLine],
    run_id: str,
    request_id: str,
    step_id: str,
    sold_on: date,
) -> dict[str, int]:
    """Write the transaction registry: one row per committed line, under the lock.

    The order's rows are written as one batch while the lock is held, so no
    other writer can come between a row and the rowid it reports. Each rowid is
    then threaded back to the request that caused it, in `transaction_links`,
    and to the quote it fulfils, in `quote_fulfilments` — the `transactions`
    table's own `id` column is NULL for every row written at runtime, so the
    rowid is the only key there is to join on.

    Args:
        lines: The lines verification committed, and only those.
        run_id: The run the sale belongs to.
        request_id: The request the sale belongs to.
        step_id: The delegation that committed it.
        sold_on: The request date, per the as-of-date convention: the whole
            simulated timeline is as-of, and dating a sale today would put it
            outside the window the balance is read over.

    Returns:
        The `transactions` rowid of every line written, by `line_id`.
    """
    if not lines:
        return {}

    rowid_by_line: dict[str, int] = {}
    async with _write_lock:
        for line in lines:
            # Blocking, and deliberately run off the event loop: the lock is
            # what serialises it, so a second caller waits at the lock rather
            # than interleaving inside the helper's two statements.
            rowid_by_line[line.line_id] = await asyncio.to_thread(
                starter.create_transaction,
                line.item_name,
                "sales",
                line.units,
                # The line total, never the unit price: `get_cash_balance` sums
                # this column directly.
                line.line_total,
                sold_on.isoformat(),
            )

    _link_transactions(lines, rowid_by_line, run_id, request_id, step_id)
    return rowid_by_line


def _link_transactions(
    lines: list[QuotedLine],
    rowid_by_line: dict[str, int],
    run_id: str,
    request_id: str,
    step_id: str,
) -> None:
    """Join each written row back to the request, and to the quote it fulfils.

    Two tables and one write path: `transaction_links` answers "which customer
    caused this money to move?" and `quote_fulfilments` answers "which offer did
    we honour?". They are written together because they are two facts about one
    row, and a row that reached only one of them would be half-threaded.

    Args:
        lines: The committed lines.
        rowid_by_line: The rowid each of them was written at.
        run_id: The run the sale belongs to.
        request_id: The request the sale belongs to.
        step_id: The delegation that committed it.
    """
    with starter.engine().begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO transaction_links (
                  transaction_rowid, run_id, request_id, step_id
                ) VALUES (:transaction_rowid, :run_id, :request_id, :step_id)
                """
            ),
            [
                {
                    "transaction_rowid": rowid_by_line[line.line_id],
                    "run_id": run_id,
                    "request_id": request_id,
                    "step_id": step_id,
                }
                for line in lines
            ],
        )
        conn.execute(
            text(
                """
                INSERT INTO quote_fulfilments (transaction_rowid, quote_line_id)
                VALUES (:transaction_rowid, :quote_line_id)
                """
            ),
            [
                {
                    "transaction_rowid": rowid_by_line[line.line_id],
                    "quote_line_id": line.quote_line_id,
                }
                for line in lines
            ],
        )
