"""Commitment: the re-check, the verify-all-then-write rule, and the money.

Everything sales decides is arithmetic over one stock reading, so it is tested
without a model: `verify_lines` and `promised_date` are ordinary functions and
the hard invariants — the line total reaching `create_transaction`, the rowid
on every committed line, nothing written when verification fails — are asserted
against the database the helpers actually write to.

What needs a model is the wiring: sales' envelope, and the third delegation the
orchestrator makes. Both run on a `FunctionModel`, so no network and no key.
"""

import asyncio
import contextlib
import json

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_core import to_jsonable_python
from sqlalchemy import text

from tests.messages import (
    NEEDS,
    PRICED,
    availability_handed_down,
    handed_down,
    handed_up,
)

from beaver import ledger, orchestrator, starter
from beaver.audit import AgentDeps
from beaver.contract import AgentName, BlockerCode
from beaver.inventory.agent import inventory_agent
from beaver.orchestrator import orchestrator_agent
from beaver.quoting.agent import quoting_agent
from beaver.replenishment.agent import replenishment_agent
from beaver.quoting.tools import price_line, price_of, quote_total
from beaver.sales.agent import sales_agent
from beaver.sales.models import LineVerdict
from beaver.sales.tools import (
    promised_date,
    read_cash,
    record_sale,
    snapshot_financials,
    verify_lines,
)

STEP_ID = "20260915T120000Z:1:001"
REQUEST_DATE = "2025-04-01"


def a_line(units: int, item_name: str = "A4 paper", line_id: str = "L1"):
    """One line as quoting priced it, without an agent."""
    return price_line(
        line_id=line_id,
        quote_line_id=f"{STEP_ID}:{line_id}",
        item_name=item_name,
        units=units,
        unit_price=price_of(item_name),
    )


def sales_rows() -> list[dict]:
    """Every `sales` transaction against an item, oldest first, by rowid.

    `transactions.id` is NULL for every row written at runtime — the seeded
    schema has the column and `create_transaction` never fills it — so the
    rowid is the only key, and the only thing `transaction_links` can join on.

    The `item_name IS NULL` row is excluded on purpose: `init_database` books
    the opening $50,000 of cash as a `sales` transaction against no item, so a
    reader that counted it would call the float a sale.
    """
    with starter.engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT rowid AS rowid, item_name, units, price, transaction_date "
                "FROM transactions WHERE transaction_type = 'sales' "
                "AND item_name IS NOT NULL ORDER BY rowid"
            )
        ).fetchall()
    return [dict(row._mapping) for row in rows]


def links() -> list[dict]:
    with starter.engine().connect() as conn:
        rows = conn.execute(text("SELECT * FROM transaction_links")).fetchall()
    return [dict(row._mapping) for row in rows]


def fulfilments() -> list[dict]:
    with starter.engine().connect() as conn:
        rows = conn.execute(text("SELECT * FROM quote_fulfilments")).fetchall()
    return [dict(row._mapping) for row in rows]


