"""The replenishment agent: do we buy, how much, and can it arrive in time?

Opens the purse only on sales' declines. The deadline guard runs before the
cash guard — if we are not buying, there is nothing to afford. Owns
`DEADLINE_UNMEETABLE`, exclusively, and `CASH_INSUFFICIENT`.

The purchase decision is complete and standalone; nothing calls it yet. The
orchestrator wiring — the intersection of sales' pass-1 declines with
inventory's restock needs, and the second sales pass — is ticket 108.
Filled by ticket 107.
"""
