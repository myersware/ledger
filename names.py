"""Payees and tags: list with usage counts, rename, merge, delete unused. Every change is an edit_log batch (revertable).
A payee is mostly free text on each row (transactions, memorized, scheduled) plus an optional row in `payees`; renames and merges move all of them."""
import core, edits
from edits import ApiError, Batch, reading

NOTE = "names"
PAYEE_USE = (("txns", "transactions", "payee_text", "transactions"), ("memorized", "memorized transactions", "payee_text", "memorized_transactions"), ("scheduled", "scheduled transactions", "payee", "scheduled"))


def _clean(v, what, n=120):
    v = " ".join(str(v or "").split())
    if not v: raise ApiError(400, f"{what} can't be blank")
    if len(v) > n: raise ApiError(400, f"{what} is too long")
    return v


def pay_usage(db):
    u = {}
    for k, _, col, tbl in PAYEE_USE:
        for n, c in db.execute(f"SELECT {col}, COUNT(*) FROM {tbl} WHERE {col} IS NOT NULL AND {col}<>'' GROUP BY {col}"): u.setdefault(n, dict(txns=0, memorized=0, scheduled=0))[k] = c
    return u


@reading
def pay_list(db, m, qs):
    u = pay_usage(db); listed = {r[0]: r[1] for r in db.execute("SELECT name, default_category FROM payees")}
    out = [dict(name=n, **x, used=sum(x.values()), in_list=n in listed, default_category=listed.get(n)) for n, x in u.items()]
    out += [dict(name=n, txns=0, memorized=0, scheduled=0, used=0, in_list=True, default_category=listed[n]) for n in listed if n and n not in u]
    hint = {}  # most common category per payee, shown greyed when there's no default (never stored)
    for n, c, k in db.execute("SELECT payee_text, category, COUNT(*) FROM transactions WHERE category IS NOT NULL AND payee_text IS NOT NULL GROUP BY 1, 2"):
        if k > hint.get(n, ("", 0))[1]: hint[n] = (c, k)
    for p in out: p["hint_category"] = hint.get(p["name"], (None,))[0]
    out.sort(key=lambda p: p["name"].lower())
    return dict(payees=out)


def pay_merge(m, d):
    """names -> into. Renaming is a merge of one name into a new one."""
    into = _clean(d.get("into"), "new payee name"); names = [n for n in dict.fromkeys(str(x) for x in (d.get("names") or [])) if n != into]
    if not names: raise ApiError(400, "nothing to merge")
    b = Batch(None, NOTE)
    try:
        rows = {r["name"]: r["payee_id"] for r in b.db.execute("SELECT payee_id, name FROM payees")}
        if into not in rows:
            src = next((n for n in names if n in rows), None)
            if src: b.update("payees", rows[src], dict(name=into)); rows[into] = rows.pop(src)
            else: rows[into] = b.insert("payees", dict(name=into, source="ledger"))
        pid, moved = rows[into], 0
        for (pk,) in b.db.execute("SELECT txn_id FROM transactions WHERE payee_text=? AND payee_id IS NOT ?", (into, pid)).fetchall(): b.update("transactions", pk, dict(payee_id=pid))
        for n in names:
            for _, _, col, tbl in PAYEE_USE:
                for (pk,) in b.db.execute(f"SELECT {edits.PK[tbl]} FROM {tbl} WHERE {col}=?", (n,)).fetchall():
                    b.update(tbl, pk, dict({col: into}, **({"payee_id": pid} if tbl == "transactions" else {}))); moved += 1
            if n in rows: b.delete("payees", rows[n])
        out = dict(payee=into, rows_changed=moved, batch=b.name); b.commit(); return out
    except Exception: b.rollback(); raise


def pay_rename(m, d):
    old = str(d.get("name") or "")
    if not old: raise ApiError(400, "name is required")
    return pay_merge(m, dict(names=[old], into=d.get("new_name")))


def pay_delete(m, d):
    n = str(d.get("name") or ""); b = Batch(None, NOTE)
    try:
        x = pay_usage(b.db).get(n, {}); row = b.db.execute("SELECT payee_id FROM payees WHERE name=?", (n,)).fetchone()
        why = [f"{x[k]} {w}" for k, w, _, _ in PAYEE_USE if x.get(k)]
        if why: raise ApiError(409, f"Can't delete '{n}': it is on {', '.join(why)}. Merge it into another payee instead.")
        if not row: raise ApiError(404, "no such payee")
        b.delete("payees", row[0]); out = dict(deleted=n, batch=b.name); b.commit(); return out
    except Exception: b.rollback(); raise