class TestTheCommitTimeRecheck:
    """Stock is read once, at the moment of commitment, and every line is judged
    against that one reading — including against what the lines above it took."""

    def test_a_line_we_hold_commits(self, seeded_db):
        [decision] = verify_lines([a_line(500)], {"A4 paper": 4_000})
        assert decision.verdict is LineVerdict.COMMITTED
        assert decision.stock_on_hand == 4_000

    def test_a_line_we_are_short_of_declines(self, seeded_db):
        [decision] = verify_lines([a_line(500)], {"A4 paper": 120})
        assert decision.verdict is LineVerdict.DECLINED
        assert "500" in decision.detail and "120" in decision.detail

    def test_a_part_short_line_is_declined_whole_rather_than_split(self, seeded_db):
        """188 of the 200 asked for is still a refusal. The line is the
        indivisible unit of decision everywhere else in this design."""
        [decision] = verify_lines([a_line(200, "Colored paper")], {"Colored paper": 188})
        assert decision.verdict is LineVerdict.DECLINED

    def test_a_line_exactly_covered_commits(self, seeded_db):
        [decision] = verify_lines([a_line(200)], {"A4 paper": 200})
        assert decision.verdict is LineVerdict.COMMITTED

    def test_an_item_we_do_not_hold_at_all_declines(self, seeded_db):
        """`get_all_inventory` omits items at zero, so a missing key is a real
        reading of nought and not a gap in the snapshot."""
        [decision] = verify_lines([a_line(10)], {})
        assert decision.verdict is LineVerdict.DECLINED
        assert decision.stock_on_hand == 0

    def test_two_lines_on_one_item_draw_on_the_same_shelf(self, seeded_db):
        """One reading, not one per line: the second line cannot be sold stock
        the first has already taken."""
        first, second = verify_lines(
            [a_line(300, line_id="L1"), a_line(300, line_id="L2")],
            {"A4 paper": 500},
        )
        assert (first.verdict, second.verdict) == (
            LineVerdict.COMMITTED,
            LineVerdict.DECLINED,
        )
        assert second.stock_on_hand == 200

    def test_a_short_line_does_not_consume_the_stock_it_was_refused(self, seeded_db):
        """A declined line ships nothing, so it takes nothing: its sibling is
        judged against the shelf as it still stands."""
        first, second = verify_lines(
            [a_line(600, line_id="L1"), a_line(300, line_id="L2")],
            {"A4 paper": 500},
        )
        assert (first.verdict, second.verdict) == (
            LineVerdict.DECLINED,
            LineVerdict.COMMITTED,
        )

    def test_the_snapshot_reads_stock_and_cash_in_one_call(self, seeded_db):
        snapshot = snapshot_financials(["A4 paper"], REQUEST_DATE)
        assert set(snapshot.stock_by_item) == {"A4 paper"}
        assert snapshot.stock_by_item["A4 paper"] > 0
        assert snapshot.cash_balance == read_cash(REQUEST_DATE)

    def test_the_snapshot_sees_a_sale_written_a_moment_earlier(self, seeded_db):
        """The point of re-reading at commit time: an earlier request in the
        same run may have taken the stock the survey saw."""
        before = snapshot_financials(["A4 paper"], REQUEST_DATE).stock_by_item["A4 paper"]
        starter.create_transaction("A4 paper", "sales", 100, 5.00, REQUEST_DATE)
        after = snapshot_financials(["A4 paper"], REQUEST_DATE).stock_by_item["A4 paper"]
        assert after == before - 100


class TestTheDeliveryPromise:
    """A property of where the goods are, never of how many were asked for."""

    def test_a_line_filled_from_stock_is_promised_the_request_date(self, seeded_db):
        request_date = _date(REQUEST_DATE)
        assert promised_date("A4 paper", request_date, {}) == request_date

    def test_a_line_we_bought_in_is_promised_the_date_it_reaches_us(self, seeded_db):
        """Pass 2's rule, built now because sales must be safely callable twice
        before anything calls it twice."""
        arrival = _date("2025-04-08")
        assert promised_date("A4 paper", _date(REQUEST_DATE), {"A4 paper": arrival}) == arrival

    def test_a_quantity_never_moves_the_promise(self, seeded_db):
        """The supplier's lead time describes a purchase. Applying it to goods
        on our own shelf refuses orders we could fill today."""
        assert promised_date("A4 paper", _date(REQUEST_DATE), {}) == _date(REQUEST_DATE)


def _date(iso: str):
    from datetime import date

    return date.fromisoformat(iso)


class TestTheOrderTotal:
    """Quoting's arithmetic over the lines that committed. Sales prices
    nothing, so the order's total has no second definition here."""

    def test_it_is_the_sum_of_the_committed_line_totals(self, seeded_db):
        assert quote_total([a_line(500), a_line(300, line_id="L2")]) == 38.75

    def test_nothing_committed_comes_to_nothing(self, seeded_db):
        assert quote_total([]) == 0.0


