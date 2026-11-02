#!/usr/bin/env python3
"""Account Details test on a SCRATCH copy of the real DB: v5->v6 migration + prefill, API edits, type/rename/delete rules,
edit_log revert, balances unchanged, headless render of the dialog."""
import glob, json, os, re, shutil, sqlite3, subprocess, sys, tempfile, time, urllib.request
import testfix; T = testfix.make()  # made-up start database -> throwaway working DB (no real data needed)
import core

tmp = tempfile.mkdtemp(); scratch = os.path.join(tmp, "s.sqlite"); PORT = 8797
testfix.v5_copy(scratch)  # a pre-v6 working DB, built from the fixture
ok = True
def check(n, c, det=""):
    global ok; ok &= bool(c); print(("PASS " if c else "FAIL ") + n + ("" if c else f"  {det}"))
def snap():
    db = core.connect(); r = {a["account_id"]: 0 for a in []}; r = {a["account_id"]: (a["balance"], a["cash"], a["securities"], a["balance_all"], a["txn_count"]) for a in core.account_balances(db) if a["account_id"] != 900}; db.close(); return r
env = dict(os.environ, LEDGER_DB=scratch)

# ---- migration
v = sqlite3.connect(scratch).execute("PRAGMA user_version").fetchone()[0]; check("scratch starts at v5", v == 5, v)
os.environ["LEDGER_DB"] = scratch; core.DB_PATH = scratch
before = snap()
n_txn = sqlite3.connect(scratch).execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
core.migrate()
db = sqlite3.connect(scratch); db.row_factory = sqlite3.Row
check("migrated to the current version", db.execute("PRAGMA user_version").fetchone()[0] == core.SCHEMA_VERSION)
check("dated backup written next to the scratch DB", glob.glob(os.path.join(tmp, "backups", f"*-pre-v{core.SCHEMA_VERSION}.sqlite")))
check("balances identical after migration", snap() == before)
check("transactions untouched", db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == n_txn)
cols = {r[1] for r in db.execute("PRAGMA table_info(accounts)")}
check("new columns", {"acct_type", "tax_deferred", "single_fund", "institution", "acct_number", "customer_id", "contact_name", "phone", "url_home", "url_activity", "url_other", "comments", "tracking", "prefilled"} <= cols)
src = sqlite3.connect(f"file:{T.source}?mode=ro", uri=True)
exp = {r[0]: (r[1], r[2], r[3]) for r in src.execute("SELECT account_id, name, description, credit_limit FROM accounts")}
check("name/description/credit_limit identical to the starting database", all(exp[r["account_id"]] == (r["name"], r["description"], r["credit_limit"]) for r in db.execute("SELECT * FROM accounts")))
sch = db.execute("SELECT * FROM accounts WHERE name='Schwab Test IRA'").fetchone()
check("Schwab Test IRA prefilled (screenshot account)", (sch["description"], sch["institution"], sch["acct_type"], sch["tax_deferred"], sch["tracking"]) == ("Rollover IRA", "Charles Schwab & Co., Inc.", "Traditional IRA", 1, "complete"), dict(sch))
check("account numbers taken from names (XX1234)", db.execute("SELECT acct_number FROM accounts WHERE name='Fidelity Test XX1234'").fetchone()[0] == "XX1234")
db.close()

