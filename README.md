# Beaver's Choice / Munder Difflin — Multi-Agent System

A multi-agent system for the Munder Difflin Paper Company: it reads a customer's
prose enquiry, works out what they are asking for, prices it, sells what it can,
buys in what it cannot, and writes one reply back. Built on
[pydantic-ai](https://ai.pydantic.dev/) against the provided `project_starter.py`
harness and its SQLite database.

## The system in one paragraph

**Five agents**, the maximum the brief allows. One **orchestrator** owns the plan
and every customer-facing word — it never touches a system of record itself.
Around it sit four domain agents, each owning exactly one question: **inventory**
(do we carry this, and how short are we?), **quoting** (what does each line
cost?), **sales** (can we fill this line right now?) and **replenishment** (can we
buy in what was refused, in time and within our means?). The sequence is fixed —
resolve, price, commit — with one bounded retry: if sales declines a line for
want of stock, the orchestrator sends replenishment to buy it in and offers the
line to sales once more. There is no third pass.

Every request ends in one of four outcomes: `FULFILLED`, `PARTIALLY_FULFILLED`,
`REJECTED`, or `PENDING_CUSTOMER_REVISION` (the enquiry is too ambiguous to price
and the reply asks the revision questions instead).

## Where to look

| What | Where |
|---|---|
| Harness entry point, agent wiring | [`project/project_starter.py`](project/project_starter.py) |
| The agents themselves | [`project/beaver/`](project/beaver/) — one package per agent |
| Workflow diagram (high level) | [`docs/workflow-high-level.md`](docs/workflow-high-level.md) |
| Workflow diagram (full, with tools) | [`docs/workflow-diagram-detailed.md`](docs/workflow-diagram-detailed.md) |
| Design notes / write-up | [`docs/reflection-report.md`](docs/reflection-report.md) |
| Test-run output | [`project/test_results/test_results.csv`](project/test_results/) |

`docs/reflection-report.md` is the main write-up and is written to be read on its
own: architecture in prose, then the measured results of one real run over the
twenty sample requests, with the SQL behind every figure in its appendix.

## Running it

```bash
pip install -r project/requirements.txt          # Python 3.8+
echo "UDACITY_OPENAI_API_KEY=your_key_here" > .env
cd project && python project_starter.py
```

It must be run from `project/` — the harness reads `quote_requests_sample.csv` by
relative path, and `init_database()` rebuilds `munder_difflin.db` from the seed on
every run, so each run starts from identical state. The run calls the
OpenAI-compatible Vocareum proxy (`gpt-4o-mini`), walks all twenty sample
requests in date order, prints each outcome and the running cash and inventory
position, and writes `project/test_results/test_results.csv`.

To run the tests instead — they use a scripted model, so they need no API key and
make no network calls:

```bash
cd project && python -m pytest
```

## Disclosure: how this was built

This project was built with [Claude Code](https://claude.com/claude-code) using
the open-source agent skills from **Matt Pocock's AI Hero** skill set —
[github.com/mattpocock/skills](https://github.com/mattpocock/skills)
([aihero.dev/skills](https://www.aihero.dev/skills), MIT licensed). Four of those
skills carried the work, in this order:

**`/wayfinder` — to chart the map.** The brief was too large to hold in one
session, and the hard part was not writing code but deciding things: how many
agents, where the responsibility boundaries fall, what an outcome is. Wayfinder
turns that fog into a map — a single GitHub issue whose child tickets are each
one open *decision* — and resolves them one at a time. The map for this project is
[issue #1](https://github.com/Vandermiet/Beaver_Choice_Paper/issues/1); every
architectural choice in `docs/reflection-report.md` traces back to a resolved
ticket on it.

**`/to-spec` — to write the decisions down.** Once the map was clear, `to-spec`
synthesised the resolved decisions into a written spec on the tracker, without
re-interviewing. This is the step that makes the design reviewable as a single
document before a line of it is built.

**`/to-tickets` — to slice it into buildable work.** The spec was then broken into
tracer-bullet tickets — thin vertical slices, each declaring which other tickets
block it. That ordering is what let the system be built and tested incrementally
rather than all at once, with a working end-to-end path available early.

**`/implement` — to build each ticket.** Each ticket was implemented test-first
(`/tdd`) at the agreed seams, with the full suite run at the end and the work
reviewed before commit. The test files under `project/tests/` are the residue of
that: one per agent, plus the audit, ledger and outcome layers.

The commit and pull-request history on
[the repository](https://github.com/Vandermiet/Beaver_Choice_Paper) follows this
sequence and can be read as the record of it.