def scripted_sales(lines, as_of_date: str = REQUEST_DATE, availability=None):
    """The model sales runs under: one snapshot, then the lines back unchanged.

    Sales' model has no judgement to exercise — every number in the envelope is
    read or computed after it returns — so the script is the shortest one in
    the suite, and that is the point rather than a shortcut.
    """
    payload = to_jsonable_python(lines)
    final = {"lines": payload, "as_of_date": as_of_date}
    if availability is not None:
        final["earliest_availability"] = {
            item: day.isoformat() for item, day in availability.items()
        }

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "snapshot_financials",
                        {
                            "item_names": sorted({line.item_name for line in lines}),
                            "as_of_date": as_of_date,
                        },
                    )
                ]
            )
        return ModelResponse(parts=[ToolCallPart("final_result", final)])

    return FunctionModel(model)


async def sell(lines, trail, as_of_date: str = REQUEST_DATE, availability=None):
    """Run sales on its own, with the step id the seam would have minted."""
    deps = AgentDeps(trail=trail, request_id="1", current_step_id=STEP_ID)
    with sales_agent.override(model=scripted_sales(lines, as_of_date, availability)):
        result = await sales_agent.run("commit these", deps=deps)
    return result.output


#: 595 on the shelf at seed 137, so a 500-unit line commits.
IN_STOCK = "Cardstock"
#: 272 on the shelf, so a 500-unit line is short by 228.
SHORT = "A4 paper"


class TestMoneyMoves:
    """A fully-stocked request commits, and the cash balance says so."""

    async def test_every_line_commits_and_cash_rises_by_the_order_total(self, trail):
        before = read_cash(REQUEST_DATE)
        response = await sell([a_line(500, IN_STOCK)], trail)
        assert [line.line_id for line in response.customer.committed] == ["L1"]
        assert response.customer.order_total == 71.25
        assert read_cash(REQUEST_DATE) == pytest.approx(before + 71.25)
        assert response.internal.cash_after - response.internal.cash_before == pytest.approx(
            71.25
        )

    async def test_the_transaction_is_written_at_the_line_total(self, trail):
        """`get_cash_balance` sums the `price` column directly, so a unit price
        there would understate this sale by 500x."""
        await sell([a_line(500, IN_STOCK)], trail)
        [row] = sales_rows()
        assert (row["item_name"], row["units"], row["price"]) == (IN_STOCK, 500, 71.25)
        assert row["price"] != 0.15
        assert row["transaction_date"] == REQUEST_DATE

    async def test_the_cash_delta_is_the_sum_of_the_line_totals(self, trail):
        before = read_cash(REQUEST_DATE)
        lines = [a_line(500, IN_STOCK, "L1"), a_line(200, "Glossy paper", "L2")]
        response = await sell(lines, trail)
        assert read_cash(REQUEST_DATE) - before == pytest.approx(
            sum(line.line_total for line in lines)
        )
        assert response.customer.order_total == pytest.approx(
            sum(row["price"] for row in sales_rows())
        )

    async def test_lines_filled_from_stock_are_promised_the_request_date(self, trail):
        response = await sell([a_line(500, IN_STOCK)], trail)
        [committed] = response.customer.committed
        assert committed.promised_delivery_date.isoformat() == REQUEST_DATE
        assert response.customer.promised_delivery_date == committed.promised_delivery_date

    async def test_the_order_is_promised_the_latest_of_its_lines(self, trail):
        """An order is delivered when its last item arrives."""
        arrival = _date("2025-04-09")
        response = await sell(
            [a_line(500, IN_STOCK, "L1"), a_line(100, "Glossy paper", "L2")],
            trail,
            availability={"Glossy paper": arrival},
        )
        assert response.customer.promised_delivery_date == arrival


