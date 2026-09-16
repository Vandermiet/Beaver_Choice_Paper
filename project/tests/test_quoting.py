"""The ladder, the precedent lookup, the registry, and the envelope they build.

The ladder is the customer's promise that the same order always earns the same
price, so it is tested at every boundary and without a model — the arithmetic
lives in tool code precisely so that it can be.

What needs a model is the wiring: quoting's envelope, and the second delegation
the orchestrator makes. Both run on a `FunctionModel`, so no network and no key.
"""

import json
from inspect import signature
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_core import to_jsonable_python
from sqlalchemy import text

from beaver import orchestrator, starter
from beaver.audit import AgentDeps
from beaver.contract import AgentName, BlockerCode, carried_catalogue
from beaver.inventory.agent import inventory_agent
from beaver.orchestrator import orchestrator_agent
from beaver.quoting import tools
from beaver.quoting.agent import quoting_agent
from beaver.quoting.models import DiscountBand
from beaver.quoting.tools import (
    band_for_units,
    catalogue_price,
    check_against_precedent,
    find_precedent,
    price_line,
    price_of,
)

STEP_ID = "20260915T120000Z:1:001"
REPLY = "Thank you for your enquiry — 500 sheets of A4 paper come to $23.75."


def read_quote_registry() -> list[dict]:
    """Every registry row, oldest first. A reader that lives in the tests, on
    purpose: the package has none, because the registry is a record and never a
    channel."""
    with starter.engine().connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM quote_registry ORDER BY quote_line_id")
        ).fetchall()
    return [dict(row._mapping) for row in rows]


def a_quoted_line(units: int, item_name: str = "A4 paper"):
    """One line priced the way the envelope prices it, without an agent."""
    return price_line(
        line_id="L1",
        quote_line_id=f"{STEP_ID}:L1",
        item_name=item_name,
        units=units,
        unit_price=price_of(item_name),
    )


class TestTheLadder:
    """Per line, on units, never compounding. The bands are the business's
    published schedule, so every boundary is asserted on both sides of it."""

    @pytest.mark.parametrize(
        "units, band",
        [
            (1, DiscountBand.NONE),
            (499, DiscountBand.NONE),
            (500, DiscountBand.BULK),
            (1_999, DiscountBand.BULK),
            (2_000, DiscountBand.VOLUME),
            (9_999, DiscountBand.VOLUME),
            (10_000, DiscountBand.WHOLESALE),
            (250_000, DiscountBand.WHOLESALE),
        ],
    )
    def test_each_band_starts_exactly_where_it_says_it_does(self, units, band):
        assert band_for_units(units) is band

    @pytest.mark.parametrize(
        "band, rate",
        [
            (DiscountBand.NONE, 0.00),
            (DiscountBand.BULK, 0.05),
            (DiscountBand.VOLUME, 0.10),
            (DiscountBand.WHOLESALE, 0.15),
        ],
    )
    def test_the_rate_is_derived_from_the_band(self, band, rate):
        assert band.rate == rate

    def test_a_wholesale_line_earns_fifteen_percent_and_not_the_rungs_below_it(self, seeded_db):
        """Compounding all four rungs would be 0.95 × 0.90 × 0.85 = 27.3%, and
        a ladder that quietly compounds is a different price list."""
        line = a_quoted_line(10_000)
        assert line.discount_rate == 0.15
        assert line.line_total == round(line.gross_total * 0.85, 2)

    def test_the_price_is_the_catalogue_price_times_the_units_less_the_band(self, seeded_db):
        line = a_quoted_line(500)
        assert (line.unit_price, line.gross_total) == (0.05, 25.00)
        assert (line.band, line.discount_rate, line.line_total) == (
            DiscountBand.BULK,
            0.05,
            23.75,
        )

    def test_the_same_line_always_earns_the_same_price(self, seeded_db):
        assert a_quoted_line(2_000) == a_quoted_line(2_000)

    def test_prices_come_from_the_catalogue_and_never_from_the_database(self, seeded_db):
        """`paper_supplies` is a static list literal, so a price cannot vary
        between calls, and `quotes.csv` — 5 totals of -1 and only 57 of the 95
        survivors reconciling — never enters the arithmetic."""
        by_name = {row["item_name"]: row["unit_price"] for row in starter.paper_supplies}
        priced = catalogue_price(sorted(carried_catalogue()))
        assert len(priced) == 18
        assert all(line.unit_price == by_name[line.item_name] for line in priced)

    def test_the_seeded_quote_history_is_never_an_arithmetic_source(self, seeded_db):
        """5 of its 100 totals are -1 and only 57 of the 95 survivors reconcile
        against the catalogue. The guarantee is structural: nothing that
        computes a price can reach the `quotes` table, because only
        `find_precedent` reads it and `price_line` cannot call it."""
        source = Path(tools.__file__).read_text()
        assert "FROM quotes" not in source
        assert signature(price_line).parameters.keys() == {
            "line_id",
            "quote_line_id",
            "item_name",
            "units",
            "unit_price",
        }

    def test_a_name_we_do_not_carry_is_refused_rather_than_priced(self, seeded_db):
        """`Matte paper` is in the product universe and not in the 18 we sell.
        Pricing it would quote a customer for something no transaction could
        ever be written against."""
        with pytest.raises(ValidationError):
            catalogue_price(["Matte paper"])


