"""The audit trail: six tables in `munder_difflin.db` plus a JSONL transcript
sidecar, written by a decorator at the delegation seam.

Filled by ticket 102. The DDL is on issues #5, #7 and #10. `bootstrap_audit()`
runs *after* `init_database`, whose `if_exists="replace"` is scoped to its own
four tables, so these survive a re-init and accumulate across runs.
"""
