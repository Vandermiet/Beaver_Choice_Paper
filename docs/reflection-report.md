# Reflection report

The Beaver's Choice / Munder Difflin multi-agent system: what it is, what it did
when it was run over the twenty sample requests, and what should be built next.

It is written to be read on its own. The architecture is explained here in
prose; [`workflow-diagram.md`](workflow-diagram.md) draws the same system and is
a companion, not a prerequisite. Every measured figure in the evaluation section
comes from the audit trail of one real run, and the SQL that produced each one
is in the appendix, so no number here has to be taken on trust.

---

## Architecture

### Five agents, and why the lines fall where they do

One orchestrator and four domain agents — inventory, quoting, sales,
replenishment. The constraint was a maximum of five; the interesting question
was never how many, but what each one *decides*.

**The orchestrator** receives one prose enquiry and the date it arrived, and
returns one customer-facing reply. It reads the enquiry into *requested lines*
that are catalogue-blind — the customer's own words, their quantity, their unit
— because inventing an item name is the one thing a language model will happily
do and a validator downstream is the only thing that can prove it did not. It
then plans and drives the delegations, and writes every customer-facing word.
It performs no operation on a system of record. In pydantic-ai that rule is
visible rather than asserted: the orchestrator's tool list contains only
delegation wrappers — no starter helper, no database handle — so "the
orchestrator never acts" is checked by reading one list.

**Inventory** answers one question: does this requested line name something we
carry, and how short of it are we? It owns the five resolution blockers — not
carried, ambiguous, size not carried, quantity missing, unit not understood —
and the four resolution rules that decide them: finish or material selects an
item, colour and quality are dropped as sales adjectives the catalogue does not
model, a named size can veto a resolution but never select one, and no line may
be resolved across a product category. A shortfall it computes is a *fact*, not
a refusal: inventory never declines a line for want of stock.

**Quoting** prices what resolved, blind to stock. The discount ladder is
arithmetic over the units on a line, applied per line and never compounding, so
"why this price?" has an answer that does not begin with "the model decided".
Prices come from the catalogue; the seeded quote history is retrieved *after*
the price is fixed and recorded as a non-binding comparison, because those
historical totals do not reconcile with the catalogue and are evidence of how
the business talks about its prices rather than of what its prices were.

**Sales** decides commitment: can we fill this priced line right now, and what
did it bring in? It re-reads the shelf at the moment of commitment rather than
trusting the earlier survey, decides every line against that one reading, and
only then writes — because a transaction in this database cannot be rolled back
and a half-written order has no repair. It owns `insufficient_stock`
exclusively.

**Replenishment** is the only agent that spends money, and it opens the purse on
exactly one trigger: a line sales has actually refused for want of stock. There
is no periodic sweep, and inventory noticing a shortfall is not a trigger, so
every purchase the business makes is traceable to a customer who asked for
something we did not have. It sizes an order to cover the shortfall *and*
rebuild the reorder floor measured after the sale, judges the supplier's lead
time against the customer's deadline before buying, and applies the cash guard
per item — one unaffordable item never withholds another we could pay for,
while within one item the spend is all-or-nothing, since half of what a line
needs is money out with the line still declined.

The rule that keeps these four from overlapping is that **ownership is a set of
decisions, not a set of tables**. Sales and replenishment both write through
`create_transaction`; both read `get_cash_balance`. That is not an overlap,
because they ask different questions of it — sales asks what this sale brought
in, after the write; replenishment asks what we can afford, before it. Two
agents making the same decision would be the overlap, and none do: each of the
nine blocker codes is owned by exactly one agent, so the business's own count of
refusals cannot be inflated by two agents reporting the same one.

### The envelope and the `AgentView` seam

Every domain agent returns the same response shape:
`AgentResponse[TCustomer, TInternal]` — one canonical envelope, parameterised by
payload type so each agent's payload stays strictly typed. *Canonical* is the
claim (no agent invents its own shape); *generic* is the mechanism that lets the
claim hold without erasing the types.

The envelope has two halves. The customer-facing half carries what the
orchestrator may put into prose or route onward to the next agent. The internal
half carries cash balances, stock counts, margins and raw error text. The
orchestrator is handed an `AgentView` — the customer half and code-only
blockers — and the internal half is extracted at the delegation seam and written
straight to the audit trail without ever entering the orchestrator's context.

