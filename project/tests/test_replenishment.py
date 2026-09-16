"""The purchase decision: the sizing, the two guards in their order, and the money.

Everything replenishment decides is arithmetic over data it was handed — the
shortfall from inventory, the deadline from the customer, the floor from the
`inventory` table — so the rules are tested as ordinary functions, and the hard
invariants are asserted against the database the helpers actually write to.

What needs a model is the wiring: replenishment's envelope. It runs on a
`FunctionModel`, so no network and no key. Nothing calls this agent yet — the
orchestrator wiring is ticket 108 — so it is delegated to directly here, with
the step id the seam would have minted.

Seed 137 facts these tests lean on:

| item | stock | floor | shelf price |
|---|---|---|---|
| `A4 paper` | 272 | 135 | $0.05 |
| `Cardstock` | 595 | 148 | $0.15 |
| `Rolls of banner paper (36-inch width)` | 546 | 65 | $2.50 |
"""

import asyncio
import contextlib
from datetime import date

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_core import to_jsonable_python
from sqlalchemy import text

from beaver import ledger, starter
from beaver.audit import AgentDeps
from beaver.contract import AgentName, BlockerCode
from beaver.inventory.models import RestockNeed
from beaver.replenishment.agent import replenishment_agent
from beaver.replenishment.models import RestockRequest, RestockVerdict
from beaver.replenishment.tools import (
    cash_available,
    decide_restocks,
    order_quantity,
    plan_restocks,
    record_restock,
    reorder_thresholds,
    restock_arrival_date,
    supplier_cost_ratio,
)

STEP_ID = "20260915T120000Z:1:001"
REQUEST_DATE = date(2025, 4, 1)

#: 272 on the shelf at seed 137, a $0.05 shelf price and a 135 floor.
CHEAP = "A4 paper"
#: A $2.50 shelf price, so a small order still costs more than a large A4 one.
DEAR = "Rolls of banner paper (36-inch width)"


def a_need(shortfall: int, item_name: str = CHEAP, line_id: str = "L1") -> RestockNeed:
    """One shortfall as inventory emitted it."""
    return RestockNeed(
        line_id=line_id, item_name=item_name, shortfall_units=shortfall
    )


def stock_order_rows() -> list[dict]:
    """Every `stock_orders` transaction, oldest first, by rowid.

    `init_database` seeds the opening stock as `stock_orders` rows dated
    2025-01-01, so a test that counts rows filters on the request date rather
    than assuming an empty table.
    """
    with starter.engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT rowid AS rowid, item_name, units, price, transaction_date "
                "FROM transactions WHERE transaction_type = 'stock_orders' "
                "ORDER BY rowid"
            )
        ).fetchall()
    return [dict(row._mapping) for row in rows]


def bought_on(day: date) -> list[dict]:
    return [row for row in stock_order_rows() if row["transaction_date"] == day.isoformat()]


def links() -> list[dict]:
    with starter.engine().connect() as conn:
        rows = conn.execute(text("SELECT * FROM transaction_links")).fetchall()
    return [dict(row._mapping) for row in rows]


class TestTheFloorRead:
    """`min_stock_level` has no helper, so the tool reads the table directly —
    and must not touch `current_stock`, which nothing updates at runtime."""

    def test_it_reads_the_floor_and_the_shelf_price_for_each_item(self, seeded_db):
        [threshold] = reorder_thresholds([CHEAP])
        assert (threshold.min_stock_level, threshold.unit_price) == (135, 0.05)

    def test_it_reports_one_row_per_item_named(self, seeded_db):
        thresholds = reorder_thresholds([CHEAP, DEAR])
        assert [t.item_name for t in thresholds] == [CHEAP, DEAR]

    def test_it_carries_no_stock_figure_at_all(self, seeded_db):
        """`inventory.current_stock` is a trap: it still reads 272 for A4 paper
        after we have sold every sheet."""
        [threshold] = reorder_thresholds([CHEAP])
        assert "current_stock" not in threshold.model_dump()
        assert 272 not in threshold.model_dump().values()


