"""One live round-trip through the Vocareum proxy, with a tool call.

The proxy was probed at the wire level on issue #2; this is the library-level
confirmation that pydantic-ai's default Tool Output mode works against it —
the one capability every agent in the design depends on. Deselected by default;
run with `BEAVER_LIVE=1 pytest -m live`.
"""

import pytest
from pydantic_ai import Agent

from beaver.llm import build_model


@pytest.mark.live
def test_one_round_trip_with_a_tool_call():
    agent = Agent(build_model(), output_type=int)

    @agent.tool_plain
    def sheets_per_ream(reams: int) -> int:
        """How many sheets are in a given number of reams.

        Args:
            reams: The number of reams.

        Returns:
            The sheet count.
        """
        return reams * 500

    result = agent.run_sync("How many sheets are in 3 reams? Use the tool.")

    assert result.output == 1500
    assert any(
        part.part_kind == "tool-call"
        for message in result.all_messages()
        for part in getattr(message, "parts", [])
    )
