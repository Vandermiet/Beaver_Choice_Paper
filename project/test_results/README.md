# Test results

One CSV per ticket: `test_results_<ticket>.csv` (e.g. `test_results_101.csv` for
ticket #101). `run_test_scenarios()` writes here automatically — set
`BEAVER_TICKET` to the ticket number before the run:

```bash
BEAVER_TICKET=101 python project_starter.py
```

Without `BEAVER_TICKET` the run writes the unsuffixed `test_results.csv`, which
is what the project rubric asks for as the final deliverable.

`test_results.csv` is ticket 109's run and carries no suffix of its own: that
run *is* the deliverable, so archiving a second identical copy under 109 would
be two names for one piece of evidence. The measured tally read back from its
audit trail is on issue #26.

## The trail behind the deliverable

`audit_20260916T111405Z.sql` is the audit trail of the run `test_results.csv`
came out of: the seven audit tables and the `transactions` rows they link to,
dumped with their rowids because `transaction_links` joins on them.
`.gitignore` keeps `munder_difflin.db` out of the repo — `init_database`
rewrites it on every run — so this dump is how the evidence travels.

`tests/test_report.py` rebuilds it in memory and runs every query in
`docs/reflection-report.md`'s appendix against it, so each figure the report
states is checked against the run it describes rather than against whatever
database happens to be on disk.
