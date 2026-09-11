# Context

The shared language of the Beaver's Choice / Munder Difflin multi-agent system. Glossary only — no implementation detail, no decisions. Decisions live on the [wayfinder map](https://github.com/Vandermiet/Beaver_Choice_Paper/issues/1) and its tickets.

## Agents

**Orchestrator** — The single agent that receives a customer request and composes the reply. It never acts on a system of record. It plans and drives the sequence of delegations, reads the signals that come back, and is the only component that writes customer-facing prose. It also owns the audit trail, which is not a system of record.

**System of record** — A store that holds the authoritative state of a core company operation: the `transactions` table (money and stock movements) and the `inventory` table. Reading or writing one is domain work and belongs to a domain agent. The audit trail is deliberately *not* a system of record — it is an observation of what the agents did, and nothing in the business reads it back.

**The orchestrator never acts** — The rule that the orchestrator performs no operation on a system of record. It constrains what the orchestrator may touch; the audit trail is outside that boundary, so the orchestrator owns it. Distinct from *single-level orchestration*, which constrains the domain agents' delegation.

**Domain agent** — One of the four agents that owns a single domain and its tools: inventory, quoting, sales, replenishment. A domain agent answers about its own domain and reports blockers in its own domain. It never delegates to another agent.

**Single-level orchestration** — The rule that all delegation is one level deep. Every sequence of steps is planned, executed and orchestrated by the orchestrator; a domain agent never orchestrates another agent. Distinct from *the orchestrator never acts*, which constrains the orchestrator's tool use; this constrains the domain agents' delegation.

## The request

**Customer request** — One prose string from the harness, plus the date it arrived. The unit of work: one request in, one resolution out.

**Requested line** — A single thing the customer asked for, in the customer's own words, with the quantity and unit as they stated them. Catalogue-blind: a requested line may name something that does not exist, or a unit the business does not sell in.

**Resolved item** — A requested line matched to an exact catalogue item name. Resolution is inventory's work; a requested line that cannot be resolved carries no resolved item and raises a blocker instead.

## Contracts

**Canonical envelope** — The single response shape every domain agent returns. *Canonical* is a claim about authority: no agent invents its own response shape.

**Generic envelope** — The same envelope parameterised by payload type, so each agent's payload stays strictly typed. *Generic* is the mechanism; *canonical* is the claim. An envelope can be canonical without being generic (one shared shape with an untyped payload), so the two terms are not interchangeable.

**Customer-facing payload** — The half of a domain agent's response that the orchestrator is given. Contains nothing the customer may not see.

**Internal payload** — The half the orchestrator is never given: cash balances, margins, stock counts, raw errors. Withheld structurally, not by instruction — the orchestrator cannot disclose what never enters its context.

## Blockers

**Blocker signal** — A structured report that something in a domain agent's own domain prevents part or all of a request from proceeding. Carries an enumerated code, the scope it applies to, a severity, and an internal detail. The code makes a rejection mechanically testable; the detail is internal-only.

**Scope** — What a blocker applies to: the whole request, or one named requested line.

**Severity** — *Fatal* (nothing in the request can be served) or *partial* (one line drops, the rest continue). A property of the code, not a free choice of the agent raising it.

**Revisable blocker** — A blocker the customer could plausibly clear by amending their order — an ambiguous item, an unrecognised unit, an unmeetable deadline. Contrasted with a flat refusal, which no amendment fixes (we do not carry it) or which the customer is never told about (insufficient cash). Revisability is a property of the code.

## Outcomes

**Outcome** — How a request ended: fulfilled, partially fulfilled, rejected, or pending customer revision.

**Pending customer revision** — An outcome, not a state of waiting. The request cannot proceed until the customer amends it, so the flow is suspended and the reply asks the revision questions. Nothing in a harness run resumes it; the customer has no channel to answer.

**Revision query** — One question put to the customer, arising from one revisable blocker.

**Suspended flow** — The persisted record of a request that ended pending customer revision: the original request, the steps already completed, and the open revision queries. Enough to resume the request without re-running the work already done.

**Resume token** — The identifier of a suspended flow. Carries its own provenance — which run and which request it resumes — so a token can be read rather than merely looked up.

## Audit

**Step** — One domain agent's turn within one request: what it was asked, what it answered, what it signalled. The unit of the audit trail.

**Run** — One pass of the harness over the sample requests. Identifies steps and suspended flows, and distinguishes a request from the same request in a later run.