def _match_names(db, sub):
    sub = " ".join(str(sub or "").split())
    if len(sub) < 3: raise ApiError(400, "type at least 3 characters to match")
    u = pay_usage(db); names = set(u) | {r[0] for r in db.execute("SELECT name FROM payees") if r[0]}
    return sub, sorted((n for n in names if sub.lower() in n.lower()), key=str.lower), u


@reading
def pay_match_preview(db, m, qs):
    """Read-only: payees whose name contains the substring (case-insensitive), with usage counts."""
    sub, names, u = _match_names(db, qs.get("q"))
    out = [dict(name=n, **u.get(n, dict(txns=0, memorized=0, scheduled=0))) for n in names]
    return dict(q=sub, payees=out, txns=sum(p["txns"] for p in out), memorized=sum(p["memorized"] for p in out), scheduled=sum(p["scheduled"] for p in out))


def pay_match_merge(m, d):
    """Merge every payee containing q into `into` (one logged batch), after a dated backup."""
    db = core.connect()
    try: sub, names, _ = _match_names(db, d.get("q"))
    finally: db.close()
    if not names: raise ApiError(400, "no payees match")
    bak = core.backup("pre-payee-merge")
    out = pay_merge(m, dict(names=names, into=d.get("into"))); out.update(payees_merged=len(names), backup=bak); return out


def _cat_plan(db, d):
    """Which of a payee's transactions get the category; transfers, splits and investment rows are skipped."""
    name = str(d.get("name") or "")
    if not name: raise ApiError(400, "payee is required")
    cat = _clean(d.get("category"), "category"); c = db.execute("SELECT category_id, name FROM categories WHERE name=?", (cat,)).fetchone()
    if not c: raise ApiError(400, f"no such category '{cat}'")
    only = str(d.get("only_uncategorized") or "").lower() in ("1", "true", "yes", "on")
    change, skip = [], dict(transfers=0, splits=0, investment=0, same=0, categorized=0)
    for r in db.execute("""SELECT t.txn_id, t.category_id, t.transfer_account_id, t.action, t.security_id, a.register_type,
                           COALESCE(t.split_count,0) + (SELECT COUNT(*) FROM splits s WHERE s.txn_id=t.txn_id) AS sp
                           FROM transactions t LEFT JOIN accounts a USING(account_id) WHERE t.payee_text=?""", (name,)):
        if r[5] == "Invst" or r[3] or r[4]: skip["investment"] += 1
        elif r[2]: skip["transfers"] += 1
        elif r[6]: skip["splits"] += 1
        elif r[1] == c[0]: skip["same"] += 1
        elif only and r[1]: skip["categorized"] += 1
        else: change.append(r[0])
    return name, c, change, skip


@reading
def pay_cat_preview(db, m, qs):
    name, c, change, skip = _cat_plan(db, qs)
    return dict(payee=name, category=c[1], change=len(change), skipped=skip)


def pay_cat_apply(m, d):
    db = core.connect()
    try: _cat_plan(db, d)
    finally: db.close()
    bak = core.backup("pre-payee-category"); b = Batch(None, NOTE)
    try:
        name, c, change, skip = _cat_plan(b.db, d)
        for pk in change: b.update("transactions", pk, dict(category_id=c[0], category=c[1]))
        _set_default(b, name, c[1])
        out = dict(payee=name, category=c[1], changed=len(change), skipped=skip, batch=b.name, backup=bak); b.commit(); return out
    except Exception: b.rollback(); raise


def _set_default(b, name, cat):
    row = b.db.execute("SELECT payee_id, default_category FROM payees WHERE name=?", (name,)).fetchone()
    if row and row[1] != cat: b.update("payees", row[0], dict(default_category=cat))
    elif not row and cat: b.insert("payees", dict(name=name, source="ledger", default_category=cat))


def pay_default(m, d):
    """Set or clear (blank) a payee's default category for future imports; no transactions change."""
    name = str(d.get("name") or "")
    if not name: raise ApiError(400, "payee is required")
    cat = " ".join(str(d.get("category") or "").split()) or None; b = Batch(None, NOTE)
    try:
        if cat:
            c = b.db.execute("SELECT name FROM categories WHERE name=?", (cat,)).fetchone()
            if not c: raise ApiError(400, f"no such category '{cat}'")
        _set_default(b, name, cat); out = dict(payee=name, default_category=cat, batch=b.name); b.commit(); return out
    except Exception: b.rollback(); raise


