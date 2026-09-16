"""The quoting agent's payloads: its customer-facing half and its internal half.

From the consolidated model set on #10 as amended by #15, and from #7's
resolution. Three things here are load-bearing beyond their shape:

- **`discount_rate` is derived from `band`, never chosen.** Two fields
  describing one decision must not be able to disagree, which is the same
  argument `BlockerCode.revisable` and `ResolutionDecision.blocker_code` make.
- **The whole priced line is customer-safe.** #9 removed margin from the design,
  so there is nothing on a quote to hide: the unit price is the price we sell
  at, and the band is the reason the total is what it is. The risk on the
  rubric's leak axis runs the other way here — saying so little that the price
  reads as asserted rather than explained.
- **`PrecedentComparison` is internal.** Past quotes check the price and inform
  the house voice; they never move it. Keeping the comparison off the customer
  half is what makes "non-binding" structural rather than instructed.
"""

from enum import StrEnum

from pydantic import BaseModel

from beaver.contract import AgentResponse, CarriedItemName, InternalPayload


class DiscountBand(StrEnum):
    """The rung of the ladder a line lands on, measured in units on that line.

    Units and not value, because catalogue prices span fiftyfold: on a value
    basis an A4 buyer would need 10,000 sheets to reach the band a table-cover
    buyer reaches at 200, which is a discount for buying expensive things
    rather than for buying many.
    """

    NONE = "none"
    BULK = "bulk"
    VOLUME = "volume"
    WHOLESALE = "wholesale"

    @property
    def rate(self) -> float:
        """The discount this band earns, as a fraction of the gross total."""
        return DISCOUNT_RATE[self]


#: The ladder itself: the fewest units that reach each band. Read in descending
#: order, so a line lands on the highest band it clears and bands never
#: compound. The rates are anchored on the seeded history rather than invented —
#: of the 16 explanations that state a percentage, 15 say 10% and one says 15%,
#: so 10% is the house number and 15% is the reach.
LADDER: tuple[tuple[int, DiscountBand], ...] = (
    (10_000, DiscountBand.WHOLESALE),
    (2_000, DiscountBand.VOLUME),
    (500, DiscountBand.BULK),
    (0, DiscountBand.NONE),
)

#: What each band is worth. Separate from `LADDER` because the thresholds and
#: the rates answer different questions, and only this one reaches the registry.
DISCOUNT_RATE: dict[DiscountBand, float] = {
    DiscountBand.NONE: 0.00,
    DiscountBand.BULK: 0.05,
    DiscountBand.VOLUME: 0.10,
    DiscountBand.WHOLESALE: 0.15,
}


class CataloguePrice(BaseModel):
    """What the catalogue says one item sells for, as `catalogue_price` returns it.

    `unit_price` is optional for one reason only: a carried item the catalogue
    has no price for is the divergence `UNPRICEABLE` guards, and reporting it as
    an absence lets the agent raise that blocker. A tool that raised instead
    would end the run the guard exists to keep alive.
    """

    item_name: CarriedItemName
    unit_price: float | None


class QuotedLine(BaseModel):
    """One resolved line, priced. Customer-safe, and the shape sales reads.

    `gross_total` and `line_total` are both carried because the discount is
    only legible as the difference between them: the orchestrator's sentence is
    "$250.00, less 10% for volume — $225.00", and a payload that held only the
    net would make the model do arithmetic to write it.
    """

    line_id: str
    #: `run_id:request_id:line_id` — the same convention as the resume token, so
    #: the key carries its own provenance and joins to the audit trail and to
    #: `quote_fulfilments` on identifiers those already use.
    quote_line_id: str
    item_name: CarriedItemName
    units: int
    unit_price: float
    band: DiscountBand
    discount_rate: float
    gross_total: float
    line_total: float


class PrecedentComparison(BaseModel):
    """What the seeded quote history had to say about one line. Internal only.

    Consulted after the price is fixed and never permitted to move it. A miss
    is the ordinary outcome rather than a failure: `search_quote_history` ANDs
    its terms despite a docstring that says "any", and the totals it returns do
    not reconcile with the catalogue in any case.
    """

    line_id: str
    #: The terms actually searched on, which are the degraded single term when
    #: the full list missed.
    search_terms: list[str]
    precedent_found: bool
    degraded_to_single_term: bool
    #: Past totals, unreconciled and non-binding. Recorded because the rubric
    #: asks that history be considered, not that it be arithmetic.
    comparable_totals: list[float]
    note: str


class PrecedentProbe(BaseModel):
    """The search terms the model chose for one line.

    The agent's only judgement. Everything else quoting does is arithmetic, and
    the envelope is rebuilt from the tools rather than from what the model says
    they returned.
    """

    line_id: str
    search_terms: list[str]


class QuotingCustomerPayload(BaseModel):
    """What the orchestrator is handed: every priced line, and what they come to."""

    quoted_lines: list[QuotedLine]
    quote_total: float


class QuotingInternalPayload(InternalPayload):
    """What only the trail sees: the precedent checks and the rows we wrote."""

    precedents: list[PrecedentComparison]
    #: The `quote_line_id`s written to the registry, one per priced line. A
    #: record of the write, not a channel — nothing reads the registry back
    #: within a run.
    quote_rows_written: list[str]


QuotingResponse = AgentResponse[QuotingCustomerPayload, QuotingInternalPayload]