procs = []
def start():
    p = subprocess.Popen([sys.executable, "app.py", "--port", str(PORT)], env=env, cwd=core.HERE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    procs.append(p); time.sleep(1.5); return p
def call(method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())
def rows(sql, *a):
    d = sqlite3.connect(scratch); d.row_factory = sqlite3.Row; r = d.execute(sql, a).fetchall(); d.close(); return r
def aid(name): return rows("SELECT account_id FROM accounts WHERE name=?", name)[0][0]
try:
    start(); base = snap()
    s, D = call("GET", f"/api/accounts/{aid('Schwab Test IRA')}/details")
    check("GET details", s == 200 and D["acct_type"] == "Traditional IRA" and D["inv"] and not D["usage"]["can_delete"] and "Can't delete" in D["usage"]["delete_blocked"], D)
    # ---- field edits
    i = aid("Main Checking")
    s, r = call("PUT", f"/api/accounts/{i}/details", dict(institution="Test Bank", acct_number="1234567890", phone="(800) 555-0100", customer_id="C1", contact_name="Pat",
                url_home="examplebank.example", comments="note\nline 2", description="my checking"))
    check("save fields", s == 200 and r["batch"] and "phone" in r["changed"] and "institution" in r["changed"], (s, r))
    a = rows("SELECT * FROM accounts WHERE account_id=?", i)[0]
    check("account number masked to last 3 digits", a["acct_number"] == "XXXXXXX890", a["acct_number"])
    check("url normalised to https", a["url_home"] == "https://examplebank.example")
    check("prefilled flag cleared on save", a["prefilled"] is None)
    check("details edit logged in edit_log (accounts row)", rows("SELECT COUNT(*) c FROM edit_log WHERE batch=? AND tbl='accounts'", r["batch"])[0][0] == 1)
    check("balances unchanged after field edit", snap() == base)
    s, r2 = call("PUT", f"/api/accounts/{i}/details", dict(institution="Test Bank"))
    check("no-op save writes no batch", s == 200 and r2["batch"] is None and r2["changed"] == [], r2)
    check("javascript: url refused", call("PUT", f"/api/accounts/{i}/details", dict(url_other="javascript:alert(1)"))[0] == 400)
    check("blank name refused", call("PUT", f"/api/accounts/{i}/details", dict(name="  "))[0] == 400)
    check("duplicate name (any case) refused", call("PUT", f"/api/accounts/{i}/details", dict(name="savings"))[0] == 409)
    check("unknown type refused", call("PUT", f"/api/accounts/{i}/details", dict(acct_type="Spaceship"))[0] == 400)
    check("revert restores fields", call("POST", "/api/import/revert", dict(batch=r["batch"]))[0] == 200 and tuple(rows("SELECT institution,acct_number,url_home,comments,description,prefilled FROM accounts WHERE account_id=?", i)[0]) == (None, None, None, None, None, "acct_type"))
    # ---- rename keeps transfer text in step; revert undoes it
    f = aid("Fidelity Test XX1234"); nm = "Fidelity Renamed"
    n_ref = rows("SELECT COUNT(*) FROM transactions WHERE transfer_account_id=?", f)[0][0] + rows("SELECT COUNT(*) FROM splits WHERE transfer_account_id=?", f)[0][0]
    s, rn = call("PUT", f"/api/accounts/{f}/details", dict(name=nm))
    left = rows("SELECT (SELECT COUNT(*) FROM transactions WHERE transfer_account='Fidelity Test XX1234')+(SELECT COUNT(*) FROM splits WHERE transfer_account='Fidelity Test XX1234')")[0][0]
    now = rows("SELECT (SELECT COUNT(*) FROM transactions WHERE transfer_account=?)+(SELECT COUNT(*) FROM splits WHERE transfer_account=?)", nm, nm)[0][0]
    check(f"rename updates {n_ref} transfer references, none left under the old name", s == 200 and n_ref > 0 and left == 0 and now == n_ref, (s, left, now, n_ref))
    check("balances unchanged after rename", snap() == base)
    check("revert of rename restores name and transfer text", call("POST", "/api/import/revert", dict(batch=rn["batch"]))[0] == 200 and
          rows("SELECT name FROM accounts WHERE account_id=?", f)[0][0] == "Fidelity Test XX1234" and
          rows("SELECT COUNT(*) FROM transactions WHERE transfer_account=?", nm)[0][0] == 0 and
          rows("SELECT COUNT(*) FROM transactions WHERE transfer_account_id=? AND transfer_account='Fidelity Test XX1234'", f)[0][0] > 0)
    # ---- retype: register_type of account + all its rows move together, balances don't
    c = aid("Test Visa"); b0 = snap()
    s, rt = call("PUT", f"/api/accounts/{c}/details", dict(acct_type="Savings"))
    ra = rows("SELECT register_type, list_type, acct_type FROM accounts WHERE account_id=?", c)[0]
    check("CCard -> Bank retype: account + txn register_type consistent", s == 200 and tuple(ra) == ("Bank", "Bank", "Savings") and
          rows("SELECT COUNT(*) FROM transactions WHERE account_id=? AND register_type<>'Bank'", c)[0][0] == 0, (s, tuple(ra)))
    check("balance of the retyped account unchanged; all others unchanged", snap() == b0)
    A = {x["account_id"]: x for x in call("GET", "/api/accounts")[1]}
    check("list shows the new type", A[c]["type"] == "Bank")
    check("revert restores type everywhere", call("POST", "/api/import/revert", dict(batch=rt["batch"]))[0] == 200 and tuple(rows("SELECT register_type,list_type,acct_type FROM accounts WHERE account_id=?", c)[0]) == ("CCard", "CCard", None)
          and rows("SELECT COUNT(*) FROM transactions WHERE account_id=? AND register_type<>'CCard'", c)[0][0] == 0 and snap() == b0)
    s, e1 = call("PUT", f"/api/accounts/{c}/details", dict(acct_type="Brokerage"))
    check("non-investment -> investment refused when it has transactions", s == 409, (s, e1))
    s, e2 = call("PUT", f"/api/accounts/{aid('Schwab Test IRA')}/details", dict(acct_type="Checking"))
    check("investment -> bank refused when it has transactions", s == 409, (s, e2))
    s, e3 = call("PUT", f"/api/accounts/{aid('Schwab Test IRA')}/details", dict(acct_type="Roth IRA", tax_deferred=True, single_fund=True, tracking="simple"))
    check("investment label / flags editable", s == 200 and tuple(rows("SELECT acct_type,tax_deferred,single_fund,tracking,register_type FROM accounts WHERE name='Schwab Test IRA'")[0]) == ("Roth IRA", 1, 1, "simple", "Invst"), (s, e3))
    check("invalid tracking refused", call("PUT", f"/api/accounts/{aid('Schwab Test IRA')}/details", dict(tracking="weird"))[0] == 400)
    # ---- empty account: retype to investment allowed; delete allowed only when empty; revert brings it back
    d = sqlite3.connect(scratch); d.execute("INSERT INTO accounts(account_id,name,list_type,register_type,description,source) VALUES(900,'Empty test','Bank','Bank','x','ledger')"); d.commit(); d.close()
    s, e4 = call("PUT", "/api/accounts/900/details", dict(acct_type="Brokerage"))
    check("empty bank account may become investment", s == 200 and rows("SELECT register_type FROM accounts WHERE account_id=900")[0][0] == "Invst", (s, e4))
    s, e5 = call("DELETE", f"/api/accounts/{aid('Schwab Test IRA')}")
    check("delete refused with transactions", s == 409 and "transactions" in e5["error"] and rows("SELECT COUNT(*) FROM accounts WHERE name='Schwab Test IRA'")[0][0] == 1, e5)
    s, e6 = call("DELETE", f"/api/accounts/{aid('Savings')}")
    check("delete refused (transfers/mapping)", s == 409)
    s, e7 = call("DELETE", "/api/accounts/900")
    check("delete empty account", s == 200 and rows("SELECT COUNT(*) FROM accounts WHERE account_id=900")[0][0] == 0, (s, e7))
    check("revert of delete restores the account row", call("POST", "/api/import/revert", dict(batch=e7["batch"]))[0] == 200 and rows("SELECT register_type,description FROM accounts WHERE account_id=900")[0][0] == "Invst")
    # ---- hidden flag via dialog (logged)
    s, h = call("PUT", f"/api/accounts/{c}/details", dict(hidden=True))
    check("hide via dialog, logged", s == 200 and rows("SELECT hidden FROM accounts WHERE account_id=?", c)[0][0] == 1 and rows("SELECT COUNT(*) FROM edit_log WHERE batch=?", h["batch"])[0][0] == 1)
    call("POST", "/api/import/revert", dict(batch=h["batch"]))
    check("hidden revert", rows("SELECT hidden FROM accounts WHERE account_id=?", c)[0][0] == 0)
    # account-details edits must not disable the OB-adjusted view (they don't touch amounts)
    A0 = {x["account_id"]: x["balance_ob_adj"] is not None for x in call("GET", "/api/accounts")[1]}
    check("OB-adjusted column survives details edits", A0.get(i) == (base is not None and A0.get(i)) and all(
        (x["balance_ob_adj"] is not None) == A0[x["account_id"]] for x in call("GET", "/api/accounts")[1]))
    # ---- dialog renders (headless) and opens from the register URL
    def render(frag):
        return subprocess.run(["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--virtual-time-budget=6000", "--dump-dom", f"http://127.0.0.1:{PORT}/{frag}"], capture_output=True, text=True, timeout=90).stdout
    dom = render(f"#/a/{aid('Schwab Test IRA')}/details")
    check("dialog opens from the register with Schwab Test IRA's values", '<dialog id="adlg" open' in dom and 'value="Schwab Test IRA"' in dom and "Delete Account" in dom and "Tracking Method" in dom and 'Rollover IRA' in dom)
    check("register header has the details link", "Account details" in dom)
    lst = render("")
    check("account list rows have the gear + double-click", lst.count("adet(") >= 7 and "ondblclick" in lst, lst.count("adet("))
    check("balances unchanged at the end", snap() == base)
finally:
    for p in procs: p.terminate()
    shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "FAILURES"); sys.exit(0 if ok else 1)
