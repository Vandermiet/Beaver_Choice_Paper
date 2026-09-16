"""The audit trail: seven tables in `munder_difflin.db` plus a JSONL transcript
sidecar, written by a decorator at the delegation seam.

Filled by ticket 102. The DDL is on issues #5, #7 and #10. `bootstrap_audit()`
runs *after* `init_database`, whose `if_exists="replace"` is scoped to its own
four tables, so these survive a re-init and accumulate across runs.

The orchestrator owns this trail, and that does not violate *the orchestrator
never acts*: the principle is *no operation on a system of record*, and nothing
in the business ever reads these tables back.

**Nothing here is called by hand.** `@delegation` wraps the delegation seam and
`AuditedToolset` wraps a domain agent's tools; between them every row in the
trail is written by something that cannot be forgotten at a call site. A
hand-written `log_step()` is one missed call away from an invisible gap, and the
decorator is in any case already the only thing that ever sees an internal
payload.
"""

from __future__ import annotations

import contextlib
import functools
import itertools
import json
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic_ai import AgentRunResult, ModelMessagesTypeAdapter, RunContext
from pydantic_ai.toolsets import AbstractToolset, ToolsetTool, WrapperToolset
from pydantic_core import to_jsonable_python
from sqlalchemy import text

from beaver import starter
from beaver.contract import (
    AgentName,
    AgentResponse,
    AgentView,
    BlockerSignal,
    MovesCash,
    SuspendedFlow,
)
from beaver.outcome import RequestJournal

#: Where the transcript sidecar lands, relative to the working directory the
#: harness runs from (`project/`).
TRANSCRIPT_DIR = Path("audit")

