#!/usr/bin/env python3
"""Reports test. Report functions are read-only (ro connection); API/UI checks run against a SCRATCH copy on its own port."""
import datetime, json, os, shutil, sqlite3, subprocess, sys, tempfile, time, urllib.request
import testfix; T = testfix.make()  # made-up start database -> throwaway working DB (no real data needed)
import sqlite3; _d = sqlite3.connect(T.db); _d.execute("UPDATE accounts SET hidden=1 WHERE name IN ('Savings','Wallet')"); _d.commit(); _d.close()  # the test hides accounts itself
import core, reports

PORT = 8794; tmp = tempfile.mkdtemp(); scratch = os.path.join(tmp, "s.sqlite")
s = sqlite3.connect(core.DB_PATH); d = sqlite3.connect(scratch); s.backup(d); d.close(); s.close()
db = core.connect(); raw = sqlite3.connect(f"file:{core.DB_PATH}?mode=ro", uri=True)
n_before = raw.execute("SELECT COUNT(*), (SELECT COUNT(*) FROM edit_log) FROM transactions").fetchone()
p = subprocess.Popen([sys.executable, "app.py", "--port", str(PORT)], env=dict(os.environ, LEDGER_DB=scratch), cwd=core.HERE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT); time.sleep(1.5)
def get(path):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}") as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())
ok = True
def check(n, c, det=""):
    global ok; ok &= bool(c); print(("PASS " if c else "FAIL ") + n + ("" if c else f"  {det}"))
def sql_total(d1, d2, hidden, stale, section=None, cat=None):
    """Independent direct SQL: sum of (plain txn amounts + split amounts), no transfers, no investment accounts, no opening balances."""
    hid = "" if hidden else "AND a.hidden=0"; st = "AND instr(COALESCE(t.merge_note,''),'STALE?')=0" if stale else ""
    sec = {"income": "AND ((c.is_income=1) OR (x.cat IS NULL AND x.amt>0))", "expense": "AND NOT ((COALESCE(c.is_income,0)=1) OR (x.cat IS NULL AND x.amt>0))", None: ""}[section]
    cf = "" if cat is None else "AND x.cat IS NULL" if cat == reports.UNCAT else "AND (x.cat=:cat OR x.cat LIKE :cat || ':%')"
    q = f"""SELECT ROUND(COALESCE(SUM(x.amt),0),2) FROM (
        SELECT t.txn_id id, t.account_id, t.category cat, t.amount amt FROM transactions t WHERE t.split_count=0 AND t.transfer_account_id IS NULL
        UNION ALL SELECT s.txn_id, t.account_id, s.category, s.amount FROM splits s JOIN transactions t ON t.txn_id=s.txn_id WHERE t.split_count>0 AND s.transfer_account_id IS NULL) x
        JOIN transactions t ON t.txn_id=x.id JOIN accounts a ON a.account_id=x.account_id LEFT JOIN categories c ON c.name=x.cat
        WHERE a.register_type IS NOT 'Invst' AND COALESCE(t.payee_text,'') NOT LIKE 'Opening Balance%' AND t.date BETWEEN :d1 AND :d2 {hid} {st} {sec} {cf}"""
    return raw.execute(q, dict(d1=d1, d2=d2, cat=cat)).fetchone()[0]
