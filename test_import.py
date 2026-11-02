#!/usr/bin/env python3
"""CSV import test: a made-up database plus sample CSV files generated here (from rows that exist in it, so 'matched' rows match by construction).
Preview, mapping, import, balances, holdings, revert - on a scratch copy; the fixture DB is never written."""
import csv, json, os, shutil, sqlite3, subprocess, sys, tempfile, time, urllib.request
import testfix; T = testfix.make()  # made-up start database -> throwaway working DB (no real data needed)
import core

tmp = tempfile.mkdtemp(); scratch = os.path.join(tmp, "s.sqlite"); imp = T.imports; PORT = 8797
s = sqlite3.connect(core.DB_PATH); d = sqlite3.connect(scratch); s.backup(d); d.close(); s.close()
fx = sqlite3.connect(T.db); fx.row_factory = sqlite3.Row
def old(aid, where, n):  # existing fixture rows to echo back as 'matched' file rows
    return fx.execute(f"SELECT * FROM transactions WHERE account_id=? AND {where} ORDER BY date, txn_id LIMIT ?", (aid, n)).fetchall()
def write(slug, rows, inv=False):
    os.makedirs(os.path.join(imp, slug)); h = ["date", "payee", "amount", "memo", "category", "txn_id", "account", "source"] + (["type", "security", "quantity", "price"] if inv else [])
    with open(os.path.join(imp, slug, f"{slug}_2026-09.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(h)
        for i, r in enumerate(rows): w.writerow([r[0], r[1], r[2], "", "" if inv else r[3], f"{slug}-{i}" if inv else r[4], slug, "plaid"] + (list(r[3:7]) if inv else []))
# bank file: 3 rows that exist in Main Checking (-> matched), 4 new, one txn_id repeated (-> duplicate)
G = old(1, "date LIKE '2026-09-%' AND category='Groceries' AND split_count=0", 3)
write("checking-a", [(g["date"], g["payee_text"], f"{g['amount']:.2f}", "FOOD_AND_DRINK_GROCERIES", f"M-{i}") for i, g in enumerate(G)] + [
    ("2026-09-10", "Corner Grocery", "-12.34", "FOOD_AND_DRINK_GROCERIES", "T-new-1"),      # plaid-map -> Groceries
    ("2026-09-11", "Streaming Co", "-5.00", "ENTERTAINMENT_OTHER_ENTERTAINMENT", "T-new-2"),  # payee history -> Entertainment
    ("2026-09-12", "Mystery Shop", "-7.77", "GENERAL_MERCHANDISE_OTHER", "T-new-3"),          # uncategorized
    ("2026-09-12", "Mystery Shop", "-7.77", "GENERAL_MERCHANDISE_OTHER", "T-new-3"),          # same txn_id twice -> duplicate
    ("2026-09-12", "Streaming Co", "-12.99", "ENTERTAINMENT_OTHER_ENTERTAINMENT", "T-new-4")])
write("card-b", [(r["date"], r["payee_text"], f"{r['amount']:.2f}", "", f"V-{i}") for i, r in enumerate(old(4, "date LIKE '2026-09-%'", 2))] + [("2026-09-20", "Gas Station", "-33.10", "TRANSPORTATION_GAS", "V-new")])
B = old(5, "date LIKE '2026-09-%' AND action='Buy' AND security_name='Alpha Fund'", 1)[0]
sweep = lambda dt, amt: (dt, "Cash Sweep", f"{-amt:.2f}", "buy", "CSWP", f"{amt}", "1")  # a money-market sweep leg: price 1, quantity == dollars
write("brokerage-c", [
    (B["date"], "Alpha Fund", f"{-B['amount']:.2f}", "buy", "ALPHX", f"{json.loads(B['invst_json'])['Q']}", f"{json.loads(B['invst_json'])['I']}"),                  # matches the existing Buy
    sweep("2026-09-28", 4.2), ("2026-09-28", "Beta Fund", "4.20", "cash", "BETAX", "0", "0"),                                       # sweep + cash = Div BETAX, matches the existing one
    ("2026-09-15", "Alpha Fund", "-140.00", "buy", "ALPHX", "7", "20"),                                                              # lone Buy
    ("2026-09-16", "Beta Fund", "-10.00", "buy", "BETAX", "0.4", "25"), ("2026-09-16", "Beta Fund", "10.00", "cash", "BETAX", "0", "0"),   # buy + cash, same security = ReinvDiv
    sweep("2026-09-17", 10), ("2026-09-17", "Alpha Fund", "10.00", "cash", "ALPHX", "0", "0"),                                       # sweep + cash = Div ALPHX
    ("2026-09-18", "Cash Sweep", "300.00", "sell", "CSWP", "-300", "1"), ("2026-09-18", "", "-300.00", "cash", "", "0", "0"),         # sweep sell + blank cash = XOut
    ("2026-09-19", "Fee", "-15.00", "fee", "", "0", "0"),
    ("2026-09-20", "Mystery", "-50.00", "buy", "ZZZZ", "1", "50"),                                                                   # unknown security -> deferred
    ("2026-09-21", "Odd", "-5.00", "transfer", "", "0", "0")], inv=True)                                                             # unsupported type -> deferred
write("ira-d", [sweep("2026-09-28", 10), ("2026-09-28", "Alpha Fund", "10.00", "cash", "ALPHX", "0", "0")], inv=True)               # matches the IRA's Div
env = dict(os.environ, LEDGER_DB=scratch, LEDGER_IMPORTS=imp); procs = []
def start():
    p = subprocess.Popen([sys.executable, "app.py", "--port", str(PORT)], env=env, cwd=core.HERE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    procs.append(p); time.sleep(1.5)
def call(method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())
def q(sql, *a):
    db = sqlite3.connect(scratch); r = db.execute(sql, a).fetchall(); db.close(); return r
def bals():
    return {a["account_id"]: (a["balance"], a["balance_all"], a["cash"], a["securities"]) for a in call("GET", "/api/accounts")[1]}
ok = True
def check(n, c, det=""):
    global ok; ok &= bool(c); print(("PASS " if c else "FAIL ") + n + ("" if c else f"  {det}"))
try:
    start()
    ACCT = {"checking-a": 1, "card-b": 4, "brokerage-c": 5, "ira-d": 6}; INV = (5, 6); FID = 5
    check("fresh install: no mapping yet; preview lists every folder, unmapped", call("GET", "/api/import/mapping")[1] == [] and all("no account mapped" in " ".join(p["warnings"]) for p in call("GET", "/api/import/preview")[1]))
    d = sqlite3.connect(scratch)
    d.executemany("INSERT INTO import_map(slug, account_id, confirmed) VALUES(?,?,0)", ACCT.items()); d.commit(); d.close()  # mapped but not confirmed
    st, M = call("GET", "/api/import/mapping")
    check("4 slugs mapped, none confirmed", len(M) == 4 and not any(m["confirmed"] for m in M), M)
    base = bals()
    hold = lambda: dict(call("GET", f"/api/accounts/{FID}/register?limit=1")[1]["holdings"])
    h0 = hold(); n0 = q("SELECT COUNT(*) FROM transactions")[0][0]; e0 = q("SELECT COALESCE(MAX(id),0) FROM edit_log")[0][0]
    st, pv = call("GET", "/api/import/preview"); P = {p["slug"]: p for p in pv}
    c = P["checking-a"]["counts"]
    check("preview: checking new=4 matched=3 duplicate=1 (8 rows)", (c["total"], c["new"], c["matched"], c["duplicate"]) == (8, 4, 3, 1), c)
    c = P["card-b"]["counts"]; check("preview: card new=1 matched=2", (c["new"], c["matched"]) == (1, 2), c)
    fc = P["brokerage-c"]["counts"]
    check("preview: brokerage 13 file rows -> 9 transactions: new 5, matched 2, deferred 2", (fc["total"], fc["proposed"], fc["new"], fc["matched"], fc["deferred"]) == (13, 9, 5, 2, 2), fc)
    check("preview: IRA 2 file rows -> 1 matched", (P["ira-d"]["counts"]["total"], P["ira-d"]["counts"]["matched"]) == (2, 1), P["ira-d"]["counts"])
    fr = {(r["date"], r["type"]): r for r in P["brokerage-c"]["rows"] if r["status"] in ("new", "deferred")}
    check("pairs collapsed: ReinvDiv / Div(ALPHX) / XOut / fee / unknown security / unsupported",
          fr[("2026-09-16", "ReinvDiv")]["status"] == "new" and fr[("2026-09-17", "Div")]["security"] == "ALPHX" and fr[("2026-09-18", "XOut")]["amount"] == 300 and fr[("2026-09-19", "MiscExp")]["status"] == "new"
          and fr[("2026-09-20", "Buy")]["status"] == "deferred" and "unknown security" in fr[("2026-09-20", "Buy")]["reason"] and fr[("2026-09-21", "?")]["status"] == "deferred", list(fr))
    nw = {r["txn_id"]: r for r in P["checking-a"]["rows"] if r["status"] == "new"}
    check("category: plaid-map / payee-history / uncategorized",
          (nw["T-new-1"]["category"], nw["T-new-1"]["category_source"]) == ("Groceries", "plaid-map") and (nw["T-new-2"]["category"], nw["T-new-2"]["category_source"]) == ("Entertainment", "payee-history") and nw["T-new-3"]["category"] is None, nw)
    check("preview wrote nothing", q("SELECT COUNT(*) FROM transactions")[0][0] == n0 and q("SELECT COALESCE(MAX(id),0) FROM edit_log")[0][0] == e0)
    st, r = call("POST", "/api/import/run", {})
    check("import refused until mapping confirmed (409), nothing written", st == 409 and q("SELECT COALESCE(MAX(id),0) FROM edit_log")[0][0] == e0, (st, r))
    st, r = call("POST", "/api/import/mapping", {"mappings": [{"slug": m["slug"], "account_id": m["account_id"]} for m in M]})
    check("mapping confirmed & persisted", st == 200 and all(m["confirmed"] for m in r), r)
    st, bad = call("POST", "/api/import/mapping", {"mappings": [{"slug": "x", "account_id": 99999}]})
    check("mapping to unknown account rejected", st == 400)
    st, run = call("POST", "/api/import/run", {})
    check("import ok, one batch", st == 200 and run["batch"], run)
    b = run["batch"]
    check("exactly one edit_log batch added", [r[0] for r in q("SELECT DISTINCT batch FROM edit_log WHERE id>?", e0)] == [b])
    after = bals(); exp = {}
    for p in pv:
        if p["account_id"]: exp[p["account_id"]] = round(sum(r.get("cash_effect", r["amount"]) for r in p["rows"] if r["status"] == "new") * 100)
    for aid, delta in exp.items():
        check(f"balance delta == sum(new rows) for {[p['account'] for p in pv if p['account_id']==aid][0]}: {delta/100:+.2f}", (after[aid][2] - base[aid][2] == delta if aid in INV else after[aid][0] - base[aid][0] == delta and after[aid][1] - base[aid][1] == delta), (after[aid], base[aid], delta))
    check("other accounts untouched", all(after[a] == base[a] for a in base if a not in exp))
    check("transactions +10 (5 bank + 5 investment new)", q("SELECT COUNT(*) FROM transactions")[0][0] == n0 + 10)
    check("ext_id stamped: 8 matched + 10 new = 18", q("SELECT COUNT(*) FROM transactions WHERE ext_id IS NOT NULL")[0][0] == 18)
    check("investment: 5 rows added (Buy, ReinvDiv, Div, XOut, MiscExp), none for deferred groups", sorted(r[0] for r in q("SELECT action FROM transactions WHERE source='import' AND register_type='Invst'")) == ["Buy", "Div", "MiscExp", "ReinvDiv", "XOut"])
    h1 = hold()
    check("holdings: Alpha +7, Beta +0.4, Cash Sweep unchanged", abs(h1["Alpha Fund"] - h0["Alpha Fund"] - 7) < 1e-6 and abs(h1["Beta Fund"] - h0["Beta Fund"] - 0.4) < 1e-6 and h1["Cash Sweep"] == h0["Cash Sweep"], (h0, h1))
    check("imported rows have the planned category", q("SELECT category FROM transactions WHERE ext_id='T-new-1'")[0][0] == "Groceries" and q("SELECT category FROM transactions WHERE ext_id='T-new-2'")[0][0] == "Entertainment" and q("SELECT category FROM transactions WHERE ext_id='T-new-3'")[0][0] is None)
    st, pv2 = call("GET", "/api/import/preview"); P2 = {p["slug"]: p["counts"] for p in pv2}
    check("re-preview: new=0 matched=0, everything importable is a duplicate", all(P2[s]["new"] == 0 and P2[s]["matched"] == 0 for s in P2) and P2["checking-a"]["duplicate"] == 8 and (P2["brokerage-c"]["duplicate"], P2["brokerage-c"]["deferred"]) == (7, 2), P2)
    st, r2 = call("POST", "/api/import/run", {})
    check("second import is a no-op (no batch)", st == 200 and r2["batch"] is None)
    st, bl = call("GET", "/api/import/batches")
    check("batch listed, not reverted", bl and bl[0]["batch"] == b and bl[0]["inserted"] == 10 and bl[0]["linked"] == 8 and not bl[0]["reverted"], bl)
    # a later edit of an imported row blocks revert
    tid = q("SELECT txn_id FROM transactions WHERE ext_id='T-new-1'")[0][0]
    st, ed = call("PUT", f"/api/transactions/{tid}", {"date": "2026-09-10", "amount": -12.34, "payee": "Corner Grocery", "category": "Groceries", "memo": "edited"})
    st, r = call("POST", "/api/import/revert", {"batch": b})
    check("revert refused (409) after the imported row was edited", st == 409, (st, r))
    call("POST", "/api/import/revert", {"batch": ed["batch"]})
    st, r = call("POST", "/api/import/revert", {"batch": b})
    check("revert ok", st == 200 and r["reverted_edits"] == 18, r)
    st, r = call("POST", "/api/import/revert", {"batch": b})
    check("reverting it again is refused cleanly (409 already reverted)", st == 409 and "already reverted" in r["error"], (st, r))
    check("balances and holdings back to baseline exactly", bals() == base and hold() == h0)
    check("transactions count and ext_ids back to baseline", q("SELECT COUNT(*) FROM transactions")[0][0] == n0 and q("SELECT COUNT(*) FROM transactions WHERE ext_id IS NOT NULL")[0][0] == 0)
    st, bl = call("GET", "/api/import/batches")
    check("batch now marked reverted", bl[0]["reverted"])
    st, pv3 = call("GET", "/api/import/preview")
    check("preview after revert equals first preview", {p["slug"]: p["counts"] for p in pv3} == {p["slug"]: p["counts"] for p in pv})
    out = subprocess.run(["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--virtual-time-budget=5000", "--dump-dom", f"http://127.0.0.1:{PORT}/#/import"], capture_output=True, text=True, timeout=90).stdout
    check("import page renders (slugs, totals, deferred notice, unconfirmed/confirm controls)", all(x in out for x in ("CSV import", "checking-a", "ira-d", "collapsed", "Import batches")), out[-300:])
finally:
    for p in procs: p.terminate()
    shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "FAILURES"); sys.exit(0 if ok else 1)
