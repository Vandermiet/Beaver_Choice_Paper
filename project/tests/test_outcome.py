"""Outcomes and suspension: how a request ends, and what it leaves behind.

Everything the orchestrator decides about a request as a whole is arithmetic
over the views it was handed, so the derivation is tested without a model: a
journal is built by hand from the envelopes the seam would have produced, and
`derive_outcome` is an ordinary function over it.

What needs a model is the two ends of it — that the seam records into the
journal at all, and that the reply which comes out carries no cash figure, no
stock count and no internal detail string. Both run on a `FunctionModel`, so
no network and no key.
"""

import json

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_core import to_jsonable_python
from sqlalchemy import text

from tests.messages import (
    NEEDS,
    PRICED,
    RESOLVED,
    availability_handed_down,
    handed_down,
    handed_up,
)

from beaver import orchestrator, starter
from beaver.contract import (
    AgentName,
    AgentView,
    BlockerCode,
    CustomerBlocker,
    Outcome,
    RequestedLine,
)
from beaver.inventory.agent import inventory_agent
from beaver.inventory.models import InventoryCustomerPayload, ResolvedLine
from beaver.orchestrator import orchestrator_agent
from beaver.outcome import (
    RequestJournal,
    derive_outcome,
    revision_queries,
    spoken_blockers,
)
from beaver.quoting.agent import quoting_agent
from beaver.replenishment.agent import replenishment_agent
from beaver.sales.agent import sales_agent
from beaver.sales.models import CommittedLine, DeclinedLine, SalesCustomerPayload

REQUEST_DATE = "2025-04-01"
STEP_ID = "20260915T120000Z:1:001"

#: 595 on the shelf at seed 137, so a 500-unit line commits.
IN_STOCK = "Cardstock"
#: 272 on the shelf, so a 500-unit line is short.
SHORT = "A4 paper"


def requested(*line_ids: str) -> list[RequestedLine]:
    """The lines the orchestrator extracted, in the customer's own words."""
    return [
        RequestedLine(
            line_id=line_id,
            item_as_stated="printer paper",
            quantity_as_stated=500,
            unit_as_stated="sheets",
        )
        for line_id in line_ids
    ]


def blockers(*pairs: tuple[str, BlockerCode]) -> list[CustomerBlocker]:
    """Blockers as the seam derives them: a line and a code, and no detail."""
    return [CustomerBlocker(line_id=line_id, code=code) for line_id, code in pairs]


def inventory_view(
    resolved: tuple[str, ...] = (),
    raised: list[CustomerBlocker] | None = None,
    step_id: str = STEP_ID,
) -> AgentView:
    """What the seam hands up from inventory, without running inventory."""
    return AgentView[InventoryCustomerPayload](
        agent=AgentName.INVENTORY,
        step_id=step_id,
        request_id="1",
        customer=InventoryCustomerPayload(
            resolved_lines=[
                ResolvedLine(
                    line_id=line_id, item_name=IN_STOCK, category="product", quantity=500
                )
                for line_id in resolved
            ],
            restock_needs=[],
        ),
        blockers=raised or [],
    )


def sales_view(
    committed: tuple[str, ...] = (),
    declined: tuple[str, ...] = (),
    raised: list[CustomerBlocker] | None = None,
    step_id: str = "20260915T120000Z:1:003",
) -> AgentView:
    """What the seam hands up from sales, without moving any money."""
    return AgentView[SalesCustomerPayload](
        agent=AgentName.SALES,
        step_id=step_id,
        request_id="1",
        customer=SalesCustomerPayload(
            committed=[
                CommittedLine(
                    line_id=line_id,
                    item_name=IN_STOCK,
                    units=500,
                    line_total=71.25,
                    promised_delivery_date=REQUEST_DATE,
                )
                for line_id in committed
            ],
            declined=[
                DeclinedLine(line_id=line_id, item_name=SHORT, units=500)
                for line_id in declined
            ],
            order_total=71.25 * len(committed),
            promised_delivery_date=REQUEST_DATE if committed else None,
        ),
        blockers=raised or [],
    )


