"""The replenishment agent: do we buy, how much, and can it arrive in time?

Opens the purse only on sales' declines. The deadline guard runs before the
cash guard — if we are not buying, there is nothing to afford. Owns
`DEADLINE_UNMEETABLE`, exclusively, and `CASH_INSUFFICIENT`.
Filled by ticket 107.
"""
