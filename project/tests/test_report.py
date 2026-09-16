"""The reflection report's figures are queries, not recollections.

`docs/reflection-report.md` discusses a run that is committed alongside it:
`project/munder_difflin.db` still holds the audit trail of run
`20260916T111405Z`, the run `test_results.csv` came out of. Every measured
figure the report states is therefore checkable, and these tests check it — the
report carries the SQL it was written from, and the suite runs that SQL against
the trail and compares the answer with the number in the prose.

The contract is the report's own appendix:

- a fenced ```sql block whose statements are each preceded by a
  `-- figure: <name>` comment, one statement per name;
- a Markdown table of `| figure | value | what it counts |`, whose first column
  is that same set of names.

Neither may carry a name the other does not, so a figure cannot be quoted
without its query and a query cannot go unquoted.
"""

import re
import sqlite3
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[1]
REPORT = PROJECT_DIR.parent / "docs" / "reflection-report.md"
TRAIL_DB = PROJECT_DIR / "munder_difflin.db"

#: The run the report discusses. A second run appended to this database would
#: change the answers, so the tests state the run they were written against.
RUN_ID = "20260916T111405Z"

_SQL_FENCE = re.compile(r"^```sql\s*$")
_FIGURE_COMMENT = re.compile(r"^--\s*figure:\s*([\w.]+)\s*$")
#: The appendix table's header row. Everything under it, until the table ends,
#: is a figure the suite holds the report to.
_APPENDIX_HEADER = "| figure | value |"

_TABLE_ROW = re.compile(r"^\|\s*`?([\w.]+)`?\s*\|\s*([^|]+?)\s*\|")


def _report_text() -> str:
    assert REPORT.exists(), f"the reflection report is missing: {REPORT}"
    return REPORT.read_text()


def _queries() -> dict[str, str]:
    """The named statements in the report's fenced SQL blocks."""
    queries: dict[str, str] = {}
    name: str | None = None
    statement: list[str] = []
    in_block = False
    for line in _report_text().splitlines():
        if not in_block:
            in_block = bool(_SQL_FENCE.match(line))
            continue
        if line.strip() == "```":
            in_block = False
            continue
        comment = _FIGURE_COMMENT.match(line.strip())
        if comment:
            assert not statement, f"figure {name!r} has no terminating semicolon"
            name = comment.group(1)
            continue
        if not line.strip():
            continue
        assert name is not None, f"SQL before any `-- figure:` comment: {line!r}"
        statement.append(line)
        if line.rstrip().endswith(";"):
            assert name not in queries, f"two queries named {name!r}"
            queries[name] = "\n".join(statement)
            name, statement = None, []
    return queries


def _stated() -> dict[str, str]:
    """The figures the appendix table states, by name.

    Only that table: the report's other tables are prose for a reader, and a
    row of one must never be mistaken for a figure under contract.
    """
    stated: dict[str, str] = {}
    in_table = False
    for line in _report_text().splitlines():
        stripped = line.strip()
        if stripped.startswith(_APPENDIX_HEADER):
            in_table = True
            continue
        if not in_table:
            continue
        if not stripped.startswith("|"):
            break
        row = _TABLE_ROW.match(stripped)
        if row:
            stated[row.group(1)] = row.group(2)
    assert stated, f"no appendix table found under {_APPENDIX_HEADER!r}"
    return stated


def _as_number(value: str) -> float:
    """A figure as the report writes it: `$1,175.35`, `**10**`, `0`."""
    cleaned = value.replace("*", "").replace("`", "").replace("$", "").replace(",", "").strip()
    return float(cleaned)


def test_the_report_exists_and_has_the_three_required_sections():
    """The rubric asks for architecture, evaluation and improvements, in prose."""
    text = _report_text()
    for heading in ("## Architecture", "## Evaluation", "## Improvements"):
        assert heading in text, f"the report has no {heading!r} section"


def test_every_stated_figure_has_a_query_and_every_query_a_figure():
    queries, stated = _queries(), _stated()
    assert queries, "the report carries no named SQL"
    assert set(queries) == set(stated), (
        f"queries without a stated figure: {sorted(set(queries) - set(stated))}; "
        f"figures without a query: {sorted(set(stated) - set(queries))}"
    )


def test_the_trail_the_report_was_written_from_is_the_committed_one():
    """A different run in this database would invalidate every figure below."""
    with sqlite3.connect(TRAIL_DB) as db:
        runs = [row[0] for row in db.execute("select distinct run_id from agent_steps")]
    assert runs == [RUN_ID]


@pytest.mark.parametrize("figure", sorted(_queries()))
def test_each_measured_figure_matches_the_audit_trail(figure):
    """The number in the prose is what its own query answers, to the penny."""
    query, stated = _queries()[figure], _stated()[figure]
    with sqlite3.connect(TRAIL_DB) as db:
        rows = db.execute(query).fetchall()
    assert len(rows) == 1 and len(rows[0]) == 1, f"{figure}: the query must return one scalar"
    measured = rows[0][0] or 0
    assert round(float(measured), 2) == pytest.approx(_as_number(stated)), (
        f"{figure}: the report says {stated}, the trail says {measured}"
    )
