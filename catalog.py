"""Categories, securities and manual prices: create / edit / delete through edit_log batches (all revertable)."""
import datetime, math, re
import core, edits
from edits import ApiError, Batch, reading, PK

NOTE = "catalog"

# ---------- categories ----------
STARTER_INC = ["Salary", "Bonus", "Interest Income", "Dividend Income", "Refunds", "Other Income"]
STARTER_EXP = {"Auto": ["Fuel", "Insurance", "Maintenance", "Registration"], "Charity": [], "Childcare": [], "Clothing": [], "Dining": [], "Education": [],
               "Entertainment": [], "Fees & Charges": [], "Gifts": [], "Groceries": [], "Healthcare": ["Dental", "Doctor", "Insurance", "Pharmacy"],
               "Home": ["Furnishings", "Mortgage Interest", "Rent", "Repairs"], "Household": [], "Insurance": [], "Pets": [], "Personal Care": [], "Subscriptions": [],
               "Taxes": ["Federal", "Property", "State"], "Travel": [], "Utilities": ["Electric", "Gas", "Internet", "Phone", "Trash", "Water"], "Miscellaneous": []}


def _name(v):
    n = ":".join(p.strip() for p in str(v or "").split(":"))
    if not n or any(not p for p in n.split(":")): raise ApiError(400, "category name can't be blank or have an empty part (use Parent:Child)")
    if len(n) > 80: raise ApiError(400, "category name is too long")
    return n


def cat_usage(db):
    """name -> dict(txns, splits, memorized, scheduled): rows that use each category."""
    import scheduled
    u = {}
    for k, sql in (("txns", "SELECT category, COUNT(*) FROM transactions WHERE category IS NOT NULL GROUP BY category"),
                   ("splits", "SELECT category, COUNT(*) FROM splits WHERE category IS NOT NULL GROUP BY category"),
                   ("memorized", "SELECT category, COUNT(*) FROM memorized_transactions WHERE category IS NOT NULL GROUP BY category")):
        for n, c in db.execute(sql): u.setdefault(n, dict(txns=0, splits=0, memorized=0, scheduled=0))[k] = c
    ids = {r[0]: r[1] for r in db.execute("SELECT category_id, name FROM categories")}
    for cid, c in scheduled.category_refs(db).items():
        if cid in ids: u.setdefault(ids[cid], dict(txns=0, splits=0, memorized=0, scheduled=0))["scheduled"] = c
    return u


