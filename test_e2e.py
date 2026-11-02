#!/usr/bin/env python3
"""End-to-end test of add/edit/delete via the HTTP API on a SCRATCH copy of the working DB (never touches the real one)."""
import urllib.parse, json, os, shutil, sqlite3, subprocess, sys, tempfile, time, urllib.request, urllib.error
import testfix; T = testfix.make()  # made-up start database -> throwaway working DB (no real data needed)
import core, edits

tmp = tempfile.mkdtemp(); scratch = os.path.join(tmp, "scratch.sqlite"); PORT = 8799
src = sqlite3.connect(core.DB_PATH); dst = sqlite3.connect(scratch); src.backup(dst); dst.close(); src.close()
env = dict(os.environ, LEDGER_DB=scratch)
srv = subprocess.Popen([sys.executable, "app.py", "--port", str(PORT)], env=env, cwd=core.HERE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
time.sleep(1.5)
passed = failed = 0

def call(method, path, body=None, ctype="application/json"):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method,
                                 data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())

def check(name, cond, detail=""):
    global passed, failed
    cond = bool(cond); passed += cond; failed += (not cond)
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  {detail}"))

def bal(name):
    return round(next(a for a in call("GET", "/api/accounts")[1] if a["name"] == name)["balance_all"] / 100, 2)
def reg(aid, q=""):
    return call("GET", f"/api/accounts/{aid}/register?limit=100000&q={urllib.parse.quote(q)}")[1]
ids = {a["name"]: a["account_id"] for a in call("GET", "/api/accounts")[1]}
M, S, C2 = "Main Checking", "Savings", "Old CD"
base = {n: bal(n) for n in (M, S, C2)}
print("baseline", base)
cat = call("GET", "/api/meta")[1]["categories"][0]

