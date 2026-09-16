"""The audit trail, exercised through a toy delegation.

The toy stands in for a domain agent: it has a tool, it returns the canonical
envelope with an internal payload, and it raises a blocker. Nothing in it calls
the trail — every row here is written by `@delegation` and `AuditedToolset`,
which is the point. A hand-written `log_step()` would be one forgotten call away
from an invisible gap.
"""

import json

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent, ModelMessagesTypeAdapter, RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy import text

import project_starter
from beaver.audit import AgentDeps, AuditedToolset, AuditTrail, bootstrap_audit, delegation
from beaver.contract import (
    AgentName,
    AgentResponse,
    AgentView,
    BlockerCode,
    BlockerSignal,
    InternalPayload,
)

# --------------------------------------------------------------------------
# The toy domain agent
# --------------------------------------------------------------------------


class ToyCustomer(BaseModel):
    """What the customer may be told."""

    message: str


class ToyInternal(InternalPayload):
    """What only the trail may see."""

    cash_before: float


ToyResponse = AgentResponse[ToyCustomer, ToyInternal]

SECRET = 45059.70
BLOCKER_DETAIL = "requested 500, stock 120"


def check_stock(item_name: str) -> int:
    """How many of an item we hold.

    Args:
        item_name: The item.

    Returns:
        The count.
    """
    return 120


async def build_envelope(ctx: RunContext[AgentDeps], message: str) -> ToyResponse:
    """The toy agent's output: the envelope, echoing the minted step id."""
    return ToyResponse(
        agent=AgentName.INVENTORY,
        step_id=ctx.deps.current_step_id,
        request_id=ctx.deps.request_id,
        customer=ToyCustomer(message=message),
        internal=ToyInternal(
            cash_before=SECRET,
            signals=[
                BlockerSignal(
                    line_id="L1",
                    code=BlockerCode.INSUFFICIENT_STOCK,
                    detail=BLOCKER_DETAIL,
                )
            ],
        ),
    )


toy_agent = Agent(
    deps_type=AgentDeps,
    output_type=build_envelope,
    toolsets=[AuditedToolset(FunctionToolset(tools=[check_stock]), agent=AgentName.INVENTORY)],
    name="toy",
)


def toy_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """One tool call, then the envelope."""
    if len(messages) == 1:
        return ModelResponse(parts=[ToolCallPart("check_stock", {"item_name": "A4 paper"})])
    return ModelResponse(parts=[ToolCallPart("final_result", {"message": "120 on the shelf"})])


async def build_envelope_with_a_stale_step_id(
    ctx: RunContext[AgentDeps], message: str
) -> ToyResponse:
    """An agent that forgets to echo the id it was handed."""
    response = await build_envelope(ctx, message)
    return response.model_copy(update={"step_id": "20260101T000000Z:9:099"})


forgetful_agent = Agent(
    deps_type=AgentDeps,
    output_type=build_envelope_with_a_stale_step_id,
    name="forgetful",
)


orchestrator = Agent(deps_type=AgentDeps, output_type=str, name="toy_orchestrator")

#: The sub-run results the delegation tool saw, so a test can compare the
#: parent's transcript against the delegate's own.
sub_runs: list = []


@orchestrator.tool
@delegation(AgentName.INVENTORY)
async def consult_inventory(ctx: RunContext[AgentDeps], line: str):
    """Ask inventory about a line.

    Args:
        line: The line to resolve.

    Returns:
        Inventory's view.
    """
    result = await toy_agent.run(line, deps=ctx.deps, usage=ctx.usage)
    sub_runs.append(result)
    return result


@orchestrator.tool
@delegation(AgentName.INVENTORY, name="forgetful_inventory")
async def consult_forgetful_inventory(ctx: RunContext[AgentDeps], line: str):
    """A delegation whose agent echoes back the wrong step id.

    Args:
        line: The line to resolve.

    Returns:
        Never — the seam refuses the envelope.
    """
    return await forgetful_agent.run(line, deps=ctx.deps, usage=ctx.usage)