class TestSizing:
    """`order_qty = shortfall_units + min_stock_level`, measured after the sale."""

    def test_it_covers_the_shortfall_and_rebuilds_the_floor(self):
        assert order_quantity(shortfall_units=9728, min_stock_level=135) == 9863

    def test_the_floor_is_what_is_left_once_the_order_has_shipped(self, seeded_db):
        """Measured before the sale, the order we just served would immediately
        eat the floor we just bought."""
        [plan] = plan_restocks([a_need(9728)], reorder_thresholds([CHEAP]), REQUEST_DATE)
        assert plan.order_qty == 9728 + 135

    def test_it_sizes_from_the_shortfall_it_was_handed(self, seeded_db):
        """Never from a stock reading of its own — that would be two agents
        answering one question."""
        [plan] = plan_restocks([a_need(1)], reorder_thresholds([CHEAP]), REQUEST_DATE)
        assert plan.order_qty == 136


class TestTwoLinesOneItem:
    """Two lines of one request can resolve to the same product — "printer
    paper" and "copy paper" are both `A4 paper` — and inventory emits a need
    per line, because it dedupes on `line_id` and not on item."""

    def test_they_become_one_purchase(self, seeded_db):
        needs = [a_need(100, CHEAP, "L1"), a_need(50, CHEAP, "L2")]
        plans = plan_restocks(needs, reorder_thresholds([CHEAP]), REQUEST_DATE)
        assert len(plans) == 1
        assert plans[0].line_ids == ["L1", "L2"]

    def test_the_floor_is_bought_once_and_not_once_per_line(self, seeded_db):
        """Two floors for one shelf is not what a target stock level is."""
        needs = [a_need(100, CHEAP, "L1"), a_need(50, CHEAP, "L2")]
        [plan] = plan_restocks(needs, reorder_thresholds([CHEAP]), REQUEST_DATE)
        assert plan.order_qty == 100 + 50 + 135

    async def test_one_row_is_written_and_both_lines_point_at_it(self, trail):
        """And the rowid both lines carry is the purchase's own.

        The provided helper's `last_insert_rowid()` cannot say which row that
        is — see `tests/test_ledger.py` — so `ledger.write_row` reads it back
        itself, and this asserts against the row rather than against the
        number the helper returned.
        """
        needs = [a_need(100, CHEAP, "L1"), a_need(50, CHEAP, "L2")]
        response = await restock(needs, trail)
        [purchase] = bought_on(REQUEST_DATE)
        rowids = response.internal.transaction_rowid_by_line
        assert set(rowids) == {"L1", "L2"}
        assert set(rowids.values()) == {purchase["rowid"]}
        assert len(links()) == 1

    async def test_both_lines_are_promised_the_one_arrival_date(self, trail):
        needs = [a_need(100, CHEAP, "L1"), a_need(50, CHEAP, "L2")]
        response = await restock(needs, trail)
        assert [item.line_id for item in response.customer.restocked] == ["L1", "L2"]
        assert len({item.available_from for item in response.customer.restocked}) == 1

    async def test_a_refusal_refuses_every_line_waiting_on_that_purchase(self, trail):
        needs = [a_need(9728, CHEAP, "L1"), a_need(50, CHEAP, "L2")]
        response = await restock(needs, trail, deadline=date(2025, 4, 2))
        assert set(response.internal.verdict_by_line) == {"L1", "L2"}
        assert {signal.line_id for signal in response.internal.signals} == {"L1", "L2"}

    async def test_the_internal_totals_do_not_collide(self, trail):
        """Every `*_by_item` field of the payload is keyed by item, so two
        purchases for one item would have overwritten each other's figures."""
        needs = [a_need(100, CHEAP, "L1"), a_need(50, CHEAP, "L2")]
        response = await restock(needs, trail)
        internal = response.internal
        assert internal.units_ordered_by_item == {CHEAP: 285}
        assert internal.total_spend == pytest.approx(
            sum(internal.spend_by_item.values())
        )


