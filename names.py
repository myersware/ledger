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
    u = pay_usage(db); listed = {r[0] for r in db.execute("SELECT name FROM payees")}
    out = [dict(name=n, **x, used=sum(x.values()), in_list=n in listed) for n, x in u.items()]
    out += [dict(name=n, txns=0, memorized=0, scheduled=0, used=0, in_list=True) for n in listed if n and n not in u]
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


ROUTES = [("GET", r"/api/payees", pay_list), ("POST", r"/api/payees/merge", pay_merge), ("PUT", r"/api/payees", pay_rename), ("DELETE", r"/api/payees", pay_delete),
          ("GET", r"/api/tags", tag_list), ("POST", r"/api/tags", tag_create), ("PUT", r"/api/tags/(\d+)", tag_update), ("DELETE", r"/api/tags/(\d+)", tag_delete)]