def journal(*views: AgentView, lines: tuple[str, ...] = ("L1",)) -> RequestJournal:
    """A request's journal, filled by hand with the views a seam would record."""
    book = RequestJournal()
    book.requested_lines = requested(*lines)
    for view in views:
        book.record(view)
    return book


class TestTheFourOutcomes:
    """Derived by the orchestrator alone, from the per-line verdicts it was
    handed. No agent states an outcome and none of them could: an outcome is a
    fact about the request, and every agent sees only its own lines."""

    def test_every_line_committed_is_fulfilled(self, seeded_db):
        book = journal(inventory_view(("L1",)), sales_view(committed=("L1",)))
        assert derive_outcome(book) is Outcome.FULFILLED

    def test_some_committed_and_some_dropped_is_partially_fulfilled(self, seeded_db):
        book = journal(
            inventory_view(("L1", "L2")),
            sales_view(
                committed=("L1",),
                declined=("L2",),
                raised=blockers(("L2", BlockerCode.INSUFFICIENT_STOCK)),
            ),
            lines=("L1", "L2"),
        )
        assert derive_outcome(book) is Outcome.PARTIALLY_FULFILLED

    def test_every_line_dropped_is_rejected(self, seeded_db):
        book = journal(
            inventory_view(
                raised=blockers(
                    ("L1", BlockerCode.ITEM_NOT_CARRIED),
                    ("L2", BlockerCode.ITEM_NOT_CARRIED),
                )
            ),
            lines=("L1", "L2"),
        )
        assert derive_outcome(book) is Outcome.REJECTED

    def test_a_pausing_blocker_before_any_money_moved_is_pending_revision(
        self, seeded_db
    ):
        book = journal(inventory_view(raised=blockers(("L1", BlockerCode.ITEM_AMBIGUOUS))))
        assert derive_outcome(book) is Outcome.PENDING_CUSTOMER_REVISION

    def test_one_surviving_line_is_enough_to_keep_a_request_out_of_rejected(
        self, seeded_db
    ):
        """No blocker refuses a whole request. `REJECTED` is reachable only
        when every one of the request's lines has dropped."""
        book = journal(
            inventory_view(("L1",), raised=blockers(("L2", BlockerCode.ITEM_NOT_CARRIED))),
            sales_view(committed=("L1",)),
            lines=("L1", "L2"),
        )
        assert derive_outcome(book) is not Outcome.REJECTED

    def test_a_line_neither_committed_nor_dropped_is_a_bug_not_a_rejection(
        self, seeded_db
    ):
        """A line inventory resolved and nothing ever decided means the
        sequence did not finish. Calling that `REJECTED` would let a crash
        masquerade as a business decision in the rubric's own count."""
        book = journal(inventory_view(("L1",)))
        with pytest.raises(ValueError, match="L1"):
            derive_outcome(book)


class TestSuspensionIsAnOutcomeAndNotAWait:
    """`PENDING_CUSTOMER_REVISION` is reachable only from the four pausing
    codes, and only before any money has moved."""

    @pytest.mark.parametrize("code", sorted(BlockerCode.__members__.values()))
    def test_only_the_four_pausing_codes_can_suspend(self, seeded_db, code):
        book = journal(inventory_view(raised=blockers(("L1", code))))
        suspended = derive_outcome(book) is Outcome.PENDING_CUSTOMER_REVISION
        assert suspended is code.pausing

    def test_a_pausing_blocker_after_commitment_does_not_suspend(self, seeded_db):
        """Money has moved, so there is nothing left to pause. The ambiguous
        line is spoken as prose beside the sale we made."""
        book = journal(
            inventory_view(("L1",), raised=blockers(("L2", BlockerCode.ITEM_AMBIGUOUS))),
            sales_view(committed=("L1",)),
            lines=("L1", "L2"),
        )
        assert derive_outcome(book) is Outcome.PARTIALLY_FULFILLED

    def test_one_pausing_line_among_several_dropped_lines_still_suspends(
        self, seeded_db
    ):
        """Nothing was sold and one line is a question we can ask. Asking beats
        refusing the lot."""
        book = journal(
            inventory_view(
                raised=blockers(
                    ("L1", BlockerCode.ITEM_NOT_CARRIED),
                    ("L2", BlockerCode.QUANTITY_MISSING),
                )
            ),
            lines=("L1", "L2"),
        )
        assert derive_outcome(book) is Outcome.PENDING_CUSTOMER_REVISION


