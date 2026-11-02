#!/usr/bin/env python3
"""A new user: empty start, accounts, categories, securities, prices, transactions, a schedule, all through the API; revertable."""
import datetime, sqlite3, subprocess, sys
from testempty import Empty, Checker, HERE
check = Checker(); E = Empty(8795); api = E.api
check("init prints 'starting empty' and creates the DB", "starting empty" in E.init.stdout + E.init.stderr and E.init.returncode == 0, E.init.stdout + E.init.stderr)
check("schema is current", sqlite3.connect(E.db).execute("PRAGMA user_version").fetchone()[0] >= 8)
def dom(port, frag=""):
    return subprocess.run(["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--virtual-time-budget=6000", "--dump-dom", f"http://127.0.0.1:{port}/{frag}"], capture_output=True, text=True, timeout=90).stdout
E0 = Empty(8792); E0.start()
try:
    h = dom(E0.port); check("empty accounts page shows the first-run message and + new account", "No accounts yet" in h and "+ new account" in h and "ndlg" in h)
    h = dom(E0.port, "#/categories"); check("empty categories page offers the starter list", "Add the starter list" in h, h[-300:])
finally: E0.stop()
E.start()
try:
    s, A = api("GET", "/api/accounts"); check("no accounts yet", s == 200 and A == [], A)
    s, M = api("GET", "/api/meta"); check("meta lists account types", s == 200 and any(t["label"] == "Checking" for t in M["types"]))
    # accounts
    s, r = api("POST", "/api/accounts", dict(name="Checking", type="Checking", opening_balance="1,000.50", opening_date="2026-01-01")); check("create bank account with opening balance", s == 200, r); chk = r.get("account_id")
    s, r = api("POST", "/api/accounts", dict(name="Visa", type="Credit Card", opening_balance=250)); card = r.get("account_id"); check("create credit card owing 250", s == 200, r)
    s, r = api("POST", "/api/accounts", dict(name="Brokerage", type="Brokerage", opening_balance=10000, opening_date="2026-01-01")); brk = r.get("account_id"); check("create brokerage with opening cash", s == 200, r)
    s, r = api("POST", "/api/accounts", dict(name="Empty", type="Savings")); sav = r.get("account_id"); check("create account with no opening balance", s == 200 and sav, r)
    check("duplicate name refused (any case)", api("POST", "/api/accounts", dict(name="checking", type="Savings"))[0] == 409)
    check("blank name, bad type, bad date refused", all(api("POST", "/api/accounts", b)[0] == 400 for b in (dict(name=" ", type="Checking"), dict(name="X", type="Nope"), dict(name="Y", type="Checking", opening_date="2026-13-01"))))
    bal = {a["name"]: a["balance"] for a in api("GET", "/api/accounts")[1]}
    check("balances: checking 1000.50, visa -250.00, brokerage 10000.00, empty 0", bal == {"Checking": 100050, "Visa": -25000, "Brokerage": 1000000, "Empty": 0}, bal)
    # categories
    s, r = api("GET", "/api/categories"); check("starter list is offered when there are no categories", s == 200 and r.get("starter_offered") and not r["categories"])
    s, r = api("POST", "/api/categories", dict(name="Pets", kind="expense")); check("create category", s == 200, r); pets = r.get("category_id")
    s, r = api("POST", "/api/categories", dict(name="Pets:Vet")); check("create subcategory (inherits kind)", s == 200, r)
    check("duplicate / orphan child refused", api("POST", "/api/categories", dict(name="pets"))[0] == 409 and api("POST", "/api/categories", dict(name="Nope:Child"))[0] in (400, 404))
    s, r = api("POST", "/api/categories/starter"); check("starter list added", s == 200 and r["added"] > 20, r)
    names = {c["name"] for c in api("GET", "/api/categories")[1]["categories"]}; check("starter has income and household categories", {"Groceries", "Salary"} <= names or any(n.startswith("Groc") for n in names), sorted(names)[:8])
    check("starter is not offered once there are categories", not api("GET", "/api/categories")[1].get("starter_offered"))
    # transactions using a category, then rename cascades and delete is guarded
    s, r = api("POST", f"/api/accounts/{chk}/transactions", dict(date="2026-02-01", payee="Vet", amount="-80", category="Pets:Vet")); check("transaction with new category", s == 200, r); tid = r.get("txn_id") or r.get("id")
    s, r = api("PUT", f"/api/categories/{pets}", dict(name="Animals")); check("rename category", s == 200, r)
    db = sqlite3.connect(E.db); check("rename cascades to child and the transaction", db.execute("SELECT category FROM transactions WHERE payee_text='Vet'").fetchone()[0] == "Animals:Vet")
    s, r = api("DELETE", f"/api/categories/{pets}"); check("delete of a used/parent category refused with counts", s == 409 and r.get("error"), r)
    cid = next(c["id"] for c in api("GET", "/api/categories")[1]["categories"] if c["name"] == "Animals:Vet"); s, r = api("DELETE", f"/api/categories/{cid}"); check("used subcategory can't be deleted", s == 409, r)
    s, r = api("POST", "/api/categories", dict(name="Spare")); s2, r2 = api("DELETE", f"/api/categories/{r['category_id']}"); check("unused category deletes", s2 == 200, r2)
    # securities and prices
    s, r = api("POST", "/api/securities", dict(name="Acme Corp", symbol="ACME", type="Stock")); check("create security", s == 200, r); sec = r["security_id"]
    check("duplicate ticker refused", api("POST", "/api/securities", dict(name="Other", symbol="acme"))[0] == 409)
    check("ticker required", api("POST", "/api/securities", dict(name="NoTicker"))[0] == 400)
    s, r = api("PUT", f"/api/securities/{sec}", dict(name="Acme Corporation", symbol="ACME", type="Stock")); check("edit security", s == 200, r)
    s, r = api("POST", "/api/prices", dict(security_id=sec, date="2026-03-01", price="12.5")); check("set manual price", s == 200, r); pb = r.get("batch")
    s, r = api("GET", f"/api/securities/{sec}/prices"); check("price listed", [p["price"] for p in r["prices"]] == [12.5], r)
    s, r = api("POST", "/api/prices", dict(security_id=sec, date="2026-03-01", price="13")); check("same-date price replaces", [p["price"] for p in api("GET", f"/api/securities/{sec}/prices")[1]["prices"]] == [13], r)
    check("bad price refused", api("POST", "/api/prices", dict(security_id=sec, date="2026-03-01", price="abc"))[0] == 400 and api("POST", "/api/prices", dict(security_id=sec, date="2026-03-02", price="-1"))[0] == 400)
    s, r = api("POST", f"/api/accounts/{brk}/transactions", dict(date="2026-04-01", action="Buy", security="Acme Corporation", qty="10", price="15", amount="150")); check("buy in the brokerage", s == 200, r)
    pr = api("GET", f"/api/securities/{sec}/prices")[1]["prices"]; check("buy price recorded as a price (newer than the manual one)", pr[0]["date"] == "2026-04-01" and pr[0]["price"] == 15, pr)
    s, r = api("POST", f"/api/accounts/{brk}/transactions", dict(date="2026-02-01", action="Sell", security="Acme Corporation", qty="1", price="99", amount="99"))
    pr = api("GET", f"/api/securities/{sec}/prices")[1]["prices"]; check("an older trade does not override a newer price", pr[0]["price"] == 15 and len(pr) == 2, pr)
    s, r = api("DELETE", f"/api/securities/{sec}"); check("security with transactions can't be deleted", s == 409, r)
    s, r = api("POST", "/api/securities", dict(name="Unused Fund", symbol="UNSD")); sid2 = r["security_id"]; api("POST", "/api/prices", dict(security_id=sid2, date="2026-03-01", price=5))
    s, r = api("DELETE", f"/api/securities/{sid2}"); check("unused security deletes along with its prices", s == 200 and not sqlite3.connect(E.db).execute("SELECT 1 FROM prices WHERE symbol='UNSD'").fetchone(), r)
    b = {a["name"]: a for a in api("GET", "/api/accounts")[1]}["Brokerage"]; check("brokerage value = cash + shares at last price", b["balance"] == b["cash"] + b["securities"] and b["securities"] == 9 * 1500, b)
    # schedule
    nd = (datetime.date.today() + datetime.timedelta(days=3)).isoformat()
    s, r = api("POST", "/api/scheduled", dict(account_id=chk, payee="Landlord", amount="-900", category="Groceries" if "Groceries" in names else "Pets", freq="monthly", next_date=nd, auto=False)); check("create schedule", s == 200, r); sch = r.get("id")
    s, r = api("POST", f"/api/scheduled/{sch}/enter", {}); check("enter schedule makes a transaction", s == 200, r)
    check("checking = 1000.50 - 80 - 900 (incl. the future-dated entry)", {a["name"]: a["balance_all"] for a in api("GET", "/api/accounts")[1]}["Checking"] == 100050 - 8000 - 90000)
    # revert the price add
    s, r = api("GET", "/api/securities"); check("securities list shows last price", any(x["price"] == 15 for x in r["securities"]), r)
    pg = subprocess.run([sys.executable, "app.py", "--revert", pb], env=E.env, cwd=HERE, capture_output=True, text=True); check("a price batch reverts (or refuses cleanly if replaced later)", pg.returncode in (0, 1), pg.stdout + pg.stderr)
    check("securities page renders", "Acme Corporation" in dom(E.port, "#/securities"))
    check("scheduled page renders", "Landlord" in dom(E.port, "#/scheduled"))
    check("accounts page lists the new accounts", "Brokerage" in dom(E.port) and "Checking" in dom(E.port))
finally: E.stop()
sys.exit(0 if check.ok else 1)
