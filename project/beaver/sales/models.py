"""The sales agent's payloads: its customer-facing half and its internal half.

From the consolidated model set on #10, as amended by #14 and #15. Four things
here are load-bearing beyond their shape:

- **A line is in `committed` iff it has a rowid in
  `transaction_rowid_by_line`.** Both are built from the same write result, so
  a committed line that moved no money — or money that belongs to no line — is
  unrepresentable rather than merely unlikely.
- **`DeclinedLine` carries no code.** The seam already hands the orchestrator
  `blockers: list[CustomerBlocker]`, which is code plus `line_id`; stating the
  code twice is two fields that can disagree about one event.
- **Cash sits on the internal half and nothing else says how much we hold.**
  `cash_before` and `cash_after` are the rubric's evidence that money moved and
  are written to the trail; the customer payload carries the order's own total
  and not a penny of the business's position.
- **The promise is a date, not a duration.** It is a property of where the
  goods are — `request_date` for a line off the shelf, the restock's arrival
  date for one we bought in — so sales stores the answer and never the lead
  time that produced it. Sales owns no deadline at all (#15).
"""

from datetime import date
from enum import StrEnum

from pydantic import BaseModel

from beaver.contract import AgentResponse, CarriedItemName, InternalPayload
from beaver.quoting.models import QuotedLine


class LineVerdict(StrEnum):
    """What sales decided about one line. Two outcomes, and no third.

    There is deliberately no `PARTIAL`: a part-short line is declined whole
    (#14 decision 4). Splitting it would cost `CommittedLine` its single
    `promised_delivery_date` and `record_sale` its one row per line, and would
    give one line two verdicts.
    """

    COMMITTED = "committed"
    DECLINED = "declined"


class FinancialSnapshot(BaseModel):
    """One read of the books at commit time, as `snapshot_financials` returns it.

    Cash and stock in a single call, which is the whole reason
    `generate_financial_report` has a home here: `inventory_summary` carries
    the stock of every item we sell, so the commit-time re-check is one query
    rather than one per line.
    """

    as_of: date
    cash_balance: float
    #: Stock on hand, keyed by the exact catalogue name. Filtered to the items
    #: on the order — the rest of the shelf is not this order's business.
    stock_by_item: dict[str, int]


class LineDecision(BaseModel):
    """One line's verdict, reached before any line is written. Internal.

    The unit of *verify all, then write*: the whole order is decided into these
    and only then does anything reach `transactions`. Nothing in this database
    can be rolled back, so a half-written order has no repair.
    """

    line: QuotedLine
    verdict: LineVerdict
    #: What the commit-time re-check said we held of this item, after the lines
    #: above it on the same order had taken their share.
    stock_on_hand: int
    #: Why it went that way, for the blocker's internal `detail`. Never
    #: rendered to a customer.
    detail: str


class CommittedLine(BaseModel):
    """One line the business has sold. Customer-safe, and money has moved.

    `line_total` is quoting's price, unchanged — sales prices nothing — and it
    is also the figure `create_transaction` is called with, because
    `get_cash_balance` sums that column directly.
    """

    line_id: str
    item_name: CarriedItemName
    units: int
    line_total: float
    promised_delivery_date: date


class DeclinedLine(BaseModel):
    """One line the business could not commit. Customer-safe, no code.

    The orchestrator holds the code already, on the blocker the seam derived,
    and joins the two on `line_id`.
    """

    line_id: str
    item_name: CarriedItemName
    units: int


class SalesCustomerPayload(BaseModel):
    """What the orchestrator is handed: what we sold, what we could not, and when.

    `promised_delivery_date` is the latest committed line's — an order is
    delivered when its last item arrives — and `None` when nothing committed,
    because a date for an order that does not exist is a promise about nothing.
    """

    committed: list[CommittedLine]
    declined: list[DeclinedLine]
    order_total: float
    promised_delivery_date: date | None


class SalesInternalPayload(InternalPayload):
    """What only the trail sees: the money, the stock read, and the rowids.

    `cash_after - cash_before` is one pass's cash movement. The per-request
    delta spans both passes of the bounded retry and is measured by the
    orchestrator, which is the only thing that sees both (#10).
    """

    cash_before: float
    cash_after: float
    #: The commit-time re-check, keyed by item. Every key is on this order.
    stock_read_by_item: dict[str, int]
    #: `line_id` -> `transactions.rowid`. The other half of the commitment
    #: invariant: exactly the lines in `committed`, and no others.
    transaction_rowid_by_line: dict[str, int]
    verdict_by_line: dict[str, LineVerdict]


SalesResponse = AgentResponse[SalesCustomerPayload, SalesInternalPayload]