class TestTheCommitmentInvariant:
    """A line is in `committed` iff it has a rowid, and each rowid is threaded
    back to the request that caused it."""

    async def test_every_committed_line_has_a_rowid_and_every_rowid_a_line(self, trail):
        response = await sell(
            [a_line(500, IN_STOCK, "L1"), a_line(500, SHORT, "L2")], trail
        )
        committed = {line.line_id for line in response.customer.committed}
        assert committed == set(response.internal.transaction_rowid_by_line)
        assert committed == {"L1"}
        assert {row["rowid"] for row in sales_rows()} == set(
            response.internal.transaction_rowid_by_line.values()
        )

    async def test_each_rowid_joins_back_to_the_request_that_caused_it(self, trail):
        response = await sell([a_line(500, IN_STOCK)], trail)
        [link] = links()
        assert link["transaction_rowid"] == response.internal.transaction_rowid_by_line["L1"]
        assert (link["request_id"], link["step_id"]) == ("1", STEP_ID)
        assert link["run_id"] == trail.run_id

    async def test_one_link_row_per_committed_line_and_no_more(self, trail):
        await sell([a_line(500, IN_STOCK, "L1"), a_line(500, SHORT, "L2")], trail)
        assert len(links()) == 1 == len(sales_rows())

    async def test_the_sale_joins_back_to_the_quote_it_fulfils(self, trail):
        line = a_line(500, IN_STOCK)
        await sell([line], trail)
        [fulfilment] = fulfilments()
        assert fulfilment["quote_line_id"] == line.quote_line_id

    async def test_a_line_reported_twice_is_committed_once(self, trail):
        """The one thing here that cannot be taken back."""
        response = await sell([a_line(100, IN_STOCK), a_line(100, IN_STOCK)], trail)
        assert len(response.customer.committed) == 1
        assert len(sales_rows()) == 1


class TestVerifyAllThenWrite:
    """Nothing here can be undone, so the whole order is decided before the
    first row is written."""

    async def test_a_failure_during_verification_writes_nothing_at_all(
        self, trail, monkeypatch
    ):
        """Halfway through deciding is the dangerous moment: an order that
        half-commits has no repair in this database."""
        from beaver.sales import agent as sales_module

        real = sales_module.verify_lines

        def fail_midway(lines, stock_by_item):
            real(lines[:1], stock_by_item)
            raise RuntimeError("the shelf caught fire")

        monkeypatch.setattr(sales_module, "verify_lines", fail_midway)
        with pytest.raises(Exception):
            await sell([a_line(100, IN_STOCK, "L1"), a_line(100, IN_STOCK, "L2")], trail)
        assert sales_rows() == []
        assert links() == []

    async def test_a_short_line_declines_and_its_siblings_still_commit(self, trail):
        """One bad line does not sink the order."""
        response = await sell(
            [a_line(500, SHORT, "L1"), a_line(500, IN_STOCK, "L2")], trail
        )
        assert [line.line_id for line in response.customer.declined] == ["L1"]
        assert [line.line_id for line in response.customer.committed] == ["L2"]
        assert response.customer.order_total == 71.25

    async def test_the_short_line_raises_insufficient_stock_from_sales(self, trail):
        response = await sell([a_line(500, SHORT)], trail)
        [signal] = response.internal.signals
        assert signal.code is BlockerCode.INSUFFICIENT_STOCK
        assert signal.line_id == "L1"
        assert "272" in signal.detail
        assert response.internal.verdict_by_line == {"L1": LineVerdict.DECLINED}

    async def test_an_order_with_no_survivors_writes_nothing(self, trail):
        response = await sell([a_line(500, SHORT)], trail)
        assert response.customer.committed == []
        assert response.customer.promised_delivery_date is None
        assert response.customer.order_total == 0.0
        assert sales_rows() == []

    async def test_a_part_short_line_is_declined_whole_rather_than_split(self, trail):
        """272 of the 500 asked for. We never ship 272 and invoice for them."""
        response = await sell([a_line(500, SHORT)], trail)
        assert response.customer.declined[0].units == 500
        assert sales_rows() == []


class TestWhatCrossesTheSeam:
    """Cash, stock and margin stay on the internal half. No profit figure is
    claimed anywhere at all."""

    async def test_no_cash_or_stock_figure_reaches_the_customer_payload(self, trail):
        response = await sell(
            [a_line(500, IN_STOCK, "L1"), a_line(500, SHORT, "L2")], trail
        )
        customer = response.customer.model_dump_json().lower()
        for forbidden in ("cash", "margin", "profit", "stock_on_hand", "45059"):
            assert forbidden not in customer

    async def test_the_internal_half_carries_the_books(self, trail):
        response = await sell([a_line(500, IN_STOCK)], trail)
        assert response.internal.cash_before == 45059.70
        assert response.internal.stock_read_by_item == {IN_STOCK: 595}

    async def test_no_profit_figure_is_claimed_anywhere(self, trail):
        """Stock bought before the supplier cost ratio existed and stock bought
        under it sit in the same bin at different costs, so any margin would be
        a guess dressed as an accounting fact."""
        response = await sell([a_line(500, IN_STOCK)], trail)
        dumped = response.model_dump_json().lower()
        assert "margin" not in dumped and "profit" not in dumped


