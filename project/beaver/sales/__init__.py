"""The sales agent: which lines can we commit, and what moves the money?

Owns the commit-time stock re-check and `INSUFFICIENT_STOCK`, exclusively.
Stateless across the bounded retry — it never learns a retry happened, which is
what makes it safely callable twice. Owns no deadline at all.
Filled by ticket 105.
"""
