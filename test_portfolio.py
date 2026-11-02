#!/usr/bin/env python3
"""Portfolio view test (made-up fixture, scratch copy): FIFO lots, totals vs account balances/holdings, hidden toggle, stale flag, nothing written, page renders."""
import datetime, json, os, shutil, sqlite3, subprocess, sys, tempfile, time, urllib.request
import testfix; T = testfix.make()
import core, portfolio

tmp = tempfile.mkdtemp(); scratch = os.path.join(tmp, "s.sqlite"); PORT = 8794
s = sqlite3.connect(core.DB_PATH); d = sqlite3.connect(scratch); s.backup(d); d.execute("UPDATE accounts SET hidden=0"); d.commit(); d.close(); s.close()
p = subprocess.Popen([sys.executable, "app.py", "--port", str(PORT)], env=dict(os.environ, LEDGER_DB=scratch), cwd=core.HERE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT); time.sleep(1.5)
def call(method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r: return json.loads(r.read())
ok = True
def check(n, c, det=""):
    global ok; ok &= bool(c); print(("PASS " if c else "FAIL ") + n + ("" if c else f"  {det}"))
def tx(i, date, act, q=None, amt=None, price=None):
    return dict(txn_id=i, date=date, action=act, security_name="X", amount=amt, invst_json=json.dumps({k: v for k, v in (("Q", q), ("I", price)) if v is not None}))
try:
    # FIFO on hand-made rows: buy 10 @100, buy 5 @120, sell 12 (oldest first), 2:1 split, transfer out 1, oversell by 3 then buy 4 (covers the debt first)
    L, debt = portfolio.fifo_lots([tx(1, "2024-01-01", "Buy", 10, 1000), tx(2, "2024-06-01", "Buy", 5, 600), tx(3, "2024-07-01", "Sell", 12, 1500),
                                   tx(4, "2024-08-01", "StkSplit", 20), tx(5, "2024-09-01", "ShrsOut", 1)])["X"]
    check("FIFO: sell uses oldest lot first, split doubles shares, cost kept, transfer-out takes from the remaining lot",
          [(l[0], float(l[1]), float(l[2])) for l in L] == [("2024-06-01", 5.0, 300.0)] and debt == 0, L)
    L, debt = portfolio.fifo_lots([tx(1, "2024-01-01", "Buy", 2, 20), tx(2, "2024-02-01", "Sell", 5, 50), tx(3, "2024-03-01", "Buy", 4, 80, None)])["X"]
    check("FIFO: oversell becomes debt, next buy covers it first (lots sum = net shares)", float(sum(l[1] for l in L)) - float(debt) == 1.0 and [float(l[1]) for l in L] == [1.0] and float(L[0][2]) == 20.0, (L, debt))
    L, _ = portfolio.fifo_lots([tx(1, "2024-01-01", "ShrsIn", 3), tx(2, "2024-01-02", "ShrsIn", 2, None, 5.0)])["X"]
    check("FIFO: no amount and no price -> no cost (None); price*qty used when only price", L[0][2] is None and float(L[1][2]) == 10.0, L)
    def txl(i, date, act, q, L=None):
        r = tx(i, date, act, q); r["invst_json"] = json.dumps({"Q": q, **({"L": L} if L else {})}); return r
    L, debt = portfolio.fifo_lots([tx(1, "2010-05-05", "Buy", 10, 12000), tx(2, "2010-07-20", "Buy", 10, 12500), txl(3, "2022-02-01", "Sell", 10, L=2)])["X"]
    check("lot key L: the sale takes the named (newer) lot, the older lot stays open with its cost", [(l[0], float(l[1]), float(l[2]), l[3]) for l in L] == [("2010-05-05", 10.0, 12000.0, 1)] and debt == 0, L)
    L, debt = portfolio.fifo_lots([tx(1, "2010-05-05", "Buy", 10, 12000), tx(2, "2010-07-20", "Buy", 4, 4000), txl(3, "2022-02-01", "Sell", 10, L=2)])["X"]
    check("lot key L: shares beyond the named lot come from the oldest lot", [(l[0], float(l[1]), float(l[2])) for l in L] == [("2010-05-05", 4.0, 4800.0)] and debt == 0, L)
    L, debt = portfolio.fifo_lots([tx(1, "2010-05-05", "Buy", 10, 12000), tx(2, "2010-07-20", "Buy", 10, 12500), txl(3, "2022-02-01", "Sell", 10, L=99)])["X"]
    check("lot key L: an unknown lot id falls back to oldest first", [(l[0], float(l[1])) for l in L] == [("2010-07-20", 10.0)] and debt == 0, L)
    check("holding period by date: 1 year exactly is short, a day later long", portfolio.term("2025-01-01", "2026-01-01") == "short" and portfolio.term("2025-01-01", "2026-01-02") == "long")

    today = datetime.date.today().isoformat()
    for group in ("security", "account"):
        P = call("GET", f"/api/portfolio?group={group}")
        A = {a["account_id"]: a for a in call("GET", "/api/accounts")}
        inv = [a for a in A.values() if a["inv"] and not a["hidden"]]
        T_ = P["totals"]
        check(f"{group}: total value = sum of investment account balances (securities + cash)", abs(T_["value"] - sum(a["balance"] for a in inv) / 100) < 0.01, (T_["value"], sum(a["balance"] for a in inv) / 100))
        check(f"{group}: securities total = sum of account securities, cash = sum of account cash", abs(T_["securities"] - sum(a["securities"] for a in inv) / 100) < 0.01 and abs(T_["cash"] - sum(a["cash"] for a in inv) / 100) < 0.01)
        check(f"{group}: % of portfolio adds to 100", abs(sum(r["pct"] for r in P["rows"]) + sum(c["pct"] for c in P["cash"]) - 100) < 0.01)
        check(f"{group}: lot shares sum to the row's shares; lot basis sums to the row's basis", all(abs(sum(l["shares"] for l in r["lots"]) - r["shares"]) < 1e-6 and abs(sum(l["cost"] for l in r["lots"]) - r["cost"]) < 0.01 for r in P["rows"]))
        check(f"{group}: gain = value - basis, lots carry term short/long", all(abs(r["gain"] - (r["value"] - r["cost"])) < 0.01 and all(l["term"] in ("short", "long") for l in r["lots"]) for r in P["rows"]))
        if group == "account":
            for g in P["groups"]:
                reg = call("GET", f"/api/accounts/{g['account_id']}/register?limit=1")
                hold = {n: sh for n, sh in reg["holdings"]}
                check(f"account {g['account']}: shares per security = register Holdings line; value = balance securities", {r["name"]: r["shares"] for r in g["rows"]} == {n: sh for n, sh in hold.items() if abs(sh) > 1e-6}
                      and abs(sum(r["value"] for r in g["rows"]) - A[g["account_id"]]["securities"] / 100) < 0.01, (hold, g["rows"]))
        else:
            sec = [r for r in P["rows"] if len(r["accounts"]) > 1]
            check("security: one row per security, shared across accounts when held in two", sec and all(len({l["account"] for l in r["lots"]}) == len(r["accounts"]) for r in sec), [r["name"] for r in P["rows"]])
    db = core.connect(); last = max(r[0] for r in db.execute("SELECT date FROM prices")); ld = datetime.date.fromisoformat(last)
    fresh = portfolio.build(db, ld.isoformat()); old = portfolio.build(db, (ld + datetime.timedelta(days=8)).isoformat()); edge = portfolio.build(db, (ld + datetime.timedelta(days=7)).isoformat()); db.close()
    check("stale flag: prices 0 days old fine, 7 days fine, 8 days flagged", not fresh["totals"]["stale"] and not edge["totals"]["stale"] and old["totals"]["stale"] == len(old["rows"]) > 0)
    # hidden toggle
    hid = [a for a in call("GET", "/api/accounts") if a["inv"]][0]["account_id"]
    before = call("GET", "/api/portfolio")["totals"]["value"]
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/accounts/{hid}/hidden", method="POST", data=b'{"hidden":true}', headers={"Content-Type": "application/json"}); urllib.request.urlopen(req).read()
    after = call("GET", "/api/portfolio")["totals"]["value"]; shown = call("GET", "/api/portfolio?hidden=1")["totals"]["value"]
    check("hidden account excluded by default, back with hidden=1", after < before and abs(shown - before) < 0.01, (before, after, shown))
    # read-only
    h0 = sqlite3.connect(scratch).execute("SELECT COUNT(*), SUM(amount) FROM transactions").fetchone(); call("GET", "/api/portfolio?group=account&hidden=1")
    check("nothing written", sqlite3.connect(scratch).execute("SELECT COUNT(*), SUM(amount) FROM transactions").fetchone() == h0 and not sqlite3.connect(scratch).execute("SELECT 1 FROM edit_log WHERE tbl='portfolio'").fetchall())
    dom = subprocess.run(["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--virtual-time-budget=6000", "--dump-dom", f"http://127.0.0.1:{PORT}/#/portfolio?expand"], capture_output=True, text=True, timeout=90).stdout
    check("page renders: table, totals row, expanded lot rows, controls", all(x in dom for x in ("Portfolio <span", "Alpha Fund", "Lot (account)", "expand all lots", "% of portfolio", ">Total<")), dom[-300:])
finally:
    p.terminate(); shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "FAILURES"); sys.exit(0 if ok else 1)