class TestADivergentCatalogue:
    """A name that passed `CarriedItemName` and is absent from the `inventory`
    table means the two have diverged. That is a bug, not a business outcome,
    and there is no blocker code for it."""

    def test_the_floor_read_refuses_rather_than_skipping(self, seeded_db, monkeypatch):
        monkeypatch.setattr(
            "beaver.contract.carried_catalogue", lambda: frozenset({"Nonesuch"})
        )
        with pytest.raises(ValueError, match="Nonesuch"):
            reorder_thresholds(["Nonesuch"])

    def test_planning_refuses_rather_than_buying_less_than_it_was_asked(self, seeded_db):
        """Buying part of a batch without saying so is the one answer worse
        than stopping: the trail would show a purchase and no refusal."""
        with pytest.raises(ValueError, match="A4 paper"):
            plan_restocks([a_need(100, CHEAP)], [], REQUEST_DATE)


class TestTheSupplierCostRatio:
    """`U(0.6, 0.8)`, drawn per item deterministically from the item name."""

    def test_it_lies_inside_the_band(self):
        for item in (CHEAP, DEAR, "Cardstock"):
            assert 0.6 <= supplier_cost_ratio(item) <= 0.8

    def test_it_is_stable_for_one_item_across_calls(self):
        assert supplier_cost_ratio(CHEAP) == supplier_cost_ratio(CHEAP)

    def test_it_is_stable_across_runs(self):
        """A per-purchase draw would be irreproducible under model
        nondeterminism: two graders running identical code would report
        different financials. This golden is what pins it."""
        assert supplier_cost_ratio(CHEAP) == pytest.approx(0.6789084635823266)

    def test_it_differs_between_items(self):
        assert supplier_cost_ratio(CHEAP) != supplier_cost_ratio(DEAR)

    def test_the_draw_order_does_not_move_it(self):
        """Keyed on the name rather than on a sequential generator, so the
        number and order of restocks in a run cannot change what we paid."""
        first = [supplier_cost_ratio(item) for item in (CHEAP, DEAR, "Cardstock")]
        second = [supplier_cost_ratio(item) for item in ("Cardstock", DEAR, CHEAP)]
        assert first == list(reversed(second))

    def test_the_cost_is_not_rounded_to_the_cent(self, seeded_db):
        """A4 paper sells at five cents: rounding its cost to the cent would
        quantise a ratio drawn from `U(0.6, 0.8)` to the ends of its range."""
        [plan] = plan_restocks([a_need(100)], reorder_thresholds([CHEAP]), REQUEST_DATE)
        assert plan.unit_cost == pytest.approx(0.05 * supplier_cost_ratio(CHEAP))
        assert plan.spend == round(plan.order_qty * plan.unit_cost, 2)


class TestTheArrivalDate:
    """When the goods reach *us*, sized by the quantity we are buying."""

    def test_it_is_sized_by_the_order(self, seeded_db):
        assert restock_arrival_date(REQUEST_DATE, 5) == REQUEST_DATE
        assert restock_arrival_date(REQUEST_DATE, 5000) == date(2025, 4, 8)

    def test_the_plan_carries_the_date_of_the_full_order(self, seeded_db):
        [plan] = plan_restocks([a_need(9728)], reorder_thresholds([CHEAP]), REQUEST_DATE)
        assert plan.arrival_date == restock_arrival_date(REQUEST_DATE, 9863)


def decide(needs, deadline=None, cash=1_000_000.0, thresholds=None):
    """Plan and rule on a batch, without a model and without writing anything.

    Keyed by line, because a decision is about a purchase and the caller asks
    about a line: two lines sharing an item share the one decision.
    """
    items = [need.item_name for need in needs]
    plans = plan_restocks(needs, thresholds or reorder_thresholds(items), REQUEST_DATE)
    return {
        line_id: decision
        for decision in decide_restocks(plans, deadline, cash)
        for line_id in decision.plan.line_ids
    }


