#!/usr/bin/env python3
"""Payee default category: set by the category apply / default endpoint, shown in the list, used for uncategorized import rows only."""
import os, sqlite3, sys
from testempty import Empty, Checker
check = Checker(); E = Empty(8793); api = E.api; E.start()
try:
    chk = api("POST", "/api/accounts", dict(name="Checking", type="Checking", opening_balance=1000, opening_date="2026-01-01"))[1]["account_id"]
    api("POST", "/api/accounts", dict(name="Savings", type="Savings"))
    for c in ("Food", "Fun", "Auto", "Auto:Fuel"): api("POST", "/api/categories", dict(name=c))
    T = lambda **k: api("POST", f"/api/accounts/{chk}/transactions", dict(dict(date="2026-02-01", amount=-10, payee="Corner Cafe"), **k))
    T(category="Fun"); T(category="Fun"); T(transfer="Savings")
    P = lambda: {p["name"]: p for p in api("GET", "/api/payees")[1]["payees"]}
    check("no default yet; hint is the most common category", P()["Corner Cafe"]["default_category"] is None and P()["Corner Cafe"]["hint_category"] == "Fun", P()["Corner Cafe"])
    s, r = api("POST", "/api/payees/category", dict(name="Corner Cafe", category="Food"))
    check("apply sets the default too; transfer skipped", s == 200 and r["changed"] == 2 and r["skipped"]["transfers"] == 2 and P()["Corner Cafe"]["default_category"] == "Food", r)
    check("set default for a payee with no payee row / unknown category refused", api("PUT", "/api/payees/default", dict(name="Gas Station", category="Food"))[0] == 200 and
          api("PUT", "/api/payees/default", dict(name="Gas Station", category="Nope"))[0] == 400 and P()["Gas Station"]["default_category"] == "Food")
    imp = os.path.join(E.tmp, "imports", "bank"); os.makedirs(imp, exist_ok=True)
    open(os.path.join(imp, "bank_2026-09.csv"), "w").write("date,payee,amount,memo,category,txn_id,account,source\n"
        "2026-09-03,corner  CAFE,-4.50,,GENERAL_MERCHANDISE_OTHER,X1,bank,plaid\n2026-09-04,Gas Station,-30,,TRANSPORTATION_GAS,X2,bank,plaid\n2026-09-05,Nobody,-1,,,X3,bank,plaid\n")
    api("POST", "/api/import/mapping", dict(mappings=[dict(slug="bank", account_id=chk)])); api("POST", "/api/import/confirm", dict(slug="bank"))
    rows = {x["payee"]: x for p in api("GET", "/api/import/preview")[1] if p["slug"] == "bank" for x in p["rows"]}
    check("preview: normalized payee gets its default, marked 'from payee default'", rows["corner  CAFE"]["category"] == "Food" and rows["corner  CAFE"]["category_source"] == "from payee default", rows)
    check("a source category is never overridden by the default", rows["Gas Station"]["category"] == "Auto:Fuel" and rows["Gas Station"]["category_source"] == "plaid-map", rows["Gas Station"])
    check("no default, no source -> uncategorized", rows["Nobody"]["category"] is None)
    db = sqlite3.connect(E.db)
    check("transfer row untouched", db.execute("SELECT category, transfer_account FROM transactions WHERE payee_text='Corner Cafe' AND transfer_account_id IS NOT NULL AND account_id=?", (chk,)).fetchone() == (None, "Savings"))
    s, r = api("PUT", "/api/payees/default", dict(name="Corner Cafe", category=""))
    check("blank clears the default only", s == 200 and P()["Corner Cafe"]["default_category"] is None and db.execute("SELECT COUNT(*) FROM transactions WHERE payee_text='Corner Cafe' AND category='Food'").fetchone()[0] == 2)
    rows = {x["payee"]: x for p in api("GET", "/api/import/preview")[1] if p["slug"] == "bank" for x in p["rows"]}
    check("after clearing, the import falls back to payee history", rows["corner  CAFE"]["category_source"] != "from payee default", rows["corner  CAFE"])
    s, r = api("GET", "/api/payees/transactions?name=Corner%20Cafe")
    check("payee transactions across accounts (both transfer sides), with account and total", s == 200 and r["count"] == 4 and {x["account"] for x in r["rows"]} == {"Checking", "Savings"} and r["total"] == -20, r)
    check("payee transactions: blank name refused", api("GET", "/api/payees/transactions?name=")[0] == 400)
finally: E.stop()
sys.exit(0 if check.ok else 1)
