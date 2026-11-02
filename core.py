"""Ledger core: DB init/migrations + balance and register logic. stdlib only."""
import datetime, json, os, shutil, sqlite3, sys
from decimal import Decimal as Dc

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("LEDGER_DB", os.path.join(HERE, "data", "ledger.sqlite"))
SCHEMA_VERSION = 10

# Investment cash rules (a best reading of QIF semantics, not Quicken's own number).
CASH_PLUS = {"Div", "CGLong", "CGShort", "IntInc", "Sell", "XIn", "Cash", "ContribX", "MiscInc"}
CASH_MINUS = {"Buy", "XOut", "CvrShrt", "MiscExp"}
SH_PLUS = {"Buy", "BuyX", "ReinvDiv", "ReinvLg", "ReinvSh", "ReinvInt", "ShrsIn", "CvrShrt"}
SH_MINUS = {"Sell", "SellX", "ShrsOut"}
INV_ACTIONS = {"Buy", "Sell", "Div", "ReinvDiv", "IntInc", "XIn", "XOut", "MiscExp", "ShrsIn", "ShrsOut", "StkSplit", "ReinvInt", "ReinvLg", "ReinvSh", "CGLong", "CGShort"}  # editable in the app


def cents(x):
    return 0 if x is None else int(round(float(x) * 100))


# The base tables Ledger reads. A new install starts with these empty; the optional separate importer (ledger-quicken-import) can fill them from a Quicken export.
BASE_DDL = """
CREATE TABLE accounts(account_id INTEGER PRIMARY KEY, name TEXT UNIQUE, list_type TEXT, register_type TEXT, description TEXT, credit_limit REAL, src_line INTEGER, source TEXT DEFAULT 'current', archive_names TEXT);
CREATE TABLE categories(category_id INTEGER PRIMARY KEY, name TEXT UNIQUE, parent_name TEXT, description TEXT, is_income INTEGER, is_expense INTEGER, tax_related INTEGER, tax_schedule TEXT, in_category_list INTEGER, source TEXT DEFAULT 'current');
CREATE TABLE tags(tag_id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT, in_tag_list INTEGER, source TEXT DEFAULT 'current');
CREATE TABLE payees(payee_id INTEGER PRIMARY KEY, name TEXT UNIQUE, source TEXT DEFAULT 'current');
CREATE TABLE securities(security_id INTEGER PRIMARY KEY, name TEXT UNIQUE, symbol TEXT, type TEXT, goal TEXT, source TEXT DEFAULT 'current');
CREATE TABLE prices(price_id INTEGER PRIMARY KEY, security_id INTEGER, symbol TEXT, date TEXT, price REAL, src_line INTEGER, source TEXT DEFAULT 'current');
CREATE TABLE transactions(txn_id INTEGER PRIMARY KEY, account_id INTEGER, register_type TEXT, date TEXT, amount REAL, amount_u REAL,
  cleared_raw TEXT, cleared_status TEXT, check_number TEXT, payee_text TEXT, payee_id INTEGER, memo TEXT,
  category_id INTEGER, category TEXT, transfer_account_id INTEGER, transfer_account TEXT, tag_id INTEGER, tag TEXT,
  split_count INTEGER, address TEXT, flags TEXT,
  action TEXT, security_id INTEGER, security_name TEXT, invst_json TEXT, src_line INTEGER, source TEXT DEFAULT 'current', source_account_name TEXT, merge_note TEXT);
CREATE TABLE splits(split_id INTEGER PRIMARY KEY, txn_id INTEGER, split_seq INTEGER, category_id INTEGER, category TEXT,
  transfer_account_id INTEGER, transfer_account TEXT, tag_id INTEGER, tag TEXT, memo TEXT, amount REAL, percent REAL, source TEXT DEFAULT 'current');
CREATE TABLE memorized_transactions(memorized_id INTEGER PRIMARY KEY, kind TEXT, payee_text TEXT, amount REAL, cleared_raw TEXT, memo TEXT,
  category_id INTEGER, category TEXT, transfer_account_id INTEGER, transfer_account TEXT, tag TEXT, address TEXT, src_line INTEGER, source TEXT DEFAULT 'current');
CREATE TABLE security_positions(account_id INTEGER, account TEXT, security TEXT, symbol TEXT, shares REAL, last_price_date TEXT, last_price REAL, market_value REAL);
CREATE TABLE opening_balance_check(account_id INTEGER, account TEXT, ob_txn_id INTEGER, suggested_ob_replacement REAL, suggested_ob_replacement_end REAL, status TEXT);
"""