class TestTheDeadlineGuard:
    """A restock that cannot arrive in time buys nothing at all."""

    def test_an_arrival_after_the_deadline_refuses_the_need(self, seeded_db):
        decisions = decide([a_need(9728)], deadline=date(2025, 4, 2))
        assert decisions["L1"].verdict is RestockVerdict.REFUSED_LATE
        assert decisions["L1"].verdict.blocker_code is BlockerCode.DEADLINE_UNMEETABLE

    def test_an_arrival_on_the_deadline_is_in_time(self, seeded_db):
        """The customer asked for the goods *by* that date, not before it."""
        arrival = restock_arrival_date(REQUEST_DATE, 9863)
        decisions = decide([a_need(9728)], deadline=arrival)
        assert decisions["L1"].verdict is RestockVerdict.RESTOCKED

    def test_no_deadline_means_nothing_to_miss(self, seeded_db):
        decisions = decide([a_need(9728)], deadline=None)
        assert decisions["L1"].verdict is RestockVerdict.RESTOCKED

    def test_it_refuses_that_need_and_no_other(self, seeded_db):
        """Every blocker is line-scoped: one late item does not close the purse
        on an item that arrives in time."""
        needs = [a_need(9728, CHEAP, "L1"), a_need(5, DEAR, "L2")]
        decisions = decide(needs, deadline=date(2025, 4, 2))
        assert decisions["L1"].verdict is RestockVerdict.REFUSED_LATE
        assert decisions["L2"].verdict is RestockVerdict.RESTOCKED

    def test_it_runs_before_the_cash_guard(self, seeded_db):
        """A need that is both late and unaffordable is refused as late: if we
        are not buying, there is nothing to afford."""
        decisions = decide([a_need(9728)], deadline=date(2025, 4, 2), cash=0.0)
        assert decisions["L1"].verdict is RestockVerdict.REFUSED_LATE

    async def test_a_late_refusal_buys_nothing_not_even_the_floor(self, trail):
        """A purchase sized only by `min_stock_level` is the threshold-driven
        buy the design ruled out — a trigger wearing a tripwire's name."""
        response = await restock([a_need(9728)], trail, deadline=date(2025, 4, 2))
        assert response.customer.restocked == []
        assert bought_on(REQUEST_DATE) == []
        assert response.internal.total_spend == 0.0


class TestTheCashGuard:
    """Per item, cheapest first, and all-or-nothing within an item."""

    def test_an_unaffordable_item_does_not_withhold_an_affordable_one(self, seeded_db):
        """Refusing A4 because banner rolls were unaffordable would decline a
        line we could have served."""
        needs = [a_need(200, DEAR, "L1"), a_need(100, CHEAP, "L2")]
        decisions = decide(needs, cash=20.0)
        assert decisions["L1"].verdict is RestockVerdict.REFUSED_CASH
        assert decisions["L2"].verdict is RestockVerdict.RESTOCKED

    def test_the_cheapest_is_weighed_first(self, seeded_db):
        """Order of evaluation, not order of arrival. With exactly the dear
        item's spend in hand, either item is affordable alone and only one can
        be had: weighing the dear one first would buy it and refuse the cheap
        one, serving one line instead of the one we could serve alongside it.

        This is the assertion that distinguishes cheapest-first from
        dearest-first at all — a cash figure that fits only the cheap item
        passes under both orders.
        """
        needs = [a_need(200, DEAR, "L1"), a_need(100, CHEAP, "L2")]
        planned = decide(needs)
        dear_spend = planned["L1"].plan.spend
        assert planned["L2"].plan.spend < dear_spend
        decisions = decide(needs, cash=dear_spend)
        assert decisions["L2"].verdict is RestockVerdict.RESTOCKED
        assert decisions["L1"].verdict is RestockVerdict.REFUSED_CASH

    def test_within_an_item_it_is_all_or_nothing(self, seeded_db):
        """Half of what a line needs is money out with the line still
        declined."""
        spend = decide([a_need(9728)])["L1"].plan.spend
        decisions = decide([a_need(9728)], cash=spend - 0.01)
        assert decisions["L1"].verdict is RestockVerdict.REFUSED_CASH
        assert decisions["L1"].plan.order_qty == 9863

    def test_each_purchase_draws_down_what_is_left(self, seeded_db):
        needs = [a_need(100, CHEAP, "L1"), a_need(100, "Cardstock", "L2")]
        both = decide(needs)
        together = both["L1"].plan.spend + both["L2"].plan.spend
        assert all(d.verdict is RestockVerdict.RESTOCKED for d in decide(needs, cash=together).values())
        short = decide(needs, cash=together - 0.01)
        assert [d.verdict for d in short.values()].count(RestockVerdict.REFUSED_CASH) == 1

    def test_the_code_it_raises_is_internal_only(self):
        assert BlockerCode.CASH_INSUFFICIENT.internal_only

    async def test_the_revenue_from_this_order_is_not_cash_on_hand(self, trail):
        """We cannot spend money we have only just been paid for goods we are
        about to buy."""
        held = cash_available(REQUEST_DATE)
        response = await restock(
            [a_need(9728)], trail, committed_revenue=held - 1.0
        )
        assert response.internal.cash_on_hand == pytest.approx(1.0)
        assert response.customer.restocked == []
        assert response.internal.verdict_by_line["L1"] is RestockVerdict.REFUSED_CASH