class TestCallableTwice:
    """Sales is stateless and never learns that a retry happened. Nothing calls
    it twice yet; it is built that way now because it is unbuildable later."""

    async def test_a_restocked_line_commits_on_a_second_call_and_only_once(self, trail):
        short = a_line(500, SHORT)
        first = await sell([short], trail)
        assert first.customer.committed == []

        # What replenishment will do: booked on the day it is paid for, so
        # sales' re-check actually sees it.
        starter.create_transaction(SHORT, "stock_orders", 228, 11.40, REQUEST_DATE)
        arrival = _date("2025-04-05")
        second = await sell([short], trail, availability={SHORT: arrival})

        assert [line.line_id for line in second.customer.committed] == ["L1"]
        assert second.customer.committed[0].promised_delivery_date == arrival
        assert len(sales_rows()) == 1
        assert len(links()) == 1


class TestTheWriteLock:
    """`create_transaction` runs `INSERT` then `SELECT last_insert_rowid()` as
    two statements through a pooled engine, and `last_insert_rowid()` is
    per-connection."""

    async def test_two_commits_cannot_interleave_inside_the_helper(
        self, trail, monkeypatch
    ):
        """Without the lock the two writers overlap inside the helper and the
        rowid one of them reports belongs to the other's sale."""
        import time

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

        async def commit(item_name: str, line_id: str):
            return await record_sale(
                [a_line(100, item_name, line_id)],
                run_id=trail.run_id,
                request_id="1",
                step_id=STEP_ID,
                sold_on=_date(REQUEST_DATE),
            )

        first, second = await asyncio.gather(
            commit(IN_STOCK, "L1"), commit("Glossy paper", "L2")
        )

        assert overlapped == []
        by_rowid = {row["rowid"]: row["item_name"] for row in sales_rows()}
        assert by_rowid[first["L1"]] == IN_STOCK
        assert by_rowid[second["L2"]] == "Glossy paper"

    async def test_the_same_two_writers_do_overlap_with_the_lock_taken_away(
        self, trail, monkeypatch
    ):
        """The other half of the claim: the hazard is real, and the lock is
        what removes it. Without this, the test above asserts only that two
        calls happened to run in order."""
        import time

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
        monkeypatch.setattr(ledger, "write_lock", contextlib.nullcontext())

        async def commit(item_name: str, line_id: str):
            return await record_sale(
                [a_line(100, item_name, line_id)],
                run_id=trail.run_id,
                request_id="1",
                step_id=STEP_ID,
                sold_on=_date(REQUEST_DATE),
            )

        await asyncio.gather(commit(IN_STOCK, "L1"), commit("Glossy paper", "L2"))
        assert overlapped != []

    async def test_a_row_is_traceable_before_the_next_one_is_written(
        self, trail, monkeypatch
    ):
        """A write that fails part-way leaves no untraceable money: each row is
        linked to its request in the same breath as it is written."""
        real = starter.create_transaction

        def fail_on_the_second(item_name, transaction_type, quantity, price, date_):
            if sales_rows():
                raise RuntimeError("the connection dropped")
            return real(item_name, transaction_type, quantity, price, date_)

        monkeypatch.setattr(starter, "create_transaction", fail_on_the_second)

        with pytest.raises(RuntimeError):
            await record_sale(
                [a_line(100, IN_STOCK, "L1"), a_line(100, "Glossy paper", "L2")],
                run_id=trail.run_id,
                request_id="1",
                step_id=STEP_ID,
                sold_on=_date(REQUEST_DATE),
            )

        assert len(sales_rows()) == 1
        assert [link["transaction_rowid"] for link in links()] == [
            sales_rows()[0]["rowid"]
        ]
        assert len(fulfilments()) == 1


#: What a customer says for the items these tests order. Inventory has to
#: resolve them before quoting or sales ever see a catalogue name.
_AS_STATED = {"A4 paper": "printer paper", "Cardstock": "cardstock"}


