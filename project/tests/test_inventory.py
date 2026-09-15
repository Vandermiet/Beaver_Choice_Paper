"""The inventory agent and the first delegation, end to end under a scripted model.

No network and no key: both agents run on a `FunctionModel`, so what is under
test is the wiring and the envelope rather than a language model's mood. The
resolution rules themselves are covered in `test_resolution.py`, without a model
at all.
"""

import json

import pytest
from pydantic import ValidationError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import text

from pydantic_core import to_jsonable_python

from beaver import orchestrator, starter
from beaver.audit import AgentDeps
from beaver.contract import AgentName, BlockerCode
from beaver.inventory.agent import inventory_agent
from beaver.inventory.models import ProductCategory, ResolutionDecision
from beaver.inventory.tools import check_stock, list_carried_catalogue, read_stock
from beaver.orchestrator import orchestrator_agent

STEP_ID = "20260915T120000Z:1:001"
REPLY = "Thank you for your enquiry — we can supply the paper you asked for."


def lines_of(*stated: tuple[str, float | None, str | None]) -> list[dict]:
    """The requested lines as the orchestrator's model would emit them."""
    return [
        {
            "line_id": f"L{index}",
            "item_as_stated": item,
            "quantity_as_stated": quantity,
            "unit_as_stated": unit,
        }
        for index, (item, quantity, unit) in enumerate(stated, start=1)
    ]


def scripted(lines: list[dict], as_of_date: str = "2025-04-01", seen: list | None = None):
    """A model that plays both parts: the orchestrator, then inventory.

    The two are told apart by the tools they are offered, which is also the
    only thing either of them is really given. Everything the orchestrator's
    turn was handed is appended to `seen`, so a test can assert on what
    actually entered its context rather than on a reconstruction of it.
    """

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
                            {"lines": lines, "as_of_date": as_of_date},
                        )
                    ]
                )
            return ModelResponse(parts=[TextPart(REPLY)])
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
            parts=[ToolCallPart("final_result", {"lines": lines, "as_of_date": as_of_date})]
        )

    return FunctionModel(model)


async def resolve(lines: list[dict], trail, as_of_date: str = "2025-04-01"):
    """Run inventory on its own, with the step id the seam would have minted."""
    deps = AgentDeps(trail=trail, request_id="1", current_step_id=STEP_ID)
    with inventory_agent.override(model=scripted(lines, as_of_date)):
        result = await inventory_agent.run("resolve these", deps=deps)
    return result.output


class TestTheHelperWrappers:
    """The two tools that wrap a provided helper, tested on their own.

    Both add something the raw helper does not: `list_carried_catalogue` joins
    the category the resolution rules need onto a snapshot that would otherwise
    be name-to-count, and it lists what we *sell* rather than what we *hold*.
    """

    def test_the_catalogue_lists_the_eighteen_items_with_their_categories(self, seeded_db):
        catalogue = list_carried_catalogue("2025-04-01")
        assert len(catalogue) == 18
        by_name = {item.item_name: item for item in catalogue}
        assert by_name["A4 paper"].category is ProductCategory.PAPER
        assert by_name["Large poster paper (24x36 inches)"].category is (
            ProductCategory.LARGE_FORMAT
        )

    def test_the_catalogue_is_what_we_sell_and_not_what_we_hold(self, seeded_db):
        """`get_all_inventory` returns only items with positive stock. A carried
        item we hold none of is still on offer, so it is still listed."""
        raw = starter.get_all_inventory("2025-04-01")
        listed = {item.item_name for item in list_carried_catalogue("2025-04-01")}
        assert listed >= set(raw)

    def test_check_stock_reads_each_resolved_item(self, seeded_db):
        readings = check_stock(["A4 paper", "Glossy paper"], "2025-04-01")
        assert [reading.item_name for reading in readings] == ["A4 paper", "Glossy paper"]
        assert readings[0].stock_on_hand == 272
        assert readings[0].as_of.isoformat() == "2025-04-01"

    def test_check_stock_refuses_a_name_we_do_not_carry(self, seeded_db):
        """The validator, not the prompt, is what keeps a hallucinated name out
        of a reading the shortfall would be sized from."""
        with pytest.raises(ValidationError):
            check_stock(["A3 glossy paper"], "2025-04-01")

    def test_stock_before_anything_was_ever_bought_is_none_of_it(self, seeded_db):
        assert read_stock("A4 paper", "2024-12-31") == 0