def scripted_replenishment(request: RestockRequest):
    """The model replenishment runs under: the reads, then the request back.

    Its model has no judgement to exercise — every number in the envelope is
    read or computed after it returns — so the script reads the three tools and
    hands the needs back unchanged.
    """
    final = to_jsonable_python({"request": request})

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "reorder_thresholds",
                        {"item_names": sorted({n.item_name for n in request.needs})},
                    ),
                    ToolCallPart(
                        "cash_available", {"as_of_date": request.request_date.isoformat()}
                    ),
                ]
            )
        return ModelResponse(parts=[ToolCallPart("final_result", final)])

    return FunctionModel(model)


async def restock(needs, trail, deadline=None, committed_revenue=0.0):
    """Run replenishment on its own, with the step id the seam would have minted."""
    request = RestockRequest(
        request_date=REQUEST_DATE,
        deadline=deadline,
        needs=needs,
        committed_revenue=committed_revenue,
    )
    deps = AgentDeps(trail=trail, request_id="1", current_step_id=STEP_ID)
    with replenishment_agent.override(model=scripted_replenishment(request)):
        result = await replenishment_agent.run("buy what we are short of", deps=deps)
    return result.output


class TestMoneyLeaves:
    """A restock is money out and stock in, on the same day."""

    async def test_it_buys_the_stock_and_cash_falls_by_the_spend(self, trail):
        before = cash_available(REQUEST_DATE)
        response = await restock([a_need(9728)], trail)
        [item] = response.customer.restocked
        assert item.item_name == CHEAP
        spend = response.internal.total_spend
        assert spend > 0
        assert cash_available(REQUEST_DATE) == pytest.approx(before - spend)
        assert response.internal.cash_after - response.internal.cash_before == pytest.approx(
            -spend
        )

    async def test_the_row_is_written_at_the_total_spend(self, trail):
        """`get_cash_balance` sums the `price` column directly, so a unit cost
        there would understate this purchase by the quantity."""
        response = await restock([a_need(9728)], trail)
        [row] = bought_on(REQUEST_DATE)
        assert (row["item_name"], row["units"]) == (CHEAP, 9863)
        assert row["price"] == pytest.approx(response.internal.total_spend)
        assert row["price"] != response.internal.unit_cost_by_item[CHEAP]

    async def test_it_is_booked_on_the_request_date_and_visible_that_day(self, trail):
        """`get_stock_level` filters transactions by date, so the booking date
        decides when the stock exists — and sales' pass-2 re-check reads the
        request date."""
        await restock([a_need(9728)], trail)
        level = starter.get_stock_level(CHEAP, REQUEST_DATE.isoformat())
        assert int(level["current_stock"].iloc[0]) == 272 + 9863

    async def test_the_lead_time_reaches_the_promise_and_not_the_booking(self, trail):
        response = await restock([a_need(9728)], trail)
        [item] = response.customer.restocked
        [row] = bought_on(REQUEST_DATE)
        assert row["transaction_date"] == REQUEST_DATE.isoformat()
        assert item.available_from > REQUEST_DATE

    async def test_every_purchase_is_traceable_to_the_request(self, trail):
        response = await restock([a_need(9728)], trail)
        [link] = links()
        [rowid] = response.internal.transaction_rowid_by_line.values()
        assert (link["transaction_rowid"], link["request_id"], link["step_id"]) == (
            rowid,
            "1",
            STEP_ID,
        )