try:
    ranges = {"2025 (year)": ("2025-01-01", "2025-12-31"), "2026-09 (month)": ("2026-09-01", "2026-09-30"), "2019 (year)": ("2019-01-01", "2019-12-31")}
    for lbl, (d1, d2) in ranges.items():
        for hidden in (False, True):
            for stale in (False, True):
                R = reports.spending(db, d1, d2, hidden, stale)
                inc, exp = sql_total(d1, d2, hidden, stale, "income"), -sql_total(d1, d2, hidden, stale, "expense")
                check(f"{lbl} hidden={hidden} exclude_stale={stale}: income {R['income']['total']:,.2f} / expense {R['expense']['total']:,.2f} == direct SQL", abs(R["income"]["total"] - inc) < .005 and abs(R["expense"]["total"] - exp) < .005, (R["income"]["total"], inc, R["expense"]["total"], exp))
    R = reports.spending(db, "2025-01-01", "2025-12-31")
    bad = []
    for sec, sgn in (("income", 1), ("expense", -1)):
        for root in R[sec]["categories"]:
            if abs(root["total"] - sgn * sql_total("2025-01-01", "2025-12-31", False, False, sec, root["name"])) > .005: bad.append((sec, root["name"], root["total"]))
        def chk(n):
            if abs(n["total"] - (n["own"] + sum(c["total"] for c in n["children"]))) > .005: bad.append(("subtotal", n["name"]))
            [chk(c) for c in n["children"]]
        [chk(n) for n in R[sec]["categories"]]
        if abs(sum(n["total"] for n in R[sec]["categories"]) - R[sec]["total"]) > .005: bad.append(("roots", sec))
    check(f"2025: every top-level category (incl. subcategories) equals direct SQL; subtotals add up ({sum(len(R[s]['categories']) for s in ('income','expense'))} roots)", not bad, bad[:5])
    sub = next(n for n in R["expense"]["categories"] if n["children"])
    ch = sub["children"][0]
    check(f"hierarchy: {sub['name']} (total {sub['total']:,.2f}) has child {ch['name']} ({ch['total']:,.2f}) == SQL", abs(ch["total"] + sql_total("2025-01-01", "2025-12-31", False, False, "expense", ch["name"])) < .005)
    # splits at the split amount
    t = raw.execute("SELECT t.txn_id, t.date FROM transactions t WHERE t.split_count>1 AND t.date BETWEEN '2025-01-01' AND '2025-12-31' AND t.transfer_account_id IS NULL AND (SELECT COUNT(DISTINCT category) FROM splits WHERE txn_id=t.txn_id AND transfer_account_id IS NULL)>1 LIMIT 1").fetchone()
    sp = raw.execute("SELECT category, amount FROM splits WHERE txn_id=? AND transfer_account_id IS NULL AND category IS NOT NULL", (t[0],)).fetchall()
    got = {}
    for sec in ("income", "expense"):
        for cat in {x[0] for x in sp}:
            for r in reports.category_transactions(db, cat, sec, "2025-01-01", "2025-12-31")["rows"]:
                if r["txn_id"] == t[0] and r["category"] == cat: got[(cat, r["amount"])] = got.get((cat, r["amount"]), 0) + 1
    check(f"split txn {t[0]}: each split line appears at its split amount ({len(sp)} lines)", all(got.get((c, a), 0) >= 1 for c, a in sp), (sp, got))
    # transfers excluded
    ids = set(); 
    for sec in ("income", "expense"):
        for root in R[sec]["categories"]:
            ids |= {r["txn_id"] for r in reports.category_transactions(db, root["name"], sec, "2025-01-01", "2025-12-31")["rows"] if not r["split"]}
    tr = raw.execute(f"SELECT COUNT(*) FROM transactions WHERE transfer_account_id IS NOT NULL AND txn_id IN ({','.join(map(str, ids)) or 0})").fetchone()[0]
    ntr = raw.execute("SELECT COUNT(*) FROM transactions WHERE transfer_account_id IS NOT NULL AND date BETWEEN '2025-01-01' AND '2025-12-31' AND register_type!='Invst'").fetchone()[0]
    check(f"transfers excluded (0 of {ntr} 2025 transfer rows appear)", tr == 0 and ntr > 0, tr)
    ob = raw.execute("SELECT COUNT(*) FROM transactions WHERE payee_text LIKE 'Opening Balance%'").fetchone()[0]
    check(f"opening-balance rows excluded ({ob} exist)", all("Opening Balance" not in r["payee"] for r in reports.category_transactions(db, reports.UNCAT, "income", "1990-01-01", "2026-12-31")["rows"] + reports.category_transactions(db, reports.UNCAT, "expense", "1990-01-01", "2026-12-31")["rows"]))
    h0, h1 = reports.spending(db, "1990-01-01", "2026-12-31", False), reports.spending(db, "1990-01-01", "2026-12-31", True)
    check("include hidden accounts changes totals by exactly the hidden accounts' rows", abs((h1["expense"]["total"] - h0["expense"]["total"]) - (-(sql_total("1990-01-01", "2026-12-31", True, False, "expense") - sql_total("1990-01-01", "2026-12-31", False, False, "expense")))) < .005 and (h1["expense"]["total"] != h0["expense"]["total"] or h1["income"]["total"] != h0["income"]["total"]))
    s0, s1 = reports.spending(db, "1990-01-01", "2026-12-31", False, False), reports.spending(db, "1990-01-01", "2026-12-31", False, True)
    check("exclude stale removes the STALE? rows' amounts", (s0["expense"]["total"], s0["income"]["total"]) != (s1["expense"]["total"], s1["income"]["total"]))
    # net worth
    today = datetime.date.today().isoformat()
    AB = core.account_balances(db)
    for hidden in (False, True):
        N = reports.networth(db, "2016-01-01", today, "month", hidden)
        want = sum(a["balance"] for a in AB if hidden or not a["hidden"]) / 100
        check(f"net worth today (hidden={hidden}) {N['points'][-1]['net']:,.2f} == sum of account-list balances {want:,.2f}", abs(N["points"][-1]["net"] - want) < .005 and N["points"][-1]["date"] == today, N["points"][-1]["date"])
        by = {}
        for a in AB:
            if hidden or not a["hidden"]: by[a["type"]] = by.get(a["type"], 0) + a["balance"]
        check(f"  by-type breakdown equals account-list type totals", all(abs(N["points"][-1]["types"].get(t, 0) - v / 100) < .005 for t, v in by.items()))
    N = reports.networth(db, "2016-01-01", today, "month")
    check("monthly points are month ends, then today", all(pt["date"][5:] in ("01-31", "02-28", "02-29", "03-31", "04-30", "05-31", "06-30", "07-31", "08-31", "09-30", "10-31", "11-30", "12-31") for pt in N["points"][:-1]) and len(N["points"]) > 100)
    Y = reports.networth(db, "1995-01-01", today, "auto")
    check(f"long range switches to yearly ({Y['granularity']}, {len(Y['points'])} points)", Y["granularity"] == "year" and all(pt["date"][5:] == "12-31" for pt in Y["points"][:-1]))
    # spot-check one past month end against register running balances
    D = "2024-06-30"; ND = reports.networth(db, "2024-06-30", "2024-06-30", "month", True, detail=True)["points"][0]
    assert ND["date"] == D
    mism, cnt = [], 0
    for a in db.execute("SELECT account_id, name, register_type FROM accounts"):
        R = core.register(db, a["account_id"], sort="date", direction="asc", limit=10**7)
        last = [r for r in R["rows"] if r["date"] <= D]
        run = round(last[-1]["balance"] * 100) if last else 0
        if a["register_type"] == "Invst":
            pass  # register balance is cash only; securities valued separately
        want = ND["accounts"][a["account_id"]]
        if a["register_type"] != "Invst":
            cnt += 1
            if run != want: mism.append((a["name"], run, want))
        else:
            if want < run - 10**12: mism.append(a["name"])
    check(f"spot check {D}: {cnt} non-investment accounts equal register running balance (cents)", not mism, mism[:5])
    lp = core.last_prices(db, D); secsym = {r["name"]: r["symbol"] for r in db.execute("SELECT name, symbol FROM securities")}; ibad = []; ni = 0
    for a in db.execute("SELECT account_id, name FROM accounts WHERE register_type='Invst'"):
        rows = [r for r in db.execute("SELECT * FROM transactions WHERE account_id=? AND date<=? ORDER BY date, txn_id", (a["account_id"], D))]
        cash = sum(core.effect(r, True) for r in rows); mv = core.securities_value(rows, secsym, lp)[0]; ni += 1
        if cash + mv != ND["accounts"][a["account_id"]]: ibad.append((a["name"], cash + mv, ND["accounts"][a["account_id"]]))
    check(f"spot check {D}: {ni} investment accounts = cash + securities at last price on/before that date (core.last_prices(D))", not ibad, ibad[:3])
    print("   net worth", D, f"{ND and reports.networth(db, D, D, 'month')['points'][0]['net']:,.2f}")
    # API + UI on scratch
    st, S = get("/api/reports/spending?from=2025-01-01&to=2025-12-31"); check("API spending == function", st == 200 and S == json.loads(json.dumps(reports.spending(db, "2025-01-01", "2025-12-31"))))
    st, T = get("/api/reports/spending/transactions?category=Groceries&section=expense&from=2025-01-01&to=2025-12-31")
    check("API category drill-down total == category total", st == 200 and abs(T["total"] * -1 - next(n["total"] for n in S["expense"]["categories"] if n["name"] == "Groceries")) < .005 and T["count"] > 10, (st, T.get("total")))
    check("bad date -> 400", get("/api/reports/spending?from=x&to=2025-01-01")[0] == 400 and get("/api/reports/networth?from=2025-01-01&to=2024-01-01")[0] == 400)
    st, NWj = get("/api/reports/networth?from=2023-01-01&to=2026-10-31&gran=month"); check("API networth ok", st == 200 and len(NWj["points"]) > 30)
    for page, needles in (("spending", ("Spending by category", "Income", "Expense", "Net", "Groceries")), ("networth", ("Net worth", "<svg", "polyline", "Assets", "Liabilities"))):
        out = subprocess.run(["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--virtual-time-budget=8000", "--dump-dom", f"http://127.0.0.1:{PORT}/#/{page}"], capture_output=True, text=True, timeout=90).stdout
        check(f"{page} page renders ({', '.join(needles)})", all(x in out for x in needles), out[-300:])
    check("reports wrote nothing (real DB: same transaction and edit_log counts)", raw.execute("SELECT COUNT(*), (SELECT COUNT(*) FROM edit_log) FROM transactions").fetchone() == n_before)
finally:
    p.terminate(); shutil.rmtree(tmp, ignore_errors=True)
print("ALL PASS" if ok else "FAILURES"); sys.exit(0 if ok else 1)
