"""Fixtures shared by the suite.

Every fixture that touches the database seeds a *temporary* `munder_difflin.db`
at seed 137 — the seed every fact in the locked design was established against.
The provided helpers read a module-global engine, so the fixture swaps that
global rather than passing an engine around.

One of them, `_a_database_to_read`, is autouse and session-wide: the helpers'
default engine is `sqlite:///munder_difflin.db`, a path SQLite resolves against
the working directory, so without it the suite quietly reads whichever database
happens to sit where pytest was started from — or creates an empty one.
"""

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

import project_starter  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _a_database_to_read(tmp_path_factory):
    """Point the helpers at a seeded database for the whole session.

    A test that never asks for `seeded_db` can still reach the database
    without meaning to: `CarriedItemName` validates through
    `carried_catalogue()`, so merely constructing a line reads the `inventory`
    table. The helpers' default engine is `sqlite:///munder_difflin.db` — a
    relative path SQLite resolves against the process working directory — so
    such a test passed from `project/`, where a seeded database happens to
    exist, and failed from anywhere else with `no such table: inventory`,
    leaving an empty database file behind where it ran.

    Seeding one here makes the suite say the same thing from any directory,
    and says it about a database of its own rather than whichever one a
    developer last left in the working directory. Tests that want a database
    they can write to still take `seeded_db`, which swaps this one out for a
    fresh one for the length of the test.

    `init_database` reads its CSVs by relative path, so the seeding — and only
    the seeding — happens from `project/`.

    Yields:
        The session's engine, already seeded at seed 137.
    """
    db = tmp_path_factory.mktemp("session_db") / "munder_difflin.db"
    engine = create_engine(f"sqlite:///{db}")
    here = Path.cwd()
    os.chdir(PROJECT_DIR)
    try:
        project_starter.init_database(engine)
    finally:
        os.chdir(here)

    was = project_starter.db_engine
    project_starter.db_engine = engine
    yield engine
    project_starter.db_engine = was


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """A freshly seeded database at seed 137, with the helpers pointed at it.

    `init_database` reads `quote_requests.csv` and `quotes.csv` by relative
    path, so the working directory moves to `project/` for the duration.
    """
    monkeypatch.chdir(PROJECT_DIR)
    engine = create_engine(f"sqlite:///{tmp_path / 'munder_difflin.db'}")
    monkeypatch.setattr(project_starter, "db_engine", engine)
    project_starter.init_database(engine)
    return engine


def pytest_collection_modifyitems(config, items):
    """Deselect the live proxy smoke test unless `BEAVER_LIVE=1`.

    It costs a model call and needs a key, so it stays out of the default run.
    """
    import os

    if os.environ.get("BEAVER_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="needs BEAVER_LIVE=1 (hits the Vocareum proxy)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def trail(seeded_db, tmp_path):
    """A trail on a seeded database that has the seven audit tables alongside its four.

    The `run_id` is fixed rather than minted, so a test can assert on step ids.
    The transcript sidecar is redirected into `tmp_path`, so a test run never
    writes into the repo's own `audit/` directory.
    """
    from beaver.audit import AuditTrail, bootstrap_audit
    from beaver.contract import forget_carried_catalogue

    forget_carried_catalogue()
    bootstrap_audit()
    return AuditTrail(run_id="20260915T120000Z", transcript_dir=tmp_path / "audit")