class TestBlockerPrecedence:
    """One blocker per line, resolution judged before units. Every blocker
    raised is still written to the trail — precedence governs what is spoken,
    not what is recorded."""

    def test_resolution_is_spoken_before_units(self, seeded_db):
        book = journal(
            inventory_view(
                raised=blockers(
                    ("L1", BlockerCode.UNIT_NOT_UNDERSTOOD),
                    ("L1", BlockerCode.ITEM_NOT_CARRIED),
                )
            )
        )
        assert spoken_blockers(book) == {"L1": BlockerCode.ITEM_NOT_CARRIED}

    def test_a_resolution_failure_outranks_a_supply_failure(self, seeded_db):
        book = journal(
            inventory_view(raised=blockers(("L1", BlockerCode.SIZE_NOT_CARRIED))),
            sales_view(
                declined=("L1",), raised=blockers(("L1", BlockerCode.INSUFFICIENT_STOCK))
            ),
        )
        assert spoken_blockers(book) == {"L1": BlockerCode.SIZE_NOT_CARRIED}

    def test_the_last_supply_blocker_is_the_one_the_customer_hears(self, seeded_db):
        """A line short of stock, restocked, then refused on the delivery
        promise is told about the promise: by then we are no longer out of it."""
        book = journal(
            sales_view(
                declined=("L1",), raised=blockers(("L1", BlockerCode.INSUFFICIENT_STOCK))
            ),
            sales_view(
                declined=("L1",),
                raised=blockers(("L1", BlockerCode.DEADLINE_UNMEETABLE)),
                step_id="20260915T120000Z:1:005",
            ),
        )
        assert spoken_blockers(book) == {"L1": BlockerCode.DEADLINE_UNMEETABLE}

    def test_an_internal_only_code_is_never_the_one_spoken(self, seeded_db):
        """The customer is never told we could not afford the stock. The line
        is still dropped, and its true reason is still in the trail."""
        book = journal(
            sales_view(
                declined=("L1",), raised=blockers(("L1", BlockerCode.INSUFFICIENT_STOCK))
            ),
            sales_view(
                raised=blockers(("L1", BlockerCode.CASH_INSUFFICIENT)),
                step_id="20260915T120000Z:1:005",
            ),
        )
        assert spoken_blockers(book) == {"L1": BlockerCode.INSUFFICIENT_STOCK}

    def test_a_line_whose_only_blocker_is_internal_is_dropped_and_unspoken(
        self, seeded_db
    ):
        book = journal(inventory_view(raised=blockers(("L1", BlockerCode.CASH_INSUFFICIENT))))
        assert spoken_blockers(book) == {}
        assert derive_outcome(book) is Outcome.REJECTED

    def test_a_committed_line_speaks_no_blocker_at_all(self, seeded_db):
        """It was short on the first pass and sold on the second. There is
        nothing to apologise for."""
        book = journal(
            sales_view(
                declined=("L1",), raised=blockers(("L1", BlockerCode.INSUFFICIENT_STOCK))
            ),
            sales_view(committed=("L1",), step_id="20260915T120000Z:1:005"),
        )
        assert spoken_blockers(book) == {}


