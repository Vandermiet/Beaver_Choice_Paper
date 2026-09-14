# Rubric-gate dry run over `quote_requests_sample.csv`

Resolves the dry-run ticket (#11) on the design map (#1). Walks all 20 sample
requests, in date order, through the design locked at #10.

**Method.** Resolution decisions (which catalogue item a line means) are made by
hand under #6's four rules — that is the LLM's job in the real system and the
only part of the walk a script cannot honestly stand in for. Everything
downstream of resolution is *computed*, not estimated: `research/rubric-dry-run.py`
seeds a throwaway `munder_difflin.db` at seed 137 and drives the real starter
helpers (`get_stock_level`, `get_supplier_delivery_date`, `create_transaction`,
`get_cash_balance`, `generate_financial_report`), so stock depletes across
requests and cash moves exactly as it would at run time. Full output in
`research/rubric-dry-run-trace.txt`.

This matters: every earlier prediction on the map was made per-request, against
the *seeded* stock table. Stock is transaction-derived and cumulative, so the
sixth request does not see the numbers the first one saw.

## Verdict on the three rubric §3 gates

| gate | required | actual | |
|---|---|---|---|
| requests that change the cash balance | ≥ 3 | **9** | pass |
| requests successfully fulfilled | ≥ 3 | **7** (3 fully, 4 partially) | pass |
| not all fulfilled, reason given | — | **13 not fulfilled** | pass |

Every gate clears, so the design does not have to change to be gradeable. But
the predicted distribution was **11 / 11 / 9** and the walk produces **9 / 7 / 13**
— the gap is four requests, and three separate causes behind it. Two of them are
design defects.

### Outcome distribution

| outcome | count | requests |
|---|---|---|
| `FULFILLED` | 3 | 1, 4, 10 |
| `PARTIALLY_FULFILLED` | 4 | 5, 6, 12, 14 |
| `PENDING_CUSTOMER_REVISION` | 8 | 3, 7, 8, 9, 11, 15, 17, 19 |
| `REJECTED` | 5 | 2, 13, 16, 18, 20 |

The pause set of 8 is exactly as #6 predicted, request for request. `UNPRICEABLE`
fires 0 times and `CASH_INSUFFICIENT` 0 times, both as predicted — cash never
drops below $45,140 against a worst-case single restock of $167.85.

Blocker counts over the run: `ITEM_NOT_CARRIED` 19, `SIZE_NOT_CARRIED` 8,
`INSUFFICIENT_STOCK` (survey) 14, `DEADLINE_UNMEETABLE` 7, `UNIT_NOT_UNDERSTOOD` 1.

### Cash

Start $45,059.70 → end **$45,140.37**, a net **+$80.67** across the whole run,
against $5,058.90 of closing inventory value (up from $4,940.30 seeded). The
business is very slightly cash-positive and noticeably more stocked than it
started — which is what buying a `min_stock_level` floor on every restock does.

## Defect 1 — three requests reject entirely on a delivery promise we did not need to make

`DEADLINE_UNMEETABLE` fires **7 times**, not "almost never" as the map's note
from #8 has it. It is the sole cause of rejection for requests **13, 16 and 18**,
and it drops the largest line in request 14.

The mechanism is that #8 reinterpreted `get_supplier_delivery_date` as *our*
promise to the customer, computed from the **line quantity** — so a 500-unit
line is always promised 4 days out and a 1,001-unit line 7 days, **whether or not
the goods are sitting on the shelf**. Every sample request bar one carries an
April 15 deadline, and request dates run to April 15:

- request 13 (Apr 8, deadline **Apr 10**): 500 sheets A4 + 200 cardstock → both promised Apr 12 → rejected.
- request 16 (Apr 13, deadline Apr 15): 500 sheets A4, **5,135 in stock**, promised Apr 17 → rejected.
- request 18 (Apr 14, deadline Apr 15): all three lines in stock or restocked, all promised Apr 18 → rejected.

Request 16 is the clean illustration: we hold ten times the stock the customer
asked for and refuse the order anyway, on a lead time that belongs to a supplier
we are not buying from.

This is not obviously wrong — shipping does take time, and #8 chose this
deliberately to give the helper a home. But it was chosen believing it would
almost never bite, and it decides three of twenty requests. It needs a decision,
not a patch.

## Defect 2 — replenishment buys stock for lines sales then refuses

Replenishment is triggered by the `INSUFFICIENT_STOCK` signal **inventory** raises
at survey time, which is before sales has ruled on the deadline. So on every
request where a line is both short and late, we buy the stock and then decline
the sale:

| request | spent | earned | net |
|---|---|---|---|
| 13 | $37.59 | $0.00 | **−$37.59** |
| 14 | $196.26 | $71.25 | **−$125.01** |
| 18 | $58.31 | $0.00 | **−$58.31** |

Request 14 buys 4,500 sheets of A4 to serve a 5,000-sheet line it then declines
on the deadline; that stock is still sitting there when request 16 arrives, and
request 16 is declined on the deadline too. **Two of the five rejected requests
have a negative cash delta** — we are worse off for having been asked.

The two defects compound, but they are separate decisions: fixing the promise
rule shrinks the damage without removing it (request 13's restock arrives Apr 12
against an Apr 10 deadline, so that purchase is wasted under any promise rule).
What is under-specified is **which signal triggers replenishment** — inventory's
survey, or sales' pass-1 declines.

## Correction — request 2 rejects; the map's tally counted it as cash-moving

#6 listed request 2 among the eleven cash-moving requests, while its own prose
used that same request as the worked example for the category guard. Both cannot
be true. Under the locked rules all three of its lines drop:

- "500 sheets of colorful **poster paper**" — category guard (`Poster paper` is
  `paper` in the universe; the only carried candidate is
  `Large poster paper (24x36 inches)`, `large_format`);
- "300 roll of streamers" — `Party streamers`, not carried;
- "200 balloons" — not a paper product.

Zero surviving lines, so request 2 is `REJECTED`, exactly like request 20. This
is a bookkeeping error in the tally, not a design flaw — the rules behaved as
written.

## Two resolution edge cases the implementation session should know about

Neither changes the gate arithmetic; both are places where the rules are sharper
than they look.

**"poster board" and "poster boards (24" x 36")" resolve differently.** Request 7's
line names the size, so the universe best match *is*
`Large poster paper (24x36 inches)` and the guard stays silent; request 16's line
does not, so the universe best match is `Poster paper` and the guard fires. The
split is deterministic and defensible — the size is what tells us they mean the
large-format product — but it hangs entirely on the scorer's choice of universe
best match, which is the one input to the guard that nothing validates.

**The category guard has no power over "table napkins."** Request 17's
`Paper napkins` and the carried `Table covers` are **both** `product`, so a
shortlist that scores "table napkins" against "Table covers" on the shared token
would resolve the line and sell table covers at $1.50 a unit. The
`CarriedItemName` validator cannot help — `Table covers` is a real carried name.
Request 17 pauses on A3 regardless, so nothing is lost on this sample, but the
guard protects against a *category* crossing and this is a collision inside one.

## Predictions from #10, checked

- Per-request cash delta measured across both sales passes — done; the deltas in
  the trace are orchestrator-side, cash before pass 1 against cash after pass 2.
- No request reaches `REJECTED` by a fatal blocker — confirmed; all five reject
  by exhaustion of lines.
- `UNPRICEABLE` 0, `CASH_INSUFFICIENT` 0, `PENDING_CUSTOMER_REVISION` 8 — all three confirmed.