def create_schema(path=None):
    """Make a new, empty working database with the base tables (migrate() then brings it to the current version)."""
    path = path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db = sqlite3.connect(path); db.executescript(BASE_DDL); db.commit(); db.close()


def init_db():
    """Create a new empty ledger when there is no database yet, then migrate (with a dated backup when the database already existed)."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    new = not os.path.exists(DB_PATH)
    if new:
        create_schema()
        print(f"starting empty: created a new empty ledger at {DB_PATH}", file=sys.stderr)
    migrate(backup=not new)


def migrate(backup=True):
    db = sqlite3.connect(DB_PATH)
    ver = db.execute("PRAGMA user_version").fetchone()[0]
    if ver >= SCHEMA_VERSION:
        db.close(); return
    db.close()
    if backup:  # a brand-new empty database has nothing to lose
        bdir = os.path.join(os.path.dirname(DB_PATH), "backups")
        os.makedirs(bdir, exist_ok=True)
        bak = os.path.join(bdir, f"ledger-{datetime.datetime.now():%Y%m%d-%H%M%S}-pre-v{SCHEMA_VERSION}.sqlite")
        shutil.copyfile(DB_PATH, bak); print("backup:", bak, file=sys.stderr)
    db = sqlite3.connect(DB_PATH)
    if ver < 1:
        db.executescript("""
          CREATE INDEX IF NOT EXISTS ix_txn_acct_date ON transactions(account_id, date, txn_id);
          CREATE INDEX IF NOT EXISTS ix_splits_txn ON splits(txn_id);
          CREATE INDEX IF NOT EXISTS ix_prices_sym_date ON prices(symbol, date);""")
    if ver < 2:  # edit log (reversible edits) + transfer links for app-created/edited transfers
        db.executescript("""
          CREATE TABLE IF NOT EXISTS edit_log(id INTEGER PRIMARY KEY, batch TEXT, ts TEXT, op TEXT, tbl TEXT, pk INTEGER,
            account_id INTEGER, before_json TEXT, after_json TEXT, note TEXT);
          CREATE INDEX IF NOT EXISTS ix_editlog_batch ON edit_log(batch);
          ALTER TABLE transactions ADD COLUMN link_id INTEGER;
          ALTER TABLE splits ADD COLUMN link_txn_id INTEGER;""")
    if ver < 3:  # per-account hidden flag (export has none: QIF !Account only carries N/T/D/L, so all start visible)
        db.execute("ALTER TABLE accounts ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
    if ver < 4:  # CSV import: external (Plaid) id on the transaction + slug->account mapping
        db.executescript("""
          ALTER TABLE transactions ADD COLUMN ext_id TEXT;
          CREATE INDEX IF NOT EXISTS ix_txn_ext ON transactions(ext_id) WHERE ext_id IS NOT NULL;
          CREATE TABLE IF NOT EXISTS import_map(slug TEXT PRIMARY KEY, account_id INTEGER, confirmed INTEGER NOT NULL DEFAULT 0, updated TEXT);""")
    if ver < 5:  # reconcile: last statement per account (rows marked R live in transactions.cleared_status, as in the export)
        db.executescript("""
          CREATE TABLE IF NOT EXISTS reconcile(id INTEGER PRIMARY KEY, account_id INTEGER, statement_date TEXT, statement_balance REAL,
            rows_marked INTEGER, ts TEXT, batch TEXT);
          CREATE INDEX IF NOT EXISTS ix_reconcile_acct ON reconcile(account_id, id);""")
    if ver < 6:  # Account Details dialog fields (the export has only name/type/description/credit limit)
        for col in ("acct_type TEXT", "tax_deferred INTEGER", "single_fund INTEGER", "institution TEXT", "acct_number TEXT", "customer_id TEXT",
                    "contact_name TEXT", "phone TEXT", "url_home TEXT", "url_activity TEXT", "url_other TEXT", "comments TEXT",
                    "tracking TEXT", "prefilled TEXT"):
            db.execute(f"ALTER TABLE accounts ADD COLUMN {col}")
        db.row_factory = sqlite3.Row
        for a in db.execute("SELECT * FROM accounts").fetchall():
            vals, guessed = guess_details(a)
            if vals:
                db.execute(f"UPDATE accounts SET {','.join(k + '=?' for k in vals)}, prefilled=? WHERE account_id=?",
                           [*vals.values(), ",".join(guessed) or None, a["account_id"]])
    if ver < 7:  # per-account download settings (read by the bot that fetches csv files; the app itself never downloads). Accounts that already have an import mapping start enabled.
        db.executescript("""
          ALTER TABLE accounts ADD COLUMN dl_enabled INTEGER NOT NULL DEFAULT 0;
          ALTER TABLE accounts ADD COLUMN dl_schedule TEXT NOT NULL DEFAULT 'monthly';
          UPDATE accounts SET dl_enabled=1 WHERE account_id IN (SELECT account_id FROM import_map);""")
    if ver < 8:  # scheduled transactions (bills & deposits). Category/transfer/split targets are stored by id, so renames follow.
        db.executescript("""
          CREATE TABLE IF NOT EXISTS scheduled(sched_id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, payee TEXT, memo TEXT, amount REAL NOT NULL,
            category_id INTEGER, transfer_account_id INTEGER, splits_json TEXT, freq TEXT NOT NULL, next_date TEXT NOT NULL, anchor_day INTEGER,
            end_date TEXT, remaining INTEGER, auto INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, last_entered TEXT);""")
    if ver < 9:  # scheduled investment transactions (the body is kept as json); a saved csv column mapping per import slug
        db.executescript("""
          ALTER TABLE scheduled ADD COLUMN inv_json TEXT;
          ALTER TABLE import_map ADD COLUMN csv_map TEXT;""")
    if ver < 10:  # household inventory (valuables list; files live in data/inventory/<item_id>/) and a small key/value settings table
        db.executescript("""
          CREATE TABLE IF NOT EXISTS inventory_items(item_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, category TEXT, make TEXT, model TEXT, serial TEXT,
            location TEXT, purchase_date TEXT, purchase_price REAL, current_value REAL, value_date TEXT, notes TEXT, txn_id INTEGER, created TEXT, updated TEXT);
          CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);""")
    db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    db.commit(); db.close()


# Account type labels shown in the Account Details dialog -> register family (accounts.register_type). Order = dropdown order.
ACCT_TYPES = [("Checking", "Bank"), ("Savings", "Bank"), ("Money Market", "Bank"), ("CD", "Bank"), ("Other bank", "Bank"),
              ("Credit Card", "CCard"), ("Cash", "Cash"), ("Brokerage", "Invst"), ("Traditional IRA", "Invst"), ("Roth IRA", "Invst"),
              ("401(k)/403(b)", "Invst"), ("Other investment", "Invst"), ("Asset", "Oth A"), ("Liability", "Oth L")]
FAMILY_DEFAULT = {"Bank": "Other bank", "CCard": "Credit Card", "Cash": "Cash", "Invst": "Other investment", "Oth A": "Asset", "Oth L": "Liability"}
# keyword -> institution (matched on the account name + description; first hit wins). Guesses, shown as such in the dialog.
INSTITUTIONS = [(r"amex|american express", "American Express"), (r"\bciti", "Citibank"), (r"ally", "Ally Bank"), (r"chase", "JPMorgan Chase"),
                (r"fidelity", "Fidelity Investments"), (r"schwab", "Charles Schwab & Co., Inc."), (r"vanguard", "Vanguard"),
                (r"discover", "Discover"), (r"wells", "Wells Fargo"), (r"bank of america|\bbofa\b", "Bank of America")]


def guess_details(a):
    """First-fill for the Account Details dialog from what an imported account carries: (values, names of guessed fields).
    Export-sourced fields (name, description, credit limit) are already in the row; the rest is read from the account name/description."""
    import re
    fam = a["register_type"] or a["list_type"]
    if fam not in FAMILY_DEFAULT: return {}, []
    txt = f"{a['name']} {a['description'] or ''}"
    vals, g = {}, []
    lab = None
    if fam == "Bank":
        lab = "Checking" if re.search(r"checking", txt, re.I) else "Money Market" if re.search(r"money market", txt, re.I) else \
            "CD" if re.search(r"\bCD\b", txt) else "Savings" if re.search(r"saving", txt, re.I) else None
    elif fam == "Invst":
        lab = "Roth IRA" if re.search(r"roth", txt, re.I) else "Traditional IRA" if re.search(r"\bIRA\b", txt) else \
            "401(k)/403(b)" if re.search(r"401|savings plan", txt, re.I) else "Brokerage" if re.search(r"fidelity|vanguard", txt, re.I) else None
    if lab: vals["acct_type"] = lab; g.append("acct_type")
    for pat, inst in INSTITUTIONS:
        if re.search(pat, txt, re.I): vals["institution"] = inst; g.append("institution"); break
    m = re.search(r"\b[xX]{2,}(\d{3,5})\b", a["name"])
    if m: vals["acct_number"] = "XX" + m[1]; g.append("acct_number")
    if fam == "Invst":
        vals["tax_deferred"] = 1 if re.search(r"\bIRA\b|roth|401|savings plan", txt, re.I) else 0; g.append("tax_deferred")
        vals["single_fund"] = 0; vals["tracking"] = "complete"
    return vals, g


def backup(tag):
    bdir = os.path.join(os.path.dirname(DB_PATH), "backups"); os.makedirs(bdir, exist_ok=True)
    stem = os.path.join(bdir, f"ledger-{datetime.datetime.now():%Y%m%d-%H%M%S}-{tag}"); bak, n = stem + ".sqlite", 1
    while os.path.exists(bak): n += 1; bak = f"{stem}-{n}.sqlite"   # two backups in the same second must not overwrite each other
    src = sqlite3.connect(DB_PATH); dst = sqlite3.connect(bak); src.backup(dst); dst.close(); src.close()
    return bak


def sync_account_names(db):
    """transfer_account / security_positions.account hold the account's name as text next to its id; keep them equal to accounts.name."""
    for tbl, col, idc in (("transactions", "transfer_account", "transfer_account_id"), ("splits", "transfer_account", "transfer_account_id"),
                          ("memorized_transactions", "transfer_account", "transfer_account_id"), ("security_positions", "account", "account_id")):
        db.execute(f"UPDATE {tbl} SET {col}=(SELECT name FROM accounts a WHERE a.account_id={tbl}.{idc}) "
                   f"WHERE {idc} IS NOT NULL AND {col} IS NOT (SELECT name FROM accounts a WHERE a.account_id={tbl}.{idc}) "
                   f"AND EXISTS(SELECT 1 FROM accounts a WHERE a.account_id={tbl}.{idc})")


