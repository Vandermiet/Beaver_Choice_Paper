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
