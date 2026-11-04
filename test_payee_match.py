#!/usr/bin/env python3
"""Merge payees matching a substring: read-only preview, guard, one logged batch with a backup, revertable."""
import os, sqlite3, subprocess, sys, urllib.parse
from testempty import Empty, Checker, HERE
check = Checker(); E = Empty(8791); api = E.api; E.start()
try:
    chk = api("POST", "/api/accounts", dict(name="Checking", type="Checking", opening_balance=1000, opening_date="2026-01-01"))[1]["account_id"]
    T = lambda p, a: api("POST", f"/api/accounts/{chk}/transactions", dict(date="2026-02-01", amount=a, payee=p))
    for p, a in (("#1 ATM 100 Elm St", -20), ("#2 atm 100 elm st", -40), ("#3 ATM 100 ELM ST", -60), ("Safeway", -5)): T(p, a)
    db = sqlite3.connect(E.db); tot = lambda: db.execute("SELECT COUNT(*), ROUND(SUM(amount),2) FROM transactions").fetchone()
    bal = lambda: {a["name"]: a["balance"] for a in api("GET", "/api/accounts")[1]}["Checking"]
    t0, b0, log0 = tot(), bal(), db.execute("SELECT COUNT(*) FROM edit_log").fetchone()[0]
    s, r = api("GET", "/api/payees/match?q=" + urllib.parse.quote("ATM 100 elm"))
    check("preview: 3 matches, 3 txns, case-insensitive", s == 200 and len(r["payees"]) == 3 and r["txns"] == 3, r)
    check("preview is read-only", db.execute("SELECT COUNT(*) FROM edit_log").fetchone()[0] == log0 and tot() == t0)
    check("short / empty substring refused", api("GET", "/api/payees/match?q=at")[0] == 400 and api("POST", "/api/payees/match-merge", dict(q=" ", into="x"))[0] == 400)
    check("no matches refused", api("POST", "/api/payees/match-merge", dict(q="zzzz", into="x"))[0] == 400)
    s, r = api("POST", "/api/payees/match-merge", dict(q="atm 100 ELM", into="ATM Downtown"))
    check("merge: 3 payees, 3 rows, one batch, backup made", s == 200 and r["payees_merged"] == 3 and r["rows_changed"] == 3 and os.path.exists(r["backup"]), r)
    check("after merge: no old names, 3 on new, totals and balance unchanged",
          db.execute("SELECT COUNT(*) FROM transactions WHERE payee_text LIKE '%elm%'").fetchone()[0] == 0 and
          db.execute("SELECT COUNT(*) FROM transactions WHERE payee_text='ATM Downtown'").fetchone()[0] == 3 and tot() == t0 and bal() == b0)
    check("one batch in edit_log", db.execute("SELECT COUNT(DISTINCT batch) FROM edit_log WHERE id>?", (log0,)).fetchone()[0] == 1)
    T("ATM 100 elm again", -1); s, r2 = api("POST", "/api/payees/match-merge", dict(q="100 elm", into="ATM Downtown"))
    check("merging into an existing name reuses that payee", s == 200 and db.execute("SELECT COUNT(*) FROM payees WHERE name='ATM Downtown'").fetchone()[0] == 1, r2)
    for b in (r2["batch"], r["batch"]): rv = subprocess.run([sys.executable, "app.py", "--revert", b], env=E.env, cwd=HERE, capture_output=True, text=True)
    check("undo restores the old names", rv.returncode == 0 and db.execute("SELECT COUNT(*) FROM transactions WHERE payee_text LIKE '%elm%'").fetchone()[0] == 4 and bal() == b0 - 100, rv.stdout + rv.stderr)
finally:
    E.stop()
sys.exit(0 if check.ok else 1)
