#!/usr/bin/env python3
"""Scheduled transactions: frequency math (month-end, leap year), enter / skip / auto-enter / count / end date / splits / transfers / revert."""
import datetime, sqlite3, subprocess, sys
from datetime import date as D
import scheduled
from testempty import Empty, Checker, HERE
check = Checker()
nx = lambda f, d, a=None: scheduled.advance(f, D.fromisoformat(d), a).isoformat()
# ---- frequency math
check("weekly / every 2 weeks", nx("weekly", "2026-12-28") == "2027-01-04" and nx("biweekly", "2026-12-28") == "2027-01-11")
check("monthly: Jan 31 -> Feb 28 -> Mar 31 (anchor kept)", nx("monthly", "2026-01-31") == "2026-02-28" and nx("monthly", "2026-02-28", 31) == "2026-03-31")
check("monthly in a leap year: Jan 31 -> Feb 29", nx("monthly", "2028-01-31") == "2028-02-29")
check("monthly: Mar 31 -> Apr 30, Dec 15 -> Jan 15 next year", nx("monthly", "2026-03-31") == "2026-04-30" and nx("monthly", "2026-12-15") == "2027-01-15")
check("quarterly from Nov 30 -> Feb 28 -> May 30 with anchor 30", nx("quarterly", "2026-11-30") == "2027-02-28" and nx("quarterly", "2027-02-28", 30) == "2027-05-30")
check("yearly: Feb 29 -> Feb 28 -> Feb 29 four years on", nx("yearly", "2028-02-29") == "2029-02-28" and nx("yearly", "2031-02-28", 29) == "2032-02-29")
d, a = D(2026, 1, 31), 31; seq = []
for _ in range(4): d = scheduled.advance("monthly", d, a); seq.append(d.isoformat())
check("monthly sequence from Jan 31", seq == ["2026-02-28", "2026-03-31", "2026-04-30", "2026-05-31"], seq)
# ---- API on a fresh ledger
E = Empty(8796); api = E.api; E.start(); today = D.today(); iso = lambda n: (today + datetime.timedelta(days=n)).isoformat()
try:
    chk = api("POST", "/api/accounts", dict(name="Checking", type="Checking", opening_balance=1000, opening_date="2026-01-01"))[1]["account_id"]
    sav = api("POST", "/api/accounts", dict(name="Savings", type="Savings"))[1]["account_id"]
    api("POST", "/api/categories", dict(name="Rent")); api("POST", "/api/categories", dict(name="Utilities")); api("POST", "/api/categories", dict(name="Pay", kind="income"))
    bal = lambda n: {a["name"]: a["balance_all"] for a in api("GET", "/api/accounts")[1]}[n]
    S = lambda **k: dict(dict(account_id=chk, payee="Landlord", amount="-100", category="Rent", freq="monthly", next_date=iso(5), auto=False), **k)
    # validation
    check("bad frequency / date / account / category refused", all(api("POST", "/api/scheduled", S(**k))[0] in (400, 404) for k in (dict(freq="daily"), dict(next_date="x"), dict(account_id=999), dict(category="Nope"), dict(end_date=iso(1)), dict(count=0))))
    s, r = api("POST", "/api/scheduled", S()); sid = r["id"]; check("create remind schedule", s == 200, r)
    L = api("GET", "/api/scheduled")[1]; check("listed as soon, nothing due", L["items"][0]["status"] == "soon" and L["due"] == 0 and L["items"][0]["category"] == "Rent", L)
    check("outside 30-day window hidden unless all=1", api("POST", "/api/scheduled", S(payee="Far", next_date=iso(90)))[0] == 200 and len(api("GET", "/api/scheduled")[1]["items"]) == 1 and len(api("GET", "/api/scheduled?all=1")[1]["items"]) == 2)
    # enter
    s, r = api("POST", f"/api/scheduled/{sid}/enter", {}); check("enter creates a transaction and advances", s == 200 and r["next_date"] == nx("monthly", iso(5)), r)
    check("balance reflects the entered bill", bal("Checking") == 90000)
    db = sqlite3.connect(E.db); check("transaction is in edit_log under the enter batch", db.execute("SELECT COUNT(*) FROM edit_log WHERE batch=? AND tbl='transactions'", (r["batch"],)).fetchone()[0] == 1 if "batch" in r else False, r)
    # one batch also contains the schedule update -> revert undoes both
    b = r["batch"]; rv = subprocess.run([sys.executable, "app.py", "--revert", b], env=E.env, cwd=HERE, capture_output=True, text=True)
    check("reverting the entry removes the transaction and restores the next date", rv.returncode == 0 and bal("Checking") == 100000 and api("GET", "/api/scheduled")[1]["items"][0]["next_date"] == iso(5), rv.stdout + rv.stderr)
    # amount override, skip
    s, r = api("POST", f"/api/scheduled/{sid}/enter", dict(amount="-123.45")); check("enter with a different amount", s == 200 and bal("Checking") == 100000 - 12345, r)
    s, r = api("POST", f"/api/scheduled/{sid}/skip", {}); check("skip advances without a transaction", s == 200 and bal("Checking") == 100000 - 12345 and r["next_date"] == nx("monthly", nx("monthly", iso(5))), r)
    # overdue and due counts
    s, r = api("POST", "/api/scheduled", S(payee="Late", next_date=iso(-3))); late = r["id"]
    L = api("GET", "/api/scheduled")[1]; check("overdue counted", L["overdue"] == 1 and L["due"] == 1, (L["due"], L["overdue"]))
    check("run does not enter remind items", api("POST", "/api/scheduled/run", {})[1]["entered"] == [] and bal("Checking") == 100000 - 12345)
    # update: moving date, editing amount
    s, r = api("PUT", f"/api/scheduled/{late}", dict(next_date=iso(2), amount="-50")); check("edit a schedule (partial body)", s == 200, r)
    it = next(i for i in api("GET", "/api/scheduled")[1]["items"] if i["id"] == late); check("edit applied", it["amount"] == -50 and it["next_date"] == iso(2) and it["payee"] == "Late", it)
    # auto-enter catch-up, count, end date
    s, r = api("POST", "/api/scheduled", dict(account_id=chk, payee="Gym", amount="-10", category="Utilities", freq="weekly", next_date=iso(-15), auto=True, count=3)); gym = r["id"]
    run = api("POST", "/api/scheduled/run", {})[1]; check("auto schedule catches up all 3 occurrences (count 3)", len(run["entered"]) == 3 and not api("GET", "/api/scheduled?all=1")[1]["items"][0] is None, run)
    n_gym = sqlite3.connect(E.db).execute("SELECT COUNT(*) FROM transactions WHERE payee_text='Gym'").fetchone()[0]
    check("dates are the occurrence dates, oldest first", [x["date"] for x in run["entered"]] == [iso(-15), iso(-8), iso(-1)], run)
    check("run again enters nothing", api("POST", "/api/scheduled/run", {})[1]["entered"] == [] and sqlite3.connect(E.db).execute("SELECT COUNT(*) FROM transactions WHERE payee_text='Gym'").fetchone()[0] == n_gym)
    s, r = api("POST", "/api/scheduled", dict(account_id=chk, payee="Trial", amount="-1", freq="weekly", next_date=iso(-20), end_date=iso(-6), auto=True))
    run = api("POST", "/api/scheduled/run", {})[1]; check("end date (inclusive) stops an auto schedule: -20, -13, -6", [x["date"] for x in run["entered"]] == [iso(-20), iso(-13), iso(-6)], run)
    tr = next(i for i in api("GET", "/api/scheduled?all=1")[1]["items"] if i["payee"] == "Trial"); check("ended schedule shows as ended and can't be entered", tr["status"] == "ended" and api("POST", f"/api/scheduled/{tr['id']}/enter", {})[0] == 409, tr)
    s, r = api("POST", "/api/scheduled", dict(account_id=chk, payee="Once", amount="-2", freq="once", next_date=iso(-1), auto=True)); run = api("POST", "/api/scheduled/run", {})[1]
    check("one-time schedule enters once then ends", len(run["entered"]) == 1 and api("POST", "/api/scheduled/run", {})[1]["entered"] == [])
    check("recent entries listed (not reverted ones)", len(api("GET", "/api/scheduled")[1]["recent"]) >= 3)
    # transfer + splits
    s, r = api("POST", "/api/scheduled", dict(account_id=chk, payee="Save", amount="-200", transfer="Savings", freq="monthly", next_date=iso(1))); tsid = r["id"]
    api("POST", f"/api/scheduled/{tsid}/enter", {}); check("transfer schedule moves money both ways", bal("Savings") == 20000, bal("Savings"))
    s, r = api("POST", "/api/scheduled", dict(account_id=chk, payee="Paycheck", amount="1000", freq="biweekly", next_date=iso(1), splits=[dict(category="Pay", amount="900"), dict(category="Pay", amount="100")]))
    check("split schedule created", s == 200, r); psid = r["id"]
    b0 = bal("Checking"); s, r = api("POST", f"/api/scheduled/{psid}/enter", {}); check("split schedule enters with its splits", s == 200 and bal("Checking") == b0 + 100000 and sqlite3.connect(E.db).execute("SELECT COUNT(*) FROM splits").fetchone()[0] >= 2, r)
    check("amount override refused for splits", api("POST", f"/api/scheduled/{psid}/enter", dict(amount="5"))[0] == 400)
    # category / account guards
    cid = next(c["id"] for c in api("GET", "/api/categories")[1]["categories"] if c["name"] == "Rent")
    s, r = api("DELETE", f"/api/categories/{cid}"); check("a category used only by a schedule can't be deleted", s == 409 and "schedul" in r["error"].lower(), r)
    s, r = api("PUT", f"/api/categories/{cid}", dict(name="Housing")); check("renaming follows into the schedule", s == 200 and any(i["category"] == "Housing" for i in api("GET", "/api/scheduled?all=1")[1]["items"]), r)
    s, r = api("DELETE", f"/api/accounts/{chk}"); check("account with schedules can't be deleted", s in (400, 409), r)
    s, r = api("DELETE", f"/api/scheduled/{sid}"); check("delete a schedule", s == 200 and all(i["id"] != sid for i in api("GET", "/api/scheduled?all=1")[1]["items"]), r)
finally: E.stop()
# ---- app start auto-enters (no daemon: it happens when the server starts)
E2 = Empty(8794); E2.start()
try:
    a = api2 = E2.api("POST", "/api/accounts", dict(name="Checking", type="Checking"))[1]["account_id"]
    E2.api("POST", "/api/scheduled", dict(account_id=a, payee="Auto", amount="-5", freq="weekly", next_date=iso(-8), auto=True))
finally: E2.stop()
E2.start()
try:
    n = sqlite3.connect(E2.db).execute("SELECT COUNT(*) FROM transactions WHERE payee_text='Auto'").fetchone()[0]
    check("auto items are entered at app start", n == 2, n)
finally: E2.stop()
sys.exit(0 if check.ok else 1)