class TestFindingPrecedent:
    """`search_quote_history` ANDs its terms despite a docstring that says
    "any", so a generous list is a guaranteed miss. Degrade once, then be
    honest about it."""

    @pytest.fixture
    def searches(self, seeded_db, monkeypatch):
        """Every term list the helper was actually asked for, in order."""
        calls: list[list[str]] = []
        real = starter.search_quote_history

        def spy(search_terms, limit=5):
            calls.append(list(search_terms))
            return real(search_terms, limit)

        monkeypatch.setattr(starter, "search_quote_history", spy)
        return calls

    def test_a_long_term_list_degrades_to_a_single_term_in_one_step(self, searches):
        comparison = find_precedent("L1", ["glossy", "paper", "wedding", "banner", "napkins"])
        assert len(searches) == 2, "one degrade, and then we stop"
        assert len(searches[1]) == 1
        assert comparison.degraded_to_single_term
        assert comparison.search_terms == searches[1]

    def test_a_hit_on_the_full_list_never_degrades(self, searches):
        comparison = find_precedent("L1", ["paper"])
        assert len(searches) == 1
        assert comparison.precedent_found
        assert not comparison.degraded_to_single_term

    def test_a_miss_is_recorded_as_a_miss(self, searches):
        comparison = find_precedent("L1", ["zzzxxq"])
        assert comparison.precedent_found is False
        assert comparison.comparable_totals == []
        assert "no precedent" in comparison.note.lower()

    def test_the_error_rows_are_not_offered_as_comparable(self, seeded_db, monkeypatch):
        """5 of the 100 seeded totals are -1. A total of -1 is not a price the
        business ever charged, so it is not a comparison either."""
        monkeypatch.setattr(tools, "PRECEDENT_LIMIT", 100)
        comparison = find_precedent("L1", ["paper"])
        assert comparison.comparable_totals
        assert all(total > 0 for total in comparison.comparable_totals)

    def test_no_terms_at_all_is_a_miss_rather_than_the_whole_corpus(self, searches):
        """With no conditions the helper's `WHERE` falls back to `1=1` and
        returns the five most recent quotes — precedent for nothing."""
        comparison = find_precedent("L1", [])
        assert searches == []
        assert comparison.precedent_found is False

    def test_a_precedent_never_moves_the_price(self, seeded_db):
        """The comparison is computed after the price and has no path back to
        it: `price_line` takes the catalogue price and the units, and nothing
        else."""
        priced = a_quoted_line(500)
        find_precedent("L1", ["paper"])
        assert a_quoted_line(500) == priced


class TestCheckingThePriceAgainstPrecedent:
    """Retrieval is precedent's first job and this is its second. It runs after
    the price, on a function the model cannot call, and writes a sentence."""

    def a_precedent(self, totals: list[float]):
        return find_precedent("L1", ["zzzxxq"]).model_copy(
            update={"precedent_found": bool(totals), "comparable_totals": totals}
        )

    @pytest.mark.parametrize(
        "line_total, expected",
        [(50.0, "within"), (500.0, "above"), (1.0, "below")],
    )
    def test_the_note_says_where_our_price_sits(self, seeded_db, line_total, expected):
        checked = check_against_precedent(self.a_precedent([10.0, 100.0]), line_total)
        assert expected in checked.note
        assert "$10.00-$100.00" in checked.note

    def test_a_miss_is_left_as_it_was(self, seeded_db):
        missed = self.a_precedent([])
        assert check_against_precedent(missed, 50.0) == missed

    def test_the_check_returns_a_note_and_nothing_a_price_is_made_of(self, seeded_db):
        """It takes the price and gives back prose. There is no path from a
        past total to `price_line`, which takes neither."""
        checked = check_against_precedent(self.a_precedent([10.0, 100.0]), 50.0)
        assert checked.model_dump(exclude={"note"}) == self.a_precedent(
            [10.0, 100.0]
        ).model_dump(exclude={"note"})


