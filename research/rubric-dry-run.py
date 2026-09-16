"""Paper dry run of the locked design over quote_requests_sample.csv.

Resolution decisions are made by hand (the LLM's job in the real system) and
encoded below. Everything downstream -- stock, restock sizing, dates, cash --
is computed against a real seeded munder_difflin DB via the starter helpers.
"""
import os, random, shutil, sys, hashlib, math
from datetime import datetime, timedelta

sys.path.insert(0, "/Users/lukaszmieten/Documents/Udacity/Beaver Choice Company/project")
os.chdir("/Users/lukaszmieten/Documents/Udacity/Beaver Choice Company/project")

SCRATCH = "/private/tmp/claude-501/-Users-lukaszmieten-Documents-Udacity-Beaver-Choice-Company/685834c7-136b-437c-9773-56f62b626ba0/scratchpad/dryrun.db"
if os.path.exists(SCRATCH):
    os.remove(SCRATCH)

import project_starter as ps
from sqlalchemy import create_engine

ps.db_engine = create_engine(f"sqlite:///{SCRATCH}")
ps.init_database(ps.db_engine, seed=137)

INV = ps.generate_sample_inventory(ps.paper_supplies, seed=137)
MIN_STOCK = dict(zip(INV.item_name, INV.min_stock_level))
PRICE = {p["item_name"]: p["unit_price"] for p in ps.paper_supplies}

def cost_ratio(item):
    """U(0.6, 0.8) drawn per item, deterministically from the item name (#9)."""
    h = int(hashlib.sha256(item.encode()).hexdigest()[:8], 16)
    return 0.6 + (h % 10**6) / 10**6 * 0.2

def band(units):
    if units < 500:   return "NONE", 0.00
    if units < 2000:  return "BULK", 0.05
    if units < 10000: return "VOLUME", 0.10
    return "WHOLESALE", 0.15

# ---------------------------------------------------------------- resolution
# (line_label, decision, item_name_or_None, quantity)
# decisions: RESOLVED | NO_CANDIDATE | CATEGORY_GUARD | SIZE_VETO | UNIT
R = "RESOLVED"; NC = "NO_CANDIDATE"; CG = "CATEGORY_GUARD"; SV = "SIZE_VETO"; UN = "UNIT"