@reading
def pay_txns(db, m, qs):
    """Read-only: every transaction of one payee across all accounts, as register rows (+ account) so the edit forms work."""
    name = str(qs.get("name") or "")
    if not name: raise ApiError(400, "payee is required")
    rows = []
    for (aid,) in db.execute("SELECT DISTINCT account_id FROM transactions WHERE payee_text=?", (name,)).fetchall():
        R = core.register(db, aid, limit=10 ** 9)
        rows += [dict(r, account=R["account"]["name"], account_id=aid, inv=R["account"]["inv"]) for r in R["rows"] if r["payee"] == name]
    rows.sort(key=lambda r: (r["date"], r["txn_id"]), reverse=True)
    return dict(payee=name, count=len(rows), total=round(sum(r["amount"] or 0 for r in rows), 2), rows=rows)


# ---------- tags ----------
TAG_USE = (("txns", "transactions", "transactions"), ("splits", "split lines", "splits"), ("memorized", "memorized transactions", "memorized_transactions"))


def tag_usage(db):
    u = {}
    for k, _, tbl in TAG_USE:
        for n, c in db.execute(f"SELECT tag, COUNT(*) FROM {tbl} WHERE tag IS NOT NULL AND tag<>'' GROUP BY tag"): u.setdefault(n, dict(txns=0, splits=0, memorized=0))[k] = c
    return u


@reading
def tag_list(db, m, qs):
    u = tag_usage(db)
    return dict(tags=[dict(id=r["tag_id"], name=r["name"], description=r["description"], used=sum(u.get(r["name"], {}).values()), usage=u.get(r["name"], {}),
                           can_delete=not u.get(r["name"])) for r in db.execute("SELECT * FROM tags ORDER BY name COLLATE NOCASE")])


def _tag_name(b, v, tid=None):
    n = _clean(v, "tag name", 40)
    if b.db.execute("SELECT 1 FROM tags WHERE name=? COLLATE NOCASE AND tag_id IS NOT ?", (n, tid)).fetchone(): raise ApiError(409, f"There is already a tag named '{n}'")
    return n


def tag_create(m, d):
    b = Batch(None, NOTE)
    try:
        tid = b.insert("tags", dict(name=_tag_name(b, d.get("name")), description=(d.get("description") or "").strip() or None, in_tag_list=1, source="ledger"))
        out = dict(tag_id=tid, batch=b.name); b.commit(); return out
    except Exception: b.rollback(); raise


def tag_update(m, d):
    tid = int(m[1]); b = Batch(None, NOTE)
    try:
        t = b.row("tags", tid)
        if not t: raise ApiError(404, "no such tag")
        new = _tag_name(b, d["name"], tid) if "name" in d else t["name"]
        ch = dict(name=new)
        if "description" in d: ch["description"] = (d.get("description") or "").strip() or None
        b.update("tags", tid, ch)
        if new != t["name"]:  # the tag name is also kept as text on every row that uses it
            for _, _, tbl in TAG_USE:
                for (pk,) in b.db.execute(f"SELECT {edits.PK[tbl]} FROM {tbl} WHERE tag=?", (t["name"],)).fetchall(): b.update(tbl, pk, dict(tag=new))
        out = dict(tag_id=tid, name=new, batch=b.name); b.commit(); return out
    except Exception: b.rollback(); raise


def tag_delete(m, d):
    tid = int(m[1]); b = Batch(None, NOTE)
    try:
        t = b.row("tags", tid)
        if not t: raise ApiError(404, "no such tag")
        x = tag_usage(b.db).get(t["name"], {}); why = [f"{x[k]} {w}" for k, w, _ in TAG_USE if x.get(k)]
        if why: raise ApiError(409, f"Can't delete tag '{t['name']}': it is on {', '.join(why)}. Rename it or clear it from those rows first.")
        b.delete("tags", tid); out = dict(deleted=t["name"], batch=b.name); b.commit(); return out
    except Exception: b.rollback(); raise


ROUTES = [("GET", r"/api/payees/transactions", pay_txns), ("PUT", r"/api/payees/default", pay_default), ("GET", r"/api/payees/category", pay_cat_preview), ("POST", r"/api/payees/category", pay_cat_apply), ("GET", r"/api/payees/match", pay_match_preview), ("POST", r"/api/payees/match-merge", pay_match_merge), ("GET", r"/api/payees", pay_list), ("POST", r"/api/payees/merge", pay_merge), ("PUT", r"/api/payees", pay_rename), ("DELETE", r"/api/payees", pay_delete),
          ("GET", r"/api/tags", tag_list), ("POST", r"/api/tags", tag_create), ("PUT", r"/api/tags/(\d+)", tag_update), ("DELETE", r"/api/tags/(\d+)", tag_delete)]
