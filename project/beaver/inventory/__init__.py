"""The inventory agent: does a requested line resolve to something we carry,
and how short of it are we?

Owns resolution and the five resolution blockers. It observes a shortfall and
emits it as a `RestockNeed` — a fact, not a refusal — and never raises
`INSUFFICIENT_STOCK`. Filled by ticket 103.
"""