class TestTheEnvelope:
    """All three verdicts, and what each half of the envelope carries."""

    async def test_a_bought_need_is_restocked(self, trail):
        response = await restock([a_need(100)], trail)
        assert response.internal.verdict_by_line["L1"] is RestockVerdict.RESTOCKED
        assert response.internal.signals == []
        assert response.agent is AgentName.REPLENISHMENT
        assert response.step_id == STEP_ID

    async def test_a_late_need_is_refused_late_and_signals_the_deadline(self, trail):
        response = await restock([a_need(9728)], trail, deadline=date(2025, 4, 2))
        assert response.internal.verdict_by_line["L1"] is RestockVerdict.REFUSED_LATE
        [signal] = response.internal.signals
        assert (signal.line_id, signal.code) == ("L1", BlockerCode.DEADLINE_UNMEETABLE)
        assert signal.detail

    async def test_an_unaffordable_need_is_refused_on_cash(self, trail):
        held = cash_available(REQUEST_DATE)
        response = await restock([a_need(9728)], trail, committed_revenue=held)
        assert response.internal.verdict_by_line["L1"] is RestockVerdict.REFUSED_CASH
        [signal] = response.internal.signals
        assert signal.code is BlockerCode.CASH_INSUFFICIENT

    async def test_a_refused_need_is_absent_from_the_customer_payload(self, trail):
        response = await restock([a_need(9728)], trail, deadline=date(2025, 4, 2))
        assert response.customer.restocked == []
        assert response.internal.transaction_rowid_by_line == {}

    async def test_the_cash_refusal_reaches_no_customer_facing_payload(self, trail):
        """`CASH_INSUFFICIENT` is internal-only, and the payload that crosses
        the seam has no field it could occupy: a refused need is simply not in
        `restocked`."""
        held = cash_available(REQUEST_DATE)
        response = await restock([a_need(9728)], trail, committed_revenue=held)
        assert BlockerCode.CASH_INSUFFICIENT.internal_only
        assert response.customer.restocked == []
        assert "cash_insufficient" not in response.customer.model_dump_json()

    async def test_the_view_carries_no_cost_and_no_cash(self, trail):
        """What we pay our supplier is the one number a margin could be
        inferred from, and the business claims none."""
        view = (await restock([a_need(9728)], trail)).to_view()
        assert "unit_cost_by_item" not in view.model_dump_json()
        assert "cash" not in view.model_dump_json()

    async def test_every_bought_need_has_a_rowid_and_every_rowid_a_need(self, trail):
        needs = [a_need(100, CHEAP, "L1"), a_need(100, DEAR, "L2")]
        response = await restock(needs, trail)
        assert set(response.internal.transaction_rowid_by_line) == {
            item.line_id for item in response.customer.restocked
        }
        assert len(bought_on(REQUEST_DATE)) == 2

    async def test_the_batch_is_one_delegation_carrying_every_shortfall(self, trail):
        """A request can short on several lines at once."""
        needs = [a_need(100, CHEAP, "L1"), a_need(100, "Cardstock", "L2")]
        response = await restock(needs, trail)
        assert set(response.internal.units_ordered_by_item) == {CHEAP, "Cardstock"}
        assert set(response.internal.spend_by_item) == {CHEAP, "Cardstock"}
        assert response.internal.total_spend == pytest.approx(
            sum(response.internal.spend_by_item.values())
        )


