"""The bounded retry: one short line, one purchase, one second pass, one merge.

The sequence this module covers is the one that closes the defect that
double-counted cash, so what it asserts hardest is arithmetic rather than
prose: each committed line has exactly one transaction row across both passes,
and the cash delta the orchestrator measures is the change the books actually
saw.

Everything the orchestrator decides about the retry is ordinary function code
in `beaver.retry`, so the merge and the intersection are tested without a
model. The sequence itself needs one, and it runs on a `FunctionModel` — no
network and no key — driving the real hand-off, including a replenishment step
whose purchase the next sales pass actually finds on the shelf.

Seed 137 facts these tests lean on:

| item | stock | floor | shelf price |
|---|---|---|---|
| `A4 paper` | 272 | 135 | $0.05 |
| `Cardstock` | 595 | 148 | $0.15 |

So a 500-unit `A4 paper` line is short by 228 and restocks 363 units, and a
500-unit `Cardstock` line commits off the shelf.
"""

import inspect
import json
from datetime import date
from types import SimpleNamespace

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import text

from tests.messages import PRICED, RESOLVED, handed_down, handed_up, returned
from tests.turns import replenishment_turn

from beaver import orchestrator, starter
from beaver.audit import AgentDeps, RequestCash
from beaver.contract import (
    AgentName,
    AgentView,
    BlockerCode,
    CustomerBlocker,
    Outcome,
)
from beaver.inventory.agent import inventory_agent
from beaver.inventory.models import RestockNeed
from beaver.orchestrator import orchestrator_agent, place_order
from beaver.quoting.agent import quoting_agent
from beaver.quoting.tools import price_line, price_of
from beaver.replenishment.agent import replenishment_agent
from beaver.replenishment.models import RestockedItem
from beaver.replenishment.tools import restock_arrival_date
from beaver.retry import (
    availability_of,
    lines_to_retry,
    merge_passes,
    restocks_for,
)
from beaver.sales.agent import build_sales_response, sales_agent
from beaver.sales.models import (
    CommittedLine,
    DeclinedLine,
    SalesCustomerPayload,
    SalesInternalPayload,
)
from beaver.sales.tools import read_cash

REQUEST_DATE = "2025-04-01"
STEP_ID = "20260915T120000Z:1:001"

#: 595 on the shelf at seed 137, so a 500-unit line commits off it.
IN_STOCK = "Cardstock"
#: 272 on the shelf, so a 500-unit line is short by 228 and restocks 363.
SHORT = "A4 paper"
#: The units a short line restocks: the 228 shortfall plus the 135 floor.
RESTOCK_UNITS = 363

#: Far enough out that a restock reaches us in time.
MEETABLE = "2025-04-15"
#: The day the request arrived. No supplier reaches us the day we order, so
#: every restock under this deadline is refused as late.
UNMEETABLE = REQUEST_DATE


# --------------------------------------------------------------------------
# The deterministic half: no model, no database.
# --------------------------------------------------------------------------


def a_committed(line_id: str, item_name: str, promised: str, total: float):
    return CommittedLine(
        line_id=line_id,
        item_name=item_name,
        units=100,
        line_total=total,
        promised_delivery_date=date.fromisoformat(promised),
    )


def a_declined(line_id: str, item_name: str = SHORT):
    return DeclinedLine(line_id=line_id, item_name=item_name, units=100)


def a_pass(committed=(), declined=(), total=0.0, promised=None):
    return SalesCustomerPayload(
        committed=list(committed),
        declined=list(declined),
        order_total=total,
        promised_delivery_date=date.fromisoformat(promised) if promised else None,
    )


def a_blocker(line_id: str, code: BlockerCode) -> CustomerBlocker:
    return CustomerBlocker(line_id=line_id, code=code)


