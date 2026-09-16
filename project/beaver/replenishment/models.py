"""The replenishment agent's payloads: its customer-facing half and its internal half.

From the consolidated model set on #10 as amended by #15. Four things here are
load-bearing beyond their shape:

- **`RestockRequest` carries `needs`, never a stock reading.** Inventory owns
  the shortfall and replenishment consumes it; the reading it was derived from
  never leaves `InventoryInternalPayload`, so replenishment structurally cannot
  answer a question inventory has already answered.
- **`RestockVerdict` names the cause, not just the refusal.** #15 split
  `REFUSED` into `REFUSED_CASH` and `REFUSED_LATE` so that one
  `blocker_signals` row states one cause — a trail whose `GROUP BY` cannot
  distinguish "could not afford it" from "could not get it in time" is a trail
  that answers neither question.
- **`RestockedItem.available_from` is customer-safe; every figure in
  `ReplenishmentInternalPayload` is not.** What we paid our supplier, and what
  fraction of our shelf price that is, is the most disclosable-looking and least
  disclosable thing in the system: it is the one number from which a margin
  could be inferred, and the business claims none.
- **`RestockDecision.plan` is kept whole.** A refused need was still sized,
  costed and dated before the guard turned it down, and the trail wants those
  numbers — `DEADLINE_UNMEETABLE` is only legible beside the arrival date that
  justified it.
"""

from datetime import date
from enum import StrEnum

from pydantic import BaseModel

from beaver.contract import AgentResponse, BlockerCode, CarriedItemName, MovesCash
from beaver.inventory.models import RestockNeed


class RestockVerdict(StrEnum):
    """What replenishment decided about one need. Three outcomes, per #15.

    The two refusals are separate values rather than one `REFUSED` with a
    reason string beside it, because each maps to its own blocker code and the
    codes are what the rubric's refusal count is read from.
    """

    RESTOCKED = "restocked"
    REFUSED_CASH = "refused_cash"
    REFUSED_LATE = "refused_late"

    @property
    def blocker_code(self) -> BlockerCode | None:
        """The code this verdict raises, or `None` if we bought the stock.

        Derived here rather than chosen by the agent, for the same reason
        inventory derives a code from a `ResolutionDecision`: two enums
        describing one event must not be able to disagree.
        """
        return _CODE_BY_VERDICT.get(self)


_CODE_BY_VERDICT: dict[RestockVerdict, BlockerCode] = {
    RestockVerdict.REFUSED_LATE: BlockerCode.DEADLINE_UNMEETABLE,
    RestockVerdict.REFUSED_CASH: BlockerCode.CASH_INSUFFICIENT,
}


class RestockRequest(BaseModel):
    """What the orchestrator asks replenishment to buy, and by when.

    #10's and #15's model carried `request_id`, `run_id` and `step_id` as
    well. They are dropped here for the reason sales' `SalesOrderRequest` was
    dissolved into arguments: all three are already on `AgentDeps`, the seam
    validates the `step_id` the agent echoes back, and a field the model fills
    from prose is a field the model can fill wrongly. What remains is the part
    only the orchestrator knows.

    `deadline` is #15's amendment and the whole of the guard that closed the
    speculative-restock defect: without it replenishment bought blind to the
    one fact that decides whether the purchase does its job.

    `committed_revenue` is the cash guard's exclusion, and it is not in the
    locked model set — see `decide_restocks`. The orchestrator routes it from
    sales' pass-1 `order_total`, a figure already on the customer side. It has
    no default on purpose: a zero default would let the wiring ticket forget to
    route it and degrade the guard silently, which is the one failure a guard
    must not have.
    """

    request_date: date
    deadline: date | None
    needs: list[RestockNeed]
    #: What this request has already paid us on pass 1. Excluded from cash on
    #: hand, because the business cannot spend the same money twice in the same
    #: breath as earning it.
    committed_revenue: float


class ReorderThreshold(BaseModel):
    """One item's stock floor and shelf price, as `reorder_thresholds` returns it.

    Deliberately not `CarriedItem`: that model carries `stock_on_hand`, read
    from `inventory.current_stock`, which `init_database` writes once and
    nothing updates at runtime. It would still read 272 for A4 paper after we
    had sold every sheet, and replenishment has no business asking.
    """

    item_name: CarriedItemName
    #: The tripwire the restock sizes to, not a trigger: nothing watches it.
    min_stock_level: int
    #: Our shelf price, from the same row. What we pay our supplier is a
    #: fraction of it — see `supplier_cost_ratio`.
    unit_price: float