def scripted_flow(lines, as_of_date: str = REQUEST_DATE, seen: list | None = None):
    """The whole sequence under one scripted model: inventory, quoting, sales.

    The orchestrator's calls to quoting and `place_order` are built from what
    the previous delegation actually handed up, rather than from constants — so
    the test exercises the real hand-off, including the `quote_line_id` quoting
    minted and the prices it computed.

    The deadline it passes is the request date itself. These are ticket 105's
    tests and their subject is pass 1, so a same-day deadline keeps the retry's
    purse shut — no supplier reaches us the day we order — and a short line
    stays short. The retry itself is `test_retry.py`.
    """
    requested = [
        {
            "line_id": line_id,
            "item_as_stated": _AS_STATED[item_name],
            "quantity_as_stated": units,
            "unit_as_stated": "sheets",
        }
        for line_id, item_name, units in lines
    ]
    resolved = [
        {
            "line_id": line_id,
            "item_name": item_name,
            "category": "paper",
            "quantity": units,
        }
        for line_id, item_name, units in lines
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
                            {"lines": requested, "as_of_date": as_of_date},
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
            if len(messages) == 5:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "place_order",
                            {
                                "lines": handed_up(messages, "consult_quoting")[
                                    "quoted_lines"
                                ],
                                "as_of_date": as_of_date,
                                "deadline": as_of_date,
                            },
                        )
                    ]
                )
            return ModelResponse(parts=[TextPart(a_letter(handed_up(messages, "place_order")))])
        if "catalogue_price" in tools:
            if len(messages) == 1:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "catalogue_price",
                            {"item_names": [line["item_name"] for line in resolved]},
                        )
                    ]
                )
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "final_result",
                        {"lines": resolved, "probes": [], "as_of_date": as_of_date},
                    )
                ]
            )
        if "snapshot_financials" in tools:
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
            final = {"lines": quoted, "as_of_date": as_of_date}
            availability = availability_handed_down(messages)
            if availability:
                final["earliest_availability"] = availability
            return ModelResponse(parts=[ToolCallPart("final_result", final)])
        if "reorder_thresholds" in tools:
            [request] = handed_down(messages, NEEDS)
            if len(messages) == 1:
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "reorder_thresholds",
                            {
                                "item_names": sorted(
                                    {need["item_name"] for need in request["needs"]}
                                )
                            },
                        ),
                        ToolCallPart(
                            "cash_available", {"as_of_date": request["request_date"]}
                        ),
                    ]
                )
            return ModelResponse(
                parts=[ToolCallPart("final_result", {"request": request})]
            )
        if len(messages) == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "shortlist_candidates",
                        {"items_as_stated": [line["item_as_stated"] for line in requested]},
                    )
                ]
            )
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "final_result", {"lines": requested, "as_of_date": as_of_date}
                )
            ]
        )

    return FunctionModel(model)


def a_letter(sales: dict) -> str:
    """The reply rendered the way the orchestrator's instructions ask for it.

    A stand-in for the live model's own words, so what is under test is whether
    the payload *lets* the letter be written — the lines we sold and the date we
    promised them for — rather than a particular turn of phrase.
    """
    sentences = [
        f"{line['units']} units of {line['item_name']} for ${line['line_total']:.2f}"
        for line in sales.get("committed", [])
    ]
    if sales.get("promised_delivery_date"):
        sentences.append(f"delivered on {sales['promised_delivery_date']}")
    for line in sales.get("declined", []):
        sentences.append(f"we are unable to supply {line['item_name']} at present")
    return ". ".join(sentences) or "We could not help with this enquiry."


