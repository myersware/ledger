"""Built-in downloads through Plaid (your own developer keys; data goes only between this machine and Plaid).
Keys: env PLAID_CLIENT_ID / PLAID_SECRET / PLAID_ENV (default production) or data/plaid.json (chmod 600). Linked items + access tokens: data/plaid_items.json (chmod 600).
A download only writes csv files to imports/<slug>/<slug>_plaid_<YYYY-MM>.csv; rows reach the ledger through the normal Import preview + confirmation."""
import csv, datetime, glob, json, os, re, threading, urllib.error, urllib.request
from decimal import Decimal
import core, downloads, edits, importer

DATA = os.environ.get("LEDGER_PLAID_DIR", os.path.dirname(core.DB_PATH))
URLS = {"production": "https://production.plaid.com", "sandbox": "https://sandbox.plaid.com"}
SOURCE = "plaid-direct"; LOCK = threading.RLock()
HEAD = "date,payee,amount,memo,category,txn_id,account,source".split(","); IHEAD = HEAD + ["type", "security", "quantity", "price"]


def _private(path):
    if os.path.exists(path) and os.stat(path).st_mode & 0o077: raise edits.ApiError(500, f"{path} is readable by others; run: chmod 600 {path}")


def config():
    """-> (client_id, secret, base_url, days_requested); env wins over data/plaid.json."""
    f, c = os.path.join(DATA, "plaid.json"), {}
    if os.path.exists(f) and not all(os.environ.get(k) for k in ("PLAID_CLIENT_ID", "PLAID_SECRET")):
        _private(f)
        try: c = json.load(open(f))
        except ValueError: raise edits.ApiError(500, f"{f} is not valid JSON")
    g = lambda k: os.environ.get(k) or c.get(k)
    cid, sec, env = g("PLAID_CLIENT_ID"), g("PLAID_SECRET"), (g("PLAID_ENV") or "production").lower()
    if not cid or not sec: raise edits.ApiError(400, "Plaid keys not set: put PLAID_CLIENT_ID and PLAID_SECRET in the environment or in data/plaid.json (chmod 600); see docs/SETUP.md")
    if env not in URLS: raise edits.ApiError(400, "PLAID_ENV must be production or sandbox")
    return cid, sec, os.environ.get("PLAID_BASE_URL") or URLS[env], int(c.get("days_requested") or 90)


