#!/usr/bin/env python3
"""Reconcile + cleared-toggle test on a SCRATCH copy of the DB: full cycle, wrong difference, undo, last-statement memory."""
import datetime, json, os, shutil, sqlite3, subprocess, sys, tempfile, time, urllib.request
import testfix; T = testfix.make()  # made-up start database -> throwaway working DB (no real data needed)
import core

tmp = tempfile.mkdtemp(); scratch = os.path.join(tmp, "s.sqlite"); PORT = 8796
s = sqlite3.connect(core.DB_PATH); d = sqlite3.connect(scratch); s.backup(d); d.close(); s.close()
env = dict(os.environ, LEDGER_DB=scratch)
p = subprocess.Popen([sys.executable, "app.py", "--port", str(PORT)], env=env, cwd=core.HERE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT); time.sleep(1.5)
def call(method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())
def q(sql, *a):
    db = sqlite3.connect(scratch); r = db.execute(sql, a).fetchall(); db.close(); return r
ok = True
def check(n, c, det=""):
    global ok; ok &= bool(c); print(("PASS " if c else "FAIL ") + n + ("" if c else f"  {det}"))
snap = lambda aid: q("SELECT txn_id, cleared_status, cleared_raw FROM transactions WHERE account_id=? ORDER BY txn_id", aid)
try:
    aid, name = q("SELECT a.account_id, a.name FROM accounts a JOIN transactions t USING(account_id) WHERE a.register_type='Bank' AND t.date<=date('now') GROUP BY 1 "
                  "HAVING SUM(t.cleared_status IS NULL)>=3 AND SUM(t.cleared_status='cleared')>=2 AND SUM(t.cleared_status='reconciled')>=3 ORDER BY COUNT(*) DESC LIMIT 1")[0]
    inv_aid = q("SELECT account_id FROM accounts WHERE register_type='Invst' LIMIT 1")[0][0]
    bal = lambda: {a["account_id"]: (a["balance"], a["balance_all"]) for a in call("GET", "/api/accounts")[1]}
    today = datetime.date.today().isoformat()
    base_bal, base_snap = bal(), snap(aid)
    n_r = q("SELECT COUNT(*) FROM transactions WHERE account_id=? AND cleared_status='reconciled'", aid)[0][0]
    st, L = call("GET", f"/api/accounts/{aid}/reconcile?date={today}")
    check(f"{name}: list returns non-R rows up to the date + R count", st == 200 and L["reconciled_rows"] == n_r and L["last"] is None and all(not r["txn_id"] is None for r in L["rows"]) and len(L["rows"]) > 3, (st, len(L.get("rows", []))))
    check("rows are only non-reconciled", all(q("SELECT IFNULL(cleared_status,'') FROM transactions WHERE txn_id=?", r["txn_id"])[0][0] != "reconciled" for r in L["rows"]))
    check("cleared rows come pre-flagged", {r["txn_id"] for r in L["rows"] if r["cleared"]} == {t[0] for t in q("SELECT txn_id FROM transactions WHERE account_id=? AND cleared_status='cleared' AND date<=?", aid, today)})
    st, bad = call("GET", f"/api/accounts/{inv_aid}/reconcile?date={today}")
    check("investment account not reconcilable (400)", st == 400, bad)
    # single-row clear toggle
    unc = next(r for r in L["rows"] if not r["cleared"]); e0 = q("SELECT COUNT(*) FROM edit_log")[0][0]
    st, r = call("POST", f"/api/transactions/{unc['txn_id']}/cleared", {"cleared": "c"})
    def reg():
        out, off = {}, 0
        while True:
            R = call("GET", f"/api/accounts/{aid}/register?limit=2000&offset={off}")[1]
            out.update({x["txn_id"]: x for x in R["rows"]}); off += 2000
            if off >= R["matched"]: return out
    check("toggle c: register shows c, one logged batch, balance unchanged", st == 200 and reg()[unc["txn_id"]]["cleared"] == "c" and len(q("SELECT DISTINCT batch FROM edit_log WHERE id>?", e0)) == 1 and bal() == base_bal, (st, r))
    st, r = call("POST", f"/api/transactions/{unc['txn_id']}/cleared", {"cleared": ""})
    check("toggle back: uncleared again", st == 200 and snap(aid) == base_snap)
    st, r = call("POST", "/api/transactions/{}/cleared".format(q("SELECT txn_id FROM transactions WHERE account_id=? AND cleared_status='reconciled' LIMIT 1", aid)[0][0]), {"cleared": ""})
    check("R row cannot be toggled (409)", st == 409, (st, r))
    st, r = call("POST", f"/api/transactions/{unc['txn_id']}/cleared", {"cleared": "x"})
    check("bad value rejected (400)", st == 400)
    # reconcile A: through the median date, all rows ticked except one cleared row left unticked
    dates = sorted(r["date"] for r in L["rows"]); dA = dates[len(dates) // 2]
    LA = call("GET", f"/api/accounts/{aid}/reconcile?date={dA}")[1]
    left = next((r for r in LA["rows"] if r["cleared"]), None)
    tickA = [r["txn_id"] for r in LA["rows"] if not left or r["txn_id"] != left["txn_id"]]
    endA = round(LA["beginning"] + sum(r["amount"] for r in LA["rows"] if r["txn_id"] in tickA), 2)
    e1 = q("SELECT COUNT(*) FROM edit_log")[0][0]
    st, r = call("POST", f"/api/accounts/{aid}/reconcile", {"statement_date": dA, "statement_balance": endA + 0.01, "cleared_ids": tickA})
    check("wrong difference refused (409), nothing written", st == 409 and "0.01" in r["error"] and q("SELECT COUNT(*) FROM edit_log")[0][0] == e1 and snap(aid) == base_snap, (st, r))
    st, r = call("POST", f"/api/accounts/{aid}/reconcile", {"statement_date": dA, "statement_balance": endA, "cleared_ids": tickA + [q("SELECT txn_id FROM transactions WHERE account_id<>? LIMIT 1", aid)[0][0]]})
    check("id from another account refused (400)", st == 400, (st, r))
    st, ra = call("POST", f"/api/accounts/{aid}/reconcile", {"statement_date": dA, "statement_balance": endA, "cleared_ids": tickA})
    check("reconcile A ok: marked rows, one batch", st == 200 and ra["marked"] == len(tickA) and len(q("SELECT DISTINCT batch FROM edit_log WHERE id>?", e1)) == 1, (st, ra))
    check("rows now reconciled (R)", all(q("SELECT cleared_status, cleared_raw FROM transactions WHERE txn_id=?", t)[0] == ("reconciled", "X") for t in tickA))
    if left: check("unticked cleared row went back to uncleared", q("SELECT cleared_status FROM transactions WHERE txn_id=?", left["txn_id"])[0][0] is None)
    check("balances unchanged by reconcile", bal() == base_bal)
    LA2 = call("GET", f"/api/accounts/{aid}/reconcile?date={dA}")[1]
    check("last statement remembered (date + balance + rows)", LA2["last"]["date"] == dA and LA2["last"]["balance"] == endA and LA2["last"]["rows"] == len(tickA) and LA2["reconciled_rows"] == n_r + len(tickA), LA2["last"])
    check("register shows R", all(reg()[t]["cleared"] == "R" for t in tickA))
    check("beginning balance for next statement = reconciled total", abs(call("GET", f"/api/accounts/{aid}/reconcile?date={today}")[1]["beginning"] - (round(sum(r[0] for r in q("SELECT amount FROM transactions WHERE account_id=? AND cleared_status='reconciled'", aid)), 2))) < 0.005)
    # reconcile B through today
    snapA = snap(aid)
    LB = call("GET", f"/api/accounts/{aid}/reconcile?date={today}")[1]
    tickB = [r["txn_id"] for r in LB["rows"]]; endB = round(LB["beginning"] + sum(r["amount"] for r in LB["rows"]), 2)
    st, rb = call("POST", f"/api/accounts/{aid}/reconcile", {"statement_date": today, "statement_balance": endB, "cleared_ids": tickB})
    check("reconcile B ok (beginning includes A)", st == 200 and rb["marked"] == len(tickB), (st, rb))
    check("last is now B", call("GET", f"/api/accounts/{aid}/reconcile?date={today}")[1]["last"]["date"] == today)
    st, u = call("POST", f"/api/accounts/{aid}/reconcile/undo", {})
    check("undo B restores the exact earlier statuses", st == 200 and snap(aid) == snapA and bal() == base_bal, (st, u))
    LU = call("GET", f"/api/accounts/{aid}/reconcile?date={today}")[1]
    check("after undo, last statement is A again", LU["last"]["date"] == dA and LU["last"]["balance"] == endA)
    st, u = call("POST", f"/api/accounts/{aid}/reconcile/undo", {})
    check("undo A restores the original statuses", st == 200 and snap(aid) == base_snap and bal() == base_bal, (st, u))
    st, u = call("POST", f"/api/accounts/{aid}/reconcile/undo", {})
    check("nothing left to undo (404)", st == 404, (st, u))
    check("no reconcile rows left", q("SELECT COUNT(*) FROM reconcile")[0][0] == 0)
    # undo blocked if a later edit touched a reconciled row
    st, rc = call("POST", f"/api/accounts/{aid}/reconcile", {"statement_date": dA, "statement_balance": endA, "cleared_ids": tickA})
    t = tickA[0]; row = reg()[t]
    st, ed = call("PUT", f"/api/transactions/{t}", {"date": row["date"], "amount": row["amount"], "payee": row["payee"], "memo": "edited after reconcile", "category": row["category"] if row["category"] != "--Split--" else "", "cleared": "R"})
    st2, u = call("POST", f"/api/accounts/{aid}/reconcile/undo", {})
    check("undo refused (409) after a later edit to a reconciled row, or edit itself refused", st2 == 409 or st != 200, (st, st2, u))
    html = subprocess.run(["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--virtual-time-budget=6000", "--dump-dom", f"http://127.0.0.1:{PORT}/#/rec/{aid}"], capture_output=True, text=True, timeout=90).stdout
    check("reconcile page renders (statement inputs, table, difference, undo control, R count)", all(x in html for x in ("Reconcile", "Ending balance", "Finish reconcile", "difference", "Undo last reconcile", "already reconciled")), html[-400:])
    html = subprocess.run(["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--virtual-time-budget=6000", "--dump-dom", f"http://127.0.0.1:{PORT}/#/a/{aid}"], capture_output=True, text=True, timeout=90).stdout
    check("register shows Reconcile link and clickable Clr cells", "#/rec/%d" % aid in html and "toggle cleared" in html, html[-300:])
finally:
    p.terminate(); shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "FAILURES"); sys.exit(0 if ok else 1)
