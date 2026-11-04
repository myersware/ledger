#!/usr/bin/env python3
"""Set a category for all of a payee's transactions: preview, skip transfers/splits/investment, backup, one batch, revertable."""
import os, sqlite3, subprocess, sys, urllib.parse
from testempty import Empty, Checker, HERE
check = Checker(); E = Empty(8792); api = E.api; E.start()
try:
    chk = api("POST", "/api/accounts", dict(name="Checking", type="Checking", opening_balance=1000, opening_date="2026-01-01"))[1]["account_id"]
    api("POST", "/api/accounts", dict(name="Savings", type="Savings"))
    for c in ("Food", "Fun"): api("POST", "/api/categories", dict(name=c))
    T = lambda **k: api("POST", f"/api/accounts/{chk}/transactions", dict(dict(date="2026-02-01", amount=-10, payee="Shop"), **k))
    T(); T(category="Fun"); T(category="Food"); T(transfer="Savings"); T(amount=-30, splits=[dict(amount=-20, category="Food"), dict(amount=-10, category="Fun")]); T(payee="Other")
    db = sqlite3.connect(E.db); tot = lambda: db.execute("SELECT COUNT(*), ROUND(SUM(amount),2) FROM transactions").fetchone()
    bal = lambda: sorted((a["name"], a["balance"]) for a in api("GET", "/api/accounts")[1])
    t0, b0, log0 = tot(), bal(), db.execute("SELECT COUNT(*) FROM edit_log").fetchone()[0]
    q = lambda **k: api("GET", "/api/payees/category?" + urllib.parse.urlencode(dict(dict(name="Shop", category="Food"), **k)))
    s, r = q()
    check("preview: 2 change, 1 already, 2 transfer sides, 1 split skipped", s == 200 and r["change"] == 2 and r["skipped"]["same"] == 1 and r["skipped"]["transfers"] == 2 and r["skipped"]["splits"] == 1, r)
    check("only uncategorized: 1 change", q(only_uncategorized="1")[1]["change"] == 1)
    check("preview read-only; unknown category / blank payee refused", db.execute("SELECT COUNT(*) FROM edit_log").fetchone()[0] == log0 and q(category="Nope")[0] == 400 and q(name="")[0] == 400)
    s, r = api("POST", "/api/payees/category", dict(name="Shop", category="Food"))
    check("apply: 2 changed, backup, one batch", s == 200 and r["changed"] == 2 and os.path.exists(r["backup"]) and db.execute("SELECT COUNT(DISTINCT batch) FROM edit_log WHERE id>?", (log0,)).fetchone()[0] == 1, r)
    cats = lambda: sorted(str(x[0]) for x in db.execute("SELECT category FROM transactions WHERE payee_text='Shop' AND transfer_account_id IS NULL AND COALESCE(split_count,0)=0"))
    check("all plain Shop rows are Food; transfer and split untouched; totals/balances same", cats() == ["Food"] * 3 and tot() == t0 and bal() == b0, cats())
    check("other payee untouched", db.execute("SELECT category FROM transactions WHERE payee_text='Other'").fetchone()[0] is None)
    rv = subprocess.run([sys.executable, "app.py", "--revert", r["batch"]], env=E.env, cwd=HERE, capture_output=True, text=True)
    check("undo restores categories", rv.returncode == 0 and cats() == ["Food", "Fun", "None"], (rv.stdout + rv.stderr, cats()))
finally: E.stop()
sys.exit(0 if check.ok else 1)