try:
    # --- add simple transfer
    st, r = call("POST", f"/api/accounts/{ids[M]}/transactions", dict(date="2026-10-31", payee="E2E Transfer", amount=-100, transfer=S, memo="t1"))
    check("add transfer: 200", st == 200, r); t1 = r["txn_id"]; cp = r["link_id"]
    check("add transfer: Checking -100", bal(M) == round(base[M] - 100, 2)); check("add transfer: Savings +100", bal(S) == round(base[S] + 100, 2))
    row = next(x for x in reg(ids[S])["rows"] if x["txn_id"] == cp)
    check("counterpart in Savings: amount +100, transfer=Checking, linked, same date/payee",
          row["amount"] == 100 and row["transfer"] == M and row["link_id"] == t1 and row["date"] == "2026-10-31" and row["payee"] == "E2E Transfer", row)
    # --- edit amount/date/payee: both sides follow
    st, r = call("PUT", f"/api/transactions/{t1}", dict(date="2026-10-30", payee="E2E Transfer 2", amount=-150, transfer=S))
    check("edit transfer: 200", st == 200, r)
    check("edit: Checking -150, Savings +150", bal(M) == round(base[M] - 150, 2) and bal(S) == round(base[S] + 150, 2))
    row = next(x for x in reg(ids[S])["rows"] if x["txn_id"] == r["link_id"])
    check("edit: counterpart date/payee/amount follow", row["amount"] == 150 and row["date"] == "2026-10-30" and row["payee"] == "E2E Transfer 2", row)
    check("edit: exactly one E2E counterpart in Savings", len(reg(ids[S], "E2E")["rows"]) == 1)
    # --- editing the mirror side syncs the original
    mir = r["link_id"]
    st, r2 = call("PUT", f"/api/transactions/{mir}", dict(date="2026-10-30", payee="E2E Transfer 2", amount=175, transfer=M))
    check("edit via mirror side: 200", st == 200, r2)
    check("edit via mirror: Checking -175, Savings +175", bal(M) == round(base[M] - 175, 2) and bal(S) == round(base[S] + 175, 2))
    t1 = r2["link_id"]  # mirror is now primary; original row recreated as its counterpart
    # --- change transfer target
    st, r = call("PUT", f"/api/transactions/{t1}", dict(date="2026-10-30", payee="E2E Transfer 2", amount=-175, transfer=C2))
    check("retarget transfer: Savings back to base, CD 2 +175", st == 200 and bal(S) == base[S] and bal(C2) == round(base[C2] + 175, 2), (st, r))
    # --- delete removes both sides
    st, r = call("DELETE", f"/api/transactions/{t1}", {})
    check("delete transfer: both sides gone, balances at baseline", st == 200 and bal(M) == base[M] and bal(C2) == base[C2] and not reg(ids[C2], "E2E")["rows"], (st, r))
    # --- split txn with a transfer leg
    st, r = call("POST", f"/api/accounts/{ids[M]}/transactions", dict(date="2026-10-31", payee="E2E Split", amount=-300,
                 splits=[dict(category=cat, amount=-200, memo="a"), dict(transfer=S, amount=-100, memo="b")]))
    check("add split: 200", st == 200, r); t2 = r["txn_id"]
    check("split: Checking -300, Savings +100", bal(M) == round(base[M] - 300, 2) and bal(S) == round(base[S] + 100, 2))
    row = next(x for x in reg(ids[M])["rows"] if x["txn_id"] == t2)
    check("split shown with 2 lines summing to -300", len(row["splits"]) == 2 and round(sum(s["amount"] for s in row["splits"]), 2) == -300 and row["category"] == "--Split--", row)
    mir = next(x for x in reg(ids[S])["rows"] if x["link_id"] == t2)
    check("split mirror in Savings: +100, transfer=Checking", mir["amount"] == 100 and mir["transfer"] == M and mir["locked"], mir)
    st, r = call("PUT", f"/api/transactions/{mir['txn_id']}", dict(date="2026-10-31", amount=5, transfer=M))
    check("editing split mirror rejected (409)", st == 409, (st, r))
    st, r = call("DELETE", f"/api/transactions/{mir['txn_id']}", {})
    check("deleting split mirror rejected (409)", st == 409, (st, r))
    # edit split: change leg amounts
    st, r = call("PUT", f"/api/transactions/{t2}", dict(date="2026-10-31", payee="E2E Split", amount=-400,
                 splits=[dict(category=cat, amount=-250), dict(transfer=S, amount=-150)]))
    check("edit split: Checking -400, Savings +150", st == 200 and bal(M) == round(base[M] - 400, 2) and bal(S) == round(base[S] + 150, 2), (st, r))
    # edit split: drop transfer leg -> mirror removed
    st, r = call("PUT", f"/api/transactions/{t2}", dict(date="2026-10-31", payee="E2E Split", amount=-400, splits=[dict(category=cat, amount=-400)]))
    check("edit split to category only: Savings back to base", st == 200 and bal(S) == base[S] and bal(M) == round(base[M] - 400, 2), (st, r))
    st, r = call("DELETE", f"/api/transactions/{t2}", {})
    check("delete split txn: balances at baseline, no splits left",
          st == 200 and bal(M) == base[M] and bal(S) == base[S] and not sqlite3.connect(scratch).execute("SELECT 1 FROM splits WHERE txn_id=?", (t2,)).fetchone(), (st, r))
    # --- validation
    check("splits not summing -> 400", call("POST", f"/api/accounts/{ids[M]}/transactions", dict(date="2026-10-31", amount=-10, splits=[dict(category=cat, amount=-5)]))[0] == 400)
    check("unknown category -> 400", call("POST", f"/api/accounts/{ids[M]}/transactions", dict(date="2026-10-31", amount=-1, category="NoSuchCat"))[0] == 400)
    check("bad date -> 400", call("POST", f"/api/accounts/{ids[M]}/transactions", dict(date="31/10/2026", amount=-1))[0] == 400)
    inv = next(a["account_id"] for a in call("GET", "/api/accounts")[1] if a["inv"])
    check("investment account add -> 400", call("POST", f"/api/accounts/{inv}/transactions", dict(date="2026-10-31", amount=1))[0] == 400)
    check("non-JSON content type -> 415", call("POST", f"/api/accounts/{ids[M]}/transactions", dict(date="2026-10-31", amount=1), ctype="text/plain")[0] == 415)
    check("failed requests changed nothing", bal(M) == base[M])
    # --- legacy (unlinked, imported) transfer: unique counterpart is kept in sync
    d = sqlite3.connect(scratch); d.row_factory = sqlite3.Row
    leg = d.execute("""SELECT a.* FROM transactions a JOIN accounts x ON x.account_id=a.account_id
        WHERE a.link_id IS NULL AND a.split_count=0 AND a.transfer_account_id IS NOT NULL AND a.transfer_account_id<>a.account_id
        AND x.register_type IN ('Bank','CCard') AND a.source='current' AND
        (SELECT COUNT(*) FROM transactions b WHERE b.account_id=a.transfer_account_id AND b.date=a.date AND ROUND(b.amount+a.amount,2)=0
         AND b.transfer_account_id=a.account_id AND b.link_id IS NULL)=1 AND
        (SELECT register_type FROM accounts WHERE account_id=a.transfer_account_id) IN ('Bank','CCard') LIMIT 1""").fetchone()
    b0 = d.execute("SELECT txn_id FROM transactions WHERE account_id=? AND date=? AND ROUND(amount+?,2)=0 AND transfer_account_id=?", (leg["transfer_account_id"], leg["date"], leg["amount"], leg["account_id"])).fetchone()[0]
    a_n = d.execute("SELECT name FROM accounts WHERE account_id=?", (leg["account_id"],)).fetchone()[0]; o_n = leg["transfer_account"]
    ba, bo = bal(a_n), bal(o_n); d.close()
    newamt = round(leg["amount"] - 7, 2)
    st, r = call("PUT", f"/api/transactions/{leg['txn_id']}", dict(date=leg["date"], payee=leg["payee_text"], amount=newamt, transfer=o_n, num=leg["check_number"] or ""))
    check(f"legacy transfer edit ({a_n} <-> {o_n}): both sides move by 7", st == 200 and bal(a_n) == round(ba - 7, 2) and bal(o_n) == round(bo + 7, 2) and not r["warnings"], (st, r, ba, bo))
    st, r = call("DELETE", f"/api/transactions/{leg['txn_id']}", {})
    check("legacy transfer delete: both sides removed", st == 200 and bal(a_n) == round(ba - leg["amount"], 2) and bal(o_n) == round(bo + leg["amount"], 2))
    # --- revert of a UI batch
    st, r = call("POST", f"/api/accounts/{ids[M]}/transactions", dict(date="2026-10-31", payee="E2E Revert", amount=-9, transfer=S))
    b_before = bal(M)
    out = subprocess.run([sys.executable, "app.py", "--revert", r["batch"]], env=env, cwd=core.HERE, capture_output=True, text=True).stdout
    check("revert batch of an add: both rows removed", bal(M) == round(b_before + 9, 2) and not reg(ids[S], "E2E Revert")["rows"], out)
    # --- reverting twice is a clean message (no traceback), and a reverted batch is no longer "live" (so a fix script may apply again under a fresh id)
    name = r["batch"]; core.DB_PATH = scratch
    out = subprocess.run([sys.executable, "app.py", "--revert", name], env=env, cwd=core.HERE, capture_output=True, text=True)
    check("second revert of the same batch: clean 'already reverted' message, no traceback, nothing changed", out.returncode != 0 and "already reverted" in (out.stdout + out.stderr) and "Traceback" not in out.stderr and bal(M) == round(b_before + 9, 2), (out.stdout, out.stderr))
    check("reverted batch is not live; next fix batch id differs; revert_live is a no-op message", edits.live_batch(core.connect(), name) is None and edits.next_batch(core.connect(), name) == name + "-r2" and edits.revert_live(name).startswith("nothing to revert"))
finally:
    srv.terminate(); shutil.rmtree(tmp, ignore_errors=True)
print(f"\n{passed} passed, {failed} failed (scratch: {scratch}; real DB untouched)")
sys.exit(1 if failed else 0)
