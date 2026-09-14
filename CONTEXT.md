# Context

The shared language of the Beaver's Choice / Munder Difflin multi-agent system. Glossary only — no implementation detail, no decisions. Decisions live on the [wayfinder map](https://github.com/Vandermiet/Beaver_Choice_Paper/issues/1) and its tickets.

## Agents

**Orchestrator** — The single agent that receives a customer request and composes the reply. It never acts on a system of record. It plans and drives the sequence of delegations, reads the signals that come back, and is the only component that writes customer-facing prose. It also owns the audit trail, which is not a system of record.

**System of record** — A store that holds the authoritative state of a core company operation: the `transactions` table (money and stock movements) and the `inventory` table. Reading or writing one is domain work and belongs to a domain agent. The audit trail is deliberately *not* a system of record — it is an observation of what the agents did, and nothing in the business reads it back.

**The orchestrator never acts** — The rule that the orchestrator performs no operation on a system of record. It constrains what the orchestrator may touch; the audit trail is outside that boundary, so the orchestrator owns it. Distinct from *single-level orchestration*, which constrains the domain agents' delegation.

**Domain agent** — One of the four agents that owns a single domain: inventory, quoting, sales, replenishment. A domain agent answers about its own domain and reports blockers in its own domain. It never delegates to another agent.

**Ownership** — What a domain agent owns is a set of *decisions*, not a set of tables. Two agents may read the same system of record while asking different questions of it, and that is not an overlap; two agents making the same decision is. Non-overlap is judged on the question answered, never on the data touched.

**Single-level orchestration** — The rule that all delegation is one level deep. Every sequence of steps is planned, executed and orchestrated by the orchestrator; a domain agent never orchestrates another agent. Distinct from *the orchestrator never acts*, which constrains the orchestrator's tool use; this constrains the domain agents' delegation.

## The request

**Customer request** — One prose string from the harness, plus the date it arrived. The unit of work: one request in, one resolution out.

**Requested line** — A single thing the customer asked for, in the customer's own words, with the quantity and unit as they stated them. Catalogue-blind: a requested line may name something that does not exist, or a unit the business does not sell in.

**Resolved item** — A requested line matched to an exact name in the carried catalogue. Resolution is inventory's work; a requested line that cannot be resolved carries no resolved item and raises a blocker instead.

## The catalogue

**Product universe** — Every item the business could conceivably sell, with its category and unit price. Reference data, not a statement of what is for sale.

**Carried catalogue** — The items the business actually sells: the subset of the product universe the company stocks, each with a unit price and a reorder threshold. This is what a requested line resolves against, and the authority on whether something is on offer at all. An item can be in the product universe and not in the carried catalogue, and that is indistinguishable, to a customer, from not existing.

**Stocked** — Carried *and* holding positive stock as of a date. A carried item that is stocked out is still on offer; an item that is not carried never was. The two are different answers and never collapse into one.

## Resolution

**Modifier** — A word the customer wraps around the thing they want. Four kinds, treated differently: *colour* and *quality* are sales adjectives the catalogue does not model and are dropped; *finish or material* is what the catalogue actually names and is what selects an item; *size* neither selects nor is ignored — see *size veto*.

**Size veto** — A size the customer names can disqualify a resolution but never select one. The catalogue has no size axis: size appears only incidentally, inside a handful of item names. A named size absent from the entire product universe refuses the line; a size the universe does name constrains nothing, and the line resolves on its finish or material instead. So "A4 glossy paper" is glossy paper, and "A3 glossy paper" is nothing we sell.

**Category guard** — The refusal to substitute across product categories. A requested line whose only carried candidate sits in a different category from the line's best match in the wider product universe is not carried, however closely the two names read. What arrives on a pallet as large-format is not what was asked for as paper.

**Default plain-paper item** — The single carried item that a generic request for ordinary paper resolves to. "Printer paper", "printing paper", "copy paper", "white paper" name no product in particular; they name the everyday sheet a paper company is expected to sell. Generic is not the same as unresolvable, and a paper business that cannot sell printer paper has failed at the only thing it does.

**Ambiguity** — Two or more carried candidates surviving every resolution rule. The business never guesses which one the customer meant; it asks. Ambiguity is a property of the candidate set, not of how far apart the candidates are priced.

**Resolution trace** — The structured record of how one requested line became, or failed to become, a resolved item: the candidates considered, the best match in the wider universe, and the rule that decided it. Internal only. It exists so that "why did it pick this?" is answerable by query rather than by reading a transcript.

## Contracts

**Canonical envelope** — The single response shape every domain agent returns. *Canonical* is a claim about authority: no agent invents its own response shape.

**Generic envelope** — The same envelope parameterised by payload type, so each agent's payload stays strictly typed. *Generic* is the mechanism; *canonical* is the claim. An envelope can be canonical without being generic (one shared shape with an untyped payload), so the two terms are not interchangeable.

**Customer-facing payload** — The half of a domain agent's response that the orchestrator is given. Contains nothing the customer may not see.