This is the design decision the rubric's information-security criterion is met
by, and it is deliberately *structural*. `AgentView` has no `internal` field, so
a leak of a stock count into a customer letter is a type error rather than a
code-review miss. The same reasoning runs through the blocker protocol: a
blocker signal carries a code, the line it applies to, and an internal detail;
the `CustomerBlocker` the orchestrator sees is *derived* from the signal rather
than raised alongside it, so the two cannot disagree and the detail cannot be
forgotten on the way up. The orchestrator cannot disclose what it was never
handed.

### Single-level orchestration and the bounded retry

All delegation is one level deep. A domain agent never orchestrates another;
every sequence is planned and driven by the orchestrator. The sequence is
inventory → quoting → sales, with replenishment called conditionally, and
exactly one second pass at sales after stock has been bought.

The retry is bounded and it is arithmetic, not judgement. Which lines to buy
for, which to offer again, and what the order finally came to are ordinary
functions over the views the delegations returned — no model decides them —
because a model that offered an already-committed line for commitment a second
time would write the same sale twice. Pass 2 carries *only* lines we actually
bought stock for: a line whose restock was refused as too late or unaffordable
never reaches sales again, so it keeps the true reason it was refused instead of
collecting a second stock refusal for stock we deliberately did not buy. Sales
itself is stateless and never learns that a retry happened; it can only sell
what it is offered, and it is never offered a line that has already sold. There
is no third pass, because there is no path back into the commit step.

Two further rules make the reply honest across the retry. **Every blocker is
line-scoped** — a failure confined to one line drops that line and the survivors
continue, and a request ends rejected only when every line has dropped. And
**the last blocker across the retry is the one the customer hears**, so a line
that was short, restocked, then refused on the delivery date is told about the
date: we never tell someone we are out of something now sitting on our shelf.
Every signal raised is still kept in the trail. Precedence governs what is
spoken, not what is recorded.

### The audit trail is an observation, not a system of record

Six tables — `agent_steps`, `blocker_signals`, `transaction_links`,
`suspended_flows`, `quote_registry`, `quote_fulfilments` — plus a JSONL
transcript sidecar carrying one line per delegation. Every delegation and every
domain tool call is written as it returns, each hanging off the delegation that
caused it.

Nothing in the business reads any of it back. That is precisely why the
orchestrator is allowed to own it while owning nothing else: writing to the
trail is not acting on a system of record, because no decision anywhere depends
on what the trail says. The trail exists so that "why was this refused?" and
"why was this priced so?" are queries rather than archaeology over transcripts —
and the evaluation section below is the first consumer of that property. This
report's own numbers are trail queries, and the suite re-runs them
(`project/tests/test_report.py`).

`transaction_links` is the join that makes it work: every runtime row in
`transactions` is tied to the request and the step that wrote it, so money can
be traced in both directions. The quote registry records a priced line whether
or not it becomes a sale, so the gap between `quote_registry` and
`quote_fulfilments` is the business's rejection history rather than a hole in
its records.

### Framework selection: why pydantic-ai

**The criteria came from the problem, not from the frameworks.** The assignment
permits `smolagents`, `pydantic-ai` or `npcsh`. Before comparing them, four
requirements were fixed by what this system has to do:

1. **Domain agents must be able to refuse.** The system is required to leave
   some requests unfulfilled with a stated reason, so the contract *between*
   agents has to carry a structured refusal — a blocker signal — and not merely
   a paragraph of English that the next agent has to interpret.
2. **An orchestrator that delegates but never acts must be demonstrable.** A
   principle that can only be asserted in prose is worth much less than one
   visible in the code.
3. **Every transaction must be auditable step by step**, reconstructable after
   the fact from recorded data.
4. **It must run against the Vocareum OpenAI-compatible proxy**, the only model
   endpoint available for this project.

**npcsh was eliminated on availability.** The `npcsh[lite]` extra referenced in
its own installation instructions no longer exists as of version 2.1.1, and the
documentation is thin. A framework that cannot be installed cleanly from its
documented instructions is not a sound base for a graded deliverable, and that
alone settled it. The architectural fit also looked poor, in a way that confirms
rather than carries the decision: npcsh is a shell rather than a composition
library — agents are declarative `.npc` YAML definitions grouped into teams,
tools are Jinja execution templates, and routing is by name-addressing. Its
documentation does not describe typed, validated returns between agents. That is
a statement about what is documented, not a proof of absence: only its README
was available for review.