class TestWhatTheSeamHandsUp:
    """Precedence governs the prose structurally: the orchestrator cannot speak
    a blocker it was never handed, exactly as it cannot disclose an internal
    payload it was never handed. Recording and narrowing are one act."""

    def test_only_the_standing_blocker_survives_the_seam(self, seeded_db):
        book = RequestJournal()
        book.requested_lines = requested("L1")
        narrowed = book.record(
            inventory_view(
                raised=blockers(
                    ("L1", BlockerCode.UNIT_NOT_UNDERSTOOD),
                    ("L1", BlockerCode.ITEM_NOT_CARRIED),
                )
            )
        )
        assert [blocker.code for blocker in narrowed.blockers] == [
            BlockerCode.ITEM_NOT_CARRIED
        ]

    def test_an_internal_only_code_never_crosses_the_seam(self, seeded_db):
        """The orchestrator is not told what we can afford, in any form — not
        even as a bare code it might be tempted to paraphrase."""
        book = RequestJournal()
        book.requested_lines = requested("L1")
        narrowed = book.record(
            inventory_view(raised=blockers(("L1", BlockerCode.CASH_INSUFFICIENT)))
        )
        assert narrowed.blockers == []

    def test_the_journal_still_holds_every_blocker_that_was_raised(self, seeded_db):
        """Precedence governs what is spoken, not what is recorded — and the
        outcome is derived from the whole of it."""
        book = RequestJournal()
        book.requested_lines = requested("L1")
        book.record(
            inventory_view(
                raised=blockers(
                    ("L1", BlockerCode.UNIT_NOT_UNDERSTOOD),
                    ("L1", BlockerCode.ITEM_NOT_CARRIED),
                )
            )
        )
        assert len(book.blockers) == 2

    def test_a_blocker_an_earlier_step_outranks_is_withheld(self, seeded_db):
        """A line we do not sell is never also told we are short of it."""
        book = RequestJournal()
        book.requested_lines = requested("L1")
        book.record(inventory_view(raised=blockers(("L1", BlockerCode.SIZE_NOT_CARRIED))))
        narrowed = book.record(
            sales_view(
                declined=("L1",), raised=blockers(("L1", BlockerCode.INSUFFICIENT_STOCK))
            )
        )
        assert narrowed.blockers == []


class TestTheRevisionQueries:
    """One message asking everything, rather than four rounds of
    correspondence."""

    def test_every_open_pausing_line_becomes_one_question(self, seeded_db):
        book = journal(
            inventory_view(
                raised=blockers(
                    ("L1", BlockerCode.ITEM_AMBIGUOUS),
                    ("L2", BlockerCode.QUANTITY_MISSING),
                )
            ),
            lines=("L1", "L2"),
        )
        queries = revision_queries(book)
        assert [query.line_id for query in queries] == ["L1", "L2"]
        assert [query.code for query in queries] == [
            BlockerCode.ITEM_AMBIGUOUS,
            BlockerCode.QUANTITY_MISSING,
        ]

    def test_a_question_names_the_line_in_the_customers_own_words(self, seeded_db):
        book = journal(inventory_view(raised=blockers(("L1", BlockerCode.ITEM_AMBIGUOUS))))
        [query] = revision_queries(book)
        assert "printer paper" in query.question_for_customer

    def test_a_non_pausing_blocker_asks_nothing(self, seeded_db):
        book = journal(inventory_view(raised=blockers(("L1", BlockerCode.ITEM_NOT_CARRIED))))
        assert revision_queries(book) == []

    def test_a_question_carries_no_internal_detail(self, seeded_db):
        book = journal(
            inventory_view(
                raised=blockers(
                    ("L1", BlockerCode.ITEM_AMBIGUOUS),
                    ("L2", BlockerCode.UNIT_NOT_UNDERSTOOD),
                )
            ),
            lines=("L1", "L2"),
        )
        for query in revision_queries(book):
            assert "stock" not in query.question_for_customer.lower()
            assert "L1" not in query.question_for_customer


