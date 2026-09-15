"""The quoting agent's tools.

Arithmetic lives in tools, judgement lives in the model — so every
deterministic rule this agent applies is ordinary function code here, testable
without a model call. In quoting that division is unusually stark: the ladder,
the rate and both totals are computed here, and the model's only judgement in
the whole agent is which search terms to feed `find_precedent`.

Two of the three tools work around a quirk of the provided data rather than
adding anything of their own:

- `catalogue_price` reads the `paper_supplies` list literal and never the
  database. A unit price is static reference data; a read implies it could vary
  between calls and it cannot. It also leaves `generate_financial_report` to
  sales and the `inventory` table to inventory, so each provided helper has one
  home rather than a contested one.
- `find_precedent` wraps the required `search_quote_history`, whose docstring
  says it matches *any* term while its code joins them with `AND`. A generous
  keyword list against a hundred rows of sales-rep prose is a guaranteed miss,
  so the wrapper degrades once and records the miss honestly.

`record_quote` is deliberately **not** offered to the model. Its arguments are
prices, and a registry row that depends on a model remembering to write it — or
on the numbers it reports back — is a row the books cannot rely on. The output
function calls it with what the arithmetic produced.

Tool docstring convention: only the leading description, the parameter
descriptions and the *first* returns entry reach the model. `Raises`, `Notes`
and `Examples` are dropped, so anything a tool contract depends on is stated in
the description or in the parameter docs.
"""

from datetime import date

from sqlalchemy import text

from beaver import starter
from beaver.contract import CarriedItemName
from beaver.quoting.models import (
    LADDER,
    CataloguePrice,
    DiscountBand,
    PrecedentComparison,
    QuotedLine,
)

#: How many past quotes a precedent lookup asks for. Five is the helper's own
#: default and plenty: the comparison is evidence that history was consulted,
#: not a sample anyone computes from.
PRECEDENT_LIMIT = 5


def catalogue_price(item_names: list[CarriedItemName]) -> list[CataloguePrice]:
    """Look up what one unit of each item sells for, from the product catalogue.

    A price is fixed reference data, identical on every call, and it is the
    price the business banks. Every name must be exactly as the carried
    catalogue spells it — anything else is refused rather than guessed at — so
    only ever pass names a colleague resolved.

    Args:
        item_names: The exact carried-catalogue names to price.

    Returns:
        One price per item, in the order the items were given.
    """
    return [
        CataloguePrice(item_name=item_name, unit_price=price_of(item_name))
        for item_name in item_names
    ]


def price_of(item_name: str) -> float | None:
    """The catalogue lookup itself, for code that must not raise on a miss.

    `catalogue_price` is what the model calls and what the trail records; this
    is what the envelope is built from. A miss here is the divergence between
    the carried catalogue and `paper_supplies` that `UNPRICEABLE` exists to
    catch, and a blocker signal is a better answer to it than an exception that
    ends the run.

    Args:
        item_name: The exact carried-catalogue name.

    Returns:
        The unit price, or `None` if the catalogue does not list the item.
    """
    for row in starter.paper_supplies:
        if row["item_name"] == item_name:
            return float(row["unit_price"])
    return None


def band_for_units(units: int) -> DiscountBand:
    """The rung of the ladder a line of this many units lands on.

    The ladder is read from the top down and stops at the first rung the line
    clears, which is what makes the bands exclusive: a 10,000-unit line earns
    15% and not 15% on top of the three rungs beneath it.

    Args:
        units: The units on the line.

    Returns:
        The band, and through it the rate.
    """
    for floor, band in LADDER:
        if units >= floor:
            return band
    return DiscountBand.NONE


def price_line(
    line_id: str,
    quote_line_id: str,
    item_name: str,
    units: int,
    unit_price: float,
) -> QuotedLine:
    """Price one resolved line: the catalogue price, the units, then the ladder.

    The whole of the business's pricing policy, in four lines of arithmetic, so
    that "why is this price what it is?" is answered by reading a function
    rather than a transcript. Nothing about stock, precedent or deliverability
    reaches it — those cannot move a price, and the way to guarantee that is to
    leave them out of the signature.

    Args:
        line_id: The requested line this prices.
        quote_line_id: The registry key, `run_id:request_id:line_id`.
        item_name: The exact carried-catalogue name.
        units: The units on the line.
        unit_price: The catalogue price of one unit.

    Returns:
        The priced line, band and rate included.
    """
    band = band_for_units(units)
    gross_total = round(units * unit_price, 2)
    return QuotedLine(
        line_id=line_id,
        quote_line_id=quote_line_id,
        item_name=item_name,
        units=units,
        unit_price=unit_price,
        band=band,
        discount_rate=band.rate,
        gross_total=gross_total,
        line_total=round(gross_total * (1 - band.rate), 2),
    )