def get_setting(db, key, default=None):
    try: r = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError: return default   # a database older than v10
    return default if r is None else r[0]


def set_setting(key, value):
    db = sqlite3.connect(DB_PATH)
    db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    db.commit(); db.close()


def household_on(db):
    """Net worth includes the inventory's 'Household items' line unless the setting is turned off (default on)."""
    return get_setting(db, "household_networth", "1") != "0"


def set_hidden(aid, hidden):
    db = sqlite3.connect(DB_PATH)
    n = db.execute("UPDATE accounts SET hidden=? WHERE account_id=?", (1 if hidden else 0, aid)).rowcount
    db.commit(); db.close()
    return n


def connect():
    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def is_stale(r):
    return bool(r["merge_note"]) and "STALE?" in r["merge_note"]


def effect(r, inv):
    """Cash effect of a register row, in cents."""
    if not inv:
        return cents(r["amount"])
    act = r["action"]
    j = json.loads(r["invst_json"] or "{}")
    amt = cents(r["amount"]) if r["amount"] is not None else cents(j.get("$"))
    return amt if act in CASH_PLUS else -amt if act in CASH_MINUS else 0


def last_prices(db, today, dated=False):
    """symbol -> latest price on/before today (Decimal); dated=True: symbol -> (price, date)."""
    lp = {}
    for r in db.execute("SELECT symbol, price, date FROM prices WHERE price IS NOT NULL AND date<=? ORDER BY date, price_id", (today,)):
        lp[r["symbol"]] = (Dc(repr(r["price"])), r["date"]) if dated else Dc(repr(r["price"]))
    return lp