def suspensions() -> list[dict]:
    """Every suspended flow this run left behind."""
    with starter.engine().connect() as conn:
        rows = conn.execute(text("SELECT * FROM suspended_flows")).fetchall()
    return [dict(row._mapping) for row in rows]


#: What a customer writes to draw each blocker out of the real resolver at seed
#: 137. Nothing here is scripted: inventory does its own work, so a test says
#: what the customer said and the codes are earned rather than asserted.
AMBIGUOUS = ("banner paper", 200, "sheets")
NOT_CARRIED = ("balloons", 300, None)
NO_QUANTITY = ("printer paper", None, "sheets")
IN_REAMS = ("printer paper", 500, "reams")
#: 595 of `Cardstock` on the shelf, so this commits.
SELLS = ("cardstock", 500, "sheets")
#: 272 of `A4 paper` on the shelf, so this is declined short.
TOO_MANY = ("printer paper", 500, "sheets")


def scripted_request(lines, as_of_date: str = REQUEST_DATE):
    """One model standing in for all four agents, driving the real sequence.

    Only the *shape* of each turn is scripted. Every line is resolved, priced
    and committed by the agents themselves, and each delegation is called with
    what the one before it actually handed up — so the blockers under test are
    earned by the resolver rather than asserted into place.

    Args:
        lines: What the customer asked for, as `(item, quantity, unit)`.
        as_of_date: The date the request arrived.

    Returns:
        The model to run every agent under.
    """
    stated = [
        {
            "line_id": f"L{index}",
            "item_as_stated": item,
            "quantity_as_stated": quantity,
            "unit_as_stated": unit,
        }
        for index, (item, quantity, unit) in enumerate(lines, start=1)
    ]

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        names = {tool.name for tool in info.function_tools}
        if "consult_inventory" in names:
            return _orchestrator_turn(messages, stated, as_of_date)
        if "catalogue_price" in names:
            return _quoting_turn(messages, as_of_date)
        if "snapshot_financials" in names:
            return _sales_turn(messages, as_of_date)
        if "reorder_thresholds" in names:
            return _replenishment_turn(messages)
        return _inventory_turn(messages, stated, as_of_date)

    return FunctionModel(model)


def _orchestrator_turn(messages, stated, as_of_date: str) -> ModelResponse:
    """Delegate down the sequence, then write the letter from what came back."""
    if len(messages) == 1:
        return ModelResponse(
            parts=[
                ToolCallPart("consult_inventory", {"lines": stated, "as_of_date": as_of_date})
            ]
        )
    resolved = handed_up(messages, "consult_inventory").get("resolved_lines", [])
    if resolved and len(messages) == 3:
        return ModelResponse(
            parts=[
                ToolCallPart("consult_quoting", {"lines": resolved, "as_of_date": as_of_date})
            ]
        )
    if resolved and len(messages) == 5:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "place_order",
                    {
                        "lines": handed_up(messages, "consult_quoting")["quoted_lines"],
                        "as_of_date": as_of_date,
                        # The request date itself. These tests are about how a
                        # request *ends*, not about the retry, and a same-day
                        # deadline keeps the purse shut: no supplier reaches us
                        # on the day we order, so a short line stays short and
                        # the outcome under test is the one being asserted.
                        "deadline": as_of_date,
                    },
                )
            ]
        )
    return ModelResponse(parts=[TextPart(a_letter(messages, stated))])


def _inventory_turn(messages, stated, as_of_date: str) -> ModelResponse:
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


def _quoting_turn(messages, as_of_date: str) -> ModelResponse:
    resolved = handed_down(messages, RESOLVED)
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


def _sales_turn(messages, as_of_date: str) -> ModelResponse:
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


