"""Test fixture: a small made-up ledger (7 accounts, 9 years of transactions, two funds), installed as a throwaway working database.
Usage in a test, BEFORE importing core:  import testfix; T = testfix.make();  import core
make() sets LEDGER_DB / LEDGER_IMPORTS to a temp dir, writes the made-up starting database there (T.source) and copies it to T.db."""
import atexit, datetime, json, os, shutil, sqlite3, tempfile

ACCTS = [(1, "Main Checking", "Bank", ""), (2, "Savings", "Bank", ""), (3, "Old CD", "Bank", ""), (4, "Test Visa", "CCard", ""), (5, "Fidelity Test XX1234", "Invst", ""),
         (6, "Schwab Test IRA", "Invst", "Rollover IRA"), (7, "Wallet", "Cash", "")]
CATS = [("Salary", None, 1), ("Interest Inc", None, 1), ("Groceries", None, 0), ("Rent", None, 0), ("Utilities", None, 0), ("Utilities:Electric", "Utilities", 0),
        ("Utilities:Gas", "Utilities", 0), ("Dining", None, 0), ("Household", None, 0), ("Entertainment", None, 0)]
SECS = [(1, "Alpha Fund", "ALPHX", "Mutual Fund"), (2, "Beta Fund", "BETAX", "Mutual Fund"), (3, "Cash Sweep", "CSWP", "Money Market")]


def months(a, b):
    y, m = a
    while (y, m) <= b:
        yield y, m; y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def build_sample(path):
    import core
    db = sqlite3.connect(path); db.executescript(core.BASE_DDL)
    db.executemany("INSERT INTO accounts(account_id,name,list_type,register_type,description,credit_limit) VALUES(?,?,?,?,?,?)", [(i, n, t, t, d or None, 5000 if t == "CCard" else None) for i, n, t, d in ACCTS])
    db.executemany("INSERT INTO categories(category_id,name,parent_name,is_income,is_expense,in_category_list) VALUES(?,?,?,?,?,1)", [(i + 1, n, p, inc, 1 - inc) for i, (n, p, inc) in enumerate(CATS)])
    db.executemany("INSERT INTO securities VALUES(?,?,?,?,NULL,'current')", SECS)
    name = {i: n for i, n, _, _ in ACCTS}; kind = {i: t for i, _, t, _ in ACCTS}; cid = {n: i + 1 for i, (n, _, _) in enumerate(CATS)}
    payees = {}; rows = []; splits = []
    def tx(a, date, amt, payee=None, cat=None, to=None, memo=None, **kw):
        pid = payees.setdefault(payee, len(payees) + 1) if payee else None
        rows.append(dict(txn_id=len(rows) + 1, account_id=a, register_type=kind[a], date=date, amount=amt, amount_u=amt, payee_text=payee, payee_id=pid, memo=memo,
                         category=cat, category_id=cid.get(cat), transfer_account_id=to, transfer_account=name.get(to), split_count=0, invst_json="{}"))
        rows[-1].update(kw); return rows[-1]
    def pair(a, b, date, amt, payee):  # unlinked legacy-style transfer: two rows pointing at each other
        tx(a, date, -amt, payee, to=b); tx(b, date, amt, payee, to=a)
    for y, m in months((2018, 1), (2026, 10)):
        i = (y - 2018) * 12 + m; d = lambda day: f"{y}-{m:02d}-{day:02d}"
        if (y, m) == (2018, 1):
            tx(1, d(1), 5000, "Opening Balance"); tx(2, d(1), 10000, "Opening Balance"); tx(7, d(1), 1000, "Opening Balance")
        tx(1, d(2), 3000, "Employer", "Salary"); tx(1, d(3), -1200, "Landlord", "Rent")
        for k, day in enumerate((8, 15, 22)): tx(1, d(day), -(80 + (i * 7 + k * 13) % 40) - k / 10, "Market", "Groceries")
        tx(1, d(10), -(60 + i % 30), "Power Co", "Utilities:Electric"); tx(1, d(12), -15.99, "Streaming Co", "Entertainment")
        pair(1, 2, d(20), 200, "Transfer"); tx(2, d(28), round(1 + i / 50, 2), "Bank Interest", "Interest Inc")
        tx(4, d(5), -45, "Burger Place", "Dining"); tx(4, d(18), -60, "Big Box", "Groceries"); pair(1, 4, d(26), 100, "Card payment")
        if m % 3 == 0: tx(7, d(14), -(10 + i % 7), "Corner Shop", "Dining")
    tx(3, "2019-01-01", 20000, "Opening Balance"); tx(3, "2019-06-30", 300, "Bank Interest", "Interest Inc")
    r = tx(1, "2025-03-15", -150, "Big Box", "--Split--", split_count=2); splits += [(r["txn_id"], 1, "Groceries", -100), (r["txn_id"], 2, "Household", -50)]
    r = tx(1, "2026-09-14", -90, "Big Box", "--Split--", split_count=2); splits += [(r["txn_id"], 1, "Groceries", -60), (r["txn_id"], 2, "Household", -30)]
    tx(1, "2025-06-10", -25, "Duplicate Charge", "Dining", merge_note="STALE? possible duplicate")
    # investments: a legacy cash-in from checking, monthly buys / dividends, a share transfer-in (an export action the app keeps locked)
    def inv(a, date, act, sec, amt=None, q=None, p=None):
        sid = [x[0] for x in SECS if x[1] == sec][0]
        return tx(a, date, amt, action=act, security_id=sid, security_name=sec, invst_json=json.dumps({k: v for k, v in (("Q", q), ("I", p)) if v is not None}))
    pair(1, 5, "2024-01-05", 20000, "Transfer"); rows[-1].update(action="XIn", payee_text=None, amount=20000, amount_u=20000, register_type="Invst", invst_json='{"$": 20000.0}')
    inv(5, "2024-01-06", "ShrsIn", "Cash Sweep", None, 100.0)
    pair(1, 6, "2024-01-05", 6000, "Transfer"); rows[-1].update(action="XIn", payee_text=None, invst_json='{"$": 6000.0}')
    px = lambda sec, i: round({"ALPHX": 10 + i * 0.1, "BETAX": 20 + i * 0.05, "CSWP": 1}[sec], 2)
    for y, m in months((2024, 2), (2026, 10)):
        i = (y - 2018) * 12 + m; d = lambda day: f"{y}-{m:02d}-{day:02d}"
        for sec, q, day, acct in (("Alpha Fund", 5, 10, 5), ("Beta Fund", 2, 11, 5)):
            p = px(sec == "Alpha Fund" and "ALPHX" or "BETAX", i); inv(acct, d(day), "Buy", sec, round(q * p, 2), q, p)
        inv(5, d(28), "Div", "Beta Fund", 4.20); inv(5, d(28), "ReinvDiv", "Alpha Fund", 1.5, 0.1, 15.0); inv(6, d(28), "Div", "Alpha Fund", 10.00)
    inv(6, "2024-02-01", "Buy", "Alpha Fund", 1000, 100, 10.0)
    for r in rows:  # statement status: old rows reconciled, this year cleared up to the 15th (to September), the rest open
        if kind[r["account_id"]] == "Invst": continue
        s = "R" if r["date"] < "2026-01-01" else "c" if r["date"] < "2026-10-01" and r["date"][8:] <= "15" else None
        r.update(cleared_raw=s, cleared_status={"R": "reconciled", "c": "cleared"}.get(s))
    for r in rows: db.execute(f"INSERT INTO transactions({','.join(r)}) VALUES({','.join('?' * len(r))})", list(r.values()))
    db.executemany("INSERT INTO payees VALUES(?,?,'current')", [(i, n) for n, i in payees.items()])
    for n, (txn, seq, cat, amt) in enumerate(splits, 1): db.execute("INSERT INTO splits(split_id,txn_id,split_seq,category_id,category,amount) VALUES(?,?,?,?,?,?)", (n, txn, seq, cid[cat], cat, amt))
    n = 0
    for _, _, sym, _ in SECS[:3]:
        for y, m in months((2018, 1), (2026, 10)):
            n += 1; dt = f"{y}-{m:02d}-28"; db.execute("INSERT INTO prices VALUES(?,?,?,?,?,NULL,'current')", (n, [s[0] for s in SECS if s[2] == sym][0], sym, dt, px(sym, (y - 2018) * 12 + m)))
    db.commit(); db.row_factory = sqlite3.Row
    today = datetime.date.today().isoformat(); lp = core.last_prices(db, today); secsym = {s[1]: s[2] for s in SECS}
    for a, nm, t, _ in ACCTS:
        rs = db.execute("SELECT * FROM transactions WHERE account_id=? ORDER BY date, txn_id", (a,)).fetchall(); inv_ = t == "Invst"
        cash = sum(core.effect(r, inv_) for r in rs) / 100; mv, sh = core.securities_value(rs, secsym, lp) if inv_ else (0, {})
        for s, q in sh.items():
            if q: db.execute("INSERT INTO security_positions VALUES(?,?,?,?,?,?,?,?)", (a, nm, s, secsym[s], float(q), "2026-10-28", float(lp[secsym[s]]), float(q * lp[secsym[s]])))
    db.commit(); db.close()