**smolagents was a real alternative, and rejecting it cost something.** It
supports hierarchy directly: a manager agent is given `managed_agents=[...]`,
each with a name and description, and invokes them from model-generated Python
executed in an interpreter. That is genuinely *more* expressive than what was
chosen. Multi-step logic — iterate the lines, branch on stock, retry the ones
that failed — can be expressed as a single generated program, where a
tool-calling framework needs a separate round trip per step.

The cost is the shape of the inter-agent contract: a task string in, a
natural-language report out, with no output schema and no validation on the
hand-back. Under that model the blocker protocol becomes a parsing exercise over
prose, `insufficient_stock` and `deadline_unmeetable` are distinguishable only
by reading English, and the audit trail records narrative instead of data.
Expressiveness was traded for contract enforcement, deliberately, because
requirements 1 and 3 are graded and the extra expressiveness is not.

**pydantic-ai was chosen because the typed contract *is* the architecture.** Its
`output_type` accepts a union and registers one output tool per member, so "an
inventory report **or** a stock blocker" is not a convention the agents are
asked to follow — it is the schema the model is constrained to satisfy. Typed
inter-agent IO then guards communication quality across the whole system:

- **Graceful failure becomes a declared variant rather than an exception path.**
  A domain agent refusing to proceed returns a blocker model, which is an
  ordinary, expected, type-checked outcome. Error handling stops being
  defensive parsing.
- **Malformed output is corrected, not crashed on.** A validation failure is fed
  back to the model as a retry prompt rather than raised, which keeps precision
  without brittleness. `CarriedItemName` is the validator that matters most: it
  guards the item name that eventually reaches `create_transaction`, so a name
  the business does not sell cannot reach the books.
- **Every hand-back is structured data.** The trail stores the same objects the
  agents exchanged, so a request can be replayed step by step rather than
  reconstructed from logs — which is what requirement 3 asks for.

The delegation model reinforces requirement 2. pydantic-ai has no handoff
primitive; a parent delegates by awaiting a sub-agent run inside one of its own
tools. The consequence is the one-list check described above. Message history is
serialisable, carrying per-message token usage, model name and run id, which
supplies the raw evidence layer of the trail at no design cost.

**Proxy compatibility was verified rather than assumed.** Tool calling is the
one capability that both function tools and pydantic-ai's default output mode
depend on; had the proxy lacked it, the entire tool-calling family of frameworks
would have been eliminated and this decision would have gone the other way. It
was checked directly — a `POST /v1/chat/completions` carrying a strict `tools`
schema returns a well-formed `tool_calls` response — before the framework was
committed to, rather than discovered during implementation.

---

## Evaluation

The run is `20260916T111405Z`, over all twenty requests of
`quote_requests_sample.csv` from an empty database, and it is the run
`project/test_results/test_results.csv` came out of. Its audit trail is
committed beside it as `project/test_results/audit_20260916T111405Z.sql`, which
is where every figure below is read from.
None of these are the design's predictions; the predictions are compared against
them further down, and where the two disagree the trail is what happened.

### What the twenty requests did

| outcome | requests |
|---|---|
| partially fulfilled | 8 |
| rejected | 6 (+1 — see below) |
| pending customer revision | 3 |
| fulfilled | 2 |

That is nineteen. The twentieth is **request 4**, which has no trail rows at
all: the proxy returned `429 GenAI Gateway Limit (50 calls parallel)` on the
orchestrator's first model call, before any delegation. It went out as the
apology and the harness recorded it as rejected, so rejected is **7 of 20** on
the harness's count and 6 in the trail.

Two figures answer the rubric's "successfully fulfilled" question and they are
not the same number. **Ten requests placed an order and moved money.** **Two**
were fulfilled in the strict sense the design uses — *every* line delivered,
nothing dropped. Both are stated because they answer different questions, and
the larger must not be quoted as though it were the smaller. The rubric's floor
is three requests changing the cash balance and three fulfilled; on the measure
each gate actually asks about, ten and ten clear it, while the strict reading
of "fulfilled" gives two.

The trail saw **58 requested lines**, of which **31** were priced into the quote
registry and **18** became sales.

### Cash