def securities_value(rows, secsym, lp):
    sh = {}
    for r in rows:
        act, name = r["action"], r["security_name"]
        if not name: continue
        q = Dc(repr((json.loads(r["invst_json"] or "{}")).get("Q") or 0))
        if act in SH_PLUS: sh[name] = sh.get(name, Dc(0)) + q
        elif act in SH_MINUS: sh[name] = sh.get(name, Dc(0)) - q
        elif act == "StkSplit": sh[name] = sh.get(name, Dc(0)) * q / 10
    mv = Dc(0)
    for name, s in sh.items():
        p = lp.get(secsym.get(name))
        if p is not None: mv += s * p
    return int((mv * 100).to_integral_value()), sh


def account_balances(db, today=None):
    """Per-account balances (cents). 'bal' = as of today; others include all dated rows."""
    today = today or datetime.date.today().isoformat()
    lp = last_prices(db, today)
    secsym = {r["name"]: r["symbol"] for r in db.execute("SELECT name, symbol FROM securities")}
    ob = {r["ob_txn_id"]: r for r in db.execute("SELECT * FROM opening_balance_check WHERE ob_txn_id IS NOT NULL")}
    rows_by = {}
    for r in db.execute("SELECT * FROM transactions ORDER BY account_id, date, txn_id"):
        rows_by.setdefault(r["account_id"], []).append(r)
    touched = {r[0] for r in db.execute(
        "SELECT account_id FROM edit_log WHERE batch NOT LIKE 'revert:%' AND batch NOT IN "
        "(SELECT substr(batch,8) FROM edit_log WHERE batch LIKE 'revert:%') "
        "AND COALESCE(note,'') NOT IN ('reconcile','clear toggle','account details','scheduled setup')")}
    out = []
    for a in db.execute("SELECT * FROM accounts ORDER BY list_type, name COLLATE NOCASE"):
        rows = rows_by.get(a["account_id"], [])
        inv = a["register_type"] == "Invst"
        cur = [r for r in rows if r["date"] <= today]
        d = dict(account_id=a["account_id"], name=a["name"], hidden=bool(a["hidden"]), type=a["register_type"] or a["list_type"],
                 inv=inv, txn_count=len(rows), first_date=rows[0]["date"] if rows else None,
                 last_date=rows[-1]["date"] if rows else None,
                 future=len(rows) - len(cur), stale_rows=sum(is_stale(r) for r in rows))
        def bal(rs, adj=None):
            c = 0
            for r in rs:
                if adj and r["txn_id"] in ob:
                    c += cents(ob[r["txn_id"]][adj])
                else:
                    c += effect(r, inv)
            return c
        mv_all = securities_value(rows, secsym, lp)[0] if inv else 0
        mv_cur = securities_value(cur, secsym, lp)[0] if inv else 0
        mv_ns = securities_value([r for r in rows if not is_stale(r)], secsym, lp)[0] if inv else 0
        d["cash"] = bal(cur); d["securities"] = mv_cur
        d["balance"] = d["cash"] + mv_cur
        d["balance_all"] = bal(rows) + mv_all  # incl. future-dated
        d["balance_excl_stale"] = bal([r for r in rows if not is_stale(r)]) + mv_ns
        has_ob = any(r["txn_id"] in ob for r in rows) and a["account_id"] not in touched  # suggestions invalid once edited
        d["edited"] = a["account_id"] in touched
        d["balance_ob_adj"] = bal(rows, "suggested_ob_replacement_end") + mv_all if has_ob else None
        out.append(d)
    return out


