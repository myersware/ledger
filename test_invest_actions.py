#!/usr/bin/env python3
"""Investment actions (ShrsIn/ShrsOut/StkSplit/Reinv*/CG*), lots, share transfers between accounts, cash transfers (invest<->invest, bank form), revert. Fresh empty ledger."""
import json, sqlite3, subprocess, sys
from testempty import Empty, Checker, HERE
check = Checker(); E = Empty(8791); api = E.api; E.start()
try:
    A = lambda n, t="Brokerage": api("POST", "/api/accounts", dict(name=n, type=t))[1]["account_id"]
    br, ira, chk = A("Broker"), A("IRA", "Roth IRA"), A("Checking", "Checking")
    api("POST", "/api/securities", dict(name="Acme Corp", symbol="ACME", type="Stock")); api("POST", "/api/securities", dict(name="Beta Fund", symbol="BETA", type="Mutual Fund"))
    I = lambda aid, **k: api("POST", f"/api/accounts/{aid}/transactions", dict(dict(date="2026-03-01"), **k))
    port = lambda: api("GET", "/api/portfolio?group=account")[1]
    def pos(acct, name=None):
        g = {x["account"]: x for x in port()["groups"]}.get(acct, dict(rows=[]))
        return {r["name"]: r for r in g["rows"]}
    cash = lambda aid: {a["account_id"]: a for a in api("GET", "/api/accounts")[1]}[aid]["cash"]  / 100
    # --- ShrsIn with cost and acquired date
    s, r = I(br, action="ShrsIn", security="Acme Corp", qty=10, amount=1000, acquired="2020-05-05", date="2026-03-01")
    check("ShrsIn with cost + acquired date", s == 200, r)
    p = pos("Broker")["Acme Corp"]; l = p["lots"][0]
    check("lot keeps the acquired date and cost; cash unchanged", l["date"] == "2020-05-05" and l["cost"] == 1000 and l["term"] == "long" and cash(br) == 0, (l, cash(br)))
    check("ShrsIn no cost -> no-basis lot", I(br, action="ShrsIn", security="Beta Fund", qty=4, date="2026-03-02")[0] == 200 and pos("Broker")["Beta Fund"]["nocost_lots"] == 1)
    check("ShrsIn validation (qty, acquired after date, bad date)", [I(br, action="ShrsIn", security="Acme Corp", qty=0)[0], I(br, action="ShrsIn", security="Acme Corp", qty=1, acquired="2027-01-01")[0],
          I(br, action="ShrsIn", security="Acme Corp", qty=1, acquired="x")[0]] == [400, 400, 400])
    # --- split 2:1 -> shares double, cost kept
    s, r = I(br, action="StkSplit", security="Acme Corp", ratio="2:1", date="2026-04-01")
    p = pos("Broker")["Acme Corp"]; check("2:1 split doubles shares, cost unchanged", s == 200 and p["shares"] == 20 and p["cost"] == 1000, (r, p["shares"], p["cost"]))
    check("split 1:10 reverse works, bad ratio refused", I(br, action="StkSplit", security="Beta Fund", ratio="1:2", date="2026-04-02")[0] == 200 and pos("Broker")["Beta Fund"]["shares"] == 2
          and I(br, action="StkSplit", security="Acme Corp", ratio="x")[0] == 400 and I(br, action="StkSplit", security="Acme Corp", ratio="0:1")[0] == 400)
    # --- ShrsOut takes the oldest lot, no cash
    s, r = I(br, action="ShrsOut", security="Acme Corp", qty=5, date="2026-05-01")
    p = pos("Broker")["Acme Corp"]; check("ShrsOut reduces shares and cost proportionally, no cash", s == 200 and p["shares"] == 15 and abs(p["cost"] - 750) < 1e-6 and cash(br) == 0, (r, p["shares"], p["cost"]))
    check("ShrsOut refuses price/amount; warns when over-selling", I(br, action="ShrsOut", security="Acme Corp", qty=1, price=3)[0] == 400
          and I(A("Tmp"), action="ShrsOut", security="Acme Corp", qty=999, date="2026-05-02")[1]["warnings"])
    # --- Reinv* and CG*
    for a in ("ReinvInt", "ReinvLg", "ReinvSh", "ReinvDiv"):
        s, r = I(br, action=a, security="Beta Fund", qty=1, price=10, date="2026-06-01"); check(f"{a} adds shares at cost, cash unchanged", s == 200, r)
    check("reinvested lots in portfolio (4 -> 2 after split + 4 reinvested = 6 shares, cost 40)", pos("Broker")["Beta Fund"]["shares"] == 6 and pos("Broker")["Beta Fund"]["cost"] == 40, pos("Broker")["Beta Fund"]["cost"])
    c0 = cash(br)
    I(br, action="CGLong", security="Acme Corp", amount=30, date="2026-06-02"); I(br, action="CGShort", security="Acme Corp", amount=20, date="2026-06-03")
    check("CGLong / CGShort add cash", abs(cash(br) - c0 - 50) < 1e-6, cash(br))
    check("CGLong needs security and amount; no qty", [I(br, action="CGLong", amount=5)[0], I(br, action="CGLong", security="Acme Corp")[0], I(br, action="CGLong", security="Acme Corp", amount=5, qty=1)[0]] == [400, 400, 400])
    # --- editing a ShrsIn keeps its meaning
    rows = api("GET", f"/api/accounts/{br}/register")[1]["rows"]; si = [x for x in rows if x["num"] == "ShrsIn" and x["security"] == "Acme Corp"][0]
    check("register exposes acquired date", si.get("acquired") == "2020-05-05", si)
    s, r = api("PUT", f"/api/transactions/{si['txn_id']}", dict(date="2026-03-01", action="ShrsIn", security="Acme Corp", qty=10, amount=1200, acquired="2019-01-01"))
    check("edit ShrsIn cost/acquired", s == 200 and pos("Broker")["Acme Corp"]["lots"][0]["date"] == "2019-01-01" and abs(pos("Broker")["Acme Corp"]["cost"] - 900) < 1e-6, (r, pos("Broker")["Acme Corp"]["cost"]))
    # --- share transfer keeps lot dates and cost
    api("POST", "/api/accounts", dict(name="Spare", type="Brokerage"))
    I(br, action="Buy", security="Acme Corp", qty=10, price=200, date="2026-07-01")   # second lot, 2000
    before = pos("Broker")["Acme Corp"]
    s, r = api("POST", "/api/investments/transfer-shares", dict(from_account=br, to_account=ira, security="Acme Corp", qty=18, date="2026-08-01"))
    check("transfer 18 shares across two lots", s == 200 and len(r["lots"]) == 2 and r["lots"][0]["acquired"] == "2019-01-01", r)
    a, b2 = pos("Broker")["Acme Corp"], pos("IRA")["Acme Corp"]
    check("source lost 18 shares, destination got 18 with original dates and cost split", a["shares"] == before["shares"] - 18 and b2["shares"] == 18 and [x["date"] for x in b2["lots"]] == ["2019-01-01", "2026-07-01"]
          and abs(b2["cost"] + a["cost"] - before["cost"]) < 0.01, (a, b2))
    check("transfer: more than held / same account / non-investment refused", [api("POST", "/api/investments/transfer-shares", dict(from_account=br, to_account=ira, security="Acme Corp", qty=500, date="2026-08-02"))[0],
          api("POST", "/api/investments/transfer-shares", dict(from_account=br, to_account=br, security="Acme Corp", qty=1, date="2026-08-02"))[0],
          api("POST", "/api/investments/transfer-shares", dict(from_account=br, to_account=chk, security="Acme Corp", qty=1, date="2026-08-02"))[0]] == [400, 400, 400])
    rows = api("GET", f"/api/accounts/{ira}/register")[1]["rows"]; g = [x for x in rows if x["num"] == "ShrsIn"]
    check("destination rows are grouped; editing refused (409), deleting removes both sides", g and g[0].get("group") and api("PUT", f"/api/transactions/{g[0]['txn_id']}", dict(date="2026-08-01", action="ShrsIn", security="Acme Corp", qty=1))[0] == 409)
    s, r = api("DELETE", f"/api/transactions/{g[0]['txn_id']}")
    check("delete moves everything back", s == 200 and "Acme Corp" not in pos("IRA") and pos("Broker")["Acme Corp"]["shares"] == before["shares"], (r, pos("IRA")))
    s, r = api("POST", "/api/investments/transfer-shares", dict(from_account=br, to_account=ira, security="Acme Corp", qty=3, date="2026-08-03", lot=si["txn_id"])); batch = r["batch"]
    check("transfer naming a lot uses that lot", s == 200 and r["lots"][0]["acquired"] == "2019-01-01", r)
    rv = subprocess.run([sys.executable, "app.py", "--revert", batch], env=E.env, cwd=HERE, capture_output=True, text=True)
    check("revert undoes the transfer", rv.returncode == 0 and "Acme Corp" not in pos("IRA"), rv.stdout + rv.stderr)
    # --- cash transfer between investment accounts, and from the bank form
    c1, c2 = cash(br), cash(ira)
    s, r = I(br, action="XOut", amount=100, transfer="IRA", date="2026-08-05")
    check("XOut to another investment account creates the XIn there", s == 200 and abs(cash(br) - (c1 - 100)) < 1e-6 and abs(cash(ira) - (c2 + 100)) < 1e-6, (r, cash(br), cash(ira)))
    s, r = api("PUT", f"/api/transactions/{r['txn_id']}", dict(date="2026-08-05", action="XOut", amount=150, transfer="IRA")); check("edit follows", s == 200 and abs(cash(ira) - (c2 + 150)) < 1e-6, r)
    mine = api("GET", f"/api/accounts/{ira}/register")[1]["rows"]; xin = [x for x in mine if x["num"] == "XIn"][0]
    check("the other side is editable too (plain cash pair)", not xin.get("locked") and api("PUT", f"/api/transactions/{xin['txn_id']}", dict(date="2026-08-05", action="XIn", amount=160, transfer="Broker"))[0] == 200 and abs(cash(br) - (c1 - 160)) < 1e-6)
    bal = lambda n: {a["name"]: a for a in api("GET", "/api/accounts")[1]}[n]["balance"] / 100
    c1, c2 = cash(br), bal("Checking")
    s, r = api("POST", f"/api/accounts/{chk}/transactions", dict(date="2026-08-06", amount=-300, payee="to broker", transfer="Broker"))
    check("bank form: transfer to an investment account makes a cash XIn", s == 200 and abs(cash(br) - (c1 + 300)) < 1e-6 and abs(bal("Checking") - (c2 - 300)) < 1e-6, (r, cash(br)))
    s, r = api("PUT", f"/api/transactions/{r['txn_id']}", dict(date="2026-08-06", amount=-200, payee="to broker", transfer="Broker")); check("bank-side edit follows", s == 200 and abs(cash(br) - (c1 + 200)) < 1e-6, r)
    check("bank-side delete removes both", api("DELETE", f"/api/transactions/{r['txn_id']}")[0] == 200 and abs(cash(br) - c1) < 1e-6)
    # --- scheduled investment transactions
    sh0 = pos("Broker")["Beta Fund"]["shares"]
    s, r = api("POST", "/api/scheduled", dict(account_id=br, freq="monthly", next_date="2026-09-01", inv=dict(action="Buy", security="Beta Fund", qty=2, price=10)))
    check("create an investment schedule", s == 200, r); sid = r["id"]
    check("investment schedule needs a valid body", [api("POST", "/api/scheduled", dict(account_id=br, freq="monthly", next_date="2026-09-01"))[0],
          api("POST", "/api/scheduled", dict(account_id=br, freq="monthly", next_date="2026-09-01", inv=dict(action="Buy", security="Beta Fund", qty=0, price=1)))[0]] == [400, 400])
    check("listed with its inv body", [x for x in api("GET", "/api/scheduled?all=1")[1]["items"] if x["id"] == sid][0]["inv"]["action"] == "Buy")
    c1 = cash(br); s, r = api("POST", f"/api/scheduled/{sid}/enter", {})
    check("enter makes the Buy (cash -20, +2 shares) and advances", s == 200 and r["next_date"] == "2026-10-01" and abs(cash(br) - (c1 - 20)) < 1e-6 and pos("Broker")["Beta Fund"]["shares"] == sh0 + 2, r)
    check("enter with another amount refused for investment schedules", api("POST", f"/api/scheduled/{sid}/enter", dict(amount=5))[0] == 400)
    rv = subprocess.run([sys.executable, "app.py", "--revert", r["batch"]], env=E.env, cwd=HERE, capture_output=True, text=True)
    check("revert undoes the investment entry", rv.returncode == 0 and pos("Broker")["Beta Fund"]["shares"] == sh0 and api("GET", "/api/scheduled?all=1")[1]["items"][0]["next_date"] == "2026-09-01", rv.stdout + rv.stderr)
    db = sqlite3.connect(E.db)
    check("every change is in edit_log", db.execute("SELECT COUNT(*) FROM edit_log WHERE tbl='transactions'").fetchone()[0] > 30)
    check("meta lists the new actions and investment accounts as cash transfer targets", {"ShrsIn", "ShrsOut", "StkSplit", "ReinvInt", "CGLong"} <= set(api("GET", "/api/meta")[1]["inv_actions"]) and "IRA" in api("GET", "/api/meta")[1]["bank_accounts"])
finally: E.stop()
sys.exit(0 if check.ok else 1)