def _replenishment_turn(messages) -> ModelResponse:
    [request] = handed_down(messages, NEEDS)
    if len(messages) == 1:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "reorder_thresholds",
                    {"item_names": sorted({need["item_name"] for need in request["needs"]})},
                ),
                ToolCallPart("cash_available", {"as_of_date": request["request_date"]}),
            ]
        )
    return ModelResponse(parts=[ToolCallPart("final_result", {"request": request})])


#: What the orchestrator's instructions ask it to ask, by code. A stand-in for
#: the live model's own words, so what is under test is whether the request
#: *lets* the letter be written rather than a particular turn of phrase.
_ASKS = {
    "item_ambiguous": "which did you mean by {item}",
    "size_not_carried": "we do not stock that size of {item}",
    "quantity_missing": "how many {item} would you like",
    "unit_not_understood": "how many sheets of {item}",
    "item_not_carried": "we do not sell {item}",
    "insufficient_stock": "we are unable to supply {item} at present",
    "deadline_unmeetable": "we could not get {item} to you by the date you need it",
}


def a_letter(messages, stated) -> str:
    """The reply, rendered from exactly what the orchestrator was handed.

    Deliberately exhaustive: it says everything the payloads and the blockers
    make sayable. A leak test against a fixed string would pass by saying
    nothing, so this letter says all of it and the test asserts that all of it
    is still safe.

    Args:
        messages: The orchestrator's own messages.
        stated: The lines as the customer stated them.

    Returns:
        The letter.
    """
    said_as = {line["line_id"]: line["item_as_stated"] for line in stated}
    sales = handed_up(messages, "place_order")
    sentences = [
        f"{line['units']} units of {line['item_name']} for ${line['line_total']:.2f}"
        for line in sales.get("committed", [])
    ]
    if sales.get("promised_delivery_date"):
        sentences.append(f"delivered on {sales['promised_delivery_date']}")
    for tool_name in ("consult_inventory", "consult_quoting", "place_order"):
        for blocker in _blockers_of(messages, tool_name):
            item = said_as.get(blocker["line_id"], "that line")
            sentences.append(_ASKS[blocker["code"]].format(item=item))
    return ". ".join(sentences) or "We could not help with this enquiry."


def _blockers_of(messages, tool_name: str) -> list[dict]:
    """The blockers one delegation handed up, as the orchestrator received them."""
    for message in reversed(messages):
        for part in getattr(message, "parts", []):
            if getattr(part, "tool_name", None) != tool_name:
                continue
            payload = to_jsonable_python(getattr(part, "content", None))
            if isinstance(payload, dict) and "blockers" in payload:
                return payload["blockers"]
    return []