class TestTheEnvelope:
    async def test_a_resolved_line_crosses_under_its_exact_catalogue_name(self, trail):
        response = await resolve(lines_of(("500 sheets of printer paper", 500, "sheets")), trail)
        [line] = response.customer.resolved_lines
        assert (line.item_name, line.quantity) == ("A4 paper", 500)
        assert not response.internal.signals

    async def test_a_refused_line_crosses_as_a_blocker_and_not_as_an_item(self, trail):
        response = await resolve(lines_of(("A3 glossy paper", 200, "sheets")), trail)
        assert not response.customer.resolved_lines
        [signal] = response.internal.signals
        assert signal.code is BlockerCode.SIZE_NOT_CARRIED
        assert signal.line_id == "L1"

    async def test_the_survivors_of_a_refused_line_still_resolve(self, trail):
        """A failure confined to one line drops that line; the rest continue."""
        response = await resolve(
            lines_of(
                ("A3 glossy paper", 200, "sheets"),
                ("A4 glossy paper", 200, "sheets"),
                ("200 balloons", 200, None),
            ),
            trail,
        )
        assert [line.item_name for line in response.customer.resolved_lines] == ["Glossy paper"]
        assert [signal.code for signal in response.internal.signals] == [
            BlockerCode.SIZE_NOT_CARRIED,
            BlockerCode.ITEM_NOT_CARRIED,
        ]

    async def test_one_line_raises_at_most_one_blocker(self, trail):
        """Uncarried *and* counted in reams is one failure, not two — otherwise
        the trail's `GROUP BY` counts one rejection twice."""
        response = await resolve(lines_of(("party streamers", 300, "packs")), trail)
        assert len(response.internal.signals) == 1
        assert response.internal.signals[0].code is BlockerCode.ITEM_NOT_CARRIED

    async def test_a_ream_is_asked_about_rather_than_converted(self, trail):
        response = await resolve(lines_of(("printer paper", 500, "reams")), trail)
        [signal] = response.internal.signals
        assert signal.code is BlockerCode.UNIT_NOT_UNDERSTOOD

    async def test_a_line_reported_twice_is_resolved_once(self, trail):
        twice = lines_of(("A4 glossy paper", 200, "sheets")) * 2
        response = await resolve(twice, trail)
        assert len(response.customer.resolved_lines) == 1


class TestTheShortfall:
    """Inventory observes a shortfall and emits it as a fact. It never refuses
    a line for want of stock — that is sales', at commit time."""

    async def test_a_shortfall_crosses_as_a_restock_need(self, trail):
        response = await resolve(lines_of(("printer paper", 10_000, "sheets")), trail)
        [need] = response.customer.restock_needs
        assert need.item_name == "A4 paper"
        assert need.shortfall_units == 10_000 - response.internal.stock_facts[0].stock_on_hand
        assert need.shortfall_units > 0

    async def test_insufficient_stock_is_never_raised_here(self, trail):
        response = await resolve(lines_of(("printer paper", 10_000, "sheets")), trail)
        assert BlockerCode.INSUFFICIENT_STOCK not in {s.code for s in response.internal.signals}
        assert response.customer.resolved_lines, "a short line is still a line we can sell"

    async def test_a_line_we_can_fill_needs_no_restock(self, trail):
        response = await resolve(lines_of(("printer paper", 10, "sheets")), trail)
        assert response.customer.restock_needs == []

    async def test_the_stock_reading_stays_on_the_internal_half(self, trail):
        response = await resolve(lines_of(("printer paper", 10_000, "sheets")), trail)
        assert response.internal.stock_facts[0].stock_on_hand > 0
        assert "stock_on_hand" not in response.customer.model_dump_json()

    async def test_the_shortfall_is_derived_in_code_and_not_from_the_model(self, trail):
        """The model never reports a number the order depends on: the reading
        comes from `get_stock_level` inside the output function."""
        response = await resolve(lines_of(("printer paper", 10_000, "sheets")), trail)
        fact = response.internal.stock_facts[0]
        assert fact.quantity_requested == 10_000
        assert fact.stock_on_hand == 272


class TestTheTrace:
    async def test_every_line_is_traced_whether_or_not_it_resolved(self, trail):
        response = await resolve(
            lines_of(("A3 glossy paper", 200, "sheets"), ("printer paper", 10, "sheets")),
            trail,
        )
        assert [trace.decision for trace in response.internal.traces] == [
            ResolutionDecision.SIZE_VETO,
            ResolutionDecision.RESOLVED,
        ]

    async def test_the_trace_records_what_the_universe_offered(self, trail):
        response = await resolve(lines_of(("matte paper", 200, "sheets")), trail)
        [trace] = response.internal.traces
        assert trace.universe_best_match == "Matte paper"
        assert trace.carried_candidates == []

    async def test_the_trace_is_internal_only(self, trail):
        response = await resolve(lines_of(("matte paper", 200, "sheets")), trail)
        assert "Matte paper" not in response.customer.model_dump_json()


