"""Household inventory (schema v10): valuables with make/model/serial, purchase and current value, an optional link to the purchase transaction,
and photos/receipts kept as local files in <data dir>/inventory/<item id>/ (never uploaded anywhere). Items are rows in `inventory_items` and every
change is an edit_log batch (revertable). Files are not in the log: deleting an item keeps its files unless delete_files is true.
The Net worth report can add one 'Household items' line from these values; it is not an account and never changes an account balance."""
import base64, datetime, os, re
from urllib.parse import unquote
import core, edits
from edits import ApiError, Batch, Raw, reading

NOTE = "inventory"
TEXT = (("name", 120), ("category", 60), ("make", 80), ("model", 80), ("serial", 80), ("location", 80))
MAX_FILE = 25 * 1024 * 1024
MAX_FILES = 40
TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".webp": "image/webp", ".pdf": "application/pdf"}  # shown in the browser; anything else downloads
STORED = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,99}")


def _dir(item_id): return os.path.join(os.path.dirname(core.DB_PATH), "inventory", str(int(item_id)))


def _date(v, what):
    if v in (None, ""): return None
    try: return datetime.date.fromisoformat(str(v)).isoformat()
    except ValueError: raise ApiError(400, f"{what} must be YYYY-MM-DD")


def _money(d, k, what):
    v = d.get(k)
    if v in (None, ""): return None
    try: x = float(str(v).replace(",", "").replace("$", ""))
    except ValueError: raise ApiError(400, f"{what} must be a number")
    if x != x or x < 0 or x > 1e9: raise ApiError(400, f"{what} must be 0 or more")
    return round(x, 2)


def _clean(b, d, old=None):
    """Body -> column dict. On update only keys present in the body change."""
    out = {}
    for k, n in TEXT:
        if k in d or old is None:
            v = " ".join(str(d.get(k) or "").split())
            if k == "name" and not v: raise ApiError(400, "name is required")
            if len(v) > n: raise ApiError(400, f"{k} is too long (max {n})")
            out[k] = v or None if k != "name" else v
    if "notes" in d or old is None:
        v = str(d.get("notes") or "").strip()
        if len(v) > 4000: raise ApiError(400, "notes are too long (max 4000)")
        out["notes"] = v or None
    for k, w in (("purchase_date", "purchase date"), ("value_date", "value date")):
        if k in d or old is None: out[k] = _date(d.get(k), w)
    for k, w in (("purchase_price", "purchase price"), ("current_value", "current value")):
        if k in d or old is None: out[k] = _money(d, k, w)
    if "txn_id" in d or old is None:
        t = d.get("txn_id")
        if t in (None, ""): out["txn_id"] = None
        else:
            try: t = int(t)
            except (TypeError, ValueError): raise ApiError(400, "txn_id must be a number")
            if not b.db.execute("SELECT 1 FROM transactions WHERE txn_id=?", (t,)).fetchone(): raise ApiError(404, "no such purchase transaction")
            out["txn_id"] = t
    cv = out.get("current_value", old["current_value"] if old else None); vd = out.get("value_date", old["value_date"] if old else None)
    if cv is not None and not vd: out["value_date"] = datetime.date.today().isoformat()   # a value needs a date; default today
    return out


def _files(item_id):
    p = _dir(item_id)
    if not os.path.isdir(p): return []
    out = []
    for f in sorted(os.listdir(p)):
        fp = os.path.join(p, f)
        if STORED.fullmatch(f) and os.path.isfile(fp):
            out.append(dict(name=f, size=os.path.getsize(fp), viewable=os.path.splitext(f)[1].lower() in TYPES, image=TYPES.get(os.path.splitext(f)[1].lower(), "").startswith("image/")))
    return out


def _view(r):
    v = r["current_value"] if r["current_value"] is not None else r["purchase_price"]
    return dict(id=r["item_id"], name=r["name"], category=r["category"] or "", make=r["make"] or "", model=r["model"] or "", serial=r["serial"] or "", location=r["location"] or "",
                purchase_date=r["purchase_date"] or "", purchase_price=r["purchase_price"], current_value=r["current_value"], value_date=r["value_date"] or "", value=v or 0,
                value_is_purchase=r["current_value"] is None and r["purchase_price"] is not None, notes=r["notes"] or "", txn_id=r["txn_id"], created=r["created"], updated=r["updated"])


