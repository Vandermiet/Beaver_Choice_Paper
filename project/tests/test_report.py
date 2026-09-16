"""The reflection report's figures are queries, not recollections.

`docs/reflection-report.md` discusses one real run, `20260916T111405Z`, and
states thirty-seven measured figures from it. `.gitignore` keeps
`munder_difflin.db` out of the repo — `init_database` rewrites it — so the run's
audit trail travels beside the report as a SQL dump instead, and these tests
rebuild it in memory and put the report's own SQL to it.

The contract is the report's appendix:

- a fenced ```sql block whose statements are each preceded by a
  `-- figure: <name>` comment, one statement per name;
- a table of `| figure | value | what it counts |` whose first column is that
  same set of names.

Neither may carry a name the other does not, so a figure cannot be quoted
without its query and a query cannot go unquoted. The appendix is the contract
and the prose above it is a reader's copy of the same numbers, so every row of
the prose tables that restates a figure is named in `PROSE_ROWS` and held to it
— a copy nothing checks is a copy that drifts.
"""

import re
import sqlite3
from functools import cache
from pathlib import Path

import pytest

PROJECT_DIR = Path(__file__).resolve().parents[1]
REPORT = PROJECT_DIR.parent / "docs" / "reflection-report.md"
TRAIL_DUMP = PROJECT_DIR / "test_results" / "audit_20260916T111405Z.sql"

#: The run the report discusses, and the only one the dump holds. A second run
#: would change every answer below, so the tests state the one they assume.
RUN_ID = "20260916T111405Z"

#: The appendix table's header, cell for cell. Everything below it is a figure
#: under contract; every other table in the report is prose for a reader.
APPENDIX_HEADER = ("figure", "value", "what it counts")

#: The prose tables' rows that restate an appendix figure, by the label in the
#: row's first cell: one figure name per cell that follows, `None` where a cell
#: is not a figure. A cell holding more than one number is compared on the
#: first, which is the one the figure names.
PROSE_ROWS = {
    "fulfilled": ("outcome.fulfilled",),
    "partially fulfilled": ("outcome.partially_fulfilled",),
    "rejected": ("outcome.rejected",),
    "pending customer revision": ("outcome.pending_revision",),
    "requests that moved cash": ("cash.requests_moving",),
    "requests that weighed the books and moved nothing": ("cash.requests_weighing_only",),
    "opening cash": ("cash.opening",),
    "closing cash": ("cash.closing",),
    "revenue this run": ("cash.revenue",),
    "spend this run": ("cash.spend",),
    "requests ending on a negative balance": ("cash.requests_ending_negative",),
    "insufficient_stock": ("blocker.insufficient_stock", "blocker.insufficient_stock_lines", None),
    "item_not_carried": ("blocker.item_not_carried", None, None),
    "deadline_unmeetable": ("blocker.deadline_unmeetable", None, None),
    "size_not_carried": ("blocker.size_not_carried", "blocker.size_not_carried_lines", None),
    "unit_not_understood": ("blocker.unit_not_understood", "blocker.unit_not_understood_lines", None),
    "unpriceable": ("blocker.unpriceable", None, None),
    "cash_insufficient": ("blocker.cash_insufficient", None, None),
}

_SQL_FENCE = re.compile(r"^```sql\s*$")
_FIGURE_COMMENT = re.compile(r"^--\s*figure:\s*([\w.]+)\s*$")
_SEPARATOR = re.compile(r"^[\s:|-]+$")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


@cache
def _report_text() -> str:
    assert REPORT.exists(), f"the reflection report is missing: {REPORT}"
    return REPORT.read_text()


def _cells(line: str) -> list[str] | None:
    """One Markdown table row as its cells, or None if the line is not one.

    A separator row (`|---|---|`) is part of its table but carries no cells, so
    it comes back empty rather than None — None is what ends a table.
    """
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return None
    if _SEPARATOR.match(stripped):
        return []
    return [cell.strip().strip("`*").strip() for cell in stripped[1:-1].split("|")]


@cache
def _tables() -> tuple[tuple[tuple[str, ...], ...], ...]:
    """Every Markdown table in the report, as rows of cells, in order."""
    tables, current = [], []
    for line in _report_text().splitlines():
        cells = _cells(line)
        if cells is None:
            if current:
                tables.append(tuple(current))
                current = []
            continue
        if cells:
            current.append(tuple(cells))
    if current:
        tables.append(tuple(current))
    return tuple(tables)


@cache
def _queries() -> dict[str, str]:
    """The named statements in the report's fenced SQL block."""
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
            assert not statement, f"the statement before {comment.group(1)!r} has no semicolon"
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
    assert not statement, f"the last statement ({name!r}) has no semicolon"
    return queries


@cache
def _stated() -> dict[str, str]:
    """The figures the appendix table states, by name."""
    for table in _tables():
        if table[0] == APPENDIX_HEADER:
            return {row[0]: row[1] for row in table[1:]}
    raise AssertionError(f"the report has no table headed {APPENDIX_HEADER}")


@cache
def _prose_cells() -> dict[str, tuple[str, ...]]:
    """Each labelled row of the report's prose tables, by its first cell."""
    rows = {}
    for table in _tables():
        if table[0] == APPENDIX_HEADER:
            continue
        for row in table:
            rows.setdefault(row[0], row[1:])
    return rows


def _as_number(value: str) -> float:
    """The first number in a cell, however the report dresses it up.

    `$45,059.70`, `**10**` and `6 (+1 — see below)` are all figures a reader
    should be able to write naturally; only the leading number is the claim.
    """
    found = _NUMBER.search(value.replace(",", ""))
    assert found, f"no number in {value!r}"
    return float(found.group())


@pytest.fixture(scope="module")
def trail():
    """The run's audit trail, rebuilt in memory from the committed dump."""
    assert TRAIL_DUMP.exists(), f"the audit-trail dump is missing: {TRAIL_DUMP}"
    db = sqlite3.connect(":memory:")
    db.executescript(TRAIL_DUMP.read_text())
    yield db
    db.close()


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


def test_the_dump_holds_the_run_the_report_was_written_from(trail):
    """A second run in this dump would invalidate every figure below."""
    runs = sorted(row[0] for row in trail.execute("select distinct run_id from agent_steps"))
    assert runs == [RUN_ID]


@pytest.mark.parametrize("figure", sorted(_queries()))
def test_each_measured_figure_matches_the_audit_trail(figure, trail):
    """The number in the prose is what its own query answers, to the penny."""
    query, stated = _queries()[figure], _stated()[figure]
    rows = trail.execute(query).fetchall()
    assert len(rows) == 1 and len(rows[0]) == 1, f"{figure}: the query must return one scalar"
    measured = rows[0][0]
    assert measured is not None, f"{figure}: the query answered NULL"
    assert float(measured) == pytest.approx(_as_number(stated), abs=0.005), (
        f"{figure}: the report says {stated}, the trail says {measured}"
    )


@pytest.mark.parametrize("label", sorted(PROSE_ROWS))
def test_each_prose_table_row_matches_the_figure_it_restates(label):
    """A number a reader meets in the prose is the one the appendix checked."""
    cells = _prose_cells().get(label)
    assert cells is not None, f"no prose table row labelled {label!r}"
    for cell, figure in zip(cells, PROSE_ROWS[label], strict=True):
        if figure is None:
            continue
        assert _as_number(cell) == _as_number(_stated()[figure]), (
            f"{label!r} says {cell!r}, but {figure} is {_stated()[figure]!r}"
        )