class TestWhatThePurseOpensOn:
    """Sales' declines, intersected with inventory's measurement of them."""

    def test_a_stock_decline_that_inventory_measured_is_bought_for(self, seeded_db):
        needs = [RestockNeed(line_id="L1", item_name=SHORT, shortfall_units=228)]
        assert restocks_for(
            [a_blocker("L1", BlockerCode.INSUFFICIENT_STOCK)], needs
        ) == needs

    def test_a_line_declined_for_any_other_reason_buys_nothing(self, seeded_db):
        """The purse opens on a refusal for want of stock and on nothing else."""
        needs = [RestockNeed(line_id="L1", item_name=SHORT, shortfall_units=228)]
        assert restocks_for([a_blocker("L1", BlockerCode.UNPRICEABLE)], needs) == []

    def test_a_need_nobody_declined_buys_nothing(self, seeded_db):
        """Inventory's survey is advisory: a line it saw short but sales
        committed — a restock from an earlier request having landed — is not a
        reason to buy."""
        needs = [RestockNeed(line_id="L1", item_name=SHORT, shortfall_units=228)]
        assert restocks_for([], needs) == []

    def test_no_declines_at_all_open_nothing(self, seeded_db):
        assert restocks_for([], []) == []

    def test_the_needs_keep_inventorys_own_order(self, seeded_db):
        needs = [
            RestockNeed(line_id="L1", item_name=SHORT, shortfall_units=1),
            RestockNeed(line_id="L2", item_name=IN_STOCK, shortfall_units=2),
        ]
        blockers = [
            a_blocker("L2", BlockerCode.INSUFFICIENT_STOCK),
            a_blocker("L1", BlockerCode.INSUFFICIENT_STOCK),
        ]
        assert [need.line_id for need in restocks_for(blockers, needs)] == ["L1", "L2"]


class TestWhatPassTwoCarries:
    """Only the lines we actually bought stock for."""

    def a_line(self, line_id: str, item_name: str = SHORT):
        return price_line(
            line_id=line_id,
            quote_line_id=f"{STEP_ID}:{line_id}",
            item_name=item_name,
            units=500,
            unit_price=price_of(item_name),
        )

    def an_item(self, line_id: str, item_name: str = SHORT, arrives: str = MEETABLE):
        return RestockedItem(
            line_id=line_id,
            item_name=item_name,
            available_from=date.fromisoformat(arrives),
        )

    def test_a_restocked_line_is_offered_again(self, seeded_db):
        lines = [self.a_line("L1")]
        assert lines_to_retry(lines, [self.an_item("L1")]) == lines

    def test_a_line_whose_restock_was_refused_is_never_offered_again(self, seeded_db):
        """It has no new stock behind it, so a second pass would replace its
        true refusal with a second `insufficient_stock` for stock we chose not
        to buy."""
        assert lines_to_retry([self.a_line("L1")], []) == []

    def test_a_line_that_already_committed_is_never_offered_again(self, seeded_db):
        """The whole of the no-double-sale guarantee: pass 2 is built from what
        we bought, and we never buy for a line we sold."""
        lines = [self.a_line("L1"), self.a_line("L2", IN_STOCK)]
        assert [line.line_id for line in lines_to_retry(lines, [self.an_item("L1")])] == [
            "L1"
        ]

    def test_the_arrival_dates_travel_by_item_name(self, seeded_db):
        """Which is how sales promises from them."""
        assert availability_of([self.an_item("L1")]) == {
            SHORT: date.fromisoformat(MEETABLE)
        }

    def test_two_lines_on_one_purchase_share_its_date(self, seeded_db):
        items = [self.an_item("L1"), self.an_item("L2")]
        assert availability_of(items) == {SHORT: date.fromisoformat(MEETABLE)}