`request_cash` records what the books did across a whole request — before its
first money-moving step against after its last — which is a figure no single
agent could report, since a sale, the purchase it triggered and the second sale
after it each see only their own movement.

| | |
|---|---|
| requests that moved cash | **10** |
| requests that weighed the books and moved nothing | 5 |
| opening cash | $45,059.70 |
| closing cash | $45,762.79 |
| revenue this run | $1,175.35 over 18 sales |
| spend this run | $472.26 over 9 purchases |
| requests ending on a negative balance | **0** |

**No profit or margin figure is claimed anywhere**, and that is a deliberate
limit rather than an omission. Stock acquired before the supplier cost ratio
existed — the seeded shelf — and stock bought under it sit in the same bin at
different costs, so a single margin number would be a guess dressed as an
accounting fact. What the business reports is what cash actually moved.

### Why the refusals happened

`blocker_signals` records every signal raised; the customer hears one per line,
so the distinct-line count is beside it.

| code | signals | distinct lines | owner |
|---|---|---|---|
| `insufficient_stock` | 20 | 20 | sales |
| `item_not_carried` | 18 | 18 | inventory |
| `deadline_unmeetable` | 11 | 11 | replenishment |
| `size_not_carried` | 9 | 8 | inventory |
| `unit_not_understood` | 2 | 1 | inventory |
| `unpriceable` | 0 | 0 | quoting |
| `cash_insufficient` | 0 | 0 | replenishment |

Every refusal in the sample is one of five things: we do not sell it, we do not
sell it in that size, we could not price the unit you counted it in, we did not
hold enough, or we could not get it to you by your date. That is the rubric's
"reasons provided for unfulfilled requests", enumerated rather than narrated.

### Specific strengths, as measured

**The bounded retry is provably bounded.** All 20 `insufficient_stock` signals
were raised on sales' *first* pass and **none on the second**. No line is
refused twice for stock, which is the retry doing exactly what it was designed
to do rather than merely terminating.

**The books reconcile without exception.** Four acceptance queries over the
trail all return zero: no runtime transaction lacks a `transaction_links` row,
no link points at a seeded row, no sale lacks a `quote_fulfilments` row, and no
fulfilment lacks its `quote_registry` row. Every penny that moved in this run is
attributable to a request, a step and a quoted line.

**The cash guard never had to fire, and the design can tell you that honestly.**
`cash_insufficient` is 0 and `unpriceable` is 0 — the two codes the design
predicted would never fire on this data, for stated reasons ($45k of cash
against a $472 purchase run; nothing carried is unpriceable). A prediction of
zero that comes back zero is weak evidence on its own; what makes it worth
stating is that both were reasoned in advance rather than observed after.

**Ownership held under load.** All 11 `deadline_unmeetable` signals were raised
by replenishment and none by sales, which is the §1 non-overlap claim surviving
contact with a real run: the agent that pays the supplier is the agent that
learns the date cannot be met.

**Concurrency did not corrupt the trail.** Request 3 made three concurrent
`consult_inventory` calls and two `consult_quoting` calls in single model turns,
and every one of them was recorded against its own step. 71 delegations, one
error.

### Where the run diverged from what the design expected

The design predicted 9 fulfilled, 11 not fulfilled, 10 moving cash, 8 pending
revision, with `unpriceable` 0, `cash_insufficient` 0, `deadline_unmeetable` 4
and `insufficient_stock` around 14. Six divergences, each with its cause:

1. **Cash-moving: 10 predicted, 10 measured.** Exact — and the only figure that
   was.

2. **Fulfilled: 9 predicted, 2 measured — a definition, not a regression.** The
   prediction counted a request as fulfilled if we sold it anything. The design
   counts `FULFILLED` only when no line dropped. On the design's own definition
   the comparable figure is fulfilled + partially fulfilled = **10**, against
   the predicted 9. The prediction was one out, and only on a word.

3. **Pending revision: 8 predicted, 3 measured.** The prediction assumed any
   revisable blocker suspends the flow. The design settled otherwise during
   implementation: a flow may suspend only when nothing has committed and no
   money has moved, because you cannot un-sell a line in order to go back and
   ask a question about its neighbour. Requests carrying both a sale and an
   ambiguous line therefore end partially fulfilled with the question asked in
   prose. The prediction was written against the older rule.