class RestockPlan(BaseModel):
    """One purchase, sized and costed and dated, before any guard has ruled on it.

    Every deterministic thing replenishment knows about a purchase is here, and
    it is all known before the money moves — which is what lets the two guards
    be comparisons rather than attempts.

    **One plan per item, not per need.** Two lines of one request can resolve
    to the same product — "printer paper" and "copy paper" are both `A4 paper`.
    Inventory measures its shortfall per item for the same reason (#36), so a
    request yields one need per item; grouping again here is what keeps that
    true of a batch that names an item twice. Buying per need would buy the
    floor twice for one item, which is not what a target stock level is, and
    would collide in every `*_by_item` field of the internal payload. A
    purchase is a thing we do with a supplier about a product, so that is what
    this is, and `line_ids` records every line it serves.
    """

    #: Every line this one purchase was made for, in the order the needs
    #: arrived. Usually one.
    line_ids: list[str]
    item_name: CarriedItemName
    #: `shortfall_units + min_stock_level`: the order served, and the floor
    #: standing again once it has shipped.
    order_qty: int
    #: `supplier_cost_ratio(item) * unit_price`, and deliberately **not**
    #: rounded to the cent: A4 paper sells at five cents, so a cent-rounded
    #: cost quantises a ratio drawn from `U(0.6, 0.8)` to 0.6 or 0.8 and
    #: destroys the variability the ratio exists to create. Money is rounded
    #: where money moves, which is `spend`.
    unit_cost: float
    #: `order_qty * unit_cost`, to the cent. This is the figure that reaches
    #: `create_transaction`, whose `price` argument is the line total.
    spend: float
    #: When the goods reach *us*, from `get_supplier_delivery_date`. Sized by
    #: the full order, which is why a late refusal cannot buy a smaller part of
    #: it and keep this date.
    arrival_date: date


class RestockDecision(BaseModel):
    """One purchase's verdict, with the plan that was weighed. Internal.

    The unit of *decide everything, then write*: both guards run over the whole
    batch and only then does anything reach `transactions`, for the same reason
    sales verifies every line before committing one — nothing in this database
    can be rolled back.
    """

    plan: RestockPlan
    verdict: RestockVerdict
    #: Why it went that way, for the blocker's internal `detail`. Never
    #: rendered to a customer, and `CASH_INSUFFICIENT` is never rendered at all.
    detail: str


class RestockedItem(BaseModel):
    """One line's item bought in, and the date it can be promised from. Customer-safe.

    One per line rather than per purchase, because sales promises a line and
    two lines sharing a purchase are still two lines to promise.

    It carries no quantity and no cost: how much we chose to buy is our
    business, and the customer's line is filled either way. What crosses the
    seam is the one fact sales needs to promise a date.
    """

    line_id: str
    item_name: CarriedItemName
    #: The supplier's arrival date, which sales promises and never beats.
    available_from: date


class ReplenishmentCustomerPayload(BaseModel):
    """What the orchestrator is handed: the items now on their way to us.

    A need that was refused is absent rather than listed, because the blocker
    the seam derives already names its line and its cause. Listing it again
    here would be the second statement of one event that #10 stripped from
    `DeclinedLine`.
    """

    restocked: list[RestockedItem]


class ReplenishmentInternalPayload(MovesCash):
    """What only the trail sees: what we bought, what we paid, and what we held.

    `unit_cost_by_item` is the one field in the system from which a margin
    could be computed. It is on this side of the envelope for exactly that
    reason — the business reports what cash moved and claims no profit figure,
    because stock bought before the supplier cost ratio existed and stock
    bought under it sit in the same bin at different costs.

    `cash_before` and `cash_after` come from `MovesCash`, which is what makes
    this step count towards the request's own cash delta.
    """

    #: What the guard was actually allowed to spend: `cash_before` less the
    #: revenue this request itself brought in on pass 1.
    cash_on_hand: float
    units_ordered_by_item: dict[str, int]
    unit_cost_by_item: dict[str, float]
    spend_by_item: dict[str, float]
    total_spend: float
    #: `line_id` -> `transactions.rowid`. The purchase half of the commitment
    #: invariant: exactly the needs in `restocked`, and no others.
    transaction_rowid_by_line: dict[str, int]
    verdict_by_line: dict[str, RestockVerdict]


ReplenishmentResponse = AgentResponse[
    ReplenishmentCustomerPayload, ReplenishmentInternalPayload
]