class TestThroughTheOrchestrator:
    """The whole path, as the harness drives it."""

    @pytest.fixture(autouse=True)
    def wired(self, trail, monkeypatch):
        monkeypatch.setenv("UDACITY_OPENAI_API_KEY", "not-used-under-a-scripted-model")
        monkeypatch.setattr(orchestrator, "trail", lambda: trail)
        self.seen: list = []
        return trail

    async def handle(self, stated, request_id=1):
        model = scripted(lines_of(*stated), seen=self.seen)
        with orchestrator_agent.override(model=model), inventory_agent.override(model=model):
            return await orchestrator.handle_request(
                "I would like to order some paper. (Date of request: 2025-04-01)",
                request_date="2025-04-01",
                request_id=request_id,
            )

    def orchestrator_saw(self) -> str:
        """Everything that entered the orchestrator's context, as JSON."""
        return json.dumps(to_jsonable_python(self.seen[-1]))

    def delegation_outputs(self) -> dict:
        with starter.engine().connect() as conn:
            raw = conn.execute(
                text("SELECT outputs FROM agent_steps WHERE kind = 'delegation'")
            ).scalar_one()
        return json.loads(raw)

    async def test_the_harness_gets_the_orchestrators_prose(self, wired):
        reply = await self.handle([("500 sheets of printer paper", 500, "sheets")])
        assert reply == REPLY

    async def test_the_delegation_and_its_tool_calls_are_both_in_the_trail(self, wired):
        await self.handle([("500 sheets of printer paper", 500, "sheets")])
        with starter.engine().connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT step_id, seq, agent, kind, name, parent_step_id "
                    "FROM agent_steps ORDER BY seq"
                )
            ).fetchall()
        assert [(row.kind, row.name) for row in rows] == [
            ("delegation", "inventory"),
            ("tool_call", "shortlist_candidates"),
        ]
        assert rows[1].parent_step_id == rows[0].step_id, "tool calls hang off the delegation"
        assert {row.agent for row in rows} == {AgentName.INVENTORY}

    async def test_the_stock_reading_and_the_trace_reach_the_trail(self, wired):
        await self.handle([("500 sheets of printer paper", 500, "sheets")])
        outputs = self.delegation_outputs()
        assert outputs["internal"]["stock_facts"][0]["stock_on_hand"] == 272
        assert outputs["internal"]["traces"][0]["decision"] == "resolved"

    async def test_neither_ever_enters_the_orchestrators_context(self, wired):
        """The seam hands back an `AgentView`, so the guarantee is structural:
        the orchestrator cannot disclose what it was never given."""
        await self.handle([("500 sheets of printer paper", 500, "sheets")])
        handed_up = self.orchestrator_saw()
        assert "stock_on_hand" not in handed_up
        assert "traces" not in handed_up
        assert "A4 paper" in handed_up, "what we can sell them is customer-safe"

    async def test_a_blocker_reaches_the_orchestrator_as_a_code_without_its_detail(self, wired):
        await self.handle([("A3 glossy paper", 200, "sheets")])
        handed_up = self.orchestrator_saw()
        assert "size_not_carried" in handed_up
        assert "product universe" not in handed_up, "the internal detail stays internal"

    async def test_a_crash_ends_one_request_and_not_the_run(self, wired):
        """A bug must not look like a business decision, and must not take the
        other nineteen requests with it: the trail holds the exception, the
        customer gets an apology, and the harness carries on."""

        def explodes(messages, info):
            raise RuntimeError("the model fell over")

        with orchestrator_agent.override(model=FunctionModel(explodes)):
            reply = await orchestrator.handle_request(
                "I would like to order some paper. (Date of request: 2025-04-01)",
                request_date="2025-04-01",
                request_id=1,
            )
        assert reply == orchestrator.APOLOGY
        with starter.engine().connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM blocker_signals")).scalar_one() == 0

    async def test_the_restock_need_is_routed_up_but_the_stock_is_not(self, wired):
        await self.handle([("10000 sheets of printer paper", 10_000, "sheets")])
        handed_up = self.orchestrator_saw()
        assert '"shortfall_units":9728' in handed_up.replace(" ", "")
        assert "272" not in handed_up