def _txn_info(db, tid):
    r = db.execute("SELECT t.txn_id, t.date, t.amount, t.payee_text, t.memo, a.name AS account FROM transactions t LEFT JOIN accounts a USING(account_id) WHERE t.txn_id=?", (tid,)).fetchone()
    return dict(txn_id=r["txn_id"], date=r["date"], amount=r["amount"], payee=r["payee_text"] or "", memo=r["memo"] or "", account=r["account"]) if r else None


@reading
def inv_list(db, m, qs):
    items = []
    for r in db.execute("SELECT * FROM inventory_items ORDER BY name COLLATE NOCASE, item_id"):
        v = _view(r); v["files"] = len(_files(r["item_id"])); v["txn"] = _txn_info(db, r["txn_id"]) if r["txn_id"] else None; items.append(v)
    cats = {}
    for v in items:
        c = cats.setdefault(v["category"] or "(none)", dict(category=v["category"] or "(none)", count=0, purchase=0, value=0))
        c["count"] += 1; c["purchase"] += core.cents(v["purchase_price"]); c["value"] += core.cents(v["value"])
    for c in cats.values(): c["purchase"] /= 100; c["value"] /= 100
    return dict(items=items, categories=sorted(cats.values(), key=lambda c: -c["value"]),
                totals=dict(count=len(items), purchase=sum(core.cents(v["purchase_price"]) for v in items) / 100, value=sum(core.cents(v["value"]) for v in items) / 100),
                household_networth=core.household_on(db), known_categories=sorted({v["category"] for v in items if v["category"]}))


@reading
def inv_get(db, m, qs):
    r = db.execute("SELECT * FROM inventory_items WHERE item_id=?", (int(m[1]),)).fetchone()
    if not r: raise ApiError(404, "no such item")
    return dict(_view(r), files=_files(r["item_id"]), txn=_txn_info(db, r["txn_id"]) if r["txn_id"] else None)


def inv_create(m, d):
    b = Batch(None, NOTE)
    try:
        v = _clean(b, d); now = datetime.datetime.now().isoformat(timespec="seconds")
        iid = b.insert("inventory_items", dict(v, created=now, updated=now)); out = dict(item_id=iid, batch=b.name); b.commit(); return out
    except Exception: b.rollback(); raise


def inv_update(m, d):
    iid = int(m[1]); b = Batch(None, NOTE)
    try:
        old = b.row("inventory_items", iid)
        if not old: raise ApiError(404, "no such item")
        v = _clean(b, d, old); v["updated"] = datetime.datetime.now().isoformat(timespec="seconds")
        b.update("inventory_items", iid, v); out = dict(item_id=iid, batch=b.name); b.commit(); return out
    except Exception: b.rollback(); raise


def inv_delete(m, d):
    iid = int(m[1]); b = Batch(None, NOTE); gone = 0
    try:
        if not b.row("inventory_items", iid): raise ApiError(404, "no such item")
        b.delete("inventory_items", iid); out = dict(deleted=iid, batch=b.name); b.commit()
    except Exception: b.rollback(); raise
    if d.get("delete_files") is True:   # not undoable: the files are not in the edit log
        for f in _files(iid): os.remove(os.path.join(_dir(iid), f["name"])); gone += 1
        try: os.rmdir(_dir(iid))
        except OSError: pass
    out["files_deleted"] = gone; return out


@reading
def txn_search(db, m, qs):
    """Pick the purchase transaction: ?q= matches payee, memo or amount (e.g. 1299 or -1299.00). Newest first, bank/card/cash/asset accounts only."""
    q = (qs.get("q") or "").strip(); where, args = ["t.register_type<>'Invst'"], []
    if q:
        try: amt = abs(float(q.replace(",", "").replace("$", "")))
        except ValueError: amt = None
        like = "%" + q.replace("%", "").replace("_", "") + "%"
        where.append("(t.payee_text LIKE ? OR t.memo LIKE ?" + (" OR ABS(t.amount)=?)" if amt is not None else ")")); args += [like, like] + ([amt] if amt is not None else [])
    rows = db.execute(f"SELECT t.txn_id, t.date, t.amount, t.payee_text, t.memo, a.name AS account FROM transactions t LEFT JOIN accounts a USING(account_id) WHERE {' AND '.join(where)} "
                      "ORDER BY t.date DESC, t.txn_id DESC LIMIT 40", args).fetchall()
    return dict(transactions=[dict(txn_id=r["txn_id"], date=r["date"], amount=r["amount"], payee=r["payee_text"] or "", memo=r["memo"] or "", account=r["account"]) for r in rows])