def call(path, body):
    cid, sec, base, _ = config()
    req = urllib.request.Request(base + path, data=json.dumps(dict(body, client_id=cid, secret=sec)).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r: return json.loads(r.read(), parse_float=Decimal)
    except urllib.error.HTTPError as e:
        try: j = json.loads(e.read())
        except ValueError: j = {}
        raise PlaidError(j.get("error_code") or str(e.code), j.get("error_message") or "Plaid request failed")
    except (urllib.error.URLError, OSError) as e: raise PlaidError("NETWORK", f"could not reach Plaid: {getattr(e, 'reason', e)}")


class PlaidError(edits.ApiError):
    def __init__(self, code, msg): super().__init__(502, f"Plaid {code}: {msg}"); self.plaid_code = code


# ---- items file (access tokens): chmod 600, written atomically
def load():
    f = os.path.join(DATA, "plaid_items.json")
    if not os.path.exists(f): return {"items": {}}
    _private(f); return json.load(open(f))


def save(d):
    f = os.path.join(DATA, "plaid_items.json"); os.makedirs(DATA, exist_ok=True)
    fd = os.open(f + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh: json.dump(d, fh, indent=1)
    os.replace(f + ".tmp", f)


def overview(db):
    try: config(); ok, why = True, None
    except edits.ApiError as e: ok, why = False, str(e)
    names = {r[0]: r[1] for r in db.execute("SELECT account_id, name FROM accounts")}
    items = [dict(item_id=i, institution=it.get("institution"), error=it.get("error"), last_sync=it.get("last_sync"), accounts=[
             dict(plaid_account_id=p, name=a["name"], mask=a.get("mask"), type=a.get("type"), subtype=a.get("subtype"), ledger_account_id=a.get("ledger_account_id"),
                  ledger_account=names.get(a.get("ledger_account_id"))) for p, a in it["accounts"].items()]) for i, it in load()["items"].items()]
    return dict(configured=ok, problem=why, env=(os.environ.get("PLAID_ENV") or "production"), items=items)


def link_token(kind="bank", item_id=None):
    _, _, _, days = config()
    b = dict(client_name="Ledger", language="en", country_codes=["US"], user={"client_user_id": "ledger-local"})
    if item_id:  # update mode: re-login for an existing item, no exchange needed afterwards
        it = load()["items"].get(item_id)
        if not it: raise edits.ApiError(404, "no such linked item")
        b["access_token"] = it["access_token"]
    elif kind == "investments": b.update(products=["investments"], optional_products=["transactions"])
    elif kind == "bank": b.update(products=["transactions"], optional_products=["investments"], transactions={"days_requested": days})
    else: raise edits.ApiError(400, "kind must be bank or investments")
    return {"link_token": call("/link/token/create", b)["link_token"]}


def exchange(public_token, institution):
    if not isinstance(public_token, str) or not public_token: raise edits.ApiError(400, "public_token required")
    r = call("/item/public_token/exchange", {"public_token": public_token}); tok, iid = r["access_token"], r["item_id"]
    accts = call("/accounts/get", {"access_token": tok})["accounts"]
    with LOCK:
        d = load(); old = d["items"].get(iid, {}).get("accounts", {})
        d["items"][iid] = dict(access_token=tok, institution=str(institution or "")[:80], cursor=None, inv_since=None, error=None, last_sync=None, accounts={
            a["account_id"]: dict(name=a.get("official_name") or a["name"], mask=a.get("mask"), type=a.get("type"), subtype=a.get("subtype"),
                                  ledger_account_id=old.get(a["account_id"], {}).get("ledger_account_id")) for a in accts})
        save(d)
    return dict(item_id=iid, accounts=len(accts))


def set_map(db, item_id, pid, ledger_id):
    with LOCK:
        d = load(); it = d["items"].get(item_id); a = it and it["accounts"].get(pid)
        if not a: raise edits.ApiError(404, "no such Plaid account")
        if ledger_id is not None:
            L = db.execute("SELECT name, register_type FROM accounts WHERE account_id=?", (ledger_id,)).fetchone()
            if not L: raise edits.ApiError(404, "no such ledger account")
            if (a.get("type") == "investment") != (L["register_type"] == "Invst"): raise edits.ApiError(400, f"'{L['name']}' and the Plaid account differ in kind (investment vs bank/card)")
            for ii in d["items"].values():
                for p2, a2 in ii["accounts"].items():
                    if a2.get("ledger_account_id") == ledger_id and p2 != pid: raise edits.ApiError(409, f"'{L['name']}' is already linked to another Plaid account")
        if a.get("ledger_account_id") != ledger_id and ledger_id is not None: it["cursor"] = it["inv_since"] = None  # re-read history so this account gets its rows
        a["ledger_account_id"] = ledger_id; save(d)
    return {"ok": True}


LABEL = {"checking": "Checking", "savings": "Savings", "money market": "Money Market", "cd": "CD", "credit card": "Credit Card", "cash management": "Checking",
         "ira": "Traditional IRA", "roth": "Roth IRA", "401k": "401(k)/403(b)", "403b": "401(k)/403(b)", "brokerage": "Brokerage"}


def create_account(db, item_id, pid, name=None):
    """A Plaid account that has no ledger account yet: make one (type from Plaid's type/subtype, institution from the item) and link it, so the next sync/download fills it."""
    import account_details
    with LOCK:
        it = load()["items"].get(item_id); a = it and it["accounts"].get(pid)
    if not a: raise edits.ApiError(404, "no such Plaid account")
    if a.get("ledger_account_id"): raise edits.ApiError(409, "this Plaid account is already linked to a ledger account")
    kind = a.get("type"); sub = (a.get("subtype") or "").lower()
    lab = LABEL.get(sub) or ("Credit Card" if kind == "credit" else "Brokerage" if kind == "investment" else "Liability" if kind == "loan" else "Other bank")
    nm = " ".join(str(name or a.get("name") or "Plaid account").split())
    if db.execute("SELECT 1 FROM accounts WHERE name=? COLLATE NOCASE", (nm,)).fetchone() and not name and a.get("mask"): nm += f" ...{a['mask']}"
    r = account_details.create(None, dict(name=nm, type=lab, institution=it.get("institution")))
    set_map(db, item_id, pid, r["account_id"])
    return dict(r, type=lab)


def remove(item_id):
    with LOCK:
        d = load(); it = d["items"].get(item_id)
        if not it: raise edits.ApiError(404, "no such linked item")
        call("/item/remove", {"access_token": it["access_token"]}); del d["items"][item_id]; save(d)
    return {"ok": True}


# ---- csv writing
def _fmt(x, n): return f"{Decimal(str(x)):.{n}f}"


def _write(slug, rows, drop, inv):
    """Merge rows (dicts keyed by csv columns) into imports/<slug>/<slug>_plaid_<YYYY-MM>.csv by txn_id; drop = ids to delete. -> rows added or changed."""
    d = os.path.join(importer.IMPORT_DIR, slug); head = IHEAD if inv else HEAD; by = {}
    for r in rows: by.setdefault(r["date"][:7], []).append(r)
    months = set(by) | {m[1] for f in glob.glob(os.path.join(d, f"{slug}_plaid_*.csv")) if (m := re.search(r"_(\d{4}-\d{2})\.csv$", f))}
    n = 0
    for mo in sorted(months):
        f = os.path.join(d, f"{slug}_plaid_{mo}.csv"); cur = list(csv.DictReader(open(f, newline="", encoding="utf-8"))) if os.path.exists(f) else []
        before = [dict(r) for r in cur]
        cur = [r for r in cur if r["txn_id"] not in drop]
        idx = {r["txn_id"]: i for i, r in enumerate(cur)}
        for r in by.get(mo, []):
            if r["txn_id"] in idx: cur[idx[r["txn_id"]]] = r
            else: cur.append(r); idx[r["txn_id"]] = len(cur) - 1
        cur.sort(key=lambda r: r["date"])  # stable: same-day rows keep their order
        if cur == before: continue
        n += sum(1 for r in cur if r not in before)
        os.makedirs(d, exist_ok=True)
        if not cur: os.remove(f); continue
        with open(f + ".tmp", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, head); w.writeheader(); w.writerows({k: r.get(k, "") for k in head} for r in cur)
        os.replace(f + ".tmp", f)
    return n


def _bank_row(t, slug):
    mn, nm = t.get("merchant_name"), t.get("name") or ""
    cat = (t.get("personal_finance_category") or {}).get("detailed") or ""
    return dict(date=t["date"], payee=mn or nm, amount=_fmt(-Decimal(str(t["amount"])), 4), memo=nm if mn and mn != nm else "", category=cat, txn_id="plaid:" + t["transaction_id"], account=slug, source=SOURCE)


def _inv_row(t, slug, sec):
    s = sec.get(t.get("security_id")) or {}; sym = s.get("ticker_symbol") or s.get("name") or ""
    return dict(date=t["date"], payee=t.get("name") or "", amount=_fmt(-Decimal(str(t["amount"])), 4), memo="", category="", txn_id="plaid:" + t["investment_transaction_id"], account=slug,
                source=SOURCE, type=t.get("type") or "", security=sym, quantity=_fmt(t.get("quantity") or 0, 10), price=_fmt(t.get("price") or 0, 8))


def _sync_item(db, iid, it, only=None):
    """One item: write csv rows for its linked accounts. -> dict(slugs={slug: rows written}, warnings=[])"""
    mapped = {p: a for p, a in it["accounts"].items() if a.get("ledger_account_id") and (only is None or a["ledger_account_id"] in only)}
    out = dict(slugs={}, warnings=[]); slug = {}
    for p, a in mapped.items():
        L = db.execute("SELECT name FROM accounts WHERE account_id=?", (a["ledger_account_id"],)).fetchone()
        if not L: out["warnings"].append(f"{a['name']}: linked ledger account no longer exists"); continue
        slug[p] = downloads.ensure_slug(db, a["ledger_account_id"], L["name"]); out["slugs"][slug[p]] = 0
    db.commit()
    bank = [p for p in slug if it["accounts"][p].get("type") != "investment"]; inv = [p for p in slug if it["accounts"][p].get("type") == "investment"]
    days = config()[3]; tok = it["access_token"]
    if bank:
        for attempt in range(3):  # restart if the data changes mid-pagination
            add, drop, nxt, ready = [], set(), it.get("cursor"), True
            try:
                while True:
                    r = call("/transactions/sync", dict(access_token=tok, cursor=nxt, count=500, options={"include_personal_finance_category": True}) if nxt else
                             dict(access_token=tok, count=500, options={"include_personal_finance_category": True}))
                    ready &= r.get("transactions_update_status") != "NOT_READY"  # Plaid still loading history: empty reply, and its cursor must not be kept
                    add += r["added"] + r["modified"]; drop |= {"plaid:" + x["transaction_id"] for x in r["removed"]}; nxt = r["next_cursor"]
                    if not r["has_more"]: break
                break
            except PlaidError as e:
                if e.plaid_code != "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION" or attempt == 2: raise
        posted = [t for t in add if not t.get("pending") and t["account_id"] in slug and t["account_id"] in bank]
        drop |= {"plaid:" + t["transaction_id"] for t in add if t.get("pending")}
        for s in {slug[p] for p in bank}:
            rows = [_bank_row(t, slug[t["account_id"]]) for t in posted if slug[t["account_id"]] == s]
            out["slugs"][s] += _write(s, rows, drop, False)
        if ready: it["cursor"] = nxt
        else: out["warnings"].append("Plaid is still loading this account's history; nothing was downloaded. Try Download again in a minute or two.")
    if inv:
        today = datetime.date.today(); since = it.get("inv_since")
        start = (datetime.date.fromisoformat(since) - datetime.timedelta(days=14)) if since else today - datetime.timedelta(days=days)
        txs, sec, off = [], {}, 0
        while True:
            r = call("/investments/transactions/get", dict(access_token=tok, start_date=start.isoformat(), end_date=today.isoformat(), options=dict(account_ids=inv, count=500, offset=off)))
            sec.update({s["security_id"]: s for s in r["securities"]}); txs += r["investment_transactions"]; off += len(r["investment_transactions"])
            if not r["investment_transactions"] or off >= r["total_investment_transactions"]: break
        for p in inv:
            rows = [_inv_row(t, slug[p], sec) for t in txs if t["account_id"] == p]
            out["slugs"][slug[p]] += _write(slug[p], rows, set(), True)
        it["inv_since"] = today.isoformat()
    for s in out["slugs"]:
        others = [os.path.basename(f) for f in glob.glob(os.path.join(importer.IMPORT_DIR, s, "*.csv")) if f"{s}_plaid_" not in f]
        if others: out["warnings"].append(f"{s}: also has csv files from another source ({len(others)}); import or remove those first, or the same transactions can show up twice")
    return out


def sync(db, account_id=None, everything=False):
    """account_id: that account (must be linked). everything: every linked account with downloads turned on. -> per-item results; writes only csv files (+ import slug)."""
    d0 = load()
    linked = {a["ledger_account_id"] for it in d0["items"].values() for a in it["accounts"].values() if a.get("ledger_account_id")}
    if everything:
        only = {x["account_id"] for x in downloads.enabled(db)} & linked
        skipped = [x["account"] for x in downloads.enabled(db) if x["account_id"] not in linked]
    else:
        if account_id not in linked: raise edits.ApiError(400, "this account is not linked to a Plaid account yet (Account Details > Download > Plaid account)")
        only, skipped = {account_id}, []
    res = []
    for iid in list(d0["items"]):
        with LOCK:
            d = load(); it = d["items"].get(iid)
            if not it or not any(a.get("ledger_account_id") in only for a in it["accounts"].values()): continue
            try:
                out = _sync_item(db, iid, it, only); it["error"] = None; it["last_sync"] = datetime.datetime.now().isoformat(timespec="seconds"); out["error"] = None
            except PlaidError as e:
                out = dict(slugs={}, warnings=[], error=str(e)); it["error"] = str(e) if e.plaid_code in ("ITEM_LOGIN_REQUIRED", "PENDING_EXPIRATION") else it.get("error")
            save(d)
        out.update(item_id=iid, institution=it.get("institution")); res.append(out)
    pv = {p["slug"]: p["counts"] for p in importer.preview(db)}
    for o in res: o["preview"] = {s: pv.get(s) for s in o["slugs"]}
    return dict(items=res, skipped_not_linked=skipped)