class TestTheRegistry:
    """One row per priced line, written at quote time — before anyone rules on
    whether the line can be delivered. The rows that never become transactions
    are the business's rejection history."""

    async def test_one_row_per_priced_line_carries_its_band(self, trail):
        response = await quote(
            [("L1", "A4 paper", 500), ("L2", "Glossy paper", 2_000)], trail
        )
        rows = read_quote_registry()
        assert [(row["line_id"], row["units"], row["band"]) for row in rows] == [
            ("L1", 500, "bulk"),
            ("L2", 2_000, "volume"),
        ]
        assert [row["quote_line_id"] for row in rows] == (
            response.internal.quote_rows_written
        )

    async def test_the_row_is_stamped_with_the_request_date(self, trail):
        await quote([("L1", "A4 paper", 500)], trail, as_of_date="2025-04-01")
        assert read_quote_registry()[0]["quoted_at"] == "2025-04-01"

    async def test_the_key_carries_its_own_provenance(self, trail):
        await quote([("L1", "A4 paper", 500)], trail)
        assert read_quote_registry()[0]["quote_line_id"] == "20260915T120000Z:1:L1"

    def test_nothing_in_the_business_reads_the_registry_back(self):
        """A registry is a record, never a channel. `read_quote_registry` is a
        test's reader and lives in the tests; if the package itself grew one,
        quoting's write would become silently load-bearing for another agent
        underneath the typed contract."""
        from pathlib import Path

        package = Path(orchestrator.__file__).parent
        readers = [
            path.name
            for path in package.rglob("*.py")
            if "FROM quote_registry" in path.read_text()
        ]
        assert readers == [], f"{readers} reads the quote registry inside the run"


def scripted(lines: list[dict], as_of_date: str = "2025-04-01", seen: list | None = None):
    """A model that plays every part: orchestrator, inventory, then quoting.

    The three are told apart by the tools they are offered. Quoting's turn
    reports the lines it was given and one short search term per line — its
    only judgement — and the envelope is rebuilt from the tools regardless.
    """
    resolved = [
        {
            "line_id": line["line_id"],
            "item_name": line["item_name"],
            "category": "paper",
            "quantity": line["units"],
        }
        for line in lines
    ]
    probes = [
        {"line_id": line["line_id"], "search_terms": [line["item_name"].split()[0].lower()]}
        for line in lines
    ]

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tools = {tool.name for tool in info.function_tools}
        if "consult_inventory" in tools:
            if seen is not None:
                seen.append(messages)
            if len(messages) == 1:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "consult_inventory",
                            {
                                "lines": [
                                    {
                                        "line_id": line["line_id"],
                                        "item_as_stated": line["item_as_stated"],
                                        "quantity_as_stated": line["units"],
                                        "unit_as_stated": "sheets",
                                    }
                                    for line in lines
                                ],
                                "as_of_date": as_of_date,
                            },
                        )
                    ]
                )
            if len(messages) == 3:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "consult_quoting",
                            {"lines": resolved, "as_of_date": as_of_date},
                        )
                    ]
                )
            return ModelResponse(parts=[TextPart(REPLY)])
        if "catalogue_price" in tools:
            if len(messages) == 1:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "catalogue_price",
                            {"item_names": [line["item_name"] for line in lines]},
                        )
                    ]
                    + [ToolCallPart("find_precedent", probe) for probe in probes]
                )
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "final_result",
                        {"lines": resolved, "probes": probes, "as_of_date": as_of_date},
                    )
                ]
            )
        if len(messages) == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "shortlist_candidates",
                        {"items_as_stated": [line["item_as_stated"] for line in lines]},
                    )
                ]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "final_result",
                    {
                        "lines": [
                            {
                                "line_id": line["line_id"],
                                "item_as_stated": line["item_as_stated"],
                                "quantity_as_stated": line["units"],
                                "unit_as_stated": "sheets",
                            }
                            for line in lines
                        ],
                        "as_of_date": as_of_date,
                    },
                )
            ]
        )

    return FunctionModel(model)