REQUESTS = [
 (1,  "2025-04-01", "2025-04-15", [
    ("200 sheets A4 glossy paper",       R,  "Glossy paper", 200),
    ("100 sheets heavy cardstock",       R,  "Cardstock", 100),
    ("100 sheets colored paper",         R,  "Colored paper", 100)]),
 (2,  "2025-04-03", "2025-04-15", [
    ("500 sheets colorful poster paper", CG, None, 500),
    ("300 roll of streamers",            NC, None, 300),
    ("200 balloons",                     NC, None, 200)]),
 (3,  "2025-04-04", "2025-04-15", [
    ("10,000 sheets A4 paper",           R,  "A4 paper", 10000),
    ("5,000 sheets A3 paper",            SV, None, 5000),
    ("500 reams of printer paper",       UN, None, 500)]),
 (4,  "2025-04-05", "2025-04-15", [
    ("500 sheets recycled cardstock",    R,  "Cardstock", 500),
    ("250 sheets A4 printer paper",      R,  "A4 paper", 250)]),
 (5,  "2025-04-05", "2025-04-15", [
    ('500 sheets 8.5"x11" colored paper',R,  "Colored paper", 500),
    ("300 sheets cardstock",             R,  "Cardstock", 300),
    ("200 rolls washi tape",             NC, None, 200)]),
 (6,  "2025-04-06", "2025-04-15", [
    ("500 sheets construction paper",    NC, None, 500),
    ("300 sheets white printer paper",   R,  "A4 paper", 300),
    ("200 sheets cardstock",             R,  "Cardstock", 200)]),
 (7,  "2025-04-07", "2025-04-15", [
    ("500 sheets glossy A4 paper",       R,  "Glossy paper", 500),
    ("1000 sheets matte A3 paper",       SV, None, 1000),
    ('300 poster boards (24"x36")',      R,  "Large poster paper (24x36 inches)", 300),
    ("200 sheets heavyweight cardstock", R,  "Cardstock", 200)]),
 (8,  "2025-04-07", "2025-04-15", [
    ("500 sheets A4 glossy paper",       R,  "Glossy paper", 500),
    ("1000 sheets A4 matte paper",       NC, None, 1000),
    ("2000 sheets A5 colored paper",     SV, None, 2000),
    ("3000 sheets A4 recycled paper",    NC, None, 3000)]),
 (9,  "2025-04-07", "2025-04-10", [
    ("200 sheets A4 white printer paper",R,  "A4 paper", 200),
    ("100 sheets A3 glossy paper",       SV, None, 100),
    ("50 packets kraft envelopes",       NC, None, 50)]),
 (10, "2025-04-08", "2025-04-15", [
    ("500 sheets glossy paper",          R,  "Glossy paper", 500),
    ("300 sheets sturdy cardstock",      R,  "Cardstock", 300)]),
 (11, "2025-04-08", "2025-04-15", [
    ("500 sheets A3 glossy paper",       SV, None, 500),
    ("300 sheets A4 matte paper",        NC, None, 300)]),
 (12, "2025-04-08", "2025-04-15", [
    ("200 sheets colorful cardstock",    R,  "Cardstock", 200),
    ("500 sheets standard printer paper",R,  "A4 paper", 500),
    ("100 paper napkins",                NC, None, 100)]),
 (13, "2025-04-08", "2025-04-10", [
    ("500 sheets A4 printing paper",     R,  "A4 paper", 500),
    ("200 sheets cardstock",             R,  "Cardstock", 200)]),
 (14, "2025-04-09", "2025-04-15", [
    ("5,000 sheets A4 paper",            R,  "A4 paper", 5000),
    ("2,000 sheets poster paper",        CG, None, 2000),
    ("500 sheets cardstock",             R,  "Cardstock", 500)]),
 (15, "2025-04-12", "2025-04-15", [
    ("10,000 sheets A4 white paper",     R,  "A4 paper", 10000),
    ("5,000 sheets A3 colored paper",    SV, None, 5000),
    ("500 reams cardboard",              NC, None, 500)]),
 (16, "2025-04-13", "2025-04-15", [
    ("500 sheets A4 printer paper",      R,  "A4 paper", 500),
    ("200 sheets construction paper",    NC, None, 200),
    ("100 sheets poster board",          CG, None, 100)]),
 (17, "2025-04-14", "2025-04-15", [
    ("1000 sheets A4 white printer paper",R, "A4 paper", 1000),
    ("500 sheets A3 colored paper",      SV, None, 500),
    ("2000 table napkins",               NC, None, 2000),
    ("1000 paper cups",                  NC, None, 1000),
    ("500 paper plates",                 R,  "Paper plates", 500)]),
 (18, "2025-04-14", "2025-04-15", [
    ("500 sheets white cardstock",       R,  "Cardstock", 500),
    ("1000 sheets standard printing paper", R, "A4 paper", 1000),
    ("200 sheets colored paper",         R,  "Colored paper", 200)]),
 (19, "2025-04-15", "2025-04-20", [
    ("2000 sheets A4 glossy paper",      R,  "Glossy paper", 2000),
    ("1500 sheets A3 matte paper",       SV, None, 1500),
    ("1000 sheets cardstock",            R,  "Cardstock", 1000)]),
 (20, "2025-04-17", "2025-05-15", [
    ("5,000 flyers",                     NC, None, 5000),
    ("2,000 posters",                    NC, None, 2000),
    ("10,000 tickets",                   NC, None, 10000)]),
]

PAUSING = {SV, UN}          # SIZE_NOT_CARRIED, UNIT_NOT_UNDERSTOOD (+ AMBIGUOUS, QUANTITY_MISSING)
BLOCKER = {NC: "ITEM_NOT_CARRIED", CG: "ITEM_NOT_CARRIED",
           SV: "SIZE_NOT_CARRIED", UN: "UNIT_NOT_UNDERSTOOD"}

def stock(item, as_of):
    df = ps.get_stock_level(item, as_of)
    return int(df["current_stock"].iloc[0])

def d(s): return datetime.fromisoformat(s).date()

log = []
counts = {"blockers": {}, "outcomes": {}}
start_cash = ps.get_cash_balance("2025-04-01")
print(f"starting cash: {start_cash:,.2f}\n")