#: The seven tables, created alongside `init_database`'s four rather than by it.
#: `IF NOT EXISTS` because rows accumulate across runs — a clean run means
#: deleting the `.db` file, not dropping these.
AUDIT_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS agent_steps (
      step_id        TEXT PRIMARY KEY,
      parent_step_id TEXT,
      run_id         TEXT NOT NULL,
      request_id     TEXT NOT NULL,
      seq            INTEGER NOT NULL,
      agent          TEXT NOT NULL,
      kind           TEXT NOT NULL,
      name           TEXT NOT NULL,
      inputs         TEXT,
      outputs        TEXT,
      started_at     TEXT NOT NULL,
      finished_at    TEXT,
      error          TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS blocker_signals (
      step_id  TEXT NOT NULL,
      line_id  TEXT NOT NULL,
      code     TEXT NOT NULL,
      detail   TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transaction_links (
      transaction_rowid INTEGER NOT NULL,
      run_id            TEXT NOT NULL,
      request_id        TEXT NOT NULL,
      step_id           TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS suspended_flows (
      resume_token TEXT PRIMARY KEY,
      run_id       TEXT NOT NULL,
      request_id   TEXT NOT NULL,
      suspended_at TEXT NOT NULL,
      payload      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quote_registry (
      quote_line_id  TEXT PRIMARY KEY,
      run_id         TEXT NOT NULL,
      request_id     TEXT NOT NULL,
      line_id        TEXT NOT NULL,
      item_name      TEXT NOT NULL,
      units          INTEGER NOT NULL,
      unit_price     REAL NOT NULL,
      band           TEXT NOT NULL,
      discount_rate  REAL NOT NULL,
      line_total     REAL NOT NULL,
      quoted_at      TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quote_fulfilments (
      transaction_rowid INTEGER NOT NULL,
      quote_line_id     TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS request_cash (
      run_id      TEXT NOT NULL,
      request_id  TEXT NOT NULL,
      cash_before REAL NOT NULL,
      cash_after  REAL NOT NULL,
      cash_delta  REAL NOT NULL,
      measured_at TEXT NOT NULL,
      PRIMARY KEY (run_id, request_id)
    )
    """,
)

def bootstrap_audit() -> None:
    """Create the seven audit tables, if they are not already there.

    Call this *after* `init_database`. That order matters only because
    `init_database` replaces its own four tables and could otherwise be thought
    to wipe these; it cannot, but the ordering makes the claim untestable
    rather than merely true.
    """
    with starter.engine().begin() as conn:
        for statement in AUDIT_DDL:
            conn.execute(text(statement))


def new_run_id() -> str:
    """Mint the id every row of this run is stamped with.

    A UTC timestamp, so it sorts, reads, and dates a resume token by itself.

    Returns:
        Something like `20260915T143002Z`.
    """
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _as_json(value: Any) -> str | None:
    """Serialise a step's inputs or outputs, never raising on the way.

    A trail that crashes the run it is observing is worse than a trail with one
    unreadable cell, so anything pydantic cannot make JSON of is stored as its
    repr.
    """
    if value is None:
        return None
    try:
        return json.dumps(to_jsonable_python(value))
    except Exception:  # noqa: BLE001 — see docstring
        return json.dumps({"unserialisable": repr(value)})


class StepKind(StrEnum):
    """The two things `agent_steps` records, which are different questions.

    "Which agent did we ask?" and "which tool did it call?" cannot share one
    column, so `kind` says which question a row answers and `name` answers it.
    """

    DELEGATION = "delegation"
    TOOL_CALL = "tool_call"


@dataclass
class Step:
    """One `agent_steps` row between its `seq` being reserved and it being written.

    It exists because the row is not writable at the moment it is opened: `seq`
    has to be taken at call time so the trail reads in causal order, while
    `outputs` and `error` are only known at return time. The gap between those
    two moments is what this holds.
    """

    step_id: str
    parent_step_id: str | None
    request_id: str
    seq: int
    agent: AgentName
    kind: StepKind
    name: str
    inputs: Any
    started_at: str
    outputs: Any = None


@dataclass
class AuditTrail:
    """One run's worth of trail: the tables, the sidecar, and the seq counter.

    `seq` is monotonic within a request and shared across both kinds of row, so
    a delegation always sits above the tool calls it caused. It is reserved when
    the call starts and the row is written when it returns, which is what makes
    the trail read in true causal order rather than in completion order.
    """

    run_id: str
    transcript_dir: Path = TRANSCRIPT_DIR
    _counters: dict[str, itertools.count] = field(default_factory=dict, repr=False)

    @property
    def transcript_path(self) -> Path:
        """The sidecar for this run, one line per delegation."""
        return self.transcript_dir / f"transcript_{self.run_id}.jsonl"

    def reserve_step(self, request_id: str) -> tuple[str, int]:
        """Take the next `seq` for a request and mint the step id that goes with it.

        Args:
            request_id: The request the step belongs to.

        Returns:
            The step id and its sequence number.
        """
        counter = self._counters.setdefault(request_id, itertools.count(1))
        seq = next(counter)
        return f"{self.run_id}:{request_id}:{seq:03d}", seq

    @contextlib.contextmanager
    def recording(
        self,
        *,
        agent: AgentName,
        kind: StepKind,
        name: str,
        request_id: str,
        parent_step_id: str | None,
        inputs: Any,
    ) -> Iterator[Step]:
        """Open a step, and write its row however the body ends.

        The body sets `step.outputs`; anything that escapes it is written to
        `error` and re-raised. Both the delegation seam and the tool seam record
        through here, so neither can drift from the other about what a step is.

        Args:
            agent: Whose step this is.
            kind: Whether this is a delegation or a tool call.
            name: The delegated agent's name, or the tool's.
            request_id: The request being handled.
            parent_step_id: The delegation this hangs off, or `None`.
            inputs: The arguments, serialised as JSON.

        Yields:
            The open step, for the body to hang its outputs on.
        """
        step_id, seq = self.reserve_step(request_id)
        step = Step(
            step_id=step_id,
            parent_step_id=parent_step_id,
            request_id=request_id,
            seq=seq,
            agent=agent,
            kind=kind,
            name=name,
            inputs=inputs,
            started_at=_now(),
        )
        try:
            yield step
        except Exception as exc:
            self.write_step(step, error=f"{type(exc).__name__}: {exc}")
            raise
        self.write_step(step)

    def write_step(self, step: Step, error: str | None = None) -> None:
        """Write one `agent_steps` row, at the moment the call returned.

        Args:
            step: The step, its outputs filled in if it got that far.
            error: The raw exception, if one escaped. Deliberately *not* a
                blocker signal: a blocker is a designed outcome and an exception
                is a bug, and collapsing them would let a crash masquerade as a
                business rejection in the rubric's count.
        """
        with starter.engine().begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO agent_steps (
                      step_id, parent_step_id, run_id, request_id, seq, agent,
                      kind, name, inputs, outputs, started_at, finished_at, error
                    ) VALUES (
                      :step_id, :parent_step_id, :run_id, :request_id, :seq, :agent,
                      :kind, :name, :inputs, :outputs, :started_at, :finished_at, :error
                    )
                    """
                ),
                {
                    "step_id": step.step_id,
                    "parent_step_id": step.parent_step_id,
                    "run_id": self.run_id,
                    "request_id": step.request_id,
                    "seq": step.seq,
                    "agent": str(step.agent),
                    "kind": str(step.kind),
                    "name": step.name,
                    "inputs": _as_json(step.inputs),
                    "outputs": _as_json(step.outputs),
                    "started_at": step.started_at,
                    "finished_at": _now(),
                    "error": error,
                },
            )

    def write_blockers(self, step_id: str, signals: list[BlockerSignal]) -> None:
        """Write one `blocker_signals` row per signal the step raised.

        Their own table rather than a JSON column on the step, because the
        rubric's rejection gate is then a `GROUP BY` rather than a script.

        Args:
            step_id: The step that raised them.
            signals: The signals, detail included.
        """
        if not signals:
            return
        with starter.engine().begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO blocker_signals (step_id, line_id, code, detail)
                    VALUES (:step_id, :line_id, :code, :detail)
                    """
                ),
                [
                    {
                        "step_id": step_id,
                        "line_id": signal.line_id,
                        "code": str(signal.code),
                        "detail": signal.detail,
                    }
                    for signal in signals
                ],
            )

    def write_suspension(self, flow: SuspendedFlow) -> None:
        """Write the one `suspended_flows` row a paused request leaves behind.

        The flow travels whole, as JSON, rather than spread over columns: it is
        read back by a resumption that does not exist yet, and a shape nothing
        queries is better kept in one piece than guessed at in five. Its key,
        its run and its request are columns anyway, because those are what a
        reader looks a suspension *up* by.

        `INSERT OR REPLACE` because the token is the request, not the attempt:
        a request handled twice in one run has one latest suspension, and two
        rows claiming the same token would be a trail disagreeing with itself.

        Args:
            flow: The suspended flow, with its questions and its completed steps.
        """
        with starter.engine().begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT OR REPLACE INTO suspended_flows (
                      resume_token, run_id, request_id, suspended_at, payload
                    ) VALUES (
                      :resume_token, :run_id, :request_id, :suspended_at, :payload
                    )
                    """
                ),
                {
                    "resume_token": flow.resume_token,
                    "run_id": flow.run_id,
                    "request_id": flow.request_id,
                    "suspended_at": flow.suspended_at.isoformat(),
                    "payload": flow.model_dump_json(),
                },
            )

    def write_cash(self, request_id: str, cash: RequestCash) -> None:
        """Write the one `request_cash` row a request that moved money leaves behind.

        The per-request delta, which no single envelope holds: a sale, a
        purchase and a second sale are three steps with three deltas of their
        own, and what the books actually did over the request is the balance
        before the first against the balance after the last. The orchestrator
        is the only thing that sees all three, so it is the only thing that can
        write this row.

        A request that never reached a cash-moving step writes nothing; one
        that reached it and moved nought writes a row saying so, because
        "we weighed this request and it changed the balance by nothing" is a
        different fact from "we never weighed it". "Which requests moved cash?"
        is then `WHERE cash_delta != 0`.

        `INSERT OR REPLACE` for the reason `suspended_flows` uses it: the key
        is the request, not the attempt.

        Args:
            request_id: The request the money moved for.
            cash: The readings the seam collected, first before and last after.
        """
        if not cash.observed:
            return
        with starter.engine().begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT OR REPLACE INTO request_cash (
                      run_id, request_id, cash_before, cash_after, cash_delta,
                      measured_at
                    ) VALUES (
                      :run_id, :request_id, :cash_before, :cash_after, :cash_delta,
                      :measured_at
                    )
                    """
                ),
                {
                    "run_id": self.run_id,
                    "request_id": request_id,
                    "cash_before": cash.before,
                    "cash_after": cash.after,
                    "cash_delta": cash.delta,
                    "measured_at": _now(),
                },
            )

    def write_transcript(self, step_id: str, messages: Any) -> None:
        """Append one delegation's raw model messages to the run's sidecar.

        Keyed on `step_id`, so it joins straight back to `agent_steps` with no
        second identifier to keep in sync.

        Args:
            step_id: The delegation these messages belong to.
            messages: The sub-run's messages, as pydantic-ai returns them.
        """
        self.transcript_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"step_id": step_id, "messages": to_jsonable_python(messages)}
        )
        with self.transcript_path.open("a", encoding="utf-8") as sidecar:
            sidecar.write(line + "\n")

    def read_transcript(self) -> list[dict[str, Any]]:
        """Read the sidecar back, messages revalidated into pydantic-ai models.

        Returns:
            One entry per delegation, each `{"step_id": ..., "messages": ...}`
            with the messages as `ModelMessage` objects again.
        """
        if not self.transcript_path.exists():
            return []
        entries = []
        for line in self.transcript_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            raw = json.loads(line)
            entries.append(
                {
                    "step_id": raw["step_id"],
                    "messages": ModelMessagesTypeAdapter.validate_python(raw["messages"]),
                }
            )
        return entries


@dataclass
class RequestCash:
    """The books either side of everything one request did to them.

    Filled by the delegation seam from the internal half of every `MovesCash`
    step, and so never by a call site that could forget. It is the measurement
    the bounded retry made necessary: sales' pass 1, replenishment's purchase
    and sales' pass 2 each report their own movement, and none of them is the
    request's. The first reading taken and the last one taken are, because
    nothing else in the run wrote between them on this request's behalf.

    It rides on `AgentDeps` beside the journal, and it is the opposite of the
    journal in what it holds: the journal is what the orchestrator was handed,
    and this is what the seam withheld from it. Neither reaches the model.
    """

    #: The balance before the first step that moved money. `None` until one has.
    before: float | None = None
    #: The balance after the most recent one.
    after: float | None = None

    def observe(self, payload: MovesCash) -> None:
        """Take one cash-moving step's readings into the request's own pair.

        Args:
            payload: The internal half of a step that wrote to `transactions`.
        """
        if self.before is None:
            self.before = payload.cash_before
        self.after = payload.cash_after

    @property
    def observed(self) -> bool:
        """Whether any step of this request weighed the books at all.

        True the moment sales or replenishment has run, whether or not it
        moved anything: a pass that committed nothing still read the balance
        either side of the writes it did not make.
        """
        return self.before is not None

    @property
    def delta(self) -> float:
        """What the request did to the balance, to the cent.

        Returns:
            The last reading less the first, or nought if nothing weighed it.
        """
        if self.before is None or self.after is None:
            return 0.0
        return round(self.after - self.before, 2)


@dataclass
class AgentDeps:
    """What every agent in the system is run with.

    It lives here rather than in `contract` because both of its moving parts
    exist for the trail: the trail itself, and the delegation step that tool
    calls should hang off. A delegate is handed a *copy* carrying its own
    `current_step_id` — see `_with_step_id` — which is what makes the step id
    reach the sub-agent's tools without any agent having to pass it along, and
    without two concurrent delegations having to share one slot to put it in.
    Everything else here is shared by reference across that copy, because
    everything else is a fact about the request rather than about one step of
    it.

    The journal rides here too, and it is deliberately *not* part of the trail:
    the trail records what the agents did, internal halves included, while the
    journal holds only what the orchestrator was handed. They travel together
    because the delegation seam is the one place that sees both and can forget
    neither — the alternative is a call site remembering to record, which is
    the arrangement this whole module exists to avoid.
    """

    trail: AuditTrail
    request_id: str
    #: The delegation this copy of the deps belongs to. `None` at the
    #: orchestrator's own level, where there is no delegation to hang anything
    #: off, and never written on an instance after it is built: the seam mints
    #: a copy per delegation rather than assigning to one shared field.
    current_step_id: str | None = None
    #: What the orchestrator has been handed about this request's lines, and
    #: what its outcome is derived from. Filled by the seam below, so a new
    #: delegation joins it by existing rather than by remembering. A domain
    #: agent run on its own gets a fresh empty one and never reads it.
    journal: RequestJournal = field(default_factory=RequestJournal)
    #: The books either side of this request, collected from the internal
    #: halves the journal never sees. Filled by the same seam, and for the same
    #: reason: a measurement a call site can forget is one that reports nought
    #: for a request that moved thousands.
    cash: RequestCash = field(default_factory=RequestCash)
    #: When stock bought in for this request reaches us, by item name. The one
    #: thing the orchestrator routes *into* a delegation rather than around it,
    #: and it travels here rather than through the prompt because a delivery
    #: promise must be exact: a date the model paraphrased, dropped or moved
    #: would promise goods that are not in the building. Empty on a first pass
    #: and for any agent that never buys anything in.
    earliest_availability: dict[str, date] = field(default_factory=dict)


def _with_step_id(ctx: RunContext[AgentDeps], step_id: str) -> RunContext[AgentDeps]:
    """The same run context, pointed at a deps that names this delegation.

    A shallow copy of both, so the trail, the journal, the cash readings and
    the availability dates stay the one set of objects the request shares —
    only `current_step_id` differs, and it differs per delegation rather than
    per request. That is what makes two delegations in one model turn safe.

    `RunContext` is pydantic-ai's dataclass rather than ours, and copying it
    is sound only while every field of it takes an initialiser: `replace`
    rebuilds the instance through `__init__`, so a field that ever became
    `init=False` upstream would raise here rather than quietly drop. Checked
    against 2.43.0, where none is, and it fails loudly at the seam if that
    changes — which is the failure mode to want.

    Args:
        ctx: The run context the tool was called with.
        step_id: The delegation now in flight.

    Returns:
        A context whose `deps.current_step_id` is this delegation's.
    """
    return replace(ctx, deps=replace(ctx.deps, current_step_id=step_id))


def delegation(agent: AgentName, name: str | None = None):
    """Make an orchestrator tool that delegates, logs itself, and cannot leak.

    The decorated function does one thing — `await sub_agent.run(..., deps=
    ctx.deps, usage=ctx.usage)` — and returns the `AgentRunResult` unchanged.
    Everything else happens here: the step id is minted and put on the deps so
    the sub-agent's own tool calls hang off it, the full response including its
    internal payload goes to `agent_steps`, its signals go to `blocker_signals`,
    the raw transcript goes to the sidecar, and what comes back to the
    orchestrator is an `AgentView` — customer payload and code-only blockers.

    The sub-run's messages are written *explicitly* rather than left to the
    parent's `all_messages()` to carry. Settled on #19 against pydantic-ai
    2.43.0: a parent run's transcript excludes its delegate's messages entirely,
    so the sidecar is the only record of them — and writing them here would have
    been correct either way.

    Args:
        agent: The agent being delegated to.
        name: The step's name in the trail. Defaults to the agent's name.

    Returns:
        A decorator turning `(ctx, ...) -> AgentRunResult[AgentResponse]` into
        `(ctx, ...) -> AgentView`.
    """
    step_name = name or str(agent)

    def decorate(
        fn: Callable[..., Awaitable[AgentRunResult[AgentResponse]]],
    ) -> Callable[..., Awaitable[AgentView]]:
        @functools.wraps(fn)
        async def delegate(ctx: RunContext[AgentDeps], *args: Any, **kwargs: Any) -> AgentView:
            deps = ctx.deps
            trail = deps.trail
            parent_step_id = deps.current_step_id

            with trail.recording(
                agent=agent,
                kind=StepKind.DELEGATION,
                name=step_name,
                request_id=deps.request_id,
                parent_step_id=parent_step_id,
                inputs={"args": list(args), "kwargs": kwargs},
            ) as step:
                # Its own deps, carrying its own step id, rather than the id
                # written onto the deps every delegation shares. A model may
                # put two tool calls in one response and pydantic-ai runs them
                # concurrently, so one mutable field is two delegations writing
                # to one slot — issue #38, which cost the ticket 109 evaluation
                # two of its twenty requests.
                result = await fn(_with_step_id(ctx, step.step_id), *args, **kwargs)

                response = result.output
                if response.step_id != step.step_id:
                    # The orchestrator mints the id and the agent echoes it
                    # back; a disagreement would silently split one step across
                    # two keys in a trail whose whole value is that it joins.
                    raise ValueError(
                        f"{step_name} echoed step_id {response.step_id!r}, "
                        f"expected {step.step_id!r}"
                    )
                step.outputs = response
                if isinstance(response.internal, MovesCash):
                    # Recognised by type rather than by name: an agent that
                    # starts writing to `transactions` is counted towards the
                    # request's cash delta by declaring what it is, not by a
                    # call site here remembering to add it.
                    deps.cash.observe(response.internal)
                trail.write_blockers(step.step_id, response.internal.signals)
                trail.write_transcript(step.step_id, result.new_messages())

            # Two narrowings, and the orchestrator is on the far side of
            # both: `to_view` drops the internal payload, and the journal drops
            # every blocker but the one now holding its line. What the model is
            # handed is therefore exactly what it may say — the leak guarantee
            # and *one blocker per line* are the same guarantee, made the same
            # way. `write_blockers` above has already recorded all of them.
            return deps.journal.record(response.to_view())

        # `functools.wraps` copied the wrapped function's return annotation, and
        # that annotation is a lie about what the orchestrator's model is handed.
        delegate.__annotations__["return"] = AgentView
        return delegate

    return decorate


@dataclass
class AuditedToolset(WrapperToolset[AgentDeps]):
    """A domain agent's tools, each call written to the trail as it returns.

    Wrapping the toolset rather than decorating each tool means a new tool is
    audited by existing, not by remembering. Rows hang off whichever delegation
    is in flight, so the trail reads in causal order: the delegation, then the
    calls it caused.

    Do **not** wrap the orchestrator's own toolset with this — its tools are the
    delegations, and `@delegation` has already written those rows.
    """

    #: Whose tools these are. No default: the wrong answer here misattributes
    #: every row the agent writes, and silently.
    agent: AgentName = field(kw_only=True)

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[AgentDeps],
        tool: ToolsetTool[AgentDeps],
    ) -> Any:
        """Call one tool and write its `agent_steps` row, however it ends.

        Args:
            name: The tool's name.
            tool_args: The arguments the model supplied.
            ctx: The run context, carrying the trail and the delegation in flight.
            tool: The tool itself.

        Returns:
            Whatever the tool returned.
        """
        deps = ctx.deps
        with deps.trail.recording(
            agent=self.agent,
            kind=StepKind.TOOL_CALL,
            name=name,
            request_id=deps.request_id,
            parent_step_id=deps.current_step_id,
            inputs=tool_args,
        ) as step:
            step.outputs = await super().call_tool(name, tool_args, ctx, tool)
        return step.outputs