class TestThroughTheOrchestrator:
    """Inventory, quoting, then sales — the sequence #15 fixed, as the harness
    drives it."""

    @pytest.fixture(autouse=True)
    def wired(self, trail, monkeypatch):
        monkeypatch.setenv("UDACITY_OPENAI_API_KEY", "not-used-under-a-scripted-model")
        monkeypatch.setattr(orchestrator, "trail", lambda: trail)
        self.seen: list = []
        return trail

    async def handle(self, lines):
        model = scripted_flow(lines, seen=self.seen)
        with (
            orchestrator_agent.override(model=model),
            inventory_agent.override(model=model),
            quoting_agent.override(model=model),
            sales_agent.override(model=model),
            replenishment_agent.override(model=model),
        ):
            return await orchestrator.handle_request(
                "I would like to order some paper. (Date of request: 2025-04-01)",
                request_date=REQUEST_DATE,
                request_id=1,
            )

    def orchestrator_saw(self) -> str:
        return json.dumps(to_jsonable_python(self.seen[-1]))

    async def test_sales_runs_last_and_exactly_once(self):
        await self.handle([("L1", "Cardstock", 500)])
        with starter.engine().connect() as conn:
            rows = conn.execute(
                text("SELECT agent, kind, name FROM agent_steps ORDER BY seq")
            ).fetchall()
        assert [row.name for row in rows if row.kind == "delegation"] == [
            "inventory",
            "quoting",
            "sales",
        ]
        assert AgentName.SALES in {row.agent for row in rows}

    async def test_a_committed_line_moves_money_and_joins_back_to_the_request(self):
        before = read_cash(REQUEST_DATE)
        await self.handle([("L1", "Cardstock", 500)])
        [row] = sales_rows()
        [link] = links()
        assert (row["item_name"], row["price"]) == ("Cardstock", 71.25)
        assert read_cash(REQUEST_DATE) == pytest.approx(before + 71.25)
        assert link["transaction_rowid"] == row["rowid"]
        assert link["request_id"] == "1"

    async def test_the_reply_states_the_delivery_date_for_what_we_sold(self):
        resolution = await self.handle([("L1", "Cardstock", 500)])
        assert "500 units of Cardstock for $71.25" in resolution.customer_message
        assert f"delivered on {REQUEST_DATE}" in resolution.customer_message

    async def test_a_short_line_is_declined_and_its_sibling_still_sells(self):
        resolution = await self.handle(
            [("L1", "A4 paper", 500), ("L2", "Cardstock", 500)]
        )
        assert "unable to supply A4 paper" in resolution.customer_message
        assert "500 units of Cardstock" in resolution.customer_message
        assert [row["item_name"] for row in sales_rows()] == ["Cardstock"]

    async def test_the_short_line_reaches_the_orchestrator_as_a_code_and_a_line(self):
        """A code and a line id, and never the sentence behind them.

        Under the same-day deadline these tests run with, the code standing on
        the line by the time the sequence ends is `deadline_unmeetable` — we
        would have bought the stock and it could not have reached us in time.
        Which code it is is `test_retry.py`'s subject; that it arrives as a
        code rather than as a detail string is this one's.
        """
        await self.handle([("L1", "A4 paper", 500)])
        handed_up_json = self.orchestrator_saw()
        assert '"deadline_unmeetable"' in handed_up_json.replace(" ", "")
        assert "requested 500" not in handed_up_json
        assert "nothing bought" not in handed_up_json

    async def test_no_cash_figure_or_stock_count_enters_the_orchestrators_context(self):
        await self.handle([("L1", "Cardstock", 500), ("L2", "A4 paper", 500)])
        handed_up_json = self.orchestrator_saw()
        for forbidden in (
            "cash_before",
            "cash_after",
            "stock_read_by_item",
            "transaction_rowid_by_line",
            "45059",
        ):
            assert forbidden not in handed_up_json

    async def test_the_internal_half_reaches_the_trail_in_full(self):
        """Withheld from the orchestrator, and withheld from nobody else: an
        audit that reads a redaction is not an audit."""
        await self.handle([("L1", "Cardstock", 500)])
        with starter.engine().connect() as conn:
            outputs = conn.execute(
                text(
                    "SELECT outputs FROM agent_steps WHERE name = 'sales' "
                    "AND kind = 'delegation'"
                )
            ).scalar_one()
        recorded = json.loads(outputs)["internal"]
        assert recorded["cash_before"] == 45059.70
        assert recorded["stock_read_by_item"] == {"Cardstock": 595}
        assert recorded["transaction_rowid_by_line"]["L1"] == sales_rows()[0]["rowid"]
