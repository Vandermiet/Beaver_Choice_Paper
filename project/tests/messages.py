"""Reading a scripted run's own messages back out of it.

Every orchestrator-level test drives the real hand-off rather than constants:
what one delegation returns is what the next one is called with. That means the
script has to read the run it is in the middle of, and these three functions are
how. They belong here rather than in one test module because `test_sales.py`,
`test_outcome.py` and every flow test after them need the same two reads —
what a delegation handed *up*, and what a delegation was handed *down*.

They are plumbing, not assertions: nothing here knows a business rule, and a
test that needs one states it itself.
"""

import json
from typing import Any

from pydantic_core import to_jsonable_python


def handed_up(messages: list[Any], tool_name: str) -> dict:
    """One delegation's customer payload, out of the orchestrator's own context.

    The most recent call wins, so a test that delegates twice reads the second
    answer rather than the first.

    Args:
        messages: The orchestrator's messages so far.
        tool_name: The delegation tool whose return to read.

    Returns:
        The customer half of what that delegation handed up, or `{}`.
    """
    for message in reversed(messages):
        for part in getattr(message, "parts", []):
            if getattr(part, "tool_name", None) != tool_name:
                continue
            payload = to_jsonable_python(getattr(part, "content", None))
            if isinstance(payload, dict) and "customer" in payload:
                return payload["customer"]
    return {}


def handed_down(messages: list[Any], marker: str) -> list[dict]:
    """The lines a delegation's prompt carried into the agent reading them.

    A delegation's prompt is the date and then one JSON line per line of the
    request, so the agent's own script parses them back rather than being told
    separately what it was sent.

    Args:
        messages: The delegate's messages so far.
        marker: A field name that only this prompt's line model carries.

    Returns:
        The lines, as the prompt carried them.
    """
    for message in reversed(messages):
        for part in getattr(message, "parts", []):
            content = getattr(part, "content", None)
            if isinstance(content, str) and marker in content:
                return [
                    json.loads(line)
                    for line in content.splitlines()
                    if line.startswith("{")
                ]
    return []


#: The field that appears on a resolved line and on nothing sent earlier.
RESOLVED = '"item_name"'
#: The field quoting mints, so it marks a priced line and only a priced line.
PRICED = '"quote_line_id"'