@orchestrator.tool
@delegation(AgentName.SALES, name="exploding_sales")
async def consult_exploding_sales(ctx: RunContext[AgentDeps], line: str):
    """A delegation that crashes, to prove a bug never looks like a refusal.

    Args:
        line: The line to fail on.

    Returns:
        Never.
    """
    raise RuntimeError("the sub-agent fell over")


def orchestrator_model(tool_name: str):
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart(tool_name, {"line": "500 sheets of A4"})])
        return ModelResponse(parts=[TextPart("Thank you for your enquiry.")])

    return model


def two_calls_in_one_turn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """Two delegations in a single response, which is what the live model did."""
    if len(messages) == 1:
        return ModelResponse(
            parts=[
                ToolCallPart("consult_inventory", {"line": "500 sheets of A4"}),
                ToolCallPart("consult_inventory", {"line": "200 sheets of cardstock"}),
            ]
        )
    return ModelResponse(parts=[TextPart("Thank you for your enquiry.")])


async def run_toy(
    trail: AuditTrail,
    request_id: str = "1",
    tool_name: str = "consult_inventory",
    model=None,
    deps: AgentDeps | None = None,
):
    """Run the toy orchestrator once, with both models scripted.

    Args:
        trail: The trail to write to.
        request_id: The request being handled.
        tool_name: Which delegation the orchestrator calls, when its turn is
            the default one.
        model: The orchestrator's scripted turn, for a test that needs one the
            default cannot express — two tool calls in a single response, say.
        deps: The deps to run with, for a test that asserts on them afterwards.

    Returns:
        The orchestrator's run result.
    """
    deps = deps or AgentDeps(trail=trail, request_id=request_id)
    orchestrator_turn = model or orchestrator_model(tool_name)
    with toy_agent.override(model=FunctionModel(toy_model)):
        with forgetful_agent.override(model=FunctionModel(toy_model)):
            with orchestrator.override(model=FunctionModel(orchestrator_turn)):
                return await orchestrator.run("500 sheets of A4 please", deps=deps)


def steps(trail: AuditTrail, request_id: str | None = None):
    sql = "SELECT * FROM agent_steps"
    params = {}
    if request_id is not None:
        sql += " WHERE request_id = :request_id"
        params = {"request_id": request_id}
    sql += " ORDER BY seq"
    with project_starter.db_engine.connect() as conn:
        return conn.execute(text(sql), params).mappings().all()


# --------------------------------------------------------------------------


class TestBootstrap:
    def test_it_creates_all_seven_tables(self, trail):
        with project_starter.db_engine.connect() as conn:
            names = set(
                conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table'")
                ).scalars()
            )
        assert {
            "agent_steps",
            "blocker_signals",
            "transaction_links",
            "suspended_flows",
            "quote_registry",
            "quote_fulfilments",
            "request_cash",
        } <= names

    def test_rows_survive_a_later_init_database(self, trail):
        """`init_database`'s `if_exists="replace"` is scoped to its own four
        tables, so re-seeding never costs us evidence."""
        with project_starter.db_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO agent_steps (step_id, run_id, request_id, seq, agent,"
                    " kind, name, started_at) VALUES ('s1','r1','1',1,'inventory',"
                    "'delegation','inventory','now')"
                )
            )
        project_starter.init_database(project_starter.db_engine)
        bootstrap_audit()
        assert len(steps(trail)) == 1


