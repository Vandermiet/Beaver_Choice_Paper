"""How a request ends: the journal, the precedence order, and the suspension.

Filled by ticket 106. Everything here is the orchestrator's own reasoning about
a request *as a whole*, and it lives outside `orchestrator.py` for one reason:
none of it needs a model. An outcome is arithmetic over the verdicts the
delegations handed up, so it is ordinary function code that a test can drive
without spending a model call.

Three rules this module exists to make structural rather than hoped for:

- **The outcome is derived, never reported.** No agent states one and none of
  them could: an agent sees only its own lines, and an outcome is a fact about
  the request. `REJECTED` in particular is reachable only when every one of the
  request's lines has dropped — a line nobody decided raises rather than
  quietly counting as a refusal, because a crash must not be able to
  masquerade as a business decision in the rubric's own count.
- **One blocker per line, resolution judged before units.** A line raises at
  most one blocker *to the customer*; every blocker raised is still written to
  the trail. Precedence governs what is spoken, not what is recorded.
- **Suspension is an outcome, not a state of waiting.** It is reachable only
  from the four pausing codes, and only before any money has moved. A revisable
  blocker raised after commitment is spoken as prose instead — you cannot
  un-sell a line to go back and ask a question about its neighbour.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from beaver.contract import (
    PAUSING,
    AgentView,
    BlockerCode,
    CustomerBlocker,
    CustomerRequest,
    Outcome,
    RequestedLine,
    RevisionQuery,
    SuspendedFlow,
)
from beaver.sales.models import SalesCustomerPayload

#: Where each code sits in the order the orchestrator speaks them, lowest
#: first. The bands are the stages of the request, so a line that failed early
#: is told why it failed early: a line we do not sell is never also told we are
#: short of it.
_PRECEDENCE: dict[BlockerCode, int] = {
    # Resolution: is this a thing we sell at all?
    BlockerCode.ITEM_NOT_CARRIED: 0,
    BlockerCode.ITEM_AMBIGUOUS: 0,
    BlockerCode.SIZE_NOT_CARRIED: 0,
    # The count and the unit, judged only on a line whose name resolved.
    BlockerCode.QUANTITY_MISSING: 1,
    BlockerCode.UNIT_NOT_UNDERSTOOD: 1,
    # Pricing.
    BlockerCode.UNPRICEABLE: 2,
    # Supply: we sell it and we priced it, but we cannot get it to them.
    BlockerCode.INSUFFICIENT_STOCK: 3,
    BlockerCode.DEADLINE_UNMEETABLE: 3,
    BlockerCode.CASH_INSUFFICIENT: 3,
}

#: The band in which a later blocker supersedes an earlier one on the same
#: line. It is the supply band and only the supply band, because it is the only
#: one a line can enter twice: a line short of stock, restocked, then refused on
#: the delivery promise is told about the promise, because by then we are no
#: longer out of it. In the earlier bands a line is judged once, so "last wins"
#: would only make the answer depend on the order two agents happened to reply.
_SUPERSEDING_BAND = 3

#: The question each pausing code puts to the customer. Deterministic rather
#: than written by the model, because it is persisted on the `SuspendedFlow`:
#: a query nobody can read back is a resume token's worth of decoration.
_QUESTION: dict[BlockerCode, str] = {
    BlockerCode.ITEM_AMBIGUOUS: (
        "More than one of the products we sell matches “{item}” — which of them "
        "did you have in mind?"
    ),
    BlockerCode.SIZE_NOT_CARRIED: (
        "We do not sell “{item}” in the size you named. Would one of the sizes "
        "we do sell suit you?"
    ),
    BlockerCode.QUANTITY_MISSING: "How many of “{item}” would you like?",
    BlockerCode.UNIT_NOT_UNDERSTOOD: (
        "We are not able to price “{item}” by the {unit} — how many individual "
        "sheets or units does that come to?"
    ),
}

# Checked here rather than discovered on a customer: a code that can pause a
# flow and has no question to ask would suspend a request and then say nothing.
assert _QUESTION.keys() == PAUSING, "every pausing code needs a question"


@dataclass
class RequestJournal:
    """What the orchestrator learned about one request's lines, as it learned it.

    Filled by the delegation seam rather than by hand, for the same reason the
    audit trail is: one a call site can forget to write to is one that decides
    an outcome from half a request. It holds only what the
    orchestrator was *handed* — customer payloads and code-only blockers — so
    nothing derived from it can disclose what the seam withheld.

    It holds the facts and takes no view on them. The rules below — which
    blocker a line speaks, how the request ended, what we still need to ask —
    are policy over those facts and stay outside the class, so that changing
    what we say about a request never means changing what we know about it.
    """

    #: The lines the orchestrator extracted from the customer's prose, in the
    #: customer's own words. The denominator of every rule here: "every line
    #: has dropped" is a claim about this list.
    requested_lines: list[RequestedLine] = field(default_factory=list)
    #: Every view the seam handed up, in the order the delegations returned.
    views: list[AgentView] = field(default_factory=list)

    def record(self, view: AgentView) -> AgentView:
        """Take one delegation's view into the journal, and narrow what it says.

        The two are one act because they must not be able to disagree: what
        goes into the journal is every blocker the agent raised, and what comes
        back out is only the one now holding each line. Precedence therefore
        governs the prose *structurally* — the orchestrator cannot speak a
        blocker it was never handed — in exactly the way the internal payload
        is withheld, and for the same reason. A blocker dropped here is still
        on its way to `blocker_signals`, which the seam has already written.

        Args:
            view: What the delegation returned, blockers and all.

        Returns:
            The same view, carrying only this line's standing blocker.
        """
        self.views.append(view)
        spoken = spoken_blockers(self)
        kept: list[CustomerBlocker] = []
        for blocker in view.blockers:
            if spoken.get(blocker.line_id) is not blocker.code:
                continue
            if any(held.line_id == blocker.line_id for held in kept):
                # One agent reporting a line twice is one failure, not two.
                continue
            kept.append(blocker)
        return view.model_copy(update={"blockers": kept})

    @property
    def line_ids(self) -> list[str]:
        """Every line of the request, in the order the customer stated them."""
        return [line.line_id for line in self.requested_lines]

    @property
    def blockers(self) -> list[CustomerBlocker]:
        """Every blocker the orchestrator was handed, oldest first."""
        return [blocker for view in self.views for blocker in view.blockers]

    @property
    def committed_line_ids(self) -> set[str]:
        """The lines that were sold, across however many passes there were."""
        return {
            line.line_id
            for view in self.views
            if isinstance(view.customer, SalesCustomerPayload)
            for line in view.customer.committed
        }

    @property
    def money_moved(self) -> bool:
        """Whether anything irreversible has happened to the books yet.

        A commitment today; replenishment's spend joins it when the purse opens
        on sales' declines in ticket 108. It is a separate question from
        "did anything commit?" precisely because a restock can leave a request
        with no committed line and money already out of the door.
        """
        return bool(self.committed_line_ids)

    @property
    def completed_step_ids(self) -> list[str]:
        """The delegations that finished, in causal order."""
        return [view.step_id for view in self.views]


def spoken_blockers(journal: RequestJournal) -> dict[str, BlockerCode]:
    """The one blocker each surviving failure is told about, by line.

    A line that was committed speaks none: it was short on the first pass and
    sold on the second, and there is nothing left to apologise for. A line
    whose only blocker is internal-only speaks none either — it is still
    dropped, and its real reason is still in the trail, but no customer is ever
    told what we can afford.

    Args:
        journal: This request's journal.

    Returns:
        The code to speak, by line id, for every line that has one.
    """
    committed = journal.committed_line_ids
    spoken: dict[str, BlockerCode] = {}
    for blocker in journal.blockers:
        if blocker.line_id in committed or blocker.code.internal_only:
            continue
        standing = spoken.get(blocker.line_id)
        if standing is None or _supersedes(blocker.code, standing):
            spoken[blocker.line_id] = blocker.code
    return spoken


def _supersedes(code: BlockerCode, standing: BlockerCode) -> bool:
    """Whether a newly raised code takes the line's voice from the one holding it.

    Args:
        code: The code just raised.
        standing: The code currently holding the line.

    Returns:
        Whether the new code is the one the customer should hear.
    """
    band, held = _PRECEDENCE[code], _PRECEDENCE[standing]
    if band != held:
        return band < held
    return band == _SUPERSEDING_BAND


def dropped_line_ids(journal: RequestJournal) -> set[str]:
    """Every line that failed, whether or not the customer is told why.

    Args:
        journal: This request's journal.

    Returns:
        The line ids that raised a blocker and were not committed.
    """
    committed = journal.committed_line_ids
    return {
        blocker.line_id for blocker in journal.blockers if blocker.line_id not in committed
    }


def derive_outcome(journal: RequestJournal) -> Outcome:
    """How this request ended, from the per-line verdicts and nothing else.

    Args:
        journal: This request's journal.

    Returns:
        The request-level outcome.

    Raises:
        ValueError: If a line was neither committed nor dropped and nothing
            sold, which means the sequence did not finish. A line nobody
            decided is a bug, and `REJECTED` is a business decision.
    """
    committed = journal.committed_line_ids
    dropped = dropped_line_ids(journal)
    if committed:
        return Outcome.PARTIALLY_FULFILLED if dropped else Outcome.FULFILLED

    undecided = [
        line_id for line_id in journal.line_ids if line_id not in dropped
    ]
    if undecided:
        raise ValueError(
            f"request ended with no verdict for {', '.join(undecided)}: "
            "a line nobody decided is an unfinished sequence, not a rejection"
        )

    if any(code.pausing for code in spoken_blockers(journal).values()):
        # Not "a revisable blocker", and not "before commitment" as a separate
        # test: `money_moved` is false here by construction, because nothing
        # committed. The guard stays explicit so that ticket 108's restock —
        # money out with no line sold — closes this door rather than finding it
        # already shut for the wrong reason.
        if not journal.money_moved:
            return Outcome.PENDING_CUSTOMER_REVISION
    return Outcome.REJECTED


def revision_queries(journal: RequestJournal) -> list[RevisionQuery]:
    """Every question standing between this request and an order, asked once.

    In the order the customer wrote their lines, so the reply reads down their
    own enquiry rather than down our processing order.

    Args:
        journal: This request's journal.

    Returns:
        One query per open pausing line.
    """
    spoken = spoken_blockers(journal)
    queries = []
    for line in journal.requested_lines:
        code = spoken.get(line.line_id)
        if code is None or not code.pausing:
            continue
        queries.append(
            RevisionQuery(
                line_id=line.line_id,
                code=code,
                question_for_customer=_QUESTION[code].format(
                    item=line.item_as_stated, unit=line.unit_as_stated or "unit named"
                ),
            )
        )
    return queries


def resume_token(run_id: str, request_id: str) -> str:
    """The key a suspended flow is filed under.

    `run_id` is a UTC timestamp, so the token dates itself and names the run it
    belongs to: it can be read as well as looked up.

    Args:
        run_id: The run the request was handled in.
        request_id: The request.

    Returns:
        The token, `{run_id}:{request_id}`.
    """
    return f"{run_id}:{request_id}"


def suspend(journal: RequestJournal, request: CustomerRequest) -> SuspendedFlow:
    """Build the flow a paused request leaves behind.

    Everything a resumption would need and nothing it would not: the request as
    it arrived, the steps already done, and the questions still open. It is
    designed to resume and nothing in a harness run resumes it — the customer
    has no channel to answer — and persisting it anyway is what keeps the
    resume token from being decorative.

    Args:
        journal: This request's journal.
        request: The request as it arrived.

    Returns:
        The flow, ready to be written to `suspended_flows`.
    """
    return SuspendedFlow(
        resume_token=resume_token(request.run_id, request.request_id),
        request_id=request.request_id,
        run_id=request.run_id,
        suspended_at=datetime.now(UTC),
        original_request=request,
        completed_step_ids=journal.completed_step_ids,
        open_questions=revision_queries(journal),
    )
