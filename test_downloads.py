#!/usr/bin/env python3
"""Download settings (Account Details > Download tab, schema v7, --download-list): v6->v7 migration defaults, API saves through edit_log, read-only slug/latest file/last import,
slug creation, validation, CLI/endpoint output, revert, headless render. Scratch DB built from the made-up fixture."""
import csv, glob, json, os, sqlite3, subprocess, sys, tempfile, time, urllib.request
import testfix; T = testfix.make()
import core

tmp = tempfile.mkdtemp(); scratch = os.path.join(tmp, "s.sqlite"); PORT = 8793
testfix.v5_copy(scratch, 6)  # a v6 working DB (no download columns yet)
d = sqlite3.connect(scratch); d.execute("INSERT INTO import_map(slug, account_id, confirmed) VALUES('my-checking', 1, 1)"); d.commit(); d.close()
os.makedirs(os.path.join(T.imports, "my-checking")); env = dict(os.environ, LEDGER_DB=scratch, LEDGER_IMPORTS=T.imports)
ok = True
def check(n, c, det=""):
    global ok; ok &= bool(c); print(("PASS " if c else "FAIL ") + n + ("" if c else f"  {det}"))
def rows(sql, *a):
    x = sqlite3.connect(scratch); x.row_factory = sqlite3.Row; r = x.execute(sql, a).fetchall(); x.close(); return r
def bal(): return rows("SELECT a.account_id, (SELECT ROUND(SUM(amount),2) FROM transactions t WHERE t.account_id=a.account_id) s FROM accounts a")
def app(*args): return subprocess.run([sys.executable, "app.py", *args], env=env, cwd=core.HERE, capture_output=True, text=True)
def call(method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())
def aid(name): return rows("SELECT account_id FROM accounts WHERE name=?", name)[0][0]
def dl(i): return call("GET", f"/api/accounts/{i}/details")[1]["download"]
def put(i, **b): return call("PUT", f"/api/accounts/{i}/details", b)
def dlist(): return json.loads(app("--download-list").stdout)