class TestAToyDelegationLogsItself:
    async def test_it_writes_one_delegation_row(self, trail):
        await run_toy(trail)
        rows = steps(trail)
        delegations = [r for r in rows if r["kind"] == "delegation"]
        assert len(delegations) == 1
        assert delegations[0]["agent"] == "inventory"
        assert delegations[0]["parent_step_id"] is None
        assert delegations[0]["error"] is None

    async def test_tool_calls_hang_off_the_delegation_and_follow_it_in_seq(self, trail):
        await run_toy(trail)
        rows = steps(trail)
        delegation_row = rows[0]
        tool_rows = [r for r in rows if r["kind"] == "tool_call"]

        assert delegation_row["kind"] == "delegation"
        assert [r["name"] for r in tool_rows] == ["check_stock"]
        assert all(r["parent_step_id"] == delegation_row["step_id"] for r in tool_rows)
        assert all(r["seq"] > delegation_row["seq"] for r in tool_rows)

    async def test_step_ids_carry_their_run_request_and_sequence(self, trail):
        await run_toy(trail, request_id="7")
        rows = steps(trail, "7")
        assert rows[0]["step_id"] == "20260915T120000Z:7:001"
        assert rows[1]["step_id"] == "20260915T120000Z:7:002"

    async def test_the_blocker_lands_in_its_own_table_with_its_detail(self, trail):
        await run_toy(trail)
        with project_starter.db_engine.connect() as conn:
            rows = conn.execute(text("SELECT * FROM blocker_signals")).mappings().all()
        assert [(r["line_id"], r["code"], r["detail"]) for r in rows] == [
            ("L1", "insufficient_stock", BLOCKER_DETAIL)
        ]


class TestTheSeamDoesNotLeak:
    async def test_the_orchestrator_is_handed_a_view_with_only_the_customer_half(
        self, trail
    ):
        result = await run_toy(trail)
        returned = [
            part
            for message in result.all_messages()
            for part in message.parts
            if getattr(part, "part_kind", None) == "tool-return"
        ]
        view = returned[0].content

        assert isinstance(view, AgentView)
        assert view.customer.message == "120 on the shelf"
        assert [b.code for b in view.blockers] == [BlockerCode.INSUFFICIENT_STOCK]
        assert not hasattr(view, "internal")

    async def test_neither_the_cash_figure_nor_the_detail_enters_the_caller_context(
        self, trail
    ):
        result = await run_toy(trail)
        transcript = result.all_messages_json().decode()
        assert str(SECRET) not in transcript
        assert BLOCKER_DETAIL not in transcript

    async def test_the_internal_payload_is_in_the_trail_in_full(self, trail):
        await run_toy(trail)
        outputs = json.loads(steps(trail)[0]["outputs"])
        assert outputs["internal"]["cash_before"] == SECRET
        assert outputs["internal"]["signals"][0]["detail"] == BLOCKER_DETAIL


class TestACrashIsNotARefusal:
    async def test_the_exception_lands_in_error_and_raises_no_blocker(self, trail):
        with pytest.raises(RuntimeError, match="fell over"):
            await run_toy(trail, tool_name="consult_exploding_sales")

        rows = steps(trail)
        assert len(rows) == 1
        assert rows[0]["error"] == "RuntimeError: the sub-agent fell over"
        assert rows[0]["outputs"] is None
        assert rows[0]["finished_at"] is not None

        with project_starter.db_engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM blocker_signals")).scalar() == 0


class TestTheStepIdMustBeEchoedBack:
    """The orchestrator mints the id and the agent echoes it. A disagreement
    would split one step across two keys in a trail whose whole value is that
    it joins, so the seam refuses the envelope rather than writing it."""

    async def test_a_stale_echo_is_refused_and_recorded_as_an_error(self, trail):
        with pytest.raises(ValueError, match="echoed step_id"):
            await run_toy(trail, tool_name="consult_forgetful_inventory")

        delegations = [r for r in steps(trail) if r["kind"] == "delegation"]
        assert delegations[0]["error"].startswith("ValueError: forgetful_inventory echoed")
        assert delegations[0]["outputs"] is None


