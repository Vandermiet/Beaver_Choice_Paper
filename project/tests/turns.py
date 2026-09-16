"""Scripted turns a `FunctionModel` can hand back, shared across the flow tests.

`messages.py` reads a scripted run; this writes one. A turn here belongs to an
agent whose model has no judgement to exercise — it reads its tools and hands
its input back — so every flow test that reaches that agent wants the same
turn, and three copies of it would be three places for the hand-off to drift
from the one the orchestrator actually makes.

Plumbing, not assertions: nothing here knows a business rule, and a test that
needs one states it itself.
"""

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart

from tests.messages import NEEDS, handed_down


def replenishment_turn(messages: list[ModelMessage]) -> ModelResponse:
    """The reads, then the restock request back unchanged.

    Replenishment decides nothing in its model: both guards run over the plans
    after it returns, against readings taken in the same breath as the writes.

    Args:
        messages: The agent's messages so far.

    Returns:
        The tool calls on its first turn, and its final result on any later one.
    """
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
