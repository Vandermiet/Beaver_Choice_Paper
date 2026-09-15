"""The shared kernel: the canonical envelope, the blocker protocol, the
carried-catalogue validator.

Filled by ticket 102 from the consolidated model set on issue #10, as amended
by #15. Everything here is shared by all five agents, so nothing domain-specific
belongs in this module.

Two invariants this module exists to make structural rather than hoped for:

- **The internal payload never reaches the orchestrator.** `AgentResponse`
  carries both halves; the delegation seam hands back an `AgentView`, which has
  no `internal` field at all. A leak is a type error, not a code-review miss.
- **Any value that must be exact is enforced by a validator, never by prompt
  discipline.** `CarriedItemName` is the one that matters most: it guards the
  item name that eventually reaches `create_transaction`.
"""

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Generic, TypeVar

from pydantic import AfterValidator, BaseModel
from sqlalchemy import text

from beaver import starter


class AgentName(StrEnum):
    """The five agents. Every audit row and every envelope names one."""

    ORCHESTRATOR = "orchestrator"
    INVENTORY = "inventory"
    QUOTING = "quoting"
    SALES = "sales"
    REPLENISHMENT = "replenishment"


class BlockerCode(StrEnum):
    """The nine reasons a line can fail, each owned by exactly one agent.

    Ownership, per #15: inventory raises the five resolution codes, sales
    raises `INSUFFICIENT_STOCK` exclusively at commit time, quoting raises
    `UNPRICEABLE` as an invariant guard, and replenishment raises both
    `DEADLINE_UNMEETABLE` and `CASH_INSUFFICIENT` at buy time.
    """

    ITEM_NOT_CARRIED = "item_not_carried"
    ITEM_AMBIGUOUS = "item_ambiguous"
    SIZE_NOT_CARRIED = "size_not_carried"
    QUANTITY_MISSING = "quantity_missing"
    UNIT_NOT_UNDERSTOOD = "unit_not_understood"
    INSUFFICIENT_STOCK = "insufficient_stock"
    UNPRICEABLE = "unpriceable"
    DEADLINE_UNMEETABLE = "deadline_unmeetable"
    CASH_INSUFFICIENT = "cash_insufficient"

    @property
    def revisable(self) -> bool:
        """Whether the customer could restate the line to clear this."""
        return self in REVISABLE

    @property
    def pausing(self) -> bool:
        """Whether this code can suspend the flow pending a revision."""
        return self in PAUSING

    @property
    def internal_only(self) -> bool:
        """Whether the customer must never be told about this."""
        return self in INTERNAL_ONLY


#: Codes the customer can do something about, by restating their request.
REVISABLE: frozenset[BlockerCode] = frozenset(
    {
        BlockerCode.ITEM_AMBIGUOUS,
        BlockerCode.SIZE_NOT_CARRIED,
        BlockerCode.QUANTITY_MISSING,
        BlockerCode.UNIT_NOT_UNDERSTOOD,
        BlockerCode.DEADLINE_UNMEETABLE,
    }
)

#: Codes that can suspend a flow. All four are raised by inventory, which is
#: what makes suspension reachable only before money moves.
PAUSING: frozenset[BlockerCode] = frozenset(
    {
        BlockerCode.ITEM_AMBIGUOUS,
        BlockerCode.SIZE_NOT_CARRIED,
        BlockerCode.QUANTITY_MISSING,
        BlockerCode.UNIT_NOT_UNDERSTOOD,
    }
)

#: Codes the customer is never told about, in any form.
INTERNAL_ONLY: frozenset[BlockerCode] = frozenset({BlockerCode.CASH_INSUFFICIENT})


_carried_by_database: dict[str, frozenset[str]] = {}


def carried_catalogue() -> frozenset[str]:
    """The exact item names the business sells, read from the `inventory` table.

    The carried catalogue is a property of the seeded database, not of
    `paper_supplies` — `init_database` selects 18 of the 46 universe items at
    seed 137. Cached per database URL, so a test's temporary database and the
    harness's own never see each other's answer.

    Returns:
        The carried item names.
    """
    db = starter.engine()
    key = str(db.url)
    if key not in _carried_by_database:
        with db.connect() as conn:
            names = conn.execute(text("SELECT item_name FROM inventory")).scalars().all()
        _carried_by_database[key] = frozenset(names)
    return _carried_by_database[key]


def forget_carried_catalogue() -> None:
    """Drop the cached catalogues, so the next read goes back to the database.

    Needed only when a database is re-seeded in place under the same URL, which
    the harness never does and a test occasionally must.
    """
    _carried_by_database.clear()


