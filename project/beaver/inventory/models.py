"""The inventory agent's payloads: its customer-facing half and its internal half.

From the consolidated model set on #10 as amended by #15. Three things here are
load-bearing beyond their shape:

- **`item_name` is customer-safe and `stock_on_hand` is not.** The exact
  catalogue name is what we sell; how much of it sits on our shelf is our
  books. So `ResolvedLine` crosses the seam and `LineStockFact` does not.
- **`RestockNeed` carries the shortfall, not the reading it came from.** It is a
  fact about the customer's own order, which is why it may travel on the
  customer side at all, and replenishment sizes its purchase from it directly
  rather than re-deriving it from a stock count it would have to be handed.
- **`ResolutionTrace` is internal.** "Why did it pick `Glossy paper`?" should be
  answerable by `GROUP BY decision`, not by reading a transcript.
"""

from datetime import date
from enum import StrEnum

from pydantic import BaseModel

from beaver.contract import AgentResponse, BlockerCode, CarriedItemName, InternalPayload


class ProductCategory(StrEnum):
    """The four categories `paper_supplies` sorts the product universe into.

    The category guard is a comparison between two of these, so they are an
    enum rather than a loose string: a typo would silently disable the rule.
    """

    PAPER = "paper"
    PRODUCT = "product"
    SPECIALTY = "specialty"
    LARGE_FORMAT = "large_format"


class ResolutionDecision(StrEnum):
    """How one requested line was decided, as the trace records it.

    Six of the seven correspond to a blocker code; `RESOLVED` is the one that
    does not, because a line that resolved raised nothing.
    """

    RESOLVED = "resolved"
    SIZE_VETO = "size_veto"
    CATEGORY_GUARD = "category_guard"
    NO_CANDIDATE = "no_candidate"
    AMBIGUOUS = "ambiguous"
    QUANTITY_MISSING = "quantity_missing"
    UNIT_NOT_UNDERSTOOD = "unit_not_understood"

    @property
    def blocker_code(self) -> BlockerCode | None:
        """The code this decision raises, or `None` if the line resolved.

        Derived here rather than chosen by the agent, for the same reason
        revisability is derived from the code: two enums describing one event
        must not be able to disagree.
        """
        return _CODE_BY_DECISION.get(self)


_CODE_BY_DECISION: dict[ResolutionDecision, BlockerCode] = {
    ResolutionDecision.SIZE_VETO: BlockerCode.SIZE_NOT_CARRIED,
    ResolutionDecision.CATEGORY_GUARD: BlockerCode.ITEM_NOT_CARRIED,
    ResolutionDecision.NO_CANDIDATE: BlockerCode.ITEM_NOT_CARRIED,
    ResolutionDecision.AMBIGUOUS: BlockerCode.ITEM_AMBIGUOUS,
    ResolutionDecision.QUANTITY_MISSING: BlockerCode.QUANTITY_MISSING,
    ResolutionDecision.UNIT_NOT_UNDERSTOOD: BlockerCode.UNIT_NOT_UNDERSTOOD,
}


class CarriedItem(BaseModel):
    """One row of the carried catalogue, as `list_carried_catalogue` returns it.

    Carried and stocked are different answers: an item with `stock_on_hand` of
    zero is still on offer, and still appears here.
    """

    item_name: CarriedItemName
    category: ProductCategory
    stock_on_hand: int


class StockReading(BaseModel):
    """What we hold of one item on one date, from `get_stock_level`."""

    item_name: CarriedItemName
    stock_on_hand: int
    as_of: date


class NameVerdict(BaseModel):
    """The deterministic verdict on one requested line's *name*.

    Everything the four resolution rules decide is decided here, in ordinary
    function code — the size veto, the category guard, the default plain-paper
    item, and the count of survivors. Quantity and unit are judged after, and
    only on a line whose name resolved.
    """

    item_as_stated: str
    decision: ResolutionDecision
    resolved_item: str | None
    carried_candidates: list[str]
    universe_best_match: str | None
    universe_best_category: ProductCategory | None
    sizes_named: list[str]
    unrecognised_size: str | None
    selecting_terms: list[str]
    #: Why the decision went the way it did, in words, for the blocker's
    #: internal `detail`. Never rendered to a customer.
    detail: str


class LineVerdict(BaseModel):
    """One requested line's decision, name and count together.

    The order the three checks run in is the whole of the *one blocker per
    line* rule: the name is judged first, then the quantity, then the unit. A
    line that names something we do not sell and counts it in reams has failed
    once, not three times.
    """

    line_id: str
    decision: ResolutionDecision
    resolved_item: str | None
    category: ProductCategory | None
    quantity: int | None
    #: The name's own verdict, kept whole. It is not the same answer: a line
    #: whose name resolved can still fail on its quantity or its unit, and then
    #: this says `resolved` while the line says why it failed anyway.
    name_verdict: NameVerdict
    detail: str


class ResolvedLine(BaseModel):
    """A requested line matched to an exact name we sell. Customer-safe."""

    line_id: str
    item_name: CarriedItemName
    category: ProductCategory
    quantity: int


class RestockNeed(BaseModel):
    """A shortfall as it travels: the lines, the item, and the units short.

    Customer-safe because it is a fact about the customer's own order rather
    than about our books — the orchestrator routes it to replenishment, which
    sizes its purchase from `shortfall_units` and never re-reads stock.

    **One need per item, not per line.** Two lines of a request can resolve to
    the same product — "printer paper" and "copy paper" are both `A4 paper` —
    and sales draws both from one shelf, in order. A need per line would
    measure each against the full reading and so subtract the stock once per
    line, understating the true requirement by `(n-1) x stock_on_hand`; worse,
    two lines that each fit the shelf alone but not together would raise no
    need at all while sales declined the second. So the shortfall is
    `sum(quantity) - stock_on_hand` over the lines naming one item, and
    `line_ids` records every line it covers — mirroring `RestockPlan`, which
    is already one purchase per item.
    """

    #: Every line this shortfall covers, in the order they arrived. Usually
    #: one.
    line_ids: list[str]
    item_name: CarriedItemName
    shortfall_units: int


class LineStockFact(BaseModel):
    """The raw readings behind one resolved line. Internal only.

    `stock_on_hand` is the number that must not cross the seam; the shortfall
    derived from it does, as a `RestockNeed`.
    """

    line_id: str
    item_name: CarriedItemName
    quantity_requested: int
    stock_on_hand: int
    as_of: date


class ResolutionTrace(BaseModel):
    """How one requested line became, or failed to become, a resolved item."""

    line_id: str
    item_as_stated: str
    carried_candidates: list[str]
    universe_best_match: str | None
    universe_best_category: ProductCategory | None
    decision: ResolutionDecision


class InventoryCustomerPayload(BaseModel):
    """What the orchestrator is handed: what we sell them, and what we are short of."""

    resolved_lines: list[ResolvedLine]
    restock_needs: list[RestockNeed]


class InventoryInternalPayload(InternalPayload):
    """What only the trail sees: the stock readings and the resolution traces."""

    as_of_date: date
    stock_facts: list[LineStockFact]
    traces: list[ResolutionTrace]


InventoryResponse = AgentResponse[InventoryCustomerPayload, InventoryInternalPayload]