class TestTheMerge:
    """`committed`, `declined`, `order_total` and the latest promise, across both
    passes. The orchestrator is the only component that sees them."""

    def test_a_line_committed_on_pass_two_joins_the_order_and_leaves_declined(self):
        first = a_pass(
            committed=[a_committed("L2", IN_STOCK, REQUEST_DATE, 71.25)],
            declined=[a_declined("L1")],
            total=71.25,
            promised=REQUEST_DATE,
        )
        second = a_pass(
            committed=[a_committed("L1", SHORT, MEETABLE, 23.75)],
            total=23.75,
            promised=MEETABLE,
        )
        order = merge_passes(first, second, {})
        assert [line.line_id for line in order.committed] == ["L2", "L1"]
        assert order.declined == []

    def test_the_total_is_both_passes_added(self):
        first = a_pass(committed=[a_committed("L2", IN_STOCK, REQUEST_DATE, 71.25)], total=71.25)
        second = a_pass(committed=[a_committed("L1", SHORT, MEETABLE, 23.75)], total=23.75)
        assert merge_passes(first, second, {}).order_total == 95.00

    def test_the_order_is_promised_the_latest_of_either_passs_lines(self):
        """An order is delivered when its last item arrives, and the thing we
        had to buy in is almost always last."""
        first = a_pass(
            committed=[a_committed("L2", IN_STOCK, REQUEST_DATE, 71.25)],
            promised=REQUEST_DATE,
        )
        second = a_pass(
            committed=[a_committed("L1", SHORT, MEETABLE, 23.75)], promised=MEETABLE
        )
        assert merge_passes(first, second, {}).promised_delivery_date == (
            date.fromisoformat(MEETABLE)
        )

    def test_a_line_still_declined_after_pass_two_stays_declined(self):
        first = a_pass(declined=[a_declined("L1")])
        second = a_pass(declined=[a_declined("L1")])
        order = merge_passes(
            first, second, {"L1": BlockerCode.INSUFFICIENT_STOCK}
        )
        assert [line.line_id for line in order.declined] == ["L1"]
        assert [blocker.code for blocker in order.blockers] == [
            BlockerCode.INSUFFICIENT_STOCK
        ]

    def test_a_line_declined_on_one_pass_is_never_declined_twice(self):
        """Pass 2 was only ever given lines pass 1 declined, so the two sets
        overlap by construction and the merge must not count them twice."""
        first = a_pass(declined=[a_declined("L1"), a_declined("L2", IN_STOCK)])
        second = a_pass(declined=[a_declined("L1")])
        assert [line.line_id for line in merge_passes(first, second, {}).declined] == [
            "L1",
            "L2",
        ]

    def test_a_request_with_no_retry_merges_to_its_first_pass(self):
        first = a_pass(
            committed=[a_committed("L1", IN_STOCK, REQUEST_DATE, 71.25)],
            total=71.25,
            promised=REQUEST_DATE,
        )
        order = merge_passes(first, None, {})
        assert order.committed == first.committed
        assert order.order_total == 71.25
        assert order.promised_delivery_date == date.fromisoformat(REQUEST_DATE)

    def test_the_declined_line_speaks_the_code_that_now_stands(self):
        """Not the one the pass that declined it raised: a line short on pass 1
        and refused on the delivery date is told about the date."""
        first = a_pass(declined=[a_declined("L1")])
        order = merge_passes(first, None, {"L1": BlockerCode.DEADLINE_UNMEETABLE})
        assert order.blockers == [a_blocker("L1", BlockerCode.DEADLINE_UNMEETABLE)]

    def test_a_line_whose_only_blocker_is_internal_speaks_none(self):
        """`spoken_blockers` drops `cash_insufficient` before it gets here, so a
        line with nothing left to say carries no blocker at all — and is still
        declined."""
        order = merge_passes(a_pass(declined=[a_declined("L1")]), None, {})
        assert [line.line_id for line in order.declined] == ["L1"]
        assert order.blockers == []

    def test_nothing_committed_is_promised_nothing(self):
        order = merge_passes(a_pass(declined=[a_declined("L1")]), a_pass(), {})
        assert order.promised_delivery_date is None
        assert order.order_total == 0.0


# --------------------------------------------------------------------------
# The sequence, under a scripted model.
# --------------------------------------------------------------------------

#: Two of our products fit "banner paper", so inventory refuses the line and
#: asks which was meant. Nothing about it reaches the order desk.
AMBIGUOUS = "banner paper"

#: What a customer says for the items these tests order.
_AS_STATED = {SHORT: "printer paper", IN_STOCK: "cardstock", AMBIGUOUS: AMBIGUOUS}