class Fixture:
    pass


def make():
    """-> object with .dir .source .db .imports (temp dir, removed at exit); the working DB is a copy of the made-up start database, already migrated."""
    T = Fixture(); T.dir = tempfile.mkdtemp(prefix="ledger-test-"); atexit.register(shutil.rmtree, T.dir, True)
    T.source, T.db, T.imports = (os.path.join(T.dir, p) for p in ("start.sqlite", "data/ledger.sqlite", "imports"))
    os.environ.update(LEDGER_DB=T.db, LEDGER_IMPORTS=T.imports)
    import core
    core.DB_PATH = T.db
    build_sample(T.source); os.makedirs(os.path.dirname(T.db)); shutil.copyfile(T.source, T.db); core.init_db()
    return T


def v5_copy(dest, ver=5):
    """An older working DB (schema `ver`, 5 or 6), made by dropping the later columns from a migrated copy: v6 = Account Details fields, v7 = download settings."""
    import core
    shutil.copyfile(core.DB_PATH, dest); d = sqlite3.connect(dest)
    drop = ("dl_enabled", "dl_schedule") + (("acct_type", "tax_deferred", "single_fund", "institution", "acct_number", "customer_id", "contact_name", "phone", "url_home", "url_activity", "url_other", "comments", "tracking", "prefilled") if ver < 6 else ())
    for c in drop: d.execute(f"ALTER TABLE accounts DROP COLUMN {c}")
    d.execute("ALTER TABLE scheduled DROP COLUMN inv_json"); d.execute("ALTER TABLE import_map DROP COLUMN csv_map")  # v9 columns
    d.execute("DROP TABLE inventory_items"); d.execute("DROP TABLE settings")  # v10 tables
    d.execute(f"PRAGMA user_version={ver}"); d.commit(); d.close()


if __name__ == "__main__":  # python3 testfix.py out.sqlite -> a made-up ledger database to try the app with: LEDGER_DB=out.sqlite python3 app.py
    import sys
    if len(sys.argv) != 2 or os.path.exists(sys.argv[1]): sys.exit("usage: python3 testfix.py NEW-FILE.sqlite  (refuses to overwrite)")
    build_sample(sys.argv[1]); print("wrote sample database:", sys.argv[1])
