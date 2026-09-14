# Product master data: units, prices and catalogue abnormalities

Findings for [issue #12](https://github.com/Vandermiet/Beaver_Choice_Paper/issues/12). **Report, not repair** — nothing in `project/` is edited by this note.

All sources are local and primary: the `paper_supplies` literal and `generate_sample_inventory` in `project/project_starter.py`, and the provided CSVs `project/quote_requests_sample.csv` (20 rows), `project/quote_requests.csv` (100 rows) and `project/quotes.csv` (100 rows). Every number below was produced by running the real seeding code (`generate_sample_inventory(paper_supplies, coverage=0.4, seed=137)`) or by counting the real CSVs, not by reading the listing.

---

## 0. Two corrections to the map before anything else

Both the map (#1) and ticket #12 state the catalogue has **49** items and that seeding carries **~19**. Both are wrong.

| Claim | Actual | Source |
|---|---|---|
| 49 items in `paper_supplies` | **46** | `project_starter.py:16-70` — 25 `paper`, 15 `product`, 2 `large_format`, 4 `specialty` |
| ~19 items carried | **18** | `int(46 * 0.4) == 18` (`project_starter.py:103`) |

The `int()` truncation matters beyond pedantry: `coverage` is applied as `int(len * coverage)`, so the carried count is a floor, not a round. Any design that hard-codes a catalogue size, or asserts a carried count in a test, must use 46/18.

## 1. The carried catalogue under seed 137

This is the entire product universe the agents will ever meet in a graded run. `current_stock` ∈ [200,800), `min_stock_level` ∈ [50,150), both `np.random.randint` (`project_starter.py:122-123`).

| # | item_name | category | unit_price | current_stock | min_stock_level | stock value |
|---|---|---|---|---|---|---|
| 1 | Paper plates | product | $0.10 | 748 | 144 | $74.80 |
| 2 | 100 lb cover stock | specialty | $0.50 | 636 | 85 | $318.00 |
| 3 | Glossy paper | paper | $0.20 | 587 | 147 | $117.40 |
| 4 | Rolls of banner paper (36-inch width) | large_format | $2.50 | 546 | 65 | $1,365.00 |
| 5 | Photo paper | paper | $0.25 | 423 | 58 | $105.75 |
| 6 | Cardstock | paper | $0.15 | 595 | 148 | $89.25 |
| 7 | Colored paper | paper | $0.10 | 788 | 143 | $78.80 |
| 8 | 80 lb text paper | specialty | $0.40 | 249 | 91 | $99.60 |
| 9 | Large poster paper (24x36 inches) | large_format | $1.00 | 699 | 89 | $699.00 |
| 10 | Table covers | product | $1.50 | 736 | 77 | $1,104.00 |
| 11 | Butcher paper | paper | $0.10 | 365 | 80 | $36.50 |
| 12 | Kraft paper | paper | $0.10 | 493 | 64 | $49.30 |
| 13 | Banner paper | paper | $0.30 | 793 | 128 | $237.90 |
| 14 | Presentation folders | product | $0.50 | 389 | 97 | $194.50 |
| 15 | Patterned paper | paper | $0.15 | 548 | 119 | $82.20 |
| 16 | A4 paper | paper | $0.05 | 272 | 135 | $13.60 |
| 17 | Invitation cards | product | $0.50 | 526 | 149 | $263.00 |
| 18 | Crepe paper | paper | $0.05 | 234 | 104 | $11.70 |

Total stock value **$4,940.30**. `init_database` books a $50,000 opening credit and one `stock_orders` row per item at `current_stock × unit_price` (`project_starter.py:212-231`), so the opening cash balance is **$45,059.70** and the entire warehouse is worth **11% of the cash on hand**.

That ratio is itself an abnormality worth naming: **the cash guard will essentially never bind.** The most expensive conceivable restock — topping every carried item back up from zero — costs under $5,000 against $45,059.70. The `CASH_INSUFFICIENT` blocker the map leaves unspecified is, on this data, unreachable through normal replenishment. It can only fire if the replenishment policy orders in units the catalogue does not price in (see §2), which makes it a *symptom detector for unit confusion* rather than a genuine financial guard. Worth stating explicitly on #7/#10 so nobody tunes a threshold against a scenario that cannot occur.

## 2. Unit coherence

### 2.1 The declared units are patchy, not wrong

`paper_supplies` declares units in three different ways and, for a quarter of the catalogue, not at all:

| Block | Items | How the unit is declared | Source |
|---|---|---|---|
| Paper Types | 25 | One section comment: *"priced per sheet unless specified"* — and nothing is ever specified | `project_starter.py:17` |
| Product Types | 15 | A per-item trailing comment on every row (`# per plate`, `# per cup`, `# per roll`, …) | `project_starter.py:44-58` |
| Large-format | 2 | One section comment: *"priced per unit"* | `project_starter.py:61` |
| **Specialty papers** | **4** | **Nothing. No section unit, no per-item comment.** | `project_starter.py:65-69` |

The four specialty items — `100 lb cover stock` ($0.50), `80 lb text paper` ($0.40), `250 gsm cardstock` ($0.30), `220 gsm poster paper` ($0.35) — carry **no declared unit anywhere**. Two of them (`100 lb cover stock`, `80 lb text paper`) are carried under seed 137. Their prices sit in the per-sheet band, so per-sheet is the only reading that makes sense, but it is an inference, not a statement.

More importantly: **none of this reaches the agents.** These are Python comments. They are not columns in `paper_supplies`, they are not written to the `inventory` table, and no required helper returns them. `get_all_inventory` returns name→stock; `generate_financial_report`'s `inventory_summary` returns stock/unit_price/value. **The unit of every item is unavailable at runtime.** Any unit reasoning the design does must be inferred by the LLM from the item *name*, or hard-coded into the design as a static table. This is the single most consequential finding in this note, and it is not about the data being odd — it is about the data being absent.

### 2.2 The unit mismatch is narrow, not widespread — and #6's refusal is correctly scoped

Ticket #6 settled `UNIT_NOT_UNDERSTOOD` as a refusal on the strength of one example ("500 reams of printer paper"). Counting the actual corpus shows the decision was right but the fear was overstated.

Requests mentioning each unit word (a request may mention several):

| Unit word | sample (n=20) | full corpus (n=100) | Catalogue can price it? |
|---|---|---|---|
| sheets | **19** | **88** | Yes — paper is priced per sheet |
| rolls | 2 | 21 | Yes for `Party streamers`, washi tape, `Rolls of banner paper` |
| reams | **2** | **17** | **No** |
| packs | 0 | 13 | **No** |
| packets | 1 | 1 | **No** |
| boxes | 0 | 2 | **No** |
| bags | 0 | 1 | Yes — `Paper party bags` is per bag |
| pads, cases, bundles, cartons, dozens | 0 | 0 | — |

**"Sheets" dominates overwhelmingly — 19 of 20 sample requests, 88 of 100 overall.** The catalogue's per-sheet pricing is therefore *aligned* with how customers actually phrase paper quantities in this corpus, not misaligned. The trade-unit objection in #12 ("paper of that kind is universally sold in reams") is true of the real world and false of this dataset.

The genuinely unpriceable units are **reams, packs, packets and boxes** — present in roughly **a third of the full corpus** (17+13+1+2 mentions, some co-occurring) but only **3 of the 20 sample requests** that the rubric is actually scored over:

- sample #3 — "500 **reams** of printer paper" (alongside two sheet-denominated lines)
- sample #15 — "500 **reams** of cardboard for signage" (also names a non-existent material)
- sample #9 — "50 **packets** of 100% recycled kraft paper envelopes" (`Envelopes` is priced per envelope)

**What the design must absorb:** `UNIT_NOT_UNDERSTOOD` must be a **partial, line-scoped** blocker, never fatal. All three sample cases are mixed requests where the other lines are perfectly resolvable in sheets. A fatal unit blocker would reject sample #3, #9 and #15 wholesale and throw away ~7 quotable lines. Per the #4 contract, `partial` drops the line and continues — that is the correct severity, and this data says so unambiguously.

Note also that #9 is a *second, distinct* unit failure the map's enumeration does not yet name: a bundling unit ("packets") applied to an item already priced atomically ("per envelope"). It is the same code but it is not a ream, and a design that pattern-matches on the literal word "ream" will miss it.

### 2.3 A unit trap hiding inside a required helper

`get_supplier_delivery_date` (`project_starter.py:371-413`) buckets lead time on raw `quantity`: ≤10 same day, ≤100 1 day, ≤1000 4 days, >1000 7 days.

Those thresholds were plainly written for *unit* counts, not *sheet* counts. 10,000 sheets of A4 (sample #3) is 20 reams — an unremarkable office order — and lands in the 7-day bucket. The identical physical order expressed as "20 reams" would land in the 1-day bucket. **The promised delivery date is a function of the unit the quantity happens to be expressed in.** Since every paper line in this corpus is sheet-denominated and 25 of ~60 real sample quantities exceed 1,000, the practical effect is that **almost every paper order quotes the maximum 7-day lead time.**

This is not fixable (helpers are out of scope) and it is not a bug the design can route around, but the quoting agent's customer-facing prose must not present a 7-day lead time as if it reflected a considered supply judgement. Relevant to #7.

### 2.4 Where the declared unit and the item name genuinely disagree

Only two, and both are in the `product` block where units *are* declared:

- **`Sticky notes` — `# per sheet`, $0.03.** The only `product`-category item declared per sheet rather than per article. Nobody orders sticky notes by the sheet; the full corpus mentions them twice, both times by the pad. The declared unit is coherent with the price ($0.03/sheet is right) and incoherent with the purchase behaviour.
- **`Notepads` — `# per pad`, $2.00.** The inverse. This is the *only* item in all 46 priced as a bundle rather than an atom, which is exactly why it is 100× its `product` peers (§3).

## 3. Price plausibility

Overall span is **125×** ($0.02 `Paper napkins` → $2.50 `Rolls of banner paper`). By category:

| category | n | min | median | max |
|---|---|---|---|---|
| paper | 25 | $0.04 | $0.12 | $0.30 |
| product | 15 | $0.02 | $0.15 | $2.00 |
| large_format | 2 | $1.00 | $1.75 | $2.50 |
| specialty | 4 | $0.30 | $0.375 | $0.50 |

**The `paper` and `specialty` bands are tight and internally coherent.** 25 paper items span 7.5× with a $0.12 median, and the ordering is sensible — `Standard copy paper` $0.04 cheapest, `Banner paper` $0.30 dearest, coated/decorative stock above plain. `specialty` spans 1.7× and sits cleanly above `paper`, as heavier stock should. Nothing in either needs flagging.

**The `product` band is the incoherent one, and the unit is the whole explanation.** Its 100× internal span ($0.02 napkin → $2.00 notepad) is not a pricing error; it is 15 items measured in 15 different units crammed into one column. `Notepads` at $2.00 is entirely plausible *per pad* and absurd against a table whose every other entry is a single article. #12 flags the napkin/notepad gap as suspicious — it is, but the resolution is "different units", not "wrong price", and this is precisely why a price-based sanity check on a quote cannot work: there is no peer group to check against.

**One genuine price implausibility, unit notwithstanding: `Rolls of banner paper (36-inch width)` at $2.50.** Read per roll — the only sensible reading of "Rolls of" — a 36-inch banner roll for $2.50 is roughly an order of magnitude below anything real. It is also the **most valuable line in the seeded warehouse** ($1,365 of $4,940, 28%), because `generate_sample_inventory` gave it 546 *rolls*. So the single largest asset on the books is a mispriced item held in an implausible quantity. It is carried under seed 137, so it will be met.

**A structural consequence for the pricing ladder (#7):** a percentage discount behaves completely differently across these bands. 10% off 10,000 sheets of A4 is $50; 10% off 10 notepads is $2. Any ladder keyed on *line total* will hand deep discounts to sheet orders and nothing to article orders, and any ladder keyed on *quantity* will do the reverse — 10,000 sheets (20 reams, a small order) trips every bulk tier while 200 table covers (a genuinely large order) trips none. **Quantity is not comparable across items in this catalogue, so the ladder must be keyed on line or order value, not on unit count.** The same warning applies to `get_supplier_delivery_date`, which the helper unfortunately keys on quantity (§2.3) and which we cannot change.

## 4. Category coherence and near-collision clusters

Clusters where two or more names could plausibly resolve from the same customer phrase. `CARRIED` marks membership of the seed-137 carried catalogue.

| Cluster | Members | Spread |
|---|---|---|
| **poster** | `Poster paper` paper $0.25 · `Large poster paper (24x36 inches)` large_format $1.00 **CARRIED** · `220 gsm poster paper` specialty $0.35 | 4× across 3 categories |
| **banner** | `Banner paper` paper $0.30 **CARRIED** · `Rolls of banner paper (36-inch width)` large_format $2.50 **CARRIED** | 8.3× |
| **cardstock / stock** | `Cardstock` paper $0.15 **CARRIED** · `250 gsm cardstock` specialty $0.30 · `100 lb cover stock` specialty $0.50 **CARRIED** | 3.3× |
| **cups** | `Paper cups` product $0.08 · `Disposable cups` product $0.10 | 1.25×, near-synonyms in one category |
| **coloured** | `Colored paper` paper $0.10 **CARRIED** · `Bright-colored paper` paper $0.12 | 1.2× |
| **cards** | `Invitation cards` product $0.50 **CARRIED** · `Name tags with lanyards` product $0.75 · `Cardstock` **CARRIED** | — |
| **decorative** | `Decorative paper` paper $0.18 · `Decorative adhesive tape (washi tape)` product $0.20 | prefix collision, different goods |
| **notes** | `Sticky notes` product $0.03 · `Notepads` product $2.00 | 66× |

**The decisive observation: seed 137 dissolves most of these.** Of the eight clusters, seven lose all but one member to the 40% coverage draw. The `poster` triple collapses to `Large poster paper` alone; the `cardstock` triple to `Cardstock` + `100 lb cover stock` (which share only the token "stock" and are unlikely to collide in practice); `cups`, `coloured`, `decorative` and `notes` collapse to zero or one carried member each.

**Exactly one cluster survives intact into the carried catalogue: `Banner paper` ($0.30, per sheet) versus `Rolls of banner paper (36-inch width)` ($2.50, per roll).** It is also the worst possible survivor — the two members differ in category, in unit, and by 8.3× in price, so a wrong resolution is an order-of-magnitude quoting error in either direction. Five requests in the full corpus mention "banner".

**What the design must absorb:** `ITEM_AMBIGUOUS` is a real but *rare* code on this data — the inventory agent's resolution step must adjudicate against the **carried catalogue, not the product universe**, and once it does, the ambiguity surface is one pair. That is a meaningful simplification for #7 and #10, and it argues against building elaborate disambiguation machinery. But the resolution prompt must be shown carried items only; resolving against all 46 would manufacture ambiguity that does not exist and would also let the agent offer things the company does not sell — which `CONTEXT.md` already names as indistinguishable, to a customer, from not existing.

### 4.1 The larger resolution problem is absence, not ambiguity

Counting terms in the sample that have **no catalogue entry at all** is far more alarming than the collisions:

| Term in requests | sample | full corpus | Catalogue status |
|---|---|---|---|
| A3 | **7 of 20** | **17 of 100** | **No A3 item exists.** Only `A4 paper`. |
| A5 | 1 | 5 | **No A5 item exists.** |
| A2 | 0 | 1 | **No A2 item exists.** |
| posters | 5 | 17 | `Poster paper`/`Large poster paper` exist; "a poster" as a finished good does not |
| napkins | 2 | 9 | `Paper napkins` exists — **not carried under seed 137** |
| cups | 1 | 9 | `Paper cups`/`Disposable cups` exist — **neither carried** |
| envelopes | 1 | 15 | `Envelopes` exists — **not carried** |
| balloons | 1 | 0 | **No entry.** Not a paper product at all. |
| tickets | 1 | 1 | **No entry.** |
| cardboard | 1 | 0 | **No entry.** |
| poster board | 2 | 1 | `Large poster paper (24x36 inches)` is the intended match |

**A3 is the headline.** Seven of the twenty graded sample requests ask for A3 paper, and there is no A3 item in the universe, let alone the carried catalogue. This is a *size* failure, distinct from both the unit failure and the name failure the map enumerates — the customer's unit ("sheets") is priceable and the material ("glossy paper", "matte paper") exists, but the size does not. The design has a choice the map has not yet made:

1. Treat "A3 glossy paper" as `Glossy paper` and quote it — silently substituting a different size, the same class of error as the 500× ream conversion #6 refused; or
2. Raise a line-scoped blocker.

**Consistency with #6 requires (2).** If a 500× unit substitution is unsafe enough to refuse, a 2× area substitution made silently is unsafe for the same reason, and refusing one while performing the other is indefensible. But note the cost: A3 lines appear in 7 of 20 sample requests, so this choice materially shapes the rubric's "≥3 fulfilled, not all fulfilled" gate. It should be a deliberate decision on #7, not an emergent LLM behaviour — and because these are partial blockers on mixed requests, the fulfilment count survives either way.

Sample #17 is the sharpest single illustration of the absence problem: it asks for napkins, cups **and** plates, and only `Paper plates` is carried. Three near-identical product lines, one resolvable, two not — and the two failures are `ITEM_NOT_CARRIED`, not `ITEM_UNKNOWN`, a distinction the customer cannot see and the audit trail must.

## 5. Stock and `min_stock_level` plausibility

`generate_sample_inventory` draws `current_stock ~ U[200,800)` and `min_stock_level ~ U[50,150)` **identically for every item, with no reference to unit, price, or category** (`project_starter.py:122-123`). The consequences:

### 5.1 Reorder thresholds are denominated in nothing

`Crepe paper` (per sheet) gets `min_stock_level` 104. `Table covers` (per cover) gets 77. Reordering crepe paper at the threshold costs **$5.20**; reordering table covers costs **$115.50**. Across the carried set the cost of a threshold-triggered restock spans **31×** ($5.20 → $162.50) purely as an artifact of which random integer landed on which item. There is no sense in which these thresholds encode a consistent policy — not days of cover, not reorder value, not service level.

**What the design must absorb (#10):** `min_stock_level` **cannot be used as a reorder quantity or as an economic signal.** It is usable only as a boolean tripwire ("stock is low for this item"), and even then it is arbitrary. Any replenishment policy that computes an order quantity should derive it from demand and headroom, not from `min_stock_level`. The map already flags that no required helper exposes `min_stock_level`; this note adds that even once a direct `inventory` read exposes it, **it carries less information than its name suggests.**

### 5.2 Stock levels are inverted relative to real demand

The `current_stock ~ U[200,800)` draw is roughly right for per-article items and catastrophically wrong for per-sheet items, because customers order sheets in the thousands and articles in the hundreds.

Extracting every quantity from the 20 sample requests (excluding date artefacts like "15" and "2025") gives ~60 real order lines: **median 300, 75th percentile 1,000, max 10,000.** Against a carried catalogue whose stock runs 234–793:

- **25 of the ~60 sample order lines exceed the largest stock holding of any carried item (793).**
- `A4 paper` holds **272 sheets** — about half a ream. Sample #3 asks for 10,000 sheets, #14 for 5,000, #15 for 10,000. Not one of the three A4 requests in the sample is coverable, nor would they be if A4 were the *only* item and held the entire warehouse.
- Conversely `Table covers` holds **736 covers** and `Paper plates` **748 plates** against a corpus that asks for plates in the hundreds. These are years of cover.

So the stock draw is upside-down: **the items with thousand-unit demand hold hundreds, and the items with hundred-unit demand hold hundreds too.**

**What the design must absorb:** `INSUFFICIENT_STOCK` is not an edge case on this data — **it is the modal outcome for paper lines**, firing on roughly 40% of sample order lines. The map's conditional replenishment path (orchestrator calls replenishment on `INSUFFICIENT_STOCK`, then one bounded retry of sales) is therefore not a rare branch to be handled defensively; **it is the main line of execution and must be the best-tested path in the design.** Conversely the "everything in stock, quote and sell" path is the rarer one.

This is good news for the rubric, which requires that *not all* sample requests be fulfilled — stock scarcity supplies that naturally, without any contrivance. It also means replenishment's lead-time answers reach the customer constantly, which loops back to §2.3: almost all of them will say seven days.

### 5.3 A trap in how stock is read back

`get_all_inventory` filters `HAVING stock > 0` (`project_starter.py:323`), so an item sold down to exactly zero **vanishes from the inventory dictionary entirely**. It becomes indistinguishable, through that helper, from an item that was never carried. `CONTEXT.md` draws exactly this distinction — *carried* versus *stocked* — and insists "the two are different answers and never collapse into one." Through `get_all_inventory` alone, they do collapse. Given §5.2 makes stock-outs the common case, the inventory agent must establish *carriage* from the `inventory` table (which is static and complete) and *stock* from the transaction-derived helpers, and must never infer "not carried" from absence in `get_all_inventory`.

## 6. The historical quote corpus is arithmetically incoherent

This was not in #12's brief, but `quotes.csv` is product master data in the sense that matters — it is seeded into the `quotes` table and is the entire return surface of `search_quote_history`, a **required** helper that #7's pricing ladder is expected to lean on. It does not survive inspection.

`quote_requests.csv` and `quotes.csv` both have 100 rows and align positionally; `init_database` assigns `request_id`/`id` by `range(1, len+1)` to each independently (`project_starter.py:172, 179`), and the `request_metadata` in `quotes.csv` matches the `job`/`need_size`/`event` of the same-indexed `quote_requests.csv` row in **100/100 cases**. So the join is sound. What it joins to is not.

**Five rows are persisted generation failures.** Rows 9, 12, 45, 54 and 86 carry `total_amount = -1` and `quote_explanation = "Error parsing response."`. `search_quote_history` applies `LIKE` filters over `quote_explanation` and `qr.response` with no validity filter, so **an error row can be returned as a pricing precedent** — and will be, for any search term matching its request text.

**`total_amount` is wildly out of family.** Across the 100 rows: median **$152**, 75th percentile **$2,003**, maximum **$231,500**, against a corpus of orders whose stated line prices are cents per sheet. The largest total is 1,500× the median for a materially similar order.

**The totals mostly do not reconcile with their own explanations.** Of the 95 non-error rows, `total_amount` appears among the dollar figures in its own explanation in only **57**; in **21** it does not, and **17** explanations quote no dollar figure at all. Worked examples:

- **Row 88, `total_amount = 231500`** — "500 reams of A4 printing paper, we've reduced the unit price to $0.045 per ream". 500 × $0.045 = **$22.50**. The recorded total is four orders of magnitude off its own stated arithmetic.
- **Row 13, `total_amount = 6000`** — the explanation computes $25.00 + $30.00 + $5.00 and states "bringing the total cost to a friendly rounded number of **$60.00**". Recorded: 6000. Exactly **100×** — a dollars/cents confusion.
- **Row 1, `total_amount = 96`** — "500 reams of A4 paper at $0.05 each, 300 reams of letter-sized at $0.06, 200 reams of cardstock at $0.15… 10% discount". That arithmetic gives $73 × 0.9 = **$65.70**. Recorded: 96.
- **Row 11, `total_amount = 26800`** — stated per-sheet prices over stated quantities total **$430.40**. Off by ~62×.

**The corpus prices reams both ways, inconsistently.** Row 1 charges the per-*sheet* price for a *ream* ($0.05 each) — the exact silent 500× under-charge that #6 refused to perform. Row 88 charges something unrelated to either reading. Row 60 ("500 reams of A4 standard paper… 200 reams of colorful cardstock") records $43,000 with no arithmetic shown at all. **There is no consistent historical convention for reams to learn from.** This is, incidentally, the strongest available evidence that #6 decided correctly: the provided history demonstrates what happens when a quoting agent guesses at units, and what happens is a 10,000× spread in outcomes for equivalent orders.

**What the design must absorb (#7):** `search_quote_history` is a **required** helper and must be wired into a tool to satisfy the rubric, but its `total_amount` values must **not** feed the pricing computation. Use it for what it is reliable for — tone, phrasing, evidence that discounting is customary, `job_type`/`order_size`/`event_type` context — and compute price from `inventory.unit_price` × quantity plus the ladder. If historical totals are surfaced to the quoting agent at all, they must be framed as prose precedent, not as numbers; and the tool should at minimum filter `total_amount > 0` so the five error rows cannot be cited. That filtering happens in *our* tool wrapper, not in the provided helper, so it stays within the map's scope line.

---

## Summary: what the design must absorb

| # | Abnormality | Lands on |
|---|---|---|
| 1 | Catalogue is **46 items / 18 carried**, not 49/~19 | map #1 correction |
| 2 | **Units are runtime-invisible** — declared only in Python comments, absent from the `inventory` table and every helper; 4 specialty items have no declared unit at all | #7, #10 — unit logic must be static design, not data-driven |
| 3 | Unit mismatch is **narrow**: "sheets" fits 19/20 sample requests; only 3 sample requests use an unpriceable unit (reams ×2, packets ×1) | #6 confirmed, scope reduced |
| 4 | `UNIT_NOT_UNDERSTOOD` must be **partial and line-scoped** — all 3 sample cases are mixed requests with resolvable sheet lines | #4 severity confirmed |
| 5 | "Packets of envelopes" is a **second unit-failure shape** the enumeration does not name | #7 |
| 6 | `get_supplier_delivery_date` buckets on raw quantity, so sheet orders almost always quote **7 days** | #7 prose |
| 7 | `product` category spans **100×** because 15 units share one column — **no price sanity check is possible** | #7 |
| 8 | Discount ladder must key on **value, not quantity** — quantity is incomparable across items | #7 |
| 9 | `Rolls of banner paper` is **mispriced and is 28% of warehouse value** | #7, #10 |
| 10 | Seed 137 dissolves 7 of 8 collision clusters; **one survives**: `Banner paper` vs `Rolls of banner paper`, 8.3× apart | #7 — resolve against the *carried* catalogue only |
| 11 | **A3 appears in 7 of 20 sample requests and does not exist** — a size failure distinct from unit and name failures | #7 — needs an explicit decision |
| 12 | `min_stock_level` is **unit-blind**; threshold restock cost spans 31×. Usable as a tripwire, never as a quantity or an economic signal | #10 |
| 13 | **`INSUFFICIENT_STOCK` is the modal outcome** (~40% of sample lines exceed all stock) — the replenishment branch is the main line, not an edge case | #10 |
| 14 | `get_all_inventory`'s `HAVING stock > 0` **collapses "stocked out" into "not carried"** — the distinction `CONTEXT.md` insists on | #7 inventory resolution |
| 15 | Cash guard is **unreachable**: whole warehouse is $4,940 against $45,060 cash | `CASH_INSUFFICIENT` policy |
| 16 | `quotes.csv` has **5 `-1` error rows** and totals that **do not reconcile** (57/95 match their own explanation; errors up to 10,000×) | #7 — `search_quote_history` for prose only, never for numbers |