def scripted_flow(lines, deadline: str | None, as_of_date: str = REQUEST_DATE):
    """The whole sequence under one model: inventory, quoting, sales, and — if a
    line goes short — replenishment and sales again.

    Every call is built from what the previous delegation actually handed up,
    so the retry under test is the real one: the lines pass 2 receives are the
    lines the orchestrator chose, and the dates it promises from are the ones
    replenishment computed.
    """
    stated = [
        {
            "line_id": line_id,
            "item_as_stated": _AS_STATED[item_name],
            "quantity_as_stated": units,
            "unit_as_stated": "sheets",
        }
        for line_id, item_name, units in lines
    ]
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        names = {tool.name for tool in info.function_tools}
        if "consult_inventory" in names:
            if len(messages) == 1:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "consult_inventory",
                            {"lines": stated, "as_of_date": as_of_date},
                        )
                    ]
                )
            resolved = handed_up(messages, "consult_inventory").get("resolved_lines", [])
            if len(messages) == 3 and resolved:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "consult_quoting",
                            {"lines": resolved, "as_of_date": as_of_date},
                        )
                    ]
                )
            if len(messages) == 5 and resolved:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "place_order",
                            {
                                "lines": handed_up(messages, "consult_quoting")[
                                    "quoted_lines"
                                ],
                                "as_of_date": as_of_date,
                                "deadline": deadline,
                            },
                        )
                    ]
                )
            return ModelResponse(
                parts=[TextPart(a_letter(returned(messages, "place_order")))]
            )
        if "catalogue_price" in names:
            if len(messages) == 1:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "catalogue_price",
                            {
                                "item_names": [
                                    line["item_name"]
                                    for line in handed_down(messages, RESOLVED)
                                ]
                            },
                        )
                    ]
                )
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "final_result",
                        {"lines": handed_down(messages, RESOLVED), "probes": [],
                         "as_of_date": as_of_date},
                    )
                ]
            )
        if "snapshot_financials" in names:
            quoted = handed_down(messages, PRICED)
            if len(messages) == 1:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "snapshot_financials",
                            {
                                "item_names": sorted({line["item_name"] for line in quoted}),
                                "as_of_date": as_of_date,
                            },
                        )
                    ]
                )
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "final_result", {"lines": quoted, "as_of_date": as_of_date}
                    )
                ]
            )
        if "reorder_thresholds" in names:
            return replenishment_turn(messages)
        if len(messages) == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "shortlist_candidates",
                        {"items_as_stated": [line["item_as_stated"] for line in stated]},
                    )
                ]
            )
        return ModelResponse(
            parts=[ToolCallPart("final_result", {"lines": stated, "as_of_date": as_of_date})]
        )

    return FunctionModel(model)


#: What the orchestrator's instructions ask it to say, by code.
_SAYS = {
    "insufficient_stock": "we are unable to supply {item} at present",
    "deadline_unmeetable": "we could not get {item} to you by then",
}


def a_letter(order: dict) -> str:
    """The reply rendered the way the orchestrator's instructions ask for it."""
    sentences = [
        f"{line['units']} units of {line['item_name']} for ${line['line_total']:.2f}"
        for line in order.get("committed", [])
    ]
    if order.get("promised_delivery_date"):
        sentences.append(f"delivered on {order['promised_delivery_date']}")
    declined = {line["line_id"]: line["item_name"] for line in order.get("declined", [])}
    for blocker in order.get("blockers", []):
        sentences.append(
            _SAYS[blocker["code"]].format(item=declined[blocker["line_id"]])
        )
    return ". ".join(sentences) or "We could not help with this enquiry."


def rows(transaction_type: str) -> list[dict]:
    """This run's transactions of one type, oldest first, excluding the seeding.

    `init_database` books the opening cash as a `sales` row against no item and
    the opening stock as `stock_orders` dated 2025-01-01, so both are filtered
    out rather than counted as this request's doing.
    """
    with starter.engine().connect() as conn:
        found = conn.execute(
            text(
                "SELECT rowid AS rowid, item_name, units, price, transaction_date "
                "FROM transactions WHERE transaction_type = :kind "
                "AND item_name IS NOT NULL AND transaction_date = :day ORDER BY rowid"
            ),
            {"kind": transaction_type, "day": REQUEST_DATE},
        ).fetchall()
    return [dict(row._mapping) for row in found]


def delegations() -> list[str]:
    with starter.engine().connect() as conn:
        found = conn.execute(
            text(
                "SELECT name FROM agent_steps WHERE kind = 'delegation' ORDER BY seq"
            )
        ).fetchall()
    return [row.name for row in found]


def signals() -> list[tuple[str, str]]:
    with starter.engine().connect() as conn:
        found = conn.execute(
            text("SELECT line_id, code FROM blocker_signals ORDER BY rowid")
        ).fetchall()
    return [(row.line_id, row.code) for row in found]


def cash_row() -> dict | None:
    with starter.engine().connect() as conn:
        found = conn.execute(text("SELECT * FROM request_cash")).fetchall()
    return dict(found[0]._mapping) if found else None