before = [tuple(r) for r in bal()]
r = app("--init-only")
check("migration ran with a dated backup first", r.returncode == 0 and glob.glob(os.path.join(tmp, "backups", f"*-pre-v{core.SCHEMA_VERSION}.sqlite")) and rows("PRAGMA user_version")[0][0] == core.SCHEMA_VERSION, r.stderr)
check("default: mapped account on + monthly, every other account off", [(x["account_id"], x["dl_enabled"], x["dl_schedule"]) for x in rows("SELECT * FROM accounts WHERE dl_enabled=1")] == [(1, 1, "monthly")] and rows("SELECT COUNT(*) c FROM accounts WHERE dl_schedule='monthly'")[0]["c"] == 7)
check("balances unchanged by the migration", [tuple(r) for r in bal()] == before)
p = subprocess.Popen([sys.executable, "app.py", "--port", str(PORT)], env=env, cwd=core.HERE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
try:
    time.sleep(1.5)
    D = dl(1); check("account 1: enabled, monthly, slug, no file yet, no import yet", D["enabled"] and D["schedule"] == "monthly" and D["slug"] == "my-checking" and D["latest_file"] is None and D["last_import"] is None, D)
    D = dl(2); check("other account: off, no slug", not D["enabled"] and D["slug"] is None and D["folder"] is None, D)
    # ---- read-only file / import info
    for m, rowsx in (("2026-08", [("2026-08-03", "Cafe", -4.5, "t1")]), ("2026-09", [("2026-09-03", "Cafe", -5.5, "t2"), ("2026-09-05", "Payroll", 100, "t3")])):
        with open(os.path.join(T.imports, "my-checking", f"my-checking_{m}.csv"), "w", newline="") as f:
            w = csv.writer(f); w.writerow("date,payee,amount,memo,category,txn_id,account,source".split(","))
            for x in rowsx: w.writerow([x[0], x[1], x[2], "", "", x[3], "my-checking", "test"])
    D = dl(1); check("latest file = newest month, with its modified time", D["latest_file"] == "my-checking_2026-09.csv" and D["latest_month"] == "2026-09" and D["latest_file_modified"], D)
    s, r = call("POST", "/api/import/run", {}); D = dl(1)
    check("last import shows the batch", s == 200 and r["batch"] and D["last_import"]["batch"] == r["batch"], (r, D))
    s, _ = call("POST", "/api/import/revert", {"batch": r["batch"]})
    check("a reverted import no longer counts as the last import", s == 200 and dl(1)["last_import"] is None)
    # ---- saves
    base = [tuple(r) for r in bal()]; s2 = aid("Savings"); sw = aid("Schwab Test IRA")
    guessed = rows("SELECT prefilled FROM accounts WHERE account_id=?", sw)[0][0]
    s, r = put(s2, dl_enabled=True, dl_schedule="on demand"); en_batch = r["batch"]
    check("enable + on demand: one batch, only the download fields changed", s == 200 and sorted(r["changed"]) == ["dl_enabled", "dl_schedule"] and r["batch"], (s, r))
    D = dl(s2); check("slug made from the account name and confirmed in import_map", D["slug"] == "savings" and D["enabled"] and D["schedule"] == "on demand" and rows("SELECT confirmed FROM import_map WHERE slug='savings'")[0][0] == 1, D)
    log = rows("SELECT tbl, op FROM edit_log WHERE batch=?", r["batch"]); check("logged as an accounts update", [tuple(x) for x in log] == [("accounts", "update")], [tuple(x) for x in log])
    check("no-change save makes no batch", put(s2, dl_enabled=True, dl_schedule="on demand")[1]["batch"] is None)
    put(sw, dl_enabled=True); check("a download toggle keeps the 'please check' guesses", rows("SELECT prefilled FROM accounts WHERE account_id=?", sw)[0][0] == guessed and guessed)
    wid = aid("Wallet"); x = sqlite3.connect(scratch); x.execute("INSERT INTO import_map(slug, account_id, confirmed) VALUES('wallet', 1, 1)"); x.commit(); x.close()
    put(wid, dl_enabled=True); check("slug collision gets a suffix", dl(wid)["slug"] == "wallet-2", dl(wid))
    for bad in (dict(dl_enabled="yes"), dict(dl_schedule="weekly"), dict(dl_enabled=1)):
        n = rows("SELECT COUNT(*) c FROM edit_log")[0]["c"]; s, r = put(aid("Old CD"), **bad)
        check(f"rejected {bad}", s == 400 and "error" in r and rows("SELECT COUNT(*) c FROM edit_log")[0]["c"] == n and not dl(aid("Old CD"))["enabled"], (s, r))
    s, rn = put(s2, name="Savings Renamed"); rn_batch = rn["batch"]
    check("rename keeps the slug and settings", s == 200 and dl(s2)["slug"] == "savings" and dl(s2)["enabled"])
    # ---- the bot's read side
    L = dlist(); names = {x["account"]: x for x in L}
    check("--download-list: JSON on stdout with the enabled accounts only", set(names) == {"Main Checking", "Savings Renamed", "Schwab Test IRA", "Wallet"} and app("--download-list").stderr == "", list(names))
    m = names["Main Checking"]; check("entry: slug, schedule, latest file, last import (reverted: none), investment flag", (m["slug"], m["schedule"], m["latest_file"], m["last_import"], m["investment"]) == ("my-checking", "monthly", "my-checking_2026-09.csv", None, False) and names["Schwab Test IRA"]["investment"] and names["Savings Renamed"]["schedule"] == "on demand", m)
    s, E = call("GET", "/api/downloads"); check("GET /api/downloads equals the CLI output", s == 200 and E == L)
    # ---- revert
    check("reverting the enable before the later rename is refused with a plain message", app("--revert", en_batch).returncode == 1)
    check("revert rename, then the enable batch", app("--revert", rn_batch).returncode == 0 and app("--revert", en_batch).returncode == 0)
    r2 = app("--revert", en_batch); check("a second revert is a clean message", r2.returncode == 1 and "already reverted" in r2.stderr + r2.stdout and "Traceback" not in r2.stderr, r2.stderr)
    check("Savings is off again and leaves the list; others stay", not dl(s2)["enabled"] and dl(s2)["schedule"] == "monthly" and {x["account"] for x in dlist()} == {"Main Checking", "Schwab Test IRA", "Wallet"})
    check("balances unchanged by all of it", [tuple(r) for r in bal()] == base)
    # ---- the tab renders (headless)
    dom = subprocess.run(["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--virtual-time-budget=6000", "--dump-dom", f"http://127.0.0.1:{PORT}/#/a/1/details"], capture_output=True, text=True, timeout=90).stdout
    check("dialog has General + Download tabs with the settings and read-only info", all(t in dom for t in ('id="adt_d"', "Download new transactions", "my-checking_2026-09.csv", "Import slug", "On demand", "Last import", "--download-list")), dom[-300:])
finally:
    p.terminate()
print("ALL PASS" if ok else "FAILURES"); sys.exit(0 if ok else 1)