def narrating(model: FunctionModel) -> FunctionModel:
    """The same scripted model, except that the orchestrator writes a real reply.

    Everywhere else the orchestrator's final turn is a fixed constant, because
    what is under test there is the wiring. Here the reply is rendered from what
    the delegation actually handed up, which is the only part of the prose rule
    a scripted model can honestly check.

    Args:
        model: The scripted model to wrap.

    Returns:
        A model that narrates the orchestrator's last turn.
    """

    def narrate(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        response = model.function(messages, info)
        if not any(isinstance(part, TextPart) for part in response.parts):
            return response
        lines = quoted_lines_handed_up(messages)
        return ModelResponse(
            parts=[TextPart(" ".join(a_price_sentence(line) for line in lines))]
        )

    return FunctionModel(narrate)


def stated(lines: list[tuple[str, str, int]]) -> list[dict]:
    """Resolved lines as `(line_id, item_name, units)`, plus the words behind them."""
    return [
        {
            "line_id": line_id,
            "item_name": item_name,
            "units": units,
            "item_as_stated": _AS_STATED.get(item_name, item_name.lower()),
        }
        for line_id, item_name, units in lines
    ]


#: What a customer says for the items these tests price. Only used by the
#: orchestrator-level tests, where inventory has to resolve them first.
_AS_STATED = {"A4 paper": "printer paper", "Glossy paper": "glossy paper"}


async def quote(lines: list[tuple[str, str, int]], trail, as_of_date: str = "2025-04-01"):
    """Run quoting on its own, with the step id the seam would have minted."""
    deps = AgentDeps(trail=trail, request_id="1", current_step_id=STEP_ID)
    with quoting_agent.override(model=scripted(stated(lines), as_of_date)):
        result = await quoting_agent.run("price these", deps=deps)
    return result.output


class TestTheEnvelope:
    """What quoting hands back: every resolved line priced, blind to stock, and
    the precedent behind each kept on the half the customer never sees."""

    async def test_two_lines_on_one_request_band_independently(self, trail):
        """Per line, not per order — so the discount is traceable to the thing
        that earned it, and a small line is not carried by a large one."""
        response = await quote([("L1", "A4 paper", 300), ("L2", "A4 paper", 2_000)], trail)
        first, second = response.customer.quoted_lines
        assert (first.band, first.discount_rate) == (DiscountBand.NONE, 0.00)
        assert (second.band, second.discount_rate) == (DiscountBand.VOLUME, 0.10)
        assert response.customer.quote_total == round(
            first.line_total + second.line_total, 2
        )

    async def test_a_line_that_will_be_short_of_stock_is_still_priced(self, trail):
        """Quoting runs before the conditional restock, so a shorted line is
        live when it runs. Skipping it would leave the retry path committing a
        line that was never costed."""
        response = await quote([("L1", "A4 paper", 10_000)], trail)
        [line] = response.customer.quoted_lines
        assert starter.get_stock_level("A4 paper", "2025-04-01")["current_stock"][0] < 10_000
        assert line.line_total == 425.00

    async def test_the_precedent_check_stays_on_the_internal_half(self, trail):
        response = await quote([("L1", "A4 paper", 500)], trail)
        [precedent] = response.internal.precedents
        assert precedent.line_id == "L1"
        assert "comparable_totals" not in response.customer.model_dump_json()

    async def test_unpriceable_fires_zero_times(self, trail):
        response = await quote([("L1", "A4 paper", 500), ("L2", "Glossy paper", 50)], trail)
        assert response.internal.signals == []

    async def test_unpriceable_guards_the_catalogue_diverging_from_the_validator(
        self, trail, monkeypatch
    ):
        """Unreachable by construction — every carried item has a catalogue
        price. Kept because the alternative to a blocker there is an unhandled
        exception that kills the run."""
        monkeypatch.setattr(
            starter,
            "paper_supplies",
            [row for row in starter.paper_supplies if row["item_name"] != "A4 paper"],
        )
        response = await quote([("L1", "A4 paper", 500)], trail)
        assert response.customer.quoted_lines == []
        [signal] = response.internal.signals
        assert signal.code is BlockerCode.UNPRICEABLE
        assert signal.line_id == "L1"

    async def test_a_line_reported_twice_is_priced_once(self, trail):
        response = await quote([("L1", "A4 paper", 500), ("L1", "A4 paper", 500)], trail)
        assert len(response.customer.quoted_lines) == 1
        assert len(read_quote_registry()) == 1


def quoted_lines_handed_up(messages) -> list[dict]:
    """The priced lines as they reached the orchestrator, out of its own context."""
    for message in messages:
        for part in getattr(message, "parts", []):
            if getattr(part, "tool_name", None) != "consult_quoting":
                continue
            payload = to_jsonable_python(getattr(part, "content", None))
            if isinstance(payload, dict) and "customer" in payload:
                return payload["customer"]["quoted_lines"]
    return []


def a_price_sentence(line: dict) -> str:
    """One line rendered the way the orchestrator's instructions ask for it.

    A stand-in for the live model's own words, so that what is under test is
    whether the payload *lets* the sentence be written — the rate, what earned
    it, and both totals — rather than a particular turn of phrase.
    """
    sentence = (
        f"{line['units']} units of {line['item_name']} at "
        f"${line['unit_price']:.2f} each — ${line['gross_total']:.2f}"
    )
    if line["discount_rate"]:
        sentence += (
            f", less {line['discount_rate']:.0%} for the size of this line — "
            f"${line['line_total']:.2f}"
        )
    return sentence


class TestThroughTheOrchestrator:
    """Inventory, then quoting — the sequence #4 fixed, as the harness drives it."""

    @pytest.fixture(autouse=True)
    def wired(self, trail, monkeypatch):
        monkeypatch.setenv("UDACITY_OPENAI_API_KEY", "not-used-under-a-scripted-model")
        monkeypatch.setattr(orchestrator, "trail", lambda: trail)
        self.seen: list = []
        return trail

    async def handle(self, lines: list[tuple[str, str, int]]):
        model = scripted(stated(lines), seen=self.seen)
        with (
            orchestrator_agent.override(model=model),
            inventory_agent.override(model=model),
            quoting_agent.override(model=model),
        ):
            return await orchestrator.handle_request(
                "I would like to order some paper. (Date of request: 2025-04-01)",
                request_date="2025-04-01",
                request_id=1,
            )

    async def compose(self, lines: list[tuple[str, str, int]]) -> str:
        """The letter the orchestrator wrote, with no outcome derived over it.

        This class stops the sequence at quoting, which leaves a priced line
        undecided — and `handle_request` rightly refuses to call that an
        outcome. Whether quoting's facts suffice for the prose is what is under
        test here, so the orchestrator is run directly.

        Args:
            lines: The lines to price.

        Returns:
            The orchestrator's letter.
        """
        model = narrating(scripted(stated(lines), seen=self.seen))
        deps = AgentDeps(trail=orchestrator.trail(), request_id="1")
        with (
            orchestrator_agent.override(model=model),
            inventory_agent.override(model=model),
            quoting_agent.override(model=model),
        ):
            result = await orchestrator_agent.run("I would like some paper.", deps=deps)
        return result.output

    def orchestrator_saw(self) -> str:
        return json.dumps(to_jsonable_python(self.seen[-1]))

    async def test_inventory_runs_first_and_quoting_second(self):
        await self.handle([("L1", "A4 paper", 500)])
        with starter.engine().connect() as conn:
            rows = conn.execute(
                text("SELECT agent, kind, name FROM agent_steps ORDER BY seq")
            ).fetchall()
        assert [(row.kind, row.name) for row in rows if row.kind == "delegation"] == [
            ("delegation", "inventory"),
            ("delegation", "quoting"),
        ]
        assert {row.agent for row in rows} == {AgentName.INVENTORY, AgentName.QUOTING}

    async def test_the_price_and_the_band_reach_the_orchestrator(self):
        """The itemisation crosses as facts; the orchestrator turns it into the
        sentence that states the rate and why it applied."""
        await self.handle([("L1", "A4 paper", 500)])
        handed_up = self.orchestrator_saw().replace(" ", "")
        assert '"unit_price":0.05' in handed_up
        assert '"band":"bulk"' in handed_up
        assert '"line_total":23.75' in handed_up

    async def test_the_precedent_never_enters_the_orchestrators_context(self):
        await self.handle([("L1", "A4 paper", 500)])
        handed_up = self.orchestrator_saw()
        assert "precedents" not in handed_up
        assert "comparable_totals" not in handed_up

    async def test_the_reply_can_state_the_rate_and_what_earned_it(self):
        """Quoting supplies facts and the orchestrator supplies the words, so
        the test the AC deserves is that the facts suffice: the rate, the line
        that earned it, and both totals are all on the customer half."""
        letter = await self.compose([("L1", "A4 paper", 500)])
        assert "500 units of A4 paper at $0.05 each — $25.00" in letter
        assert "less 5% for the size of this line — $23.75" in letter

    async def test_a_line_that_earned_nothing_gets_no_discount_sentence(self):
        """A discount sentence on every line makes the real ones invisible."""
        letter = await self.compose([("L1", "A4 paper", 300)])
        assert "$15.00" in letter
        assert "less" not in letter

    async def test_the_registry_row_is_written_before_anyone_rules_on_delivery(self):
        """Sales does not exist yet, and the row exists anyway. That gap is the
        rejection history the rubric's gates are proved from."""
        await self.handle([("L1", "A4 paper", 500)])
        assert [row["line_id"] for row in read_quote_registry()] == ["L1"]