def register(db, aid, q="", sort="date", direction="desc", offset=0, limit=200, hide_stale=False, today=None):
    a = db.execute("SELECT * FROM accounts WHERE account_id=?", (aid,)).fetchone()
    if not a: return None
    inv = a["register_type"] == "Invst"
    splits = {}
    for s in db.execute("SELECT s.* FROM splits s JOIN transactions t USING(txn_id) WHERE t.account_id=? ORDER BY s.txn_id, s.split_seq", (aid,)):
        splits.setdefault(s["txn_id"], []).append(dict(category=s["category"], transfer=s["transfer_account"],
                         memo=s["memo"], amount=s["amount"], tag=s["tag"]))
    items, run = [], 0
    locked_ids = {r[0] for r in db.execute(
        "SELECT t.txn_id FROM transactions t JOIN transactions p ON p.txn_id=t.link_id WHERE t.account_id=? AND (p.split_count>0 OR (p.register_type='Invst' AND NOT "
        "(p.action IN ('XIn','XOut') AND p.link_id=t.txn_id AND p.transfer_account_id=t.account_id)))", (aid,))}
    allrows = []
    for r in db.execute("SELECT * FROM transactions WHERE account_id=? ORDER BY date, txn_id", (aid,)):
        st = is_stale(r)
        if hide_stale and st: continue
        run += effect(r, inv); allrows.append(r)
        j = json.loads(r["invst_json"] or "{}") if inv else {}
        items.append(dict(txn_id=r["txn_id"], date=r["date"], num=(r["action"] if inv else r["check_number"]) or "",
                          payee=r["payee_text"] or "", memo=r["memo"] or "", security=r["security_name"] or "",
                          category=r["category"] or ("--Split--" if r["split_count"] else ""),
                          transfer=r["transfer_account"] or "", tag=r["tag"] or "",
                          cleared={"cleared": "c", "reconciled": "R"}.get(r["cleared_status"], ""),
                          qty=j.get("Q"), price=j.get("I"), amount=r["amount"], acquired=j.get("D"), lot=j.get("L"), group=j.get("G"), balance=run / 100,
                          stale=st, note=r["merge_note"] if st or (r["merge_note"] and "opening" in r["merge_note"]) else None,
                          source=r["source"], splits=splits.get(r["txn_id"], []), link_id=r["link_id"],
                          locked=((None if r["action"] in INV_ACTIONS and not r["split_count"] else f"action {r['action']} is not editable here") if inv else "mirror of a split transfer or investment row; edit the original" if r["txn_id"] in locked_ids else None)))
    holdings = None
    if inv:
        sh = securities_value(allrows, {}, {})[1]
        holdings = sorted(([n, float(s)] for n, s in sh.items() if abs(s) > Dc("0.000001")), key=lambda x: x[0].lower())
    total = len(items)
    terms = q.lower().split()
    if terms:
        def hay(i):
            parts = [i["date"], i["num"], i["payee"], i["memo"], i["security"], i["category"], i["transfer"], i["tag"],
                     "%.2f" % (i["amount"] or 0), "%.2f" % (abs(i["amount"] or 0))]
            for s in i["splits"]:
                parts += [s["category"] or "", s["transfer"] or "", s["memo"] or "", "%.2f" % (s["amount"] or 0)]
            return " ".join(parts).lower()
        items = [i for i in items if all(t in hay(i) for t in terms)]
    keyf = {"date": lambda i: (i["date"], i["txn_id"]), "num": lambda i: i["num"].lower(),
            "payee": lambda i: i["payee"].lower(), "category": lambda i: (i["category"] or i["transfer"]).lower(),
            "amount": lambda i: i["amount"] or 0, "balance": lambda i: i["balance"], "memo": lambda i: i["memo"].lower(),
            "security": lambda i: i["security"].lower()}.get(sort, lambda i: (i["date"], i["txn_id"]))
    items.sort(key=keyf, reverse=(direction == "desc"))
    return dict(account=dict(account_id=aid, name=a["name"], type=a["register_type"] or a["list_type"], inv=inv),
                total=total, matched=len(items), offset=offset, rows=items[offset:offset + limit],
                ending_balance=run / 100, holdings=holdings)