# ---------- files ----------
def _need_item(iid):
    db = core.connect()
    try:
        if not db.execute("SELECT 1 FROM inventory_items WHERE item_id=?", (iid,)).fetchone(): raise ApiError(404, "no such item")
    finally: db.close()


def _safe_name(n):
    n = os.path.basename(str(n or "").replace("\\", "/")); n = re.sub(r"[^A-Za-z0-9._ -]+", "_", n).strip(" .") or "file"
    return n[:100].lstrip(" .-") or "file"


def file_list(m, qs):
    iid = int(m[1]); _need_item(iid); return dict(files=_files(iid))


def file_add(m, d):
    """{name, data: base64} -> stored as data/inventory/<id>/<name> (a number is added if the name is taken)."""
    iid = int(m[1]); _need_item(iid)
    try: raw = base64.b64decode(str(d.get("data") or ""), validate=True)
    except (ValueError, TypeError): raise ApiError(400, "data must be base64")
    if not raw: raise ApiError(400, "the file is empty")
    if len(raw) > MAX_FILE: raise ApiError(413, f"file is too big (max {MAX_FILE // 1024 // 1024} MB)")
    if len(_files(iid)) >= MAX_FILES: raise ApiError(400, f"too many files on one item (max {MAX_FILES})")
    name = _safe_name(d.get("name")); stem, ext = os.path.splitext(name); p = _dir(iid); os.makedirs(p, exist_ok=True); n, final = 1, name
    while os.path.exists(os.path.join(p, final)): n += 1; final = f"{stem}-{n}{ext}"
    with open(os.path.join(p, final), "wb") as f: f.write(raw)
    return dict(name=final, size=len(raw))


def _stored(m):
    iid, name = int(m[1]), unquote(m[2])   # the url may carry %20 etc.; the strict name pattern below rejects any slash or leading dot
    if not STORED.fullmatch(name) or not os.path.isfile(os.path.join(_dir(iid), name)): raise ApiError(404, "no such file")
    return iid, name


def file_get(m, qs):
    iid, name = _stored(m); ext = os.path.splitext(name)[1].lower()
    with open(os.path.join(_dir(iid), name), "rb") as f: body = f.read()
    return Raw(body, TYPES.get(ext, "application/octet-stream"), name, inline=ext in TYPES and qs.get("download") != "1")


def file_delete(m, d):
    iid, name = _stored(m); os.remove(os.path.join(_dir(iid), name))
    try: os.rmdir(_dir(iid))
    except OSError: pass
    return dict(deleted=name)


# ---------- settings ----------
def set_get(m, qs):
    db = core.connect()
    try: return dict(household_networth=core.household_on(db))
    finally: db.close()


def set_put(m, d):
    if "household_networth" in d:
        if not isinstance(d["household_networth"], bool): raise ApiError(400, "household_networth must be true or false")
        core.set_setting("household_networth", "1" if d["household_networth"] else "0")
    return set_get(m, {})


ROUTES = [("GET", r"/api/inventory", inv_list), ("POST", r"/api/inventory", inv_create), ("GET", r"/api/inventory/txn-search", txn_search),
          ("GET", r"/api/inventory/(\d+)", inv_get), ("PUT", r"/api/inventory/(\d+)", inv_update), ("DELETE", r"/api/inventory/(\d+)", inv_delete),
          ("GET", r"/api/inventory/(\d+)/files", file_list), ("POST", r"/api/inventory/(\d+)/files", file_add),
          ("GET", r"/api/inventory/(\d+)/files/([^/]+)", file_get), ("DELETE", r"/api/inventory/(\d+)/files/([^/]+)", file_delete),
          ("GET", r"/api/settings", set_get), ("PUT", r"/api/settings", set_put)]