class TestThroughTheOrchestrator:
    """The journal is filled by the seam rather than by hand, and the resolution
    that comes out is the one the harness prints."""

    @pytest.fixture(autouse=True)
    def wired(self, trail, monkeypatch):
        monkeypatch.setenv("UDACITY_OPENAI_API_KEY", "not-used-under-a-scripted-model")
        monkeypatch.setattr(orchestrator, "trail", lambda: trail)
        return trail

    async def handle(self, *lines):
        model = scripted_request(list(lines))
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

    async def test_a_fully_served_request_is_fulfilled_and_suspends_nothing(self):
        resolution = await self.handle(SELLS)
        assert resolution.outcome is Outcome.FULFILLED
        assert resolution.resume_token is None
        assert suspensions() == []
        assert resolution.request_id == "1"

    async def test_a_short_line_beside_a_sold_one_is_partially_fulfilled(self):
        resolution = await self.handle(SELLS, TOO_MANY)
        assert resolution.outcome is Outcome.PARTIALLY_FULFILLED

    async def test_a_request_we_carry_nothing_of_is_rejected(self):
        resolution = await self.handle(NOT_CARRIED)
        assert resolution.outcome is Outcome.REJECTED
        assert resolution.resume_token is None
        assert suspensions() == []

    async def test_a_paused_request_ends_pending_revision_with_a_resume_token(self, trail):
        resolution = await self.handle(AMBIGUOUS)
        assert resolution.outcome is Outcome.PENDING_CUSTOMER_REVISION
        assert resolution.resume_token == f"{trail.run_id}:1"

    async def test_a_suspended_request_writes_one_row_carrying_its_own_provenance(
        self, trail
    ):
        await self.handle(AMBIGUOUS, NO_QUANTITY, IN_REAMS)
        [row] = suspensions()
        assert row["resume_token"] == f"{trail.run_id}:1"
        assert (row["run_id"], row["request_id"]) == (trail.run_id, "1")

        flow = json.loads(row["payload"])
        assert flow["original_request"]["raw_text"].startswith("I would like")
        assert [line["line_id"] for line in flow["original_request"]["lines"]] == [
            "L1",
            "L2",
            "L3",
        ]
        assert flow["completed_step_ids"] == [f"{trail.run_id}:1:001"]

    async def test_a_suspended_reply_asks_every_open_question_in_one_message(self):
        """One round of correspondence clears it, rather than four."""
        await self.handle(AMBIGUOUS, NO_QUANTITY, IN_REAMS)
        [row] = suspensions()
        queries = json.loads(row["payload"])["open_questions"]
        assert [query["line_id"] for query in queries] == ["L1", "L2", "L3"]
        assert [query["code"] for query in queries] == [
            "item_ambiguous",
            "quantity_missing",
            "unit_not_understood",
        ]

    async def test_a_line_we_simply_do_not_sell_asks_nothing_and_still_suspends(self):
        """One question is enough to be worth asking, and the flat refusal
        beside it is spoken in the same message rather than in another one."""
        await self.handle(NOT_CARRIED, AMBIGUOUS)
        [row] = suspensions()
        queries = json.loads(row["payload"])["open_questions"]
        assert [query["line_id"] for query in queries] == ["L2"]

    async def test_a_revisable_blocker_after_commitment_suspends_nothing(self):
        """Money has moved, so the ambiguous line is prose and not a pause."""
        resolution = await self.handle(SELLS, AMBIGUOUS)
        assert resolution.outcome is Outcome.PARTIALLY_FULFILLED
        assert resolution.resume_token is None
        assert suspensions() == []

    async def test_every_blocker_raised_is_in_the_trail_even_when_unspoken(self):
        await self.handle(AMBIGUOUS, NOT_CARRIED)
        with starter.engine().connect() as conn:
            rows = conn.execute(
                text("SELECT line_id, code, detail FROM blocker_signals ORDER BY line_id")
            ).fetchall()
        assert [(row.line_id, row.code) for row in rows] == [
            ("L1", "item_ambiguous"),
            ("L2", "item_not_carried"),
        ]
        assert all(row.detail for row in rows)

    async def test_the_reply_is_one_message_and_leaks_nothing(self):
        """The §4 guarantee as a test rather than a promise: the orchestrator
        structurally cannot disclose a cash balance, a stock count or an
        internal detail string, because it was never handed one."""
        resolution = await self.handle(SELLS, TOO_MANY, AMBIGUOUS)
        assert isinstance(resolution.customer_message, str)
        message = resolution.customer_message.lower()
        forbidden = (
            "45059",  # the cash balance
            "cash",
            "margin",
            "profit",
            "272",  # what we hold of the line we were short of
            "595",  # what we hold of the line we sold
            "shortfall",
        )
        for word in forbidden:
            assert word not in message

    async def test_the_internal_details_of_this_very_request_stay_out_of_the_reply(self):
        """Asserted against the strings this run actually produced, so a new
        detail string cannot slip past a hard-coded list."""
        resolution = await self.handle(TOO_MANY, AMBIGUOUS)
        with starter.engine().connect() as conn:
            details = conn.execute(text("SELECT detail FROM blocker_signals")).scalars().all()
        assert details
        for detail in details:
            assert detail not in resolution.customer_message