class Flow:
    """The harness call, under a scripted model, on a trail of its own.

    Inherited rather than repeated: every class below drives the same request
    through the same five agents and differs only in what it then asserts.
    """

    @pytest.fixture(autouse=True)
    def wired(self, trail, monkeypatch):
        monkeypatch.setenv("UDACITY_OPENAI_API_KEY", "not-used-under-a-scripted-model")
        monkeypatch.setattr(orchestrator, "trail", lambda: trail)
        return trail

    async def handle(self, lines, deadline: str | None = MEETABLE):
        model = scripted_flow(lines, deadline)
        with (
            orchestrator_agent.override(model=model),
            inventory_agent.override(model=model),
            quoting_agent.override(model=model),
            sales_agent.override(model=model),
            replenishment_agent.override(model=model),
        ):
            return await orchestrator.handle_request(
                "I would like to place an order. (Date of request: 2025-04-01)",
                request_date=REQUEST_DATE,
                request_id=1,
            )


class TestTheFullSequence(Flow):
    """A request with one line short of stock commits its other lines, buys the
    stock it was short of, and commits the short line on a second pass."""

    async def test_the_stocked_line_sells_on_pass_one_and_the_short_line_on_pass_two(self):
        resolution = await self.handle([("L1", SHORT, 500), ("L2", IN_STOCK, 500)])
        assert delegations() == [
            "inventory",
            "quoting",
            "sales",
            "replenishment",
            "sales",
        ]
        assert resolution.outcome is Outcome.FULFILLED
        assert [row["item_name"] for row in rows("sales")] == [IN_STOCK, SHORT]

    async def test_each_committed_line_has_exactly_one_transaction_row(self):
        """The defect this ticket closes: a line offered for commitment twice
        is a sale written twice, and nothing here can be taken back."""
        await self.handle([("L1", SHORT, 500), ("L2", IN_STOCK, 500)])
        written = rows("sales")
        assert len(written) == 2
        assert len({row["item_name"] for row in written}) == 2

    async def test_the_purchase_is_made_once_and_covers_the_shortfall_and_the_floor(self):
        await self.handle([("L1", SHORT, 500)])
        [bought] = rows("stock_orders")
        assert (bought["item_name"], bought["units"]) == (SHORT, RESTOCK_UNITS)

    async def test_replenishment_is_not_invoked_when_nothing_went_short(self):
        """Every purchase this business makes is traceable to a customer who
        asked for something we did not have."""
        await self.handle([("L1", IN_STOCK, 500)])
        assert delegations() == ["inventory", "quoting", "sales"]
        assert rows("stock_orders") == []

    async def test_pass_two_is_given_only_the_line_that_was_declined(self):
        await self.handle([("L1", SHORT, 500), ("L2", IN_STOCK, 500)])
        with starter.engine().connect() as conn:
            [_, second] = conn.execute(
                text(
                    "SELECT inputs FROM agent_steps WHERE name = 'sales' "
                    "AND kind = 'delegation' ORDER BY seq"
                )
            ).scalars().all()
        offered = json.loads(second)["args"][0]
        assert [line["line_id"] for line in offered] == ["L1"]

    async def test_the_totals_and_the_promise_are_merged_across_both_passes(self):
        resolution = await self.handle([("L1", SHORT, 500), ("L2", IN_STOCK, 500)])
        arrival = restock_arrival_date(date.fromisoformat(REQUEST_DATE), RESTOCK_UNITS)
        message = resolution.customer_message
        assert f"500 units of {IN_STOCK}" in message
        assert f"500 units of {SHORT}" in message
        # The latest of the two, which is the line we had to buy in.
        assert f"delivered on {arrival.isoformat()}" in message

    async def test_a_restocked_line_is_promised_the_date_it_reaches_us(self):
        """And a line off the shelf is promised the day the request arrived —
        the promise is a property of where the goods are."""
        arrival = restock_arrival_date(date.fromisoformat(REQUEST_DATE), RESTOCK_UNITS)
        await self.handle([("L1", SHORT, 500), ("L2", IN_STOCK, 500)])
        with starter.engine().connect() as conn:
            outputs = conn.execute(
                text(
                    "SELECT outputs FROM agent_steps WHERE name = 'sales' "
                    "AND kind = 'delegation' ORDER BY seq"
                )
            ).scalars().all()
        promised = {
            line["line_id"]: line["promised_delivery_date"]
            for step in outputs
            for line in json.loads(step)["customer"]["committed"]
        }
        assert promised == {"L2": REQUEST_DATE, "L1": arrival.isoformat()}

    async def test_the_cash_delta_is_measured_across_both_passes(self):
        """Pass 2's own `cash_before` already holds pass 1's revenue and the
        purchase between them, so neither envelope's delta is the request's."""
        before = read_cash(REQUEST_DATE)
        await self.handle([("L1", SHORT, 500), ("L2", IN_STOCK, 500)])
        after = read_cash(REQUEST_DATE)

        row = cash_row()
        assert row["cash_before"] == pytest.approx(before)
        assert row["cash_after"] == pytest.approx(after)
        assert row["cash_delta"] == pytest.approx(after - before)
        assert row["request_id"] == "1"

    async def test_the_delta_is_the_sales_less_the_purchase(self):
        await self.handle([("L1", SHORT, 500), ("L2", IN_STOCK, 500)])
        sold = sum(row["price"] for row in rows("sales"))
        spent = sum(row["price"] for row in rows("stock_orders"))
        assert cash_row()["cash_delta"] == pytest.approx(sold - spent)

    async def test_a_request_that_moved_nothing_is_measured_at_nought(self):
        """A weighed request that moved nothing is a different fact from one
        nobody weighed, so it says so rather than being absent."""
        await self.handle([("L1", SHORT, 500)], deadline=UNMEETABLE)
        assert cash_row()["cash_delta"] == 0.0