def find_precedent(
    line_id: str, search_terms: list[str], limit: int = PRECEDENT_LIMIT
) -> PrecedentComparison:
    """Look for past quotes that resemble this line, after its price is fixed.

    Precedent is a comparison and never an instruction: it cannot change what
    the line costs, and finding none is an ordinary outcome that is recorded as
    one. Keep the terms **short** — every term must appear in the same past
    quote, so a long list finds nothing. If the list finds nothing, this retries
    once with the single most distinctive term and then stops.

    Past totals do not reconcile with our catalogue and are never arithmetic:
    they are evidence of how the business talks about its prices.

    Args:
        line_id: The line being compared.
        search_terms: A word or two from the item's name. Fewer is better.
        limit: How many past quotes to retrieve at most.

    Returns:
        What the history had to say, including whether it said nothing.
    """
    terms = _usable(search_terms)
    if not terms:
        # No conditions at all leaves the helper's `WHERE` as `1=1`, which
        # returns the five most recent quotes — precedent for nothing in
        # particular, dressed as a hit.
        return _comparison(line_id, [], [], degraded=False)

    rows = starter.search_quote_history(terms, limit)
    degraded = False
    if not rows and len(terms) > 1:
        terms = [_most_distinctive(terms)]
        degraded = True
        rows = starter.search_quote_history(terms, limit)
    return _comparison(line_id, terms, rows, degraded=degraded)


def _usable(search_terms: list[str]) -> list[str]:
    """The terms worth searching on: trimmed, de-duplicated, blanks dropped."""
    trimmed = (term.strip().lower() for term in search_terms)
    return list(dict.fromkeys(term for term in trimmed if term))


def _most_distinctive(terms: list[str]) -> str:
    """The one term to fall back to: the longest, ties broken alphabetically.

    Longest rather than rarest, because rarity would mean a corpus-wide count
    on every lookup to choose between "paper" and "glossy" — and in item names
    the longer word is the one that says which product it is.

    Args:
        terms: The terms that found nothing together.

    Returns:
        The term to search on alone.
    """
    return max(terms, key=lambda term: (len(term), term))


def _comparison(
    line_id: str, terms: list[str], rows: list[dict], *, degraded: bool
) -> PrecedentComparison:
    """Build the comparison from what the helper returned.

    Args:
        line_id: The line being compared.
        terms: The terms actually searched on.
        rows: The rows the helper returned, if any.
        degraded: Whether those terms are the fallback single term.

    Returns:
        The comparison, with a note that reads as evidence either way.
    """
    totals = [
        float(row["total_amount"])
        for row in rows
        # 5 of the 100 seeded totals are -1. An error row is not a price the
        # business ever charged, so it is not a comparison either.
        if row.get("total_amount") is not None and float(row["total_amount"]) > 0
    ]
    if not rows:
        note = f"no precedent found for {terms or 'this line'}"
    else:
        note = (
            f"{len(rows)} past quotes mention {terms}; their totals are house "
            f"voice rather than arithmetic and did not touch this price"
        )
    return PrecedentComparison(
        line_id=line_id,
        search_terms=terms,
        precedent_found=bool(rows),
        degraded_to_single_term=degraded,
        comparable_totals=totals,
        note=note,
    )


def record_quote(
    lines: list[QuotedLine], run_id: str, request_id: str, quoted_at: date
) -> list[str]:
    """Write the quote registry: one row per priced line, at quote time.

    Written *before* anyone rules on whether the line can be delivered, so the
    rows that never become transactions are the business's rejection history
    rather than a hole in its records. A record and never a channel: nothing
    reads these rows back within a run.

    Args:
        lines: The priced lines, exactly as they were quoted.
        run_id: The run the quote belongs to.
        request_id: The request the quote belongs to.
        quoted_at: The request date, per the as-of-date convention.

    Returns:
        The `quote_line_id` of every row written.
    """
    if not lines:
        return []
    with starter.engine().begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO quote_registry (
                  quote_line_id, run_id, request_id, line_id, item_name, units,
                  unit_price, band, discount_rate, line_total, quoted_at
                ) VALUES (
                  :quote_line_id, :run_id, :request_id, :line_id, :item_name, :units,
                  :unit_price, :band, :discount_rate, :line_total, :quoted_at
                )
                """
            ),
            [
                {
                    "quote_line_id": line.quote_line_id,
                    "run_id": run_id,
                    "request_id": request_id,
                    "line_id": line.line_id,
                    "item_name": line.item_name,
                    "units": line.units,
                    "unit_price": line.unit_price,
                    "band": str(line.band),
                    "discount_rate": line.discount_rate,
                    "line_total": line.line_total,
                    "quoted_at": quoted_at.isoformat(),
                }
                for line in lines
            ],
        )
    return [line.quote_line_id for line in lines]