4. **`deadline_unmeetable`: 4 predicted, 11 measured.** The prediction counted
   *requests* whose deadline the supplier could not meet; the trail counts
   *lines* refused at purchase, and a request short of three items on one
   deadline raises three signals. Per request it is **8**, and those eight are
   the requests that named a deadline and went short. The two numbers are
   compatible; only the unit differs.

5. **`insufficient_stock`: ~14 predicted, 20 measured.** The excess was a real
   defect, since fixed (#36); the figures above are from the run that exposed
   it. Inventory measured each line's shortfall against the full shelf,
   independently, while sales draws the lines in order against one
   reading — so two lines resolving to the same item (the default plain-paper
   item collects "printer paper", "copy paper" and "white paper") can each fit
   the shelf alone while not fitting together. Sales declines the second, and
   inventory raised no restock need for it, so there is nothing for replenishment
   to buy against. The defect predicts *more* stock refusals than a hand-count
   assuming one line per item, which is what we see. It also falsified a claim
   in the retry module's own docstring: the intersection of sales' declines with
   inventory's needs was argued there to be lossless by construction, and that
   argument covers stock *changing* between the two reads, not the two agents
   measuring one reading differently. Inventory now measures per item over the
   lines of the request, the way sales draws the shelf, and emits one
   `RestockNeed` per item carrying every line it covers — so the claim holds in
   fact and not only in the argument.

6. **Signals exceeding lines.** `size_not_carried` 9 over 8 lines and
   `unit_not_understood` 2 over 1: both surplus signals belong to request 3,
   whose lines were resolved twice because the orchestrator split the enquiry
   across three `consult_inventory` calls. The trail records every signal
   raised; precedence governs what is spoken. This is the trail being right, not
   a refusal counted twice — count lines, not signals.

### Two things the run and the design process taught that the rubric did not ask for

**The severity axis collapsed under its own evidence.** The delegation contract
originally gave every blocker a `severity`: a `partial` blocker drops its line
and the request continues, a `fatal` one short-circuits the whole request. Four
design sessions then specified their own agent's blockers independently — and
each of the four argued its way from fatal to partial on the same ground:
declining an entire order because one line of it failed is a worse answer than
serving the rest. By the time the four were reconciled, all nine codes sat at
`line` scope and `partial` severity, the axis had exactly one value, and the
short-circuit branch was unreachable code. It was **removed** rather than kept
at zero, so the business now has no notion of a fatal blocker at all and a
request is rejected only by exhaustion of its lines. An axis that four
independent attempts to use never once used is not a design dimension; it is a
habit. Removing it is why "every blocker is line-scoped" is a fact about the
type system rather than a convention someone might break later.

**The dry run corrected the design twice.** Before anything was built, a
rubric-gate dry run walked all twenty sample requests through the locked design
with the real starter helpers, seeding a throwaway database so stock depleted
and cash moved as it would at run time. The map's predicted distribution was
11 / 11 / 9 and the walk produced 9 / 7 / 13 — a gap of four requests. The
methodological cause was that every earlier prediction had been made per
request, against the **seeded** `inventory` table, whereas live stock is a
cumulative sum over `transactions`: the sixth request does not see the numbers
the first one saw. Two genuine design defects fell out of that gap, and neither
would have been found by reading the design again:

- The delivery promise was computed from the *line quantity* via
  `get_supplier_delivery_date`, so a 500-sheet line was promised four days out
  whether or not the goods were on the shelf. It rejected a request for 500
  sheets while 5,135 sat in stock. The promise became stock-aware — from stock,
  the request date; restocked, the date the supplier gave us — which cost
  nothing to build, because the field carrying it already existed with no
  consumer.
- Replenishment was triggered by inventory's *survey* rather than by sales'
  refusal at commitment, so on any request where a line was both short and late
  we bought the stock and then declined the sale. Two of the five rejected
  requests in that walk had a negative cash delta: we were worse off for having
  been asked. The trigger moved to the refusal, which is why the system now
  holds that every purchase is traceable to a customer who was told no.

A dry run that had only confirmed the design would have been worth little. This
one changed it twice before a line of it was written, which is the argument for
walking a design over real data before building it.

---

## Improvements

**1. Make inventory measure the shelf the way sales draws it.** *(Done since
this run, on #36.)* This is divergence 5 above and the one defect in the system
that cost a customer an order they could have had. Inventory now computes
shortfalls per *item* over the lines of a request — lines naming one item
considered together against one reading, in the order they arrived — and emits
one restock need per item carrying every line it covers, sized
`sum(quantity) − stock_on_hand`. Before that, the sum of independent shortfalls
understated the true requirement by `(n−1) × stock`, and a line that shorted
only because a sibling took the stock first got no need at all, which is
exactly the line a restock rescues. The fix belonged to inventory: the
shortfall is its decision to own, and replenishment could not recover the
arithmetic because neither the stock reading nor the requested quantity crosses
the seam — deliberately so.

**2. Give an empty delegation an answer instead of a model.** One of 71
delegations in this run errored, and it took a request down with it: the
orchestrator called `consult_quoting` a second time with no lines, the model
invented line ids to fill the gap, `CarriedItemName` correctly refused each one,
and three refusals exhausted the retry budget —
`UnexpectedModelBehavior: Tool 'catalogue_price' exceeded max retries count of
3`. The request went out as the apology and was counted rejected. The
orchestrator's instructions already say to call quoting once; an instruction is
one model turn from being ignored, which is precisely the argument the commit
guard is built on. A delegation called with an empty line list has a correct
answer that needs no model — the canonical envelope, empty payload, no signals
— and returning it at the seam would cost nothing, could not loop, and would
turn a lost request into a non-event. The same hole exists on `consult_inventory`
and `place_order`; they were simply never called empty here.

**3. Survive the proxy.** Request 4 was lost to
`429 GenAI Gateway Limit (50 calls parallel)` on its very first model call —
not a defect in this system, but a 5% loss of the run to an entirely
foreseeable condition. The harness processes requests without concurrency
control and retries nothing. A bounded exponential backoff on 429 at the model
client, plus a semaphore capping concurrent calls, would have cost one delay
and saved the request. Worth noting what the design already got right here:
`handle_request` caught the failure and the customer got an apology rather than
a stack trace, and the request was recorded as rejected rather than silently
skipped.

**4. Track a cost basis, so the business can eventually say something about
margin.** The report claims no profit figure because it cannot honestly do so:
seeded stock and stock bought under the supplier cost ratio sit in the same bin
at different costs. That is a data problem with a known shape. Recording a
weighted-average cost per item as restocks land — seeded stock entering at an
explicitly stated assumed cost, flagged as such — would let the business report
gross margin per sale with its assumption visible in the figure's provenance
rather than buried. The rule to keep is the current one: never report a margin
the cost data cannot support. The improvement is to make the cost data support
one.

**5. A business advisor agent, if a sixth agent were permitted.** The audit
trail already holds everything such an agent would need — every refusal with its
code, every quote that never became a sale, every purchase tied to the customer
who triggered it. A standing query over it would surface, without anyone asking,
that `item_not_carried` fired on 18 lines in twenty requests (demand for
products we do not stock) and that `deadline_unmeetable` cost us eight requests'
lines (a lead-time problem, not a stock problem). This is the one proposal that
adds a capability rather than repairing one, and it is deliberately last: the
four above are defects and gaps, and defects come first.

---

## Appendix: every measured figure and its query

Each figure in the evaluation section is named below with the SQL that produced
it. `init_database` rewrites `munder_difflin.db` on every run, so the repo does
not carry it; the run's audit trail travels instead as
`project/test_results/audit_20260916T111405Z.sql`, a dump of the seven audit
tables and the transactions they link to. `project/tests/test_report.py`
rebuilds that dump in memory, executes every query below against it, and asserts
each one answers the number stated — including the figures restated in the prose
tables above. The prose cannot drift away from the trail.

| figure | value | what it counts |
|---|---|---|
| `requests.in_trail` | 19 | requests with any step recorded (request 4 has none) |
| `outcome.fulfilled` | 2 | committed at least one line, dropped none |
| `outcome.partially_fulfilled` | 8 | committed at least one line and dropped at least one |
| `outcome.rejected` | 6 | committed nothing and was not suspended |
| `outcome.pending_revision` | 3 | a suspended flow was persisted |
| `requests.placing_an_order` | 10 | committed at least one line |
| `lines.seen` | 58 | distinct requested lines that reached a domain agent |
| `quote.lines` | 31 | rows in the quote registry |
| `quote.fulfilments` | 18 | quoted lines that became a sale |
| `cash.requests_moving` | 10 | requests with a non-zero cash delta |
| `cash.requests_weighing_only` | 5 | requests that read the books and moved nothing |
| `cash.opening` | 45059.70 | cash before the first request |
| `cash.closing` | 45762.79 | cash after the last request |
| `cash.revenue` | 1175.35 | total of this run's sales |
| `cash.sales_rows` | 18 | this run's sale transactions |
| `cash.spend` | 472.26 | total of this run's purchases |
| `cash.purchase_rows` | 9 | this run's stock-order transactions |
| `cash.requests_ending_negative` | 0 | requests ending on a negative balance |
| `blocker.insufficient_stock` | 20 | signals raised |
| `blocker.insufficient_stock_lines` | 20 | distinct lines they cover |
| `blocker.insufficient_stock_pass_2` | 0 | raised on sales' second pass |
| `blocker.item_not_carried` | 18 | signals raised |
| `blocker.deadline_unmeetable` | 11 | signals raised |
| `blocker.deadline_unmeetable_requests` | 8 | requests they touch |
| `blocker.deadline_unmeetable_by_sales` | 0 | raised by any agent but replenishment |
| `blocker.size_not_carried` | 9 | signals raised |
| `blocker.size_not_carried_lines` | 8 | distinct lines they cover |
| `blocker.unit_not_understood` | 2 | signals raised |
| `blocker.unit_not_understood_lines` | 1 | distinct lines they cover |
| `blocker.unpriceable` | 0 | signals raised |
| `blocker.cash_insufficient` | 0 | signals raised |
| `trail.delegations` | 71 | delegation steps recorded |
| `trail.request_3_inventory_delegations` | 3 | request 3's concurrent inventory calls |
| `trail.request_3_quoting_delegations` | 2 | request 3's concurrent quoting calls |
| `trail.errored_delegations` | 1 | steps carrying an error |
| `trail.unlinked_runtime_transactions` | 0 | runtime transactions with no link row |
| `trail.links_to_seeded_rows` | 0 | links pointing at a seeded transaction |
| `trail.sales_without_fulfilment` | 0 | linked sales with no fulfilment row |
| `trail.fulfilments_without_quote` | 0 | fulfilments with no quote-registry row |

```sql
-- figure: requests.in_trail
select count(distinct request_id) from agent_steps;

-- figure: outcome.fulfilled
with committed as (select q.request_id, q.line_id from quote_registry q
                   join quote_fulfilments f using (quote_line_id)),
     dropped as (select s.request_id, b.line_id from blocker_signals b
                 join agent_steps s using (step_id)
                 except select * from committed)
select count(*) from (select distinct request_id from agent_steps)
 where request_id in (select request_id from committed)
   and request_id not in (select request_id from dropped);

-- figure: outcome.partially_fulfilled
with committed as (select q.request_id, q.line_id from quote_registry q
                   join quote_fulfilments f using (quote_line_id)),
     dropped as (select s.request_id, b.line_id from blocker_signals b
                 join agent_steps s using (step_id)
                 except select * from committed)
select count(*) from (select distinct request_id from agent_steps)
 where request_id in (select request_id from committed)
   and request_id in (select request_id from dropped);

-- figure: outcome.rejected
with committed as (select q.request_id from quote_registry q
                   join quote_fulfilments f using (quote_line_id))
select count(*) from (select distinct request_id from agent_steps)
 where request_id not in (select request_id from committed)
   and request_id not in (select request_id from suspended_flows);

-- figure: outcome.pending_revision
select count(*) from suspended_flows;

-- figure: requests.placing_an_order
select count(distinct q.request_id) from quote_registry q
 join quote_fulfilments f using (quote_line_id);

-- figure: lines.seen
select count(*) from (
  select request_id, line_id from quote_registry
  union
  select s.request_id, b.line_id from blocker_signals b join agent_steps s using (step_id));

-- figure: quote.lines
select count(*) from quote_registry;

-- figure: quote.fulfilments
select count(*) from quote_fulfilments;

-- figure: cash.requests_moving
select count(*) from request_cash where cash_delta != 0;

-- figure: cash.requests_weighing_only
select count(*) from request_cash where cash_delta = 0;

-- figure: cash.opening
select cash_before from request_cash order by cast(request_id as integer) limit 1;

-- figure: cash.closing
select cash_after from request_cash order by cast(request_id as integer) desc limit 1;

-- figure: cash.revenue
select round(sum(price), 2) from transactions
 where transaction_type = 'sales' and transaction_date not like '2025-01-01%';

-- figure: cash.sales_rows
select count(*) from transactions
 where transaction_type = 'sales' and transaction_date not like '2025-01-01%';

-- figure: cash.spend
select round(sum(price), 2) from transactions
 where transaction_type = 'stock_orders' and transaction_date not like '2025-01-01%';

-- figure: cash.purchase_rows
select count(*) from transactions
 where transaction_type = 'stock_orders' and transaction_date not like '2025-01-01%';

-- figure: cash.requests_ending_negative
select count(*) from request_cash where cash_after < 0;

-- figure: blocker.insufficient_stock
select count(*) from blocker_signals where code = 'insufficient_stock';

-- figure: blocker.insufficient_stock_lines
select count(*) from (select distinct s.request_id, b.line_id
  from blocker_signals b join agent_steps s using (step_id)
  where b.code = 'insufficient_stock');

-- figure: blocker.insufficient_stock_pass_2
select count(*) from blocker_signals b join agent_steps s using (step_id)
 where b.code = 'insufficient_stock'
   and s.step_id not in (
     select step_id from (
       select step_id, row_number() over (partition by request_id order by seq) as pass
       from agent_steps where agent = 'sales' and kind = 'delegation')
     where pass = 1);

-- figure: blocker.item_not_carried
select count(*) from blocker_signals where code = 'item_not_carried';

-- figure: blocker.deadline_unmeetable
select count(*) from blocker_signals where code = 'deadline_unmeetable';

-- figure: blocker.deadline_unmeetable_requests
select count(distinct s.request_id) from blocker_signals b
 join agent_steps s using (step_id) where b.code = 'deadline_unmeetable';

-- figure: blocker.deadline_unmeetable_by_sales
select count(*) from blocker_signals b join agent_steps s using (step_id)
 where b.code = 'deadline_unmeetable' and s.agent != 'replenishment';

-- figure: blocker.size_not_carried
select count(*) from blocker_signals where code = 'size_not_carried';

-- figure: blocker.size_not_carried_lines
select count(*) from (select distinct s.request_id, b.line_id
  from blocker_signals b join agent_steps s using (step_id)
  where b.code = 'size_not_carried');

-- figure: blocker.unit_not_understood
select count(*) from blocker_signals where code = 'unit_not_understood';

-- figure: blocker.unit_not_understood_lines
select count(*) from (select distinct s.request_id, b.line_id
  from blocker_signals b join agent_steps s using (step_id)
  where b.code = 'unit_not_understood');

-- figure: blocker.unpriceable
select count(*) from blocker_signals where code = 'unpriceable';

-- figure: blocker.cash_insufficient
select count(*) from blocker_signals where code = 'cash_insufficient';

-- figure: trail.delegations
select count(*) from agent_steps where kind = 'delegation';

-- figure: trail.request_3_inventory_delegations
select count(*) from agent_steps
 where request_id = '3' and agent = 'inventory' and kind = 'delegation';

-- figure: trail.request_3_quoting_delegations
select count(*) from agent_steps
 where request_id = '3' and agent = 'quoting' and kind = 'delegation';

-- figure: trail.errored_delegations
select count(*) from agent_steps where error is not null;

-- figure: trail.unlinked_runtime_transactions
select count(*) from transactions t
 where t.transaction_date not like '2025-01-01%'
   and not exists (select 1 from transaction_links l where l.transaction_rowid = t.rowid);

-- figure: trail.links_to_seeded_rows
select count(*) from transaction_links l
 join transactions t on t.rowid = l.transaction_rowid
 where t.transaction_date like '2025-01-01%';

-- figure: trail.sales_without_fulfilment
select count(*) from transactions t join transaction_links l on l.transaction_rowid = t.rowid
 where t.transaction_type = 'sales'
   and not exists (select 1 from quote_fulfilments f where f.transaction_rowid = l.transaction_rowid);

-- figure: trail.fulfilments_without_quote
select count(*) from quote_fulfilments f
 where not exists (select 1 from quote_registry q where q.quote_line_id = f.quote_line_id);
```
