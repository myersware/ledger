#!/usr/bin/env python3
"""Generic csv column mapping (single amount, debit+credit, date formats, repeat import), 'create this security/account' data paths, Plaid 'create ledger account'. Fresh empty ledger."""
import json, os, sys
from testempty import Empty, Checker
check = Checker(); E = Empty(8789); api = E.api; E.start()
try:
    imp = os.path.join(E.tmp, "imports"); os.makedirs(os.path.join(imp, "mybank")); os.makedirs(os.path.join(imp, "cu"))
    open(os.path.join(imp, "mybank", "mybank_2026-09.csv"), "w").write(
        "Posted,Description,Amount,Notes\n09/03/2026,Coffee Shop,-4.50,latte\n09/03/2026,Coffee Shop,-4.50,second\n09/05/2026,Paycheck,\"1,200.00\",\n09/06/2026,Refund,(20.00),parens\nbad,x,1,\n")
    open(os.path.join(imp, "cu", "cu_2026-09.csv"), "w").write("Date,Payee,Debit,Credit\n2026-09-02,Landlord,500.00,\n2026-09-04,Employer,,900.00\n")
    P = lambda: {x["slug"]: x for x in api("GET", "/api/import/preview")[1]}
    check("without a map the rows are skipped with a warning", P()["mybank"]["counts"]["total"] == 0 and any("no txn_id" in w or "bad date" in w for w in P()["mybank"]["warnings"]))
    s, r = api("GET", "/api/import/csv-columns?slug=mybank"); check("columns + sample offered for the mapping dialog", s == 200 and r["files"][0]["columns"] == ["Posted", "Description", "Amount", "Notes"] and len(r["files"][0]["sample"]) == 5 and r["map"] is None, r)
    check("map validation", [api("POST", "/api/import/csv-map", dict(slug="mybank", map=dict(date="Posted")))[0], api("POST", "/api/import/csv-map", dict(slug="mybank", map=dict(date="Nope", amount="Amount")))[0],
          api("POST", "/api/import/csv-map", dict(slug="../x", map=None))[0], api("POST", "/api/import/csv-map", dict(slug="mybank", map=dict(date="Posted", amount="Amount", date_format="zz")))[0]] == [400, 400, 400, 400])
    s, r = api("POST", "/api/import/csv-map", dict(slug="mybank", map=dict(date="Posted", payee="Description", amount="Amount", memo="Notes", date_format="mdy"))); check("save map", s == 200, r)
    s, r = api("POST", "/api/import/csv-map", dict(slug="cu", map=dict(date="Date", payee="Payee", debit="Debit", credit="Credit"))); check("save debit/credit map", s == 200, r)
    p = P()["mybank"]; check("unmapped account still needs mapping, but rows are read", p["counts"]["total"] == 4 and any("no account mapped" in w for w in p["warnings"]) and len([w for w in p["warnings"] if "bad date" in w]) == 1, p)
    chk = api("POST", "/api/accounts", dict(name="My Bank", type="Checking"))[1]["account_id"]; cu = api("POST", "/api/accounts", dict(name="CU", type="Checking"))[1]["account_id"]
    api("POST", "/api/import/mapping", dict(mappings=[dict(slug="mybank", account_id=chk), dict(slug="cu", account_id=cu)]))
    p = P()["mybank"]; amts = sorted(x["amount"] for x in p["rows"])
    check("single amount column: thousands separator, parentheses negative, repeated identical rows kept", p["counts"]["new"] == 4 and amts == [-20.0, -4.5, -4.5, 1200.0], (p["counts"], amts))
    check("debit/credit columns -> signed amount, iso dates", sorted((x["amount"], x["date"]) for x in P()["cu"]["rows"]) == [(-500.0, "2026-09-02"), (900.0, "2026-09-04")])
    s, r = api("POST", "/api/import/run", dict(slugs=["mybank", "cu"])); check("import runs", s == 200 and sum(a["inserted"] for a in r["accounts"]) == 6, r)
    bal = lambda n: {a["name"]: a["balance"] for a in api("GET", "/api/accounts")[1]}[n] / 100
    check("balances", bal("My Bank") == 1200 - 9 - 20 and bal("CU") == 400, (bal("My Bank"), bal("CU")))
    p = P()["mybank"]; check("importing the same files again finds only duplicates", p["counts"]["duplicate"] == 4 and p["counts"]["new"] == 0, p["counts"])
    open(os.path.join(imp, "mybank", "mybank_2026-10.csv"), "w").write("Posted,Description,Amount,Notes\n09/05/2026,Paycheck,\"1,200.00\",overlap\n10/01/2026,Rent,-800,\n")
    p = P()["mybank"]; check("an overlapping statement: the shared row is a duplicate, the new one is new", p["counts"]["new"] == 1 and p["counts"]["duplicate"] == 5, p["counts"])
    os.remove(os.path.join(imp, "mybank", "mybank_2026-09.csv")); open(os.path.join(imp, "mybank", "mybank_2026-11.csv"), "w").write("Other,Cols\n1,2\n")
    check("a file missing the mapped columns is skipped with a warning", any("no column" in w for w in P()["mybank"]["warnings"]))
    check("clear the map", api("POST", "/api/import/csv-map", dict(slug="cu", map=None))[0] == 200 and api("GET", "/api/import/csv-columns?slug=cu")[1]["map"] is None)
    # investment preview: unknown security is deferred with a reason the UI turns into 'create this security'
    br = api("POST", "/api/accounts", dict(name="Broker", type="Brokerage"))[1]["account_id"]; os.makedirs(os.path.join(imp, "brk"))
    open(os.path.join(imp, "brk", "brk_2026-09.csv"), "w").write("date,payee,amount,memo,category,txn_id,account,source,type,security,quantity,price\n2026-09-15,Newco,-100.00,,,b1,brk,plaid,buy,NEWC,2,50\n")
    api("POST", "/api/import/mapping", dict(mappings=[dict(slug="brk", account_id=br)]))
    row = P()["brk"]["rows"][0]; check("unknown security -> deferred, names it", row["status"] == "deferred" and "unknown security: NEWC" in row["reason"] and row["security"] == "NEWC", row)
    api("POST", "/api/securities", dict(name="NEWC", symbol="NEWC")); check("after creating the security the row is new", P()["brk"]["rows"][0]["status"] == "new")
    # plaid: create a ledger account from a plaid account
    items = dict(items=dict(it1=dict(institution="Test Bank", cursor="x", accounts={"pa1": dict(name="Plaid Checking", mask="1234", type="depository", subtype="checking", ledger_account_id=None),
        "pa2": dict(name="Plaid Brokerage", mask="9", type="investment", subtype="roth", ledger_account_id=None), "pa3": dict(name="Plaid Card", mask="5", type="credit", subtype="credit card", ledger_account_id=None)})))
    f = os.path.join(os.path.dirname(E.db), "plaid_items.json"); open(f, "w").write(json.dumps(items)); os.chmod(f, 0o600)
    s, r = api("POST", "/api/plaid/create_account", dict(item_id="it1", plaid_account_id="pa1")); check("create ledger account from a plaid account", s == 200 and r["type"] == "Checking" and r["name"] == "Plaid Checking", r)
    acc = {a["name"]: a for a in api("GET", "/api/accounts")[1]}
    check("account exists, typed, with the institution, and is linked", "Plaid Checking" in acc and [a for a in api("GET", "/api/plaid")[1]["items"][0]["accounts"] if a["plaid_account_id"] == "pa1"][0]["ledger_account"] == "Plaid Checking")
    check("investment and credit accounts get the right types", api("POST", "/api/plaid/create_account", dict(item_id="it1", plaid_account_id="pa2"))[1]["type"] == "Roth IRA" and api("POST", "/api/plaid/create_account", dict(item_id="it1", plaid_account_id="pa3"))[1]["type"] == "Credit Card")
    check("already linked / unknown plaid account refused", [api("POST", "/api/plaid/create_account", dict(item_id="it1", plaid_account_id="pa1"))[0], api("POST", "/api/plaid/create_account", dict(item_id="it1", plaid_account_id="zz"))[0]] == [409, 404])
finally: E.stop()
sys.exit(0 if check.ok else 1)