for rid, rdate, deadline, lines in REQUESTS:
    cash_before = ps.get_cash_balance("2099-01-01")
    ev = [f"=== request {rid}  ({rdate}, deadline {deadline})"]
    blockers = []
    resolved = []
    for label, dec, item, qty in lines:
        if dec == R:
            resolved.append((label, item, qty))
        else:
            blockers.append((label, BLOCKER[dec]))
            counts["blockers"][BLOCKER[dec]] = counts["blockers"].get(BLOCKER[dec], 0) + 1

    paused = any(dec in PAUSING for _, dec, _, _ in lines)
    for label, code in blockers:
        ev.append(f"    inventory blocker {code:<20} {label}")

    if paused:
        ev.append("  -> PENDING_CUSTOMER_REVISION (pausing blocker before money moves)")
        counts["outcomes"]["PENDING_CUSTOMER_REVISION"] = counts["outcomes"].get("PENDING_CUSTOMER_REVISION", 0) + 1
        log.append("\n".join(ev)); continue

    if not resolved:
        ev.append("  -> REJECTED (every line dropped)")
        counts["outcomes"]["REJECTED"] = counts["outcomes"].get("REJECTED", 0) + 1
        log.append("\n".join(ev)); continue

    # inventory survey: stock read per resolved line
    needs = []
    for label, item, qty in resolved:
        on_hand = stock(item, rdate)
        short = max(0, qty - on_hand)
        ev.append(f"    survey {item:<36} want {qty:>6}  on hand {on_hand:>6}"
                  + (f"  SHORT {short}" if short else ""))
        if short:
            needs.append((label, item, short, qty))
            counts["blockers"]["INSUFFICIENT_STOCK(survey)"] = counts["blockers"].get("INSUFFICIENT_STOCK(survey)", 0) + 1

    # quoting: prices every resolved line, blind to stock
    quoted = {}
    for label, item, qty in resolved:
        b, rate = band(qty)
        gross = qty * PRICE[item]
        quoted[label] = (item, qty, b, gross * (1 - rate))

    # sales pass 1: lines with stock, deadline check
    committed, declined = [], []
    available_from = {}
    for label, item, qty in resolved:
        on_hand = stock(item, rdate)
        promised = d(ps.get_supplier_delivery_date(rdate, qty))
        if on_hand < qty:
            declined.append((label, "INSUFFICIENT_STOCK")); continue
        if promised > d(deadline):
            declined.append((label, "DEADLINE_UNMEETABLE"))
            counts["blockers"]["DEADLINE_UNMEETABLE"] = counts["blockers"].get("DEADLINE_UNMEETABLE", 0) + 1
            continue
        ps.create_transaction(item, "sales", qty, quoted[label][3], rdate)
        committed.append((label, item, qty, quoted[label][3], promised))

    # replenishment, conditional on the INSUFFICIENT_STOCK signal
    restocked = []
    if needs:
        for label, item, short, qty in needs:
            order_qty = short + int(MIN_STOCK[item])
            unit_cost = round(cost_ratio(item) * PRICE[item], 4)
            spend = order_qty * unit_cost
            ps.create_transaction(item, "stock_orders", order_qty, spend, rdate)
            arrival = d(ps.get_supplier_delivery_date(rdate, order_qty))
            available_from[item] = arrival
            restocked.append((item, order_qty, spend, arrival))
            ev.append(f"    RESTOCK {item:<34} {order_qty:>6} @ {unit_cost:.4f} = {spend:>9,.2f}  arrives {arrival}")

        # sales pass 2: only the INSUFFICIENT_STOCK-declined lines
        retry = [l for l, c in declined if c == "INSUFFICIENT_STOCK"]
        declined = [(l, c) for l, c in declined if c != "INSUFFICIENT_STOCK"]
        for label in retry:
            item, qty, _, total = quoted[label]
            on_hand = stock(item, rdate)
            promised = max(d(ps.get_supplier_delivery_date(rdate, qty)),
                           available_from.get(item, d(rdate)))
            if on_hand < qty:
                declined.append((label, "INSUFFICIENT_STOCK(commit)"))
                counts["blockers"]["INSUFFICIENT_STOCK(commit)"] = counts["blockers"].get("INSUFFICIENT_STOCK(commit)", 0) + 1
            elif promised > d(deadline):
                declined.append((label, "DEADLINE_UNMEETABLE"))
                counts["blockers"]["DEADLINE_UNMEETABLE"] = counts["blockers"].get("DEADLINE_UNMEETABLE", 0) + 1
            else:
                ps.create_transaction(item, "sales", qty, total, rdate)
                committed.append((label, item, qty, total, promised))

    for label, item, qty, total, promised in committed:
        ev.append(f"    COMMIT  {item:<34} {qty:>6} = {total:>9,.2f}  promised {promised}")
    for label, code in declined:
        ev.append(f"    DECLINE {code:<20} {label}")

    dropped = len(blockers) + len(declined)
    if not committed:
        outcome = "REJECTED"
    elif dropped:
        outcome = "PARTIALLY_FULFILLED"
    else:
        outcome = "FULFILLED"
    counts["outcomes"][outcome] = counts["outcomes"].get(outcome, 0) + 1

    cash_after = ps.get_cash_balance("2099-01-01")
    ev.append(f"  -> {outcome}   revenue {sum(c[3] for c in committed):,.2f}"
              f"   cash {cash_before:,.2f} -> {cash_after:,.2f}"
              f"   delta {cash_after - cash_before:+,.2f}")
    log.append("\n".join(ev))

print("\n\n".join(log))
print("\n================ TALLY ================")
print("outcomes:", counts["outcomes"])
print("blockers:", counts["blockers"])
final = ps.get_cash_balance("2099-01-01")
print(f"cash {start_cash:,.2f} -> {final:,.2f}  ({final-start_cash:+,.2f})")
rep = ps.generate_financial_report("2099-01-01")
print("final inventory value:", round(rep["inventory_value"], 2))
