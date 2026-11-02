#!/usr/bin/env python3
"""Investment add / edit / delete test on a SCRATCH copy: cash and holdings must stay consistent with how the app computes them."""
import json, os, shutil, sqlite3, subprocess, sys, tempfile, time, urllib.request
import testfix; T = testfix.make()  # made-up start database -> throwaway working DB (no real data needed)
import core

tmp = tempfile.mkdtemp(); scratch = os.path.join(tmp, "s.sqlite"); PORT = 8795
s = sqlite3.connect(core.DB_PATH); d = sqlite3.connect(scratch); s.backup(d); d.close(); s.close()
p = subprocess.Popen([sys.executable, "app.py", "--port", str(PORT)], env=dict(os.environ, LEDGER_DB=scratch), cwd=core.HERE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT); time.sleep(1.5)
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
try:
    aid, name = q("SELECT account_id, name FROM accounts WHERE name='Fidelity Test XX1234'")[0]
    bank = q("SELECT account_id, name FROM accounts WHERE name='Main Checking'")[0]
    accts = lambda: {a["account_id"]: a for a in call("GET", "/api/accounts")[1]}
    hold = lambda: dict(call("GET", f"/api/accounts/{aid}/register?limit=1")[1]["holdings"])
    state = lambda: (accts()[aid]["cash"], accts()[aid]["securities"], accts()[bank[0]]["balance"], tuple(sorted(hold().items())))
    h = hold(); secs = [n for n, sh in h.items() if sh > 1]
    sec = secs[0]; sym = q("SELECT symbol FROM securities WHERE name=?", sec)[0][0]
    sec2 = secs[1]
    e0 = q("SELECT MAX(id) FROM edit_log")[0][0] or 0
    base = state(); cash0 = accts()[aid]["cash"]; sh0 = h[sec]; sh20 = h[sec2]
    def add(body, expect=200):
        st, r = call("POST", f"/api/accounts/{aid}/transactions", dict(date="2026-09-15", **body)); check(f"add {body['action']} -> {expect}", st == expect, (st, r)); return r
    def delta(cash=0, shares=0, sh2=0, label=""):
        a = accts()[aid]; hh = hold()
        check(f"{label}: cash {cash:+.2f}, shares {shares:+g}", a["cash"] - cash0 == round(cash * 100) and abs(hh[sec] - sh0 - shares) < 1e-6 and abs(hh[sec2] - sh20 - sh2) < 1e-6, (a["cash"] - cash0, hh[sec] - sh0))
    buy = add(dict(action="Buy", security=sec, qty=10, price=12.5)); delta(-125, 10, label="Buy 10 x 12.50 (amount computed)")
    check("register row: amount 125, qty/price stored", q("SELECT amount, invst_json, action, source FROM transactions WHERE txn_id=?", buy["txn_id"])[0] == (125.0, '{"I": 12.5, "Q": 10.0}', "Buy", "ledger"))
    sell = add(dict(action="Sell", security=sym, qty=4, price=13, amount=52)); delta(-125 + 52, 6, label="Sell 4 (by ticker)")
    div = add(dict(action="Div", security=sec, amount=7.25)); delta(-125 + 52 + 7.25, 6, label="Dividend")
    rdv = add(dict(action="ReinvDiv", security=sec, qty=0.5, price=20)); delta(-125 + 52 + 7.25, 6.5, label="Reinvested dividend (cash unchanged)")
    ii = add(dict(action="IntInc", amount=3.10)); delta(-125 + 52 + 7.25 + 3.10, 6.5, label="Interest")
    fee = add(dict(action="MiscExp", amount=15)); delta(-125 + 52 + 7.25 + 3.10 - 15, 6.5, label="Fee")
    xo = add(dict(action="XOut", amount=200)); delta(-125 + 52 + 7.25 + 3.10 - 15 - 200, 6.5, label="Cash out")
    cashnow = -125 + 52 + 7.25 + 3.10 - 15 - 200
    bb = accts()[bank[0]]["balance"]
    xi = add(dict(action="XIn", amount=1000, transfer=bank[1], payee="Transfer")); delta(cashnow + 1000, 6.5, label="Cash in with bank mirror")
    mir = q("SELECT txn_id, amount, link_id, transfer_account FROM transactions WHERE link_id=?", xi["txn_id"])
    check("mirror created in the bank account (-1000), linked both ways", len(mir) == 1 and mir[0][1] == -1000 and mir[0][3] == name and q("SELECT link_id FROM transactions WHERE txn_id=?", xi["txn_id"])[0][0] == mir[0][0] and accts()[bank[0]]["balance"] - bb == -100000, mir)
    d0 = q("SELECT date FROM transactions WHERE txn_id=?", mir[0][0])[0][0]
    st, r = call("PUT", f"/api/transactions/{mir[0][0]}", dict(date=d0, amount=-1000, payee="Transfer", transfer=name))
    check("bank-side mirror of a plain cash pair is editable (the investment side is rebuilt)", st == 200 and r["link_id"] and q("SELECT action, amount FROM transactions WHERE txn_id=?", r["link_id"]) == [("XIn", 1000.0)], (st, r))
    xi = dict(xi, txn_id=r["link_id"]); mir = [(r["txn_id"],)]
    st, r = call("PUT", f"/api/transactions/{xi['txn_id']}", dict(date="2026-09-16", action="XIn", amount=800, transfer=bank[1], payee="Transfer"))
    mir = q("SELECT amount, date FROM transactions WHERE link_id=?", xi["txn_id"])
    delta(cashnow + 800, 6.5, label="edit cash-in 1000 -> 800: cash") ; check("mirror followed the edit (-800, new date, still one)", st == 200 and mir == [(-800.0, "2026-09-16")] and accts()[bank[0]]["balance"] - bb == -80000, (st, r, mir))
    # edit buy: qty 10 -> 12, amount recomputed from price
    st, r = call("PUT", f"/api/transactions/{buy['txn_id']}", dict(date="2026-09-15", action="Buy", security=sec, qty=12, price=12.5))
    delta(cashnow + 800 - 25, 8.5, label="edit Buy 10 -> 12 shares")
    st, r = call("PUT", f"/api/transactions/{buy['txn_id']}", dict(date="2026-09-15", action="Div", security=sec, amount=5))
    delta(cashnow + 800 - 25 + 150 + 5, 8.5 - 12, label="edit Buy(12) -> Div 5: Buy effect removed, dividend applied")
    st, r = call("PUT", f"/api/transactions/{buy['txn_id']}", dict(date="2026-09-15", action="Buy", security=sec, qty=10, price=12.5))
    delta(cashnow + 800, 6.5, label="edit back to Buy 10")
    # validation
    add(dict(action="Buy", security=sec, price=1), 400); add(dict(action="Sell", security=sec, qty=1, amount=-5), 400)
    add(dict(action="Buy", security="NO SUCH SEC", qty=1, price=1), 400); add(dict(action="XIn", security=sec, amount=5), 400)
    add(dict(action="Div", security=sec, qty=2, amount=5), 400); add(dict(action="Bogus", amount=5), 400)
    add(dict(action="XOut", amount=5, transfer="Nonexistent"), 400); add(dict(action="Buy", security=sec, qty=1, price=1, date_x=1, transfer=bank[1]), 400)
    delta(cashnow + 800, 6.5, label="rejected adds changed nothing")
    st, r = call("POST", f"/api/accounts/{aid}/transactions", dict(date="bad", action="Buy", security=sec, qty=1, price=1)); check("bad date rejected", st == 400)
    sellmore = add(dict(action="Sell", security=sec, qty=1000, price=1))
    check("selling more than held warns", any("sold more than held" in w for w in sellmore["warnings"]), sellmore)
    call("DELETE", f"/api/transactions/{sellmore['txn_id']}", {})
    # editing an export cash-transfer row (unlinked counterpart in a bank account) must not touch the other side
    leg = q("SELECT txn_id, date, amount, action, transfer_account, payee_text FROM transactions WHERE account_id=? AND action IN ('XIn','XOut') AND transfer_account_id IS NOT NULL AND link_id IS NULL AND source<>'ledger' LIMIT 1", aid)
    if leg:
        t, dt_, am, act, tr, pay = leg[0]; nb = q("SELECT COUNT(*) FROM transactions")[0][0]; st0 = state()
        st, r = call("PUT", f"/api/transactions/{t}", dict(date=dt_, action=act, amount=abs(am), transfer=tr, payee=pay, memo="legacy edited"))
        check("edit legacy XIn/XOut memo: ok, transfer label kept, no row added/removed, balances unchanged",
              st == 200 and q("SELECT memo, transfer_account FROM transactions WHERE txn_id=?", t)[0] == ("legacy edited", tr) and q("SELECT COUNT(*) FROM transactions")[0][0] == nb and state() == st0, (st, r))
        st, r = call("PUT", f"/api/transactions/{t}", dict(date=dt_, action=act, amount=abs(am) + 1, transfer=tr, payee=pay))
        check("amount change on a legacy transfer warns that the other side was not changed", st == 200 and any("was not changed" in w for w in r["warnings"]), r)
        call("POST", "/api/import/revert", {"batch": r["batch"]})
    # a not-supported export action stays locked
    odd = q("SELECT txn_id, action FROM transactions WHERE account_id=? AND action IS NOT NULL AND action NOT IN ('Buy','Sell','Div','ReinvDiv','IntInc','XIn','XOut','MiscExp','ShrsIn','ShrsOut','StkSplit','ReinvInt','ReinvLg','ReinvSh','CGLong','CGShort') LIMIT 1", aid)
    if odd:
        st, r = call("PUT", f"/api/transactions/{odd[0][0]}", dict(date="2026-09-15", action="Buy", security=sec, qty=1, price=1)); check(f"export action {odd[0][1]} stays locked (409)", st == 409, (st, r))
    # delete everything created (mirror goes with its cash-in)
    for t in (xi, xo, fee, ii, rdv, div, sell, buy):
        st, r = call("DELETE", f"/api/transactions/{t['txn_id']}", {}); assert st == 200, (st, r)
    check("all deleted: cash, securities, holdings, bank balance exactly back to baseline", state() == base, (state(), base))
    check("mirror deleted with the cash-in", not q("SELECT 1 FROM transactions WHERE link_id=? OR txn_id=?", xi["txn_id"], xi["txn_id"]))
    # revert everything (batches in reverse order): scratch DB returns to the original row count and edit_log shows every change
    batches = [r[0] for r in q("SELECT batch FROM edit_log WHERE id>? AND batch NOT LIKE 'revert:%' AND batch NOT IN (SELECT substr(batch, 8) FROM edit_log WHERE batch LIKE 'revert:%') GROUP BY batch ORDER BY MIN(id) DESC", e0)]
    n_ok = 0
    for b in batches:
        st, r = call("POST", "/api/import/revert", {"batch": b}); n_ok += st == 200
    check("every investment batch reverts cleanly", n_ok == len(batches) and state() == base, (n_ok, len(batches), state() == base))
    check("no rows from this test left", q("SELECT COUNT(*) FROM transactions WHERE source='ledger' AND account_id IN (?,?) AND date IN ('2026-09-15','2026-09-16')", aid, bank[0])[0][0] == 0)
    # revert of a fresh add
    r = add(dict(action="Buy", security=sec, qty=3, price=10)); st, rv = call("POST", "/api/import/revert", {"batch": r["batch"]})
    check("revert of a single add restores holdings and cash", st == 200 and state() == base, (st, rv))
    html = subprocess.run(["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--virtual-time-budget=6000", "--dump-dom", f"http://127.0.0.1:{PORT}/#/a/{aid}"], capture_output=True, text=True, timeout=90).stdout
    check("investment register shows Add button, Holdings line and edit links", all(x in html for x in ("+ Add investment transaction", "Holdings:", "iform(")), html[-300:])
    for k in ("idlg", "i_act"): check(f"dialog element {k} present", f'id="{k}"' in html or f"id={k}" in html)
finally:
    p.terminate(); shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "FAILURES"); sys.exit(0 if ok else 1)