class TestTheSharedDoor:
    """Sales and replenishment write through one lock, not one each."""

    async def test_two_writers_cannot_interleave_inside_the_helper(
        self, trail, monkeypatch
    ):
        """A lock per agent would serialise each against itself and neither
        against the other — the one arrangement that looks safe and is not."""
        overlapped = await _race(trail, monkeypatch)
        assert overlapped == []

    def test_it_survives_the_harness_running_one_loop_per_request(
        self, trail, monkeypatch
    ):
        """The harness calls `asyncio.run()` once per request. A single
        `asyncio.Lock` binds to the loop that first contends it and raises on
        every later one — so this would have failed on the second request, and
        only on runs where two writers actually overlapped."""
        for _ in range(2):
            assert asyncio.run(_race(trail, monkeypatch)) == []

    async def test_the_same_two_writers_do_overlap_with_the_lock_taken_away(
        self, trail, monkeypatch
    ):
        """The other half of the claim: the hazard is real."""
        monkeypatch.setattr(ledger, "write_lock", contextlib.nullcontext())
        overlapped = await _race(trail, monkeypatch)
        assert overlapped != []


class TestTheTwoLinksOnASale:
    """The ledger's link joins a caller's transaction when it is given one."""

    async def test_a_sale_writes_both_of_its_links_in_one_transaction(
        self, trail, monkeypatch
    ):
        """Sales has two facts about one row — the request that caused it and
        the offer it honoured — and a row traceable to one but not the other is
        half a record. So the `quote_fulfilments` insert failing must take the
        `transaction_links` row with it."""
        from beaver.quoting.tools import price_line, price_of
        from beaver.sales.tools import record_sale

        with starter.engine().begin() as conn:
            conn.execute(text("DROP TABLE quote_fulfilments"))

        with contextlib.suppress(Exception):
            await record_sale(
                [
                    price_line(
                        line_id="L1",
                        quote_line_id=f"{STEP_ID}:L1",
                        item_name="Cardstock",
                        units=100,
                        unit_price=price_of("Cardstock"),
                    )
                ],
                run_id=trail.run_id,
                request_id="1",
                step_id=STEP_ID,
                sold_on=REQUEST_DATE,
            )
        assert links() == []


async def _race(trail, monkeypatch) -> list[str]:
    """Run a sale and a restock at once through a deliberately slow helper."""
    import time

    from beaver.quoting.tools import price_line, price_of
    from beaver.sales.tools import record_sale

    real = starter.create_transaction
    inside: list[str] = []
    overlapped: list[str] = []

    def slow(item_name, transaction_type, quantity, price, date_):
        if inside:
            overlapped.append(item_name)
        inside.append(item_name)
        time.sleep(0.05)
        rowid = real(item_name, transaction_type, quantity, price, date_)
        time.sleep(0.05)
        inside.pop()
        return rowid

    monkeypatch.setattr(starter, "create_transaction", slow)

    async def sell():
        return await record_sale(
            [
                price_line(
                    line_id="L1",
                    quote_line_id=f"{STEP_ID}:L1",
                    item_name="Cardstock",
                    units=100,
                    unit_price=price_of("Cardstock"),
                )
            ],
            run_id=trail.run_id,
            request_id="1",
            step_id=STEP_ID,
            sold_on=REQUEST_DATE,
        )

    async def buy():
        plans = plan_restocks([a_need(100)], reorder_thresholds([CHEAP]), REQUEST_DATE)
        return await record_restock(
            plans,
            run_id=trail.run_id,
            request_id="1",
            step_id=STEP_ID,
            bought_on=REQUEST_DATE,
        )

    await asyncio.gather(sell(), buy())
    return overlapped