def must_be_carried(item_name: str) -> str:
    """Reject any name outside the carried catalogue.

    Args:
        item_name: The name to check.

    Returns:
        The name, unchanged, if the business carries it.

    Raises:
        ValueError: If the name is not one the business sells.
    """
    if item_name not in carried_catalogue():
        raise ValueError(f"{item_name!r} is not in the carried catalogue")
    return item_name


#: An item name the business demonstrably sells. The guarantee that a
#: transaction is never written against a product we do not carry rests on this
#: annotation rather than on any agent's instructions.
CarriedItemName = Annotated[str, AfterValidator(must_be_carried)]


class RequestedLine(BaseModel):
    """One line of the customer's request, as stated and not yet resolved."""

    line_id: str
    item_as_stated: str
    quantity_as_stated: float | None
    unit_as_stated: str | None


class CustomerRequest(BaseModel):
    """One customer enquiry, the unit of work the whole system is sized around."""

    request_id: str
    run_id: str
    request_date: date
    raw_text: str
    deadline: date | None
    lines: list[RequestedLine]


class CustomerBlocker(BaseModel):
    """A blocker as the orchestrator sees it: which line, and which code.

    Derived from a `BlockerSignal` rather than raised beside one, so the
    internal `detail` cannot be forgotten on the way up.
    """

    line_id: str
    code: BlockerCode



class BlockerSignal(BaseModel):
    """A refusal as the raising agent states it, `detail` included.

    Internal only: it reaches the audit trail and never the orchestrator.
    Every blocker is line-scoped — there is no request scope and no severity,
    because all nine codes converged on line-scoped and partial (#10).
    """

    line_id: str
    code: BlockerCode
    detail: str


    def to_customer(self) -> CustomerBlocker:
        """Narrow this signal to the half the orchestrator may see.

        Returns:
            The same line and code, without the detail.
        """
        return CustomerBlocker(line_id=self.line_id, code=self.code)


class InternalPayload(BaseModel):
    """The base every agent's internal half inherits, carrying its signals."""

    signals: list[BlockerSignal] = []


TCustomer = TypeVar("TCustomer", bound=BaseModel)
TInternal = TypeVar("TInternal", bound=InternalPayload)


class AgentView(BaseModel, Generic[TCustomer]):
    """Everything the orchestrator is handed back from a delegation.

    There is no `internal` field, and the blockers carry no `detail`. That is
    the whole of the leak guarantee: the orchestrator cannot disclose what it
    was never given.
    """

    agent: AgentName
    step_id: str
    request_id: str
    customer: TCustomer
    blockers: list[CustomerBlocker]


class AgentResponse(BaseModel, Generic[TCustomer, TInternal]):
    """The canonical envelope every domain agent returns.

    The customer half carries both what may be rendered into prose and what may
    be routed onward to another agent — the orchestrator is a courier as well
    as an author, and the distinction is about use, not secrecy. The internal
    half carries cash, stock and the reasoning behind a refusal, and goes to the
    audit trail only.
    """

    agent: AgentName
    step_id: str
    request_id: str
    customer: TCustomer
    internal: TInternal

    def to_view(self) -> AgentView[TCustomer]:
        """Drop the internal half and derive the customer-visible blockers.

        Parametrised on the payload's actual class rather than on `TCustomer`,
        which is still an unbound type variable at runtime: leaving it bound to
        the variable would hand the orchestrator's model an `AgentView` whose
        `customer` has no schema at all.

        Returns:
            The view the delegation seam hands the orchestrator.
        """
        return AgentView[type(self.customer)](
            agent=self.agent,
            step_id=self.step_id,
            request_id=self.request_id,
            customer=self.customer,
            blockers=[signal.to_customer() for signal in self.internal.signals],
        )


class Outcome(StrEnum):
    """The request-level result, derived by the orchestrator alone."""

    FULFILLED = "fulfilled"
    PARTIALLY_FULFILLED = "partially_fulfilled"
    REJECTED = "rejected"
    PENDING_CUSTOMER_REVISION = "pending_customer_revision"


class RevisionQuery(BaseModel):
    """One question the customer must answer before a paused line can proceed."""

    line_id: str
    code: BlockerCode
    question_for_customer: str


class SuspendedFlow(BaseModel):
    """A request paused before money moved, persisted so it could be resumed.

    Designed to resume and never resumed in a harness run: the harness sends one
    request and reads one reply. Persisting it is what keeps the resume token
    from being decorative.
    """

    resume_token: str
    request_id: str
    run_id: str
    suspended_at: datetime
    original_request: CustomerRequest
    completed_step_ids: list[str]
    open_questions: list[RevisionQuery]


class RequestResolution(BaseModel):
    """What the harness prints: one outcome and one message, per request."""

    request_id: str
    outcome: Outcome
    customer_message: str
    resume_token: str | None