class TestTheTranscriptSidecar:
    async def test_one_line_per_delegation_keyed_to_its_step(self, trail):
        await run_toy(trail)
        lines = trail.transcript_path.read_text().strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["step_id"] == steps(trail)[0]["step_id"]

    async def test_the_messages_round_trip_through_the_type_adapter(self, trail):
        await run_toy(trail)
        entry = trail.read_transcript()[0]
        restored = entry["messages"]
        assert isinstance(restored, list)
        assert ModelMessagesTypeAdapter.dump_python(restored)
        assert any(
            getattr(part, "tool_name", None) == "check_stock"
            for message in restored
            for part in message.parts
        )

    async def test_a_second_delegation_appends_rather_than_replaces(self, trail):
        await run_toy(trail, request_id="1")
        await run_toy(trail, request_id="2")
        assert len(trail.transcript_path.read_text().strip().splitlines()) == 2


class TestRowsAccumulateAcrossRuns:
    async def test_two_runs_against_the_same_database_keep_both(self, trail, tmp_path):
        await run_toy(trail, request_id="1")

        second = AuditTrail(run_id="20260915T130000Z", transcript_dir=tmp_path / "audit")
        await run_toy(second, request_id="1")

        with project_starter.db_engine.connect() as conn:
            run_ids = conn.execute(
                text("SELECT DISTINCT run_id FROM agent_steps ORDER BY run_id")
            ).scalars().all()
        assert run_ids == ["20260915T120000Z", "20260915T130000Z"]
        assert second.transcript_path != trail.transcript_path


class TestTheAllMessagesHypothesis:
    """Settled empirically on pydantic-ai 2.43.0, and recorded on issue #19.

    A parent run's `all_messages()` does **not** contain a delegated sub-agent's
    messages — the delegation appears only as a tool call and its return. The
    transcript design does not depend on this: `@delegation` writes the sub-run's
    messages explicitly, so it would be correct either way.
    """

    async def test_the_parent_transcript_excludes_the_delegates_messages(self, trail):
        sub_runs.clear()
        result = await run_toy(trail)
        sub_run = sub_runs[-1]

        parent_run_ids = {m.run_id for m in result.all_messages()}
        assert parent_run_ids == {result.run_id}
        assert sub_run.run_id not in parent_run_ids

        parent_tools = {
            getattr(part, "tool_name", None)
            for message in result.all_messages()
            for part in message.parts
        }
        assert "check_stock" not in parent_tools

    async def test_which_is_why_the_sidecar_has_the_delegates_tool_call(self, trail):
        sub_runs.clear()
        await run_toy(trail)
        restored = trail.read_transcript()[0]["messages"]
        assert [m.run_id for m in restored] == [sub_runs[-1].run_id] * len(restored)


class TestTwoDelegationsInOneTurn:
    """A model may put two tool calls in one response, and pydantic-ai runs them
    concurrently. The trail has to survive that: measured on the ticket 109
    evaluation run, where two of twenty requests crashed because both
    delegations wrote their step id to the one `AgentDeps` they shared, and each
    agent then echoed back the id its sibling had minted.
    """

    async def test_both_delegations_complete_and_keep_their_own_step_id(self, trail):
        await run_toy(trail, model=two_calls_in_one_turn)
        delegations = [row for row in steps(trail) if row["kind"] == "delegation"]
        assert len(delegations) == 2
        assert [row["error"] for row in delegations] == [None, None]
        assert len({row["step_id"] for row in delegations}) == 2

    async def test_each_delegations_tool_calls_hang_off_its_own_delegation(self, trail):
        await run_toy(trail, model=two_calls_in_one_turn)
        rows = steps(trail)
        delegations = {row["step_id"] for row in rows if row["kind"] == "delegation"}
        tool_calls = [row for row in rows if row["kind"] == "tool_call"]
        assert len(tool_calls) == 2
        assert {row["parent_step_id"] for row in tool_calls} == delegations

    async def test_neither_delegation_is_recorded_beneath_the_other(self, trail):
        """Both hang off the orchestrator, which has no step of its own — two
        tool calls of one turn are siblings, whatever order they finish in."""
        await run_toy(trail, model=two_calls_in_one_turn)
        delegations = [row for row in steps(trail) if row["kind"] == "delegation"]
        assert [row["parent_step_id"] for row in delegations] == [None, None]