**Internal payload** — The half the orchestrator is never given: cash balances, margins, stock counts, raw errors. Withheld structurally, not by instruction — the orchestrator cannot disclose what never enters its context.

## Blockers

**Blocker signal** — A structured report that something in a domain agent's own domain prevents part or all of a request from proceeding. Carries an enumerated code, the scope it applies to, a severity, and an internal detail. The code makes a rejection mechanically testable; the detail is internal-only.

**Scope** — What a blocker applies to: the whole request, or one named requested line.

**Severity** — *Fatal* (nothing in the request can be served) or *partial* (one line drops, the rest continue). A property of the code, not a free choice of the agent raising it.

**Commitment** — The moment a line stops being an offer and becomes money moving. Everything a domain agent verifies on a line is verified *before* any line is committed, because nothing here can be undone.

**Revisable blocker** — A blocker the customer could plausibly clear by amending their order — an ambiguous item, an unrecognised unit, an unmeetable deadline. Contrasted with a flat refusal, which no amendment fixes (we do not carry it) or which the customer is never told about (insufficient cash). Revisability is a property of the code. It does **not** imply suspension: a revisable blocker raised after commitment is spoken as prose — the reply names the alternative we could have met — because a flow can only suspend before money moves.

## Registries

**Registry** — A store of a business event as it happened: quotes issued, transactions written. A registry is a *record*, never a channel — it is written by exactly one agent through exactly one tool, and nothing reads it back to learn something another agent already said in the envelope.

**Quote registry** — The record of every priced line the business offered, written at quote time, before anyone rules on whether it can be delivered. Separate from the transaction registry even though the two hold near-identical facts: a quote is an offer, a transaction is money moving, and the gap between them is the business's rejection history.

**Transaction registry** — The record of money and stock actually moving: the `transactions` table, the authority that cash and inventory are read from.

**Quote-as-order** — The deliberate collapse of quoting and ordering into one act, because the simulation has no channel through which a customer could accept. A quote request is treated as a firm order: it is priced, committed and written in one turn.

## Replenishment

**Reorder threshold** — The per-item stock floor recorded in the carried catalogue. It is a *tripwire for sizing*, not a trigger: nothing watches it, and falling below it starts nothing on its own.

**Reorder trigger** — The only thing that sets replenishment in motion: a customer order that cannot be served from stock on hand. There is no periodic sweep. Every purchase this business makes is traceable to a customer who asked for something we did not have.

**Shortfall** — The units by which a requested line exceeds the stock held for that item. What the restock must at minimum cover.

**Target stock level** — What replenishment restocks *to*, as distinct from what the order needs: the shortfall covered, and stock left back at the reorder threshold once the order has shipped. A restock serves the customer in front of us and rebuilds the floor in the same purchase. Measured *after* the sale — measured before, the order would immediately eat the floor we just bought.

**Supplier cost ratio** — The fraction of an item's catalogue price the business pays its supplier for it. Varies by product rather than by purchase: the supplier holds a standing rate per item, so the same item always costs the same to buy, and different items differ. The provided data contains no supplier price at all, so this ratio is the business's own invention, introduced to make the simulation's cash behave like a trading business rather than one that buys at its own shelf price.

**No margin is claimed** — The business reports what cash actually moved and never asserts a profit figure. Stock acquired before the supplier cost ratio existed and stock bought under it sit in the same bin at different costs, so any single margin number would be a guess dressed as an accounting fact.

**Restock** — A purchase of stock from the supplier: money out, stock in. Booked on the day it is paid for, so the stock is on our books immediately; the supplier's lead time is carried forward into the delivery date we promise the customer, never into when the stock appears.

**Cash guard** — Replenishment's refusal to spend more than the cash on hand. It applies **per item**: one item being unaffordable never withholds another we could pay for. Within a single item it is all-or-nothing — a shortfall is never part-filled, because half of what a line needs is money out with the line still declined. Revenue from the order being served does not count as cash on hand; the business cannot spend what it has not been paid.

## Outcomes

**Outcome** — How a request ended: fulfilled, partially fulfilled, rejected, or pending customer revision.

**Pending customer revision** — An outcome, not a state of waiting. The request cannot proceed until the customer amends it, so the flow is suspended and the reply asks the revision questions. Reachable only before any line is committed — once money has moved, the request ends in an outcome rather than a question. Nothing in a harness run resumes it; the customer has no channel to answer.

**Revision query** — One question put to the customer, arising from one revisable blocker.

**Suspended flow** — The persisted record of a request that ended pending customer revision: the original request, the steps already completed, and the open revision queries. Enough to resume the request without re-running the work already done.

**Resume token** — The identifier of a suspended flow. Carries its own provenance — which run and which request it resumes — so a token can be read rather than merely looked up.

## Audit

**Step** — One domain agent's turn within one request: what it was asked, what it answered, what it signalled. The unit of the audit trail.

**Run** — One pass of the harness over the sample requests. Identifies steps and suspended flows, and distinguishes a request from the same request in a later run.