@reading
def cat_list(db, m, qs):
    u = cat_usage(db); out = []
    for r in db.execute("SELECT * FROM categories ORDER BY name COLLATE NOCASE"):
        x = u.get(r["name"], {}); n = sum(x.values())
        kids = db.execute("SELECT COUNT(*) FROM categories WHERE name LIKE ? ESCAPE '\\'", (r["name"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ":%",)).fetchone()[0]
        out.append(dict(id=r["category_id"], name=r["name"], parent=r["parent_name"], kind="income" if r["is_income"] else "expense", description=r["description"],
                        used=n, usage=x, children=kids, can_delete=n == 0 and kids == 0))
    return dict(categories=out, starter_offered=not out)


def _kind(v, default="expense"):
    v = default if v in (None, "") else v
    if v not in ("income", "expense"): raise ApiError(400, "kind must be income or expense")
    return v


def _parent(b, name, cid=None):
    if ":" not in name: return None, None
    p = name.rsplit(":", 1)[0]
    r = b.db.execute("SELECT * FROM categories WHERE name=? COLLATE NOCASE", (p,)).fetchone()
    if not r: raise ApiError(400, f"parent category '{p}' doesn't exist; create it first")
    return r["name"], r


def _dup(b, name, cid=None):
    if b.db.execute("SELECT 1 FROM categories WHERE name=? COLLATE NOCASE AND category_id IS NOT ?", (name, cid)).fetchone():
        raise ApiError(409, f"there is already a category named '{name}'")


def _ins_cat(b, name, kind, desc=None):
    parent, pr = _parent(b, name); _dup(b, name)
    if pr: kind = "income" if pr["is_income"] else "expense"
    return b.insert("categories", dict(name=name, parent_name=parent, description=desc, is_income=int(kind == "income"), is_expense=int(kind == "expense"),
                                       tax_related=0, in_category_list=1, source="ledger"))


def cat_create(m, d):
    b = Batch(None, NOTE)
    try:
        n = _name(d.get("name")); cid = _ins_cat(b, n, _kind(d.get("kind")), (d.get("description") or "").strip() or None)
        out = dict(category_id=cid, batch=b.name); b.commit(); return out
    except Exception: b.rollback(); raise


def cat_starter(m, d):
    """Add the starter list (skips names that already exist)."""
    b = Batch(None, "starter categories")
    try:
        names = [(n, "income") for n in STARTER_INC] + [(p, "expense") for p in STARTER_EXP] + [(f"{p}:{c}", "expense") for p, cs in STARTER_EXP.items() for c in cs]
        n = 0
        for name, kind in names:
            if not b.db.execute("SELECT 1 FROM categories WHERE name=? COLLATE NOCASE", (name,)).fetchone(): _ins_cat(b, name, kind); n += 1
        out = dict(added=n, batch=b.name if n else None); b.commit(); return out
    except Exception: b.rollback(); raise


def cat_update(m, d):
    cid = int(m[1]); b = Batch(None, NOTE)
    try:
        c = b.row("categories", cid)
        if not c: raise ApiError(404, "no such category")
        old = c["name"]; new = _name(d["name"]) if "name" in d else old
        if new.lower() == old.lower() and new != old: pass  # case-only rename
        elif new != old:
            if (new + ":").lower().startswith(old.lower() + ":"): raise ApiError(400, "a category can't be moved under itself")
            _dup(b, new, cid)
        parent, pr = _parent(b, new)
        kind = ("income" if pr["is_income"] else "expense") if pr else _kind(d.get("kind"), "income" if c["is_income"] else "expense")
        desc = (d.get("description") or "").strip() or None if "description" in d else c["description"]
        subtree = [(r["category_id"], r["name"]) for r in b.db.execute("SELECT category_id, name FROM categories WHERE name LIKE ? ESCAPE '\\' ORDER BY name",
                   (old.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ":%",)).fetchall()]
        for i, nm in [(cid, old)] + subtree:
            nn = new + nm[len(old):]; pn = nn.rsplit(":", 1)[0] if ":" in nn else None
            ch = dict(name=nn, parent_name=pn, is_income=int(kind == "income"), is_expense=int(kind == "expense"))
            if i == cid: ch["description"] = desc
            before = b.row("categories", i)
            if any(before[k] != v for k, v in ch.items()): b.update("categories", i, ch)
            if nn != nm:  # the category name is also kept as text on every row that uses it
                for tbl in ("transactions", "splits", "memorized_transactions"):
                    for (pk,) in b.db.execute(f"SELECT {PK[tbl]} FROM {tbl} WHERE category=? OR category_id=?", (nm, i)).fetchall():
                        b.update(tbl, pk, dict(category=nn, category_id=i))
        out = dict(category_id=cid, name=new, batch=b.name); b.commit(); return out
    except Exception: b.rollback(); raise


def cat_delete(m, d):
    cid = int(m[1]); b = Batch(None, NOTE)
    try:
        c = b.row("categories", cid)
        if not c: raise ApiError(404, "no such category")
        x = cat_usage(b.db).get(c["name"], {}); kids = b.db.execute("SELECT COUNT(*) FROM categories WHERE name LIKE ? ESCAPE '\\'",
            (c["name"].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ":%",)).fetchone()[0]
        why = [f"{n} {w}" for k, w in (("txns", "transactions"), ("splits", "split lines"), ("memorized", "memorized transactions"), ("scheduled", "scheduled transactions")) if (n := x.get(k))]
        if kids: why.append(f"{kids} subcategories")
        if why: raise ApiError(409, f"Can't delete '{c['name']}': it has {', '.join(why)}. Recategorize or rename them first.")
        b.delete("categories", cid); out = dict(deleted=c["name"], batch=b.name); b.commit(); return out
    except Exception: b.rollback(); raise


# ---------- securities ----------
def _sym(v):
    s = str(v or "").strip().upper()
    if not s: raise ApiError(400, "ticker is required (any short unique code; prices are kept per ticker)")
    if len(s) > 20 or re.search(r"\s", s): raise ApiError(400, "ticker must be one word of up to 20 characters")
    return s


def _sec_fields(b, d, sid=None, cur=None):
    name = str(d.get("name", cur["name"] if cur else "")).strip(); sym = _sym(d.get("symbol", cur["symbol"] if cur else ""))
    typ = str(d.get("type", cur["type"] if cur else "") or "").strip() or None
    if not name: raise ApiError(400, "name is required")
    if len(name) > 120 or (typ and len(typ) > 40): raise ApiError(400, "name or type is too long")
    if b.db.execute("SELECT 1 FROM securities WHERE name=? COLLATE NOCASE AND security_id IS NOT ?", (name, sid)).fetchone(): raise ApiError(409, f"there is already a security named '{name}'")
    if b.db.execute("SELECT 1 FROM securities WHERE symbol=? COLLATE NOCASE AND security_id IS NOT ?", (sym, sid)).fetchone(): raise ApiError(409, f"ticker {sym} is already used by another security")
    return dict(name=name, symbol=sym, type=typ)


def sec_usage(db, sid, name):
    return db.execute("SELECT COUNT(*) FROM transactions WHERE security_id=? OR security_name=?", (sid, name)).fetchone()[0]


@reading
def sec_list(db, m, qs):
    out = []
    lp = core.last_prices(db, "9999-12-31", True)
    grp = db.execute("SELECT security_id, security_name, COUNT(*) FROM transactions WHERE security_id IS NOT NULL OR security_name IS NOT NULL GROUP BY 1, 2").fetchall()
    for r in db.execute("SELECT * FROM securities ORDER BY name COLLATE NOCASE"):
        p = lp.get(r["symbol"]); n = sum(c for i, nm, c in grp if i == r["security_id"] or nm == r["name"])
        out.append(dict(id=r["security_id"], name=r["name"], symbol=r["symbol"], type=r["type"], used=n, can_delete=n == 0,
                        price=p and float(p[0]), price_date=p and p[1], prices=db.execute("SELECT COUNT(*) FROM prices WHERE symbol=?", (r["symbol"],)).fetchone()[0]))
    return dict(securities=out)


def sec_create(m, d):
    b = Batch(None, NOTE)
    try:
        sid = b.insert("securities", dict(_sec_fields(b, d), source="ledger")); out = dict(security_id=sid, batch=b.name); b.commit(); return out
    except Exception: b.rollback(); raise


def sec_update(m, d):
    sid = int(m[1]); b = Batch(None, NOTE)
    try:
        s = b.row("securities", sid)
        if not s: raise ApiError(404, "no such security")
        ch = _sec_fields(b, d, sid, s); ch = {k: v for k, v in ch.items() if s[k] != v}
        if ch: b.update("securities", sid, ch)
        if "name" in ch:  # the name is also kept as text on each transaction
            for (t,) in b.db.execute("SELECT txn_id FROM transactions WHERE security_id=? OR security_name=?", (sid, s["name"])).fetchall():
                b.update("transactions", t, dict(security_name=ch["name"], security_id=sid))
        if "symbol" in ch:
            for (p,) in b.db.execute("SELECT price_id FROM prices WHERE security_id=? OR symbol=?", (sid, s["symbol"])).fetchall():
                b.update("prices", p, dict(symbol=ch["symbol"], security_id=sid))
        if ch:  # security_positions is derived from the transactions (unlogged, like account names on transfers)
            b.db.execute("UPDATE security_positions SET security=?, symbol=? WHERE security=?", (ch.get("name", s["name"]), ch.get("symbol", s["symbol"]), s["name"]))
        out = dict(security_id=sid, changed=list(ch), batch=b.name if ch else None); b.commit(); return out
    except Exception: b.rollback(); raise


def sec_delete(m, d):
    sid = int(m[1]); b = Batch(None, NOTE)
    try:
        s = b.row("securities", sid)
        if not s: raise ApiError(404, "no such security")
        n = sec_usage(b.db, sid, s["name"])
        if n: raise ApiError(409, f"Can't delete '{s['name']}': {n} transactions use it.")
        for (p,) in b.db.execute("SELECT price_id FROM prices WHERE security_id=? OR symbol=?", (sid, s["symbol"])).fetchall(): b.delete("prices", p)
        b.delete("securities", sid); out = dict(deleted=s["name"], batch=b.name); b.commit(); return out
    except Exception: b.rollback(); raise


# ---------- prices ----------
def _price(d):
    try: p = float(d.get("price"))
    except (TypeError, ValueError): raise ApiError(400, "price must be a number")
    if not math.isfinite(p) or p <= 0: raise ApiError(400, "price must be greater than 0")
    try: datetime.date.fromisoformat(str(d.get("date", "")))
    except ValueError: raise ApiError(400, "date must be YYYY-MM-DD")
    return round(p, 6), str(d["date"])


@reading
def price_list(db, m, qs):
    s = db.execute("SELECT * FROM securities WHERE security_id=?", (int(m[1]),)).fetchone()
    if not s: raise ApiError(404, "no such security")
    rows = db.execute("SELECT price_id, date, price, source FROM prices WHERE symbol=? ORDER BY date DESC, price_id DESC LIMIT ?", (s["symbol"], int(qs.get("limit", 100)))).fetchall()
    return dict(security=s["name"], symbol=s["symbol"], prices=[dict(r) for r in rows])


def price_set(m, d):
    """Add a price for a security (by id, name or ticker) on a date; an existing price for the same date is replaced (both logged)."""
    p, date = _price(d); b = Batch(None, "price")
    try:
        ref = str(d.get("security") or d.get("security_id") or "").strip()
        s = b.db.execute("SELECT * FROM securities WHERE security_id=? OR name=? COLLATE NOCASE OR symbol=? COLLATE NOCASE", (ref if ref.isdigit() else -1, ref, ref)).fetchone()
        if not s: raise ApiError(404, f"unknown security: {ref}")
        old = b.db.execute("SELECT price_id FROM prices WHERE symbol=? AND date=? ORDER BY price_id DESC", (s["symbol"], date)).fetchone()
        if old: b.update("prices", old[0], dict(price=p, security_id=s["security_id"])); pid = old[0]
        else: pid = b.insert("prices", dict(security_id=s["security_id"], symbol=s["symbol"], date=date, price=p, source="ledger"))
        out = dict(price_id=pid, replaced=bool(old), batch=b.name); b.commit(); return out
    except Exception: b.rollback(); raise


def price_update(m, d):
    pid = int(m[1]); b = Batch(None, "price")
    try:
        r = b.row("prices", pid)
        if not r: raise ApiError(404, "no such price")
        p, date = _price(dict(price=d.get("price", r["price"]), date=d.get("date", r["date"])))
        if (p, date) != (r["price"], r["date"]): b.update("prices", pid, dict(price=p, date=date))
        out = dict(price_id=pid, batch=b.name); b.commit(); return out
    except Exception: b.rollback(); raise


def price_delete(m, d):
    pid = int(m[1]); b = Batch(None, "price")
    try:
        if not b.row("prices", pid): raise ApiError(404, "no such price")
        b.delete("prices", pid); out = dict(deleted=pid, batch=b.name); b.commit(); return out
    except Exception: b.rollback(); raise


ROUTES = [("GET", r"/api/categories", cat_list), ("POST", r"/api/categories", cat_create), ("POST", r"/api/categories/starter", cat_starter),
          ("PUT", r"/api/categories/(\d+)", cat_update), ("DELETE", r"/api/categories/(\d+)", cat_delete),
          ("GET", r"/api/securities", sec_list), ("POST", r"/api/securities", sec_create), ("PUT", r"/api/securities/(\d+)", sec_update),
          ("DELETE", r"/api/securities/(\d+)", sec_delete), ("GET", r"/api/securities/(\d+)/prices", price_list),
          ("POST", r"/api/prices", price_set), ("PUT", r"/api/prices/(\d+)", price_update), ("DELETE", r"/api/prices/(\d+)", price_delete)]