class TestWhatTheModelCannotTouch(Flow):
    """The two things the sequence refuses to leave to a model."""

    def test_sales_is_never_handed_the_date_it_promises_from(self):
        """It reaches the output function on the deps. A model that dropped or
        moved it would promise goods that are not in the building for today."""
        assert "earliest_availability" not in inspect.signature(
            build_sales_response
        ).parameters

    async def test_a_restocked_line_is_still_promised_its_arrival_date(self):
        """The other half of the claim: routing it around the model did not
        lose it."""
        arrival = restock_arrival_date(date.fromisoformat(REQUEST_DATE), RESTOCK_UNITS)
        resolution = await self.handle([("L1", SHORT, 500)])
        assert f"delivered on {arrival.isoformat()}" in resolution.customer_message

    async def test_the_order_desk_refuses_a_second_call(self, trail):
        """The instructions say to call `place_order` once. An instruction is
        one model turn away from selling a line we have already written."""
        deps = AgentDeps(trail=trail, request_id="1")
        deps.journal.views = [_a_sales_view()]
        with pytest.raises(ModelRetry):
            await place_order(SimpleNamespace(deps=deps, usage=None), [], REQUEST_DATE)


def _a_sales_view() -> AgentView[SalesCustomerPayload]:
    """A journal that has already been through the order desk."""
    return AgentView[SalesCustomerPayload](
        agent=AgentName.SALES,
        step_id=STEP_ID,
        request_id="1",
        customer=a_pass(),
        blockers=[],
    )


class TestTheMeasurementItself:
    """The pair of readings the seam collects, without a database or a model."""

    def a_payload(self, before: float, after: float):
        return SalesInternalPayload(
            cash_before=before,
            cash_after=after,
            stock_read_by_item={},
            transaction_rowid_by_line={},
            verdict_by_line={},
        )

    def test_a_request_nobody_weighed_has_no_delta_to_report(self):
        cash = RequestCash()
        assert not cash.observed
        assert cash.delta == 0.0

    def test_it_keeps_the_first_reading_and_the_latest_one(self):
        """Three steps, three deltas of their own, and none of them is the
        request's: pass 2's `cash_before` already holds pass 1's revenue and
        the purchase between them."""
        cash = RequestCash()
        cash.observe(self.a_payload(45_000.00, 45_071.25))
        cash.observe(self.a_payload(45_071.25, 44_951.25))
        cash.observe(self.a_payload(44_951.25, 44_975.00))
        assert (cash.before, cash.after) == (45_000.00, 44_975.00)
        assert cash.delta == -25.00

    def test_a_weighed_request_that_moved_nothing_is_observed_all_the_same(self):
        cash = RequestCash()
        cash.observe(self.a_payload(45_000.00, 45_000.00))
        assert cash.observed and cash.delta == 0.0


class TestWhenTheRestockIsRefused(Flow):
    """A line we could have got but not in time keeps its true reason, and no
    request ends cash-negative."""

    async def test_a_late_restock_buys_nothing_at_all(self):
        """Not even the floor portion: a purchase sized by the threshold alone
        is the periodic sweep this business ruled out, under a customer's name."""
        await self.handle([("L1", SHORT, 500)], deadline=UNMEETABLE)
        assert rows("stock_orders") == []
        assert rows("sales") == []

    async def test_the_line_is_spoken_as_the_delivery_refusal(self):
        resolution = await self.handle([("L1", SHORT, 500)], deadline=UNMEETABLE)
        assert "could not get A4 paper to you by then" in resolution.customer_message
        assert "unable to supply" not in resolution.customer_message

    async def test_both_blockers_are_still_in_the_trail(self):
        """Precedence governs what is spoken, never what is recorded."""
        await self.handle([("L1", SHORT, 500)], deadline=UNMEETABLE)
        assert signals() == [
            ("L1", "insufficient_stock"),
            ("L1", "deadline_unmeetable"),
        ]

    async def test_a_refused_line_is_never_offered_to_sales_again(self):
        """Offering it would replace its true refusal with a second
        `insufficient_stock` for stock we deliberately did not buy."""
        await self.handle([("L1", SHORT, 500)], deadline=UNMEETABLE)
        assert delegations() == ["inventory", "quoting", "sales", "replenishment"]

    async def test_a_sibling_line_still_sells(self):
        resolution = await self.handle(
            [("L1", SHORT, 500), ("L2", IN_STOCK, 500)], deadline=UNMEETABLE
        )
        assert resolution.outcome is Outcome.PARTIALLY_FULFILLED
        assert [row["item_name"] for row in rows("sales")] == [IN_STOCK]

    async def test_a_request_that_bought_nothing_can_still_suspend(self):
        """The door ticket 106 left open, from the other side: suspension is
        reachable only before money moves, and a refused restock moved none —
        so the ambiguous line beside it is still a question we may ask."""
        resolution = await self.handle(
            [("L1", SHORT, 500), ("L2", AMBIGUOUS, 200)], deadline=UNMEETABLE
        )
        assert resolution.outcome is Outcome.PENDING_CUSTOMER_REVISION
        assert resolution.resume_token is not None

    async def test_the_suspended_flow_carries_the_deadline_it_was_given(self):
        """Read out of the customer's prose by the orchestrator and recorded at
        the one step that needs it, so a resumption knows the date to beat."""
        await self.handle(
            [("L1", SHORT, 500), ("L2", AMBIGUOUS, 200)], deadline=UNMEETABLE
        )
        with starter.engine().connect() as conn:
            payload = conn.execute(text("SELECT payload FROM suspended_flows")).scalar_one()
        assert json.loads(payload)["original_request"]["deadline"] == UNMEETABLE

    async def test_the_request_does_not_end_cash_negative(self):
        before = read_cash(REQUEST_DATE)
        await self.handle(
            [("L1", SHORT, 500), ("L2", IN_STOCK, 500)], deadline=UNMEETABLE
        )
        assert read_cash(REQUEST_DATE) >= before


class TestTheRetryIsBounded(Flow):
    """Exactly one second attempt, so a request terminates rather than looping."""

    @pytest.fixture(autouse=True)
    def undersized_restock(self, monkeypatch):
        """A purchase that covers one unit of the shortfall.

        So the line reaching pass 2 is still short. That branch is an invariant
        guard expected at zero — a restock is sized to cover the shortfall
        exactly — and undersizing it is what makes "the line declines rather
        than being retried again" testable at all.
        """
        from beaver.replenishment import tools as replenishment_tools

        monkeypatch.setattr(
            replenishment_tools,
            "order_quantity",
            lambda shortfall_units, min_stock_level: 1,
        )

    async def test_a_line_still_short_after_pass_two_is_declined_not_retried(self):
        resolution = await self.handle([("L1", SHORT, 500), ("L2", IN_STOCK, 500)])
        assert delegations() == [
            "inventory",
            "quoting",
            "sales",
            "replenishment",
            "sales",
        ]
        assert resolution.outcome is Outcome.PARTIALLY_FULFILLED
        assert "unable to supply A4 paper" in resolution.customer_message

    async def test_the_line_is_committed_no_more_than_once_either_way(self):
        await self.handle([("L1", SHORT, 500), ("L2", IN_STOCK, 500)])
        assert [row["item_name"] for row in rows("sales")] == [IN_STOCK]
