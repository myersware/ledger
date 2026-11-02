"""Reversible edits: every change goes through Batch and is recorded in edit_log (before/after JSON)."""
import datetime, json, re, sqlite3, uuid
import core

PK = {"transactions": "txn_id", "splits": "split_id", "reconcile": "id", "accounts": "account_id", "categories": "category_id", "securities": "security_id",
      "prices": "price_id", "scheduled": "sched_id", "memorized_transactions": "memorized_id", "payees": "payee_id", "tags": "tag_id"}
OK_TYPES = {"Bank", "CCard", "Cash", "Oth A", "Oth L"}


def batch_id(prefix):  # unique even for two batches in the same second
    return prefix + datetime.datetime.now().strftime("-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


class Raw:
    """A non-JSON response (a download)."""
    def __init__(self, body, ctype, filename): self.body, self.ctype, self.filename = body, ctype, filename


class ApiError(Exception):
    def __init__(self, code, msg): super().__init__(msg); self.code = code


def route(routes, method, path):
    """routes: [(method, regex, fn(match, arg))] -> callable(arg) for the first match, else None. (arg = JSON body, or the query dict for GET)"""
    for me, rx, fn in routes:
        m = me == method and re.fullmatch(rx, path)
        if m: return lambda arg: fn(m, arg)


def reading(fn):
    """GET handler wrapper: fn(db, match, query) with a read-only connection."""
    def g(m, qs):
        db = core.connect()
        try: return fn(db, m, qs)
        finally: db.close()
    return g


class Batch:
    def __init__(self, name=None, note=""):
        self.db = sqlite3.connect(core.DB_PATH); self.db.row_factory = sqlite3.Row
        self.name = name or "ui-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.note = note; self.warnings = []; self.sync = False

    def _log(self, op, tbl, pk, acct, before, after):
        self.db.execute("INSERT INTO edit_log(batch,ts,op,tbl,pk,account_id,before_json,after_json,note) VALUES(?,?,?,?,?,?,?,?,?)",
                        (self.name, datetime.datetime.now().isoformat(timespec="seconds"), op, tbl, pk, acct,
                         json.dumps(before) if before else None, json.dumps(after) if after else None, self.note))

    def row(self, tbl, pk):
        r = self.db.execute(f"SELECT * FROM {tbl} WHERE {PK[tbl]}=?", (pk,)).fetchone()
        return dict(r) if r else None

    def _acct(self, tbl, row):
        if "account_id" in row: return row["account_id"]
        if "txn_id" not in row: return None  # categories, securities, prices...
        p = self.db.execute("SELECT account_id FROM transactions WHERE txn_id=?", (row["txn_id"],)).fetchone()
        return p[0] if p else None

    def insert(self, tbl, vals):
        cols = list(vals)
        cur = self.db.execute(f"INSERT INTO {tbl}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [vals[c] for c in cols])
        row = self.row(tbl, cur.lastrowid); self._log("insert", tbl, cur.lastrowid, self._acct(tbl, row), None, row)
        return cur.lastrowid

    def update(self, tbl, pk, changes):
        before = self.row(tbl, pk)
        self.db.execute(f"UPDATE {tbl} SET {','.join(c + '=?' for c in changes)} WHERE {PK[tbl]}=?", [*changes.values(), pk])
        if tbl == "accounts" and "name" in changes and changes["name"] != before["name"]: self.sync = True
        self._log("update", tbl, pk, self._acct(tbl, before), before, self.row(tbl, pk))

    def delete(self, tbl, pk):
        before = self.row(tbl, pk)
        self.db.execute(f"DELETE FROM {tbl} WHERE {PK[tbl]}=?", (pk,))
        self._log("delete", tbl, pk, self._acct(tbl, before), before, None)

    def commit(self):
        if self.sync: core.sync_account_names(self.db)  # a renamed account: refresh the name text kept on transfers (not logged; derived from account_id)
        self.db.commit(); self.db.close()
    def rollback(self): self.db.rollback(); self.db.close()


def is_reverted(db, name):
    q = lambda n: db.execute("SELECT 1 FROM edit_log WHERE batch=?", (n,)).fetchone()
    return bool(q("revert:" + name)) and not q("revert:revert:" + name)


def live_batch(db, base):
    """Newest batch named <base> or <base>-rN that was applied and not reverted (None if there is none). For one-off fix scripts."""
    for (n,) in db.execute("SELECT batch FROM edit_log WHERE batch=? OR batch LIKE ? GROUP BY batch ORDER BY MIN(id) DESC", (base, base + "-r%")):
        if not is_reverted(db, n): return n


def next_batch(db, base):
    """<base> the first time, then <base>-r2, -r3... (a reverted fix can be applied again under a fresh batch id)."""
    n = db.execute("SELECT COUNT(DISTINCT batch) FROM edit_log WHERE batch=? OR batch LIKE ?", (base, base + "-r%")).fetchone()[0]
    return base if not n else f"{base}-r{n + 1}"


def revert_live(base):
    b = live_batch(core.connect(), base)
    return f"reverted {revert_batch(b)} edits of {b}" if b else "nothing to revert (not applied, or already reverted)"


def revert_batch(name):
    """Undo a logged batch (as a new 'revert:<name>' batch, itself logged)."""
    b = Batch("revert:" + name, f"revert of {name}")
    try:
        ents = b.db.execute("SELECT * FROM edit_log WHERE batch=? ORDER BY id DESC", (name,)).fetchall()
        if not ents: raise ApiError(404, f"no such batch {name}")
        if is_reverted(b.db, name): raise ApiError(409, f"batch {name} is already reverted")
        last = max(x["id"] for x in ents)
        for x in ents:  # refuse if a later, still-effective edit touched the same row (later edits that were already reverted are fine)
            for (lb,) in b.db.execute("SELECT DISTINCT batch FROM edit_log WHERE id>? AND tbl=? AND pk=? AND batch<>?", (last, x["tbl"], x["pk"], b.name)):
                neutral = (lb.startswith("revert:") and not lb.startswith("revert:revert:")) or \
                    b.db.execute("SELECT 1 FROM edit_log WHERE batch=?", ("revert:" + lb,)).fetchone()
                if not neutral: raise ApiError(409, f"{x['tbl']} row {x['pk']} was changed after batch {name} (batch {lb}); revert that first")
        for e in ents:
            before = json.loads(e["before_json"]) if e["before_json"] else None
            if e["op"] == "insert": b.delete(e["tbl"], e["pk"])
            elif e["op"] == "delete": b.insert(e["tbl"], before)
            else: b.update(e["tbl"], e["pk"], {k: v for k, v in before.items() if k != PK[e["tbl"]]})
        b.commit()
    except Exception:
        b.rollback(); raise
    return len(ents)


# ---------- transactions ----------
def _acct(b, aid):
    a = b.db.execute("SELECT * FROM accounts WHERE account_id=?", (aid,)).fetchone()
    if not a: raise ApiError(404, "no such account")
    if a["register_type"] not in OK_TYPES: raise ApiError(400, f"editing {a['register_type'] or a['list_type']} accounts is not supported in v1")
    return a


def _lookup(b, d, aid):
    """Validate/normalise a request body -> (txn column dict, splits list)."""
    if not isinstance(d, dict): raise ApiError(400, "JSON object expected")
    date = str(d.get("date", ""))
    try: datetime.date.fromisoformat(date)
    except ValueError: raise ApiError(400, "date must be YYYY-MM-DD")
    try: amt = round(float(d.get("amount")), 2)
    except (TypeError, ValueError): raise ApiError(400, "amount must be a number")
    cl = d.get("cleared") or ""
    if cl not in ("", "c", "R"): raise ApiError(400, "cleared must be '', 'c' or 'R'")
    me = b.db.execute("SELECT name FROM accounts WHERE account_id=?", (aid,)).fetchone()[0]

    def cat_xfer(src):
        cat, xf = (src.get("category") or "").strip(), (src.get("transfer") or "").strip()
        if cat and xf: raise ApiError(400, "a line can have a category or a transfer, not both")
        c = x = None
        if cat:
            c = b.db.execute("SELECT category_id, name FROM categories WHERE name=?", (cat,)).fetchone()
            if not c: raise ApiError(400, f"unknown category: {cat}")
        if xf:
            x = b.db.execute("SELECT account_id, name, register_type FROM accounts WHERE name=?", (xf,)).fetchone()
            if not x: raise ApiError(400, f"unknown transfer account: {xf}")
            if x["account_id"] == aid: raise ApiError(400, "cannot transfer to the same account")
            if x["register_type"] not in OK_TYPES | {"Invst"}: raise ApiError(400, "can't transfer to this kind of account")
        return c, x
    splits = d.get("splits") or []
    t = dict(date=date, amount=amt, amount_u=amt, check_number=(d.get("num") or "").strip() or None,
             payee_text=(d.get("payee") or "").strip() or None, memo=(d.get("memo") or "").strip() or None,
             cleared_raw={"": None, "c": "*", "R": "X"}[cl], cleared_status={"": None, "c": "cleared", "R": "reconciled"}[cl],
             category_id=None, category=None, transfer_account_id=None, transfer_account=None, split_count=len(splits))
    if "tag" in d:  # only touch the tag when the form sends one
        tg = (d.get("tag") or "").strip(); t["tag"] = t["tag_id"] = None
        if tg:
            row = b.db.execute("SELECT tag_id, name FROM tags WHERE name=? COLLATE NOCASE", (tg,)).fetchone()
            if not row: raise ApiError(400, f"unknown tag: {tg} (create it on the Tags page)")
            t["tag_id"], t["tag"] = row["tag_id"], row["name"]
    t["payee_id"] = None
    if t["payee_text"]:
        p = b.db.execute("SELECT payee_id FROM payees WHERE name=?", (t["payee_text"],)).fetchone()
        t["payee_id"] = p[0] if p else None
    out = []
    if splits:
        for i, s in enumerate(splits, 1):
            c, x = cat_xfer(s)
            try: sa = round(float(s.get("amount")), 2)
            except (TypeError, ValueError): raise ApiError(400, f"split {i}: amount must be a number")
            out.append(dict(split_seq=i, category_id=c and c["category_id"], category=c and c["name"],
                            transfer_account_id=x and x["account_id"], transfer_account=x and x["name"],
                            memo=(s.get("memo") or "").strip() or None, amount=sa, source="ledger"))
        if abs(sum(s["amount"] for s in out) - amt) > 0.005:
            raise ApiError(400, f"splits sum to {sum(s['amount'] for s in out):.2f}, transaction amount is {amt:.2f}")
    else:
        c, x = cat_xfer(d)
        if c: t["category_id"], t["category"] = c["category_id"], c["name"]
        if x: t["transfer_account_id"], t["transfer_account"] = x["account_id"], x["name"]
    return t, out, me


def _mirror(b, txn_id, t, other_id, other_name, amount, me_id, me_name):
    if b.db.execute("SELECT register_type FROM accounts WHERE account_id=?", (other_id,)).fetchone()[0] == "Invst":  # the other side is an investment account: a cash XIn/XOut
        return b.insert("transactions", dict(account_id=other_id, register_type="Invst", date=t["date"], amount=abs(amount), amount_u=abs(amount), action="XOut" if amount > 0 else "XIn",
            invst_json=json.dumps({"$": abs(amount)}), payee_text=t["payee_text"], memo=t["memo"], transfer_account_id=me_id, transfer_account=me_name, split_count=0, source="ledger", link_id=txn_id))
    return b.insert("transactions", dict(account_id=other_id, register_type=b.db.execute(
        "SELECT register_type FROM accounts WHERE account_id=?", (other_id,)).fetchone()[0], date=t["date"], amount=-amount,
        amount_u=-amount, payee_text=t["payee_text"], payee_id=t["payee_id"], memo=t["memo"], transfer_account_id=me_id,
        transfer_account=me_name, split_count=0, source="ledger", link_id=txn_id))


def _make_counterparts(b, txn_id, t, splits, me_id, me_name):
    if t["transfer_account_id"]:
        cp = _mirror(b, txn_id, t, t["transfer_account_id"], t["transfer_account"], t["amount"], me_id, me_name)
        b.update("transactions", txn_id, {"link_id": cp})
    for s in b.db.execute("SELECT * FROM splits WHERE txn_id=? ORDER BY split_seq", (txn_id,)).fetchall():
        if s["transfer_account_id"]:
            cp = _mirror(b, txn_id, t, s["transfer_account_id"], s["transfer_account"], s["amount"], me_id, me_name)
            b.update("splits", s["split_id"], {"link_txn_id": cp})


def _remove_counterparts(b, row):
    """Delete the other side(s) of row's transfer(s). Returns nothing; unmatched legacy legs add warnings."""
    cps = []
    if row["link_id"]:
        o = b.row("transactions", row["link_id"])
        if o and o["link_id"] == row["txn_id"]: cps.append(o["txn_id"])
    for s in b.db.execute("SELECT * FROM splits WHERE txn_id=?", (row["txn_id"],)).fetchall():
        if s["link_txn_id"] and b.row("transactions", s["link_txn_id"]): cps.append(s["link_txn_id"])
        elif s["transfer_account_id"]:
            b.warnings.append(f"split transfer to {s['transfer_account']} ({s['amount']:.2f}) was not linked; counterpart left unchanged")
    if (row["transfer_account_id"] and row["transfer_account_id"] != row["account_id"] and not cps and not row["link_id"]
            and not row["split_count"]):  # legacy unlinked transfer: sync only if exactly one obvious counterpart
        c = b.db.execute("SELECT txn_id FROM transactions WHERE account_id=? AND date=? AND ROUND(amount+?,2)=0 AND transfer_account_id=? AND link_id IS NULL AND txn_id<>?",
                         (row["transfer_account_id"], row["date"], row["amount"], row["account_id"], row["txn_id"])).fetchall()
        if len(c) == 1: cps.append(c[0][0])
        else: b.warnings.append(f"transfer to {row['transfer_account']}: {len(c)} candidate counterparts; other side left unchanged")
    for cid in cps: b.delete("transactions", cid)


def _guard(b, row):
    if not row: raise ApiError(404, "no such transaction")
    _acct(b, row["account_id"])
    if row["link_id"]:
        p = b.row("transactions", row["link_id"])
        if p and (p["split_count"] or (p["register_type"] == "Invst" and not (p["action"] in ("XIn", "XOut") and p["link_id"] == row["txn_id"] and p["transfer_account_id"] == row["account_id"]))): raise ApiError(409, f"this is the mirror of a split transfer in transaction {p['txn_id']}; edit that one (or the investment row)")


def _is_inv(aid=None, tid=None):
    db = sqlite3.connect(core.DB_PATH)
    try:
        if tid is not None:
            r = db.execute("SELECT account_id FROM transactions WHERE txn_id=?", (tid,)).fetchone(); aid = r and r[0]
        r = db.execute("SELECT register_type FROM accounts WHERE account_id=?", (aid,)).fetchone()
        return bool(r) and r[0] == "Invst"
    finally: db.close()


def create(aid, d, b=None):
    """Add a bank/card/cash/asset/liability transaction. Pass an open Batch to make it part of a larger batch (the caller commits)."""
    if b is None and _is_inv(aid): return inv_create(aid, d)
    own = b is None; b = b or Batch(note="add")
    try:
        _acct(b, aid); t, splits, me = _lookup(b, d, aid)
        tid = b.insert("transactions", dict(t, account_id=aid, register_type=b.db.execute("SELECT register_type FROM accounts WHERE account_id=?", (aid,)).fetchone()[0], source="ledger"))
        for s in splits: b.insert("splits", dict(s, txn_id=tid))
        _make_counterparts(b, tid, t, splits, aid, me)
        res = dict(txn_id=tid, link_id=b.row("transactions", tid)["link_id"], warnings=b.warnings, batch=b.name)
        if own: b.commit()
        return res
    except Exception:
        if own: b.rollback()
        raise


def update(tid, d):
    if _is_inv(tid=tid): return inv_update(tid, d)
    b = Batch(note="edit")
    try:
        row = b.row("transactions", tid); _guard(b, row); aid = row["account_id"]
        t, splits, me = _lookup(b, d, aid)
        _remove_counterparts(b, row)
        for s in b.db.execute("SELECT split_id FROM splits WHERE txn_id=?", (tid,)).fetchall(): b.delete("splits", s[0])
        b.update("transactions", tid, dict(t, link_id=None))
        for s in splits: b.insert("splits", dict(s, txn_id=tid))
        _make_counterparts(b, tid, t, splits, aid, me)
        res = dict(txn_id=tid, link_id=b.row("transactions", tid)["link_id"], warnings=b.warnings, batch=b.name); b.commit(); return res
    except Exception: b.rollback(); raise


def delete(tid):
    if _is_inv(tid=tid): return inv_delete(tid)
    b = Batch(note="delete")
    try:
        row = b.row("transactions", tid); _guard(b, row)
        _remove_counterparts(b, row)
        for s in b.db.execute("SELECT split_id FROM splits WHERE txn_id=?", (tid,)).fetchall(): b.delete("splits", s[0])
        b.delete("transactions", tid)
        res = dict(deleted=tid, warnings=b.warnings, batch=b.name); b.commit(); return res
    except Exception: b.rollback(); raise


# ---------- investment transactions (Buy, Sell, Div, ReinvDiv, IntInc, XIn/XOut, MiscExp) ----------
INV_ACTIONS = core.INV_ACTIONS
NEEDS_QTY = {"Buy", "Sell", "ReinvDiv", "ReinvInt", "ReinvLg", "ReinvSh", "ShrsIn", "ShrsOut"}
NEEDS_SEC = NEEDS_QTY | {"Div", "CGLong", "CGShort", "StkSplit"}
JKEYS = ("I", "Q", "$", "D", "L", "G")   # invst_json keys inv_t sets itself; any other key on an existing row (export data) is kept when the row is edited


def _ratio(v):
    """'2:1' (new:old), '1:10' or a plain number -> new shares per old share."""
    try:
        if ":" in str(v): a, c = str(v).split(":"); x = float(a) / float(c)
        else: x = float(v)
    except (TypeError, ValueError, ZeroDivisionError): raise ApiError(400, "ratio must look like 2:1 (new:old) or be a number")
    if x <= 0: raise ApiError(400, "ratio must be above 0")
    return x


def _sec(b, aid, s):
    s = (s or "").strip()
    if not s: return None
    r = b.db.execute("SELECT security_id, name FROM securities WHERE name=?", (s,)).fetchone()
    if not r:  # a ticker: prefer the name this account already uses
        r = b.db.execute("SELECT t.security_id, t.security_name AS name FROM transactions t JOIN securities s ON s.name=t.security_name "
                         "WHERE t.account_id=? AND s.symbol=? ORDER BY t.date DESC, t.txn_id DESC LIMIT 1", (aid, s.upper())).fetchone()
    if not r: r = b.db.execute("SELECT security_id, name FROM securities WHERE symbol=? ORDER BY security_id LIMIT 1", (s.upper(),)).fetchone()
    if not r: raise ApiError(400, f"unknown security: {s}")
    return r


def _num(d, k, req=False):
    v = d.get(k)
    if v in (None, ""):
        if req: raise ApiError(400, f"{k} is required")
        return None
    try: return float(v)
    except (TypeError, ValueError): raise ApiError(400, f"{k} must be a number")


def inv_t(b, aid, d, cleared=None, old=None):
    """Validate an investment body {date, action, security, qty, price, amount, memo, payee, transfer, ratio, acquired, lot} -> txn column dict.
    ShrsIn: qty + cost basis (amount, or price) + acquired date. ShrsOut: qty (+ lot). StkSplit: ratio. None of the three moves cash."""
    if not isinstance(d, dict): raise ApiError(400, "JSON object expected")
    date = str(d.get("date", ""))
    try: datetime.date.fromisoformat(date)
    except ValueError: raise ApiError(400, "date must be YYYY-MM-DD")
    act = d.get("action")
    if act not in INV_ACTIONS: raise ApiError(400, f"action must be one of {sorted(INV_ACTIONS)}")
    sec = _sec(b, aid, d.get("security"))
    if act in NEEDS_SEC and not sec: raise ApiError(400, f"{act} needs a security")
    if act in ("XIn", "XOut") and sec: raise ApiError(400, f"{act} is a plain cash movement: no security")
    if act not in NEEDS_SEC and act not in ("XIn", "XOut") and sec: raise ApiError(400, f"{act} takes no security")
    q, p, amt = _num(d, "qty"), _num(d, "price"), _num(d, "amount")
    j = {}
    if act == "StkSplit":
        if q or p or amt: raise ApiError(400, "StkSplit takes only a ratio, such as 2:1")
        j["Q"] = round(10 * _ratio(d.get("ratio")), 6); amt = None   # same scale as the export: shares x Q / 10
    elif act == "ShrsOut":
        if not q or q <= 0: raise ApiError(400, "qty must be > 0")
        if p or amt: raise ApiError(400, "ShrsOut moves no cash: no price or amount")
        j["Q"] = q; amt = None
    elif act == "ShrsIn":
        if not q or q <= 0: raise ApiError(400, "qty must be > 0")
        if amt is None and p is not None: amt = round(q * p, 2)
        if amt is not None and amt < 0: raise ApiError(400, "cost basis can't be negative")
        j["Q"] = q
        if amt is not None: j["I"] = round(amt / q, 6)
        acq = (d.get("acquired") or "").strip() if isinstance(d.get("acquired"), str) else d.get("acquired")
        if acq:
            try: datetime.date.fromisoformat(str(acq))
            except ValueError: raise ApiError(400, "acquired date must be YYYY-MM-DD")
            if str(acq) > date: raise ApiError(400, "acquired date can't be after the date shares came in")
            if str(acq) != date: j["D"] = str(acq)
    else:
        if act in NEEDS_QTY:
            if not q or q <= 0: raise ApiError(400, "qty must be > 0")
            if amt is None and p is not None: amt = round(q * p, 2)
            if p is None and amt: p = round(amt / q, 6)
        elif q or p: raise ApiError(400, f"{act} takes no qty/price")
        if amt is None or amt <= 0: raise ApiError(400, "amount must be > 0 (the action gives the direction)")
        j = {"I": p, "Q": q} if act in NEEDS_QTY else {"$": amt} if act in ("XIn", "XOut") else {}
    if amt is not None: amt = round(amt, 2)
    lot = d.get("lot") if "lot" in d else (old or {}).get("L")
    if lot not in (None, ""):
        if act not in ("Sell", "ShrsOut"): raise ApiError(400, "only Sell and ShrsOut can name a lot")
        try: j["L"] = int(lot)
        except (TypeError, ValueError): raise ApiError(400, "lot must be the transaction number of the buy it comes from")
    j = {**{k: v for k, v in (old or {}).items() if k not in JKEYS}, **j}
    me = b.db.execute("SELECT name FROM accounts WHERE account_id=?", (aid,)).fetchone()[0]
    x = None
    xf = (d.get("transfer") or "").strip()
    if xf:
        if act not in ("XIn", "XOut"): raise ApiError(400, "only XIn/XOut take a transfer account")
        x = b.db.execute("SELECT account_id, name, register_type FROM accounts WHERE name=?", (xf,)).fetchone()
        if not x: raise ApiError(400, f"unknown transfer account: {xf}")
        if x["account_id"] == aid: raise ApiError(400, "cannot transfer to the same account")
        if x["register_type"] not in OK_TYPES | {"Invst"}: raise ApiError(400, "can't transfer to this kind of account")
    cl = cleared
    if "cleared" in d:
        if d["cleared"] not in ("", "c", "R"): raise ApiError(400, "cleared must be '', 'c' or 'R'")
        cl = d["cleared"]
    return dict(date=date, amount=amt, amount_u=amt, action=act, security_id=sec and sec["security_id"], security_name=sec and sec["name"],
                invst_json=json.dumps(j), payee_text=(d.get("payee") or "").strip() or None, memo=(d.get("memo") or "").strip() or None,
                check_number=None, cleared_raw={"": None, "c": "*", "R": "X", None: None}[cl], cleared_status={"": None, "c": "cleared", "R": "reconciled", None: None}[cl],
                category_id=None, category=None, transfer_account_id=x and x["account_id"], transfer_account=x and x["name"],
                split_count=0, payee_id=None), me


def _inv_guard(b, row):
    if not row: raise ApiError(404, "no such transaction")
    a = b.db.execute("SELECT register_type FROM accounts WHERE account_id=?", (row["account_id"],)).fetchone()
    if a[0] != "Invst": raise ApiError(400, "not an investment row")
    if row["action"] not in INV_ACTIONS or row["split_count"]: raise ApiError(409, f"action {row['action']} is not editable here")


def _group(row):
    return json.loads(row["invst_json"] or "{}").get("G")


def _group_ids(b, g):
    return [r[0] for r in b.db.execute("SELECT txn_id FROM transactions WHERE json_valid(invst_json) AND json_extract(invst_json,'$.G')=? ORDER BY txn_id", (g,))]


def _inv_mirror(b, tid, t, aid, me):
    if not t["transfer_account_id"]: return
    eff = t["amount"] if t["action"] == "XIn" else -t["amount"]
    oid = t["transfer_account_id"]
    if b.db.execute("SELECT register_type FROM accounts WHERE account_id=?", (oid,)).fetchone()[0] == "Invst":  # cash between two investment accounts
        cp = b.insert("transactions", dict(account_id=oid, register_type="Invst", date=t["date"], amount=t["amount"], amount_u=t["amount"], action="XOut" if t["action"] == "XIn" else "XIn",
            invst_json=json.dumps({"$": t["amount"]}), payee_text=t["payee_text"], memo=t["memo"], transfer_account_id=aid, transfer_account=me, split_count=0, source="ledger", link_id=tid))
        b.update("transactions", tid, {"link_id": cp}); return
    cp = b.insert("transactions", dict(account_id=oid, register_type=b.db.execute("SELECT register_type FROM accounts WHERE account_id=?", (oid,)).fetchone()[0],
        date=t["date"], amount=-eff, amount_u=-eff, payee_text=t["payee_text"], memo=t["memo"], transfer_account_id=aid, transfer_account=me,
        split_count=0, source="ledger", link_id=tid))
    b.update("transactions", tid, {"link_id": cp})


def _inv_unmirror(b, row):
    cps = []
    if row["link_id"]:
        o = b.row("transactions", row["link_id"])
        if o and o["link_id"] == row["txn_id"]: cps.append(o["txn_id"])
    elif row["transfer_account_id"] and row["action"] in ("XIn", "XOut"):  # legacy unlinked: sync only if exactly one obvious counterpart
        eff = core.effect(row, True) / 100
        c = b.db.execute("SELECT txn_id FROM transactions WHERE account_id=? AND date=? AND ROUND(amount+?,2)=0 AND transfer_account_id=? AND link_id IS NULL AND txn_id<>?",
                         (row["transfer_account_id"], row["date"], eff, row["account_id"], row["txn_id"])).fetchall()
        if len(c) == 1: cps.append(c[0][0])
        else: b.warnings.append(f"transfer with {row['transfer_account']}: {len(c)} candidate counterparts; other side left unchanged")
    for cid in cps: b.delete("transactions", cid)


def _inv_warn(b, aid, t):
    if t["action"] not in ("Sell", "ShrsOut"): return
    rows = b.db.execute("SELECT * FROM transactions WHERE account_id=?", (aid,)).fetchall()
    s = core.securities_value(rows, {}, {})[1].get(t["security_name"])
    if s is not None and s < -core.Dc("0.0005"): b.warnings.append(f"{t['security_name']}: position is now {float(s):.4f} shares (sold more than held)")


def _note_price(b, t):
    """A Buy/Sell at a price records that price for the security unless the database already has a price on or after that date."""
    p = json.loads(t["invst_json"] or "{}").get("I")
    if t["action"] not in ("Buy", "Sell") or not t["security_id"] or not p or p <= 0: return
    sym = b.db.execute("SELECT symbol FROM securities WHERE security_id=?", (t["security_id"],)).fetchone()
    if not sym or not sym[0] or b.db.execute("SELECT 1 FROM prices WHERE symbol=? AND date>=? AND price IS NOT NULL", (sym[0], t["date"])).fetchone(): return
    b.insert("prices", dict(security_id=t["security_id"], symbol=sym[0], date=t["date"], price=p, source="ledger"))


def inv_create(aid, d, b=None):
    own = b is None; b = b or Batch(note="add")
    try:
        t, me = inv_t(b, aid, d)
        tid = b.insert("transactions", dict(t, account_id=aid, register_type="Invst", source="ledger"))
        _inv_mirror(b, tid, t, aid, me); _inv_warn(b, aid, t); _note_price(b, t)
        res = dict(txn_id=tid, link_id=b.row("transactions", tid)["link_id"], warnings=b.warnings, batch=b.name)
        if own: b.commit()
        return res
    except Exception:
        if own: b.rollback()
        raise


def inv_update(tid, d):
    b = Batch(note="edit")
    try:
        row = b.row("transactions", tid); _inv_guard(b, row)
        if _group(row): raise ApiError(409, "this is part of a share transfer between accounts; delete it (all of it goes) and enter the transfer again")
        old = json.loads(row["invst_json"] or "{}")
        legacy = (not row["link_id"] and row["transfer_account_id"] and row["action"] in ("XIn", "XOut")
                  and (d.get("transfer") or "").strip() in ("", row["transfer_account"]))
        t, me = inv_t(b, row["account_id"], dict(d, transfer="") if legacy else d, cleared={"cleared": "c", "reconciled": "R"}.get(row["cleared_status"], ""), old=old)
        if legacy:  # export row with an unlinked counterpart in another account: keep the transfer label, leave the other side alone
            t.update(transfer_account_id=row["transfer_account_id"], transfer_account=row["transfer_account"])
            if (t["date"], t["amount"], t["action"]) != (row["date"], row["amount"], row["action"]):
                b.warnings.append(f"the matching transaction in {row['transfer_account']} was not changed; edit it separately")
            b.update("transactions", tid, t)
        else:
            _inv_unmirror(b, row)
            b.update("transactions", tid, dict(t, link_id=None)); _inv_mirror(b, tid, t, row["account_id"], me)
        _inv_warn(b, row["account_id"], t); _note_price(b, t)
        res = dict(txn_id=tid, link_id=b.row("transactions", tid)["link_id"], warnings=b.warnings, batch=b.name); b.commit(); return res
    except Exception: b.rollback(); raise


def inv_delete(tid):
    b = Batch(note="delete")
    try:
        row = b.row("transactions", tid); _inv_guard(b, row)
        g = _group(row)
        if g:  # a share transfer between accounts: both sides and every lot go together
            for i in _group_ids(b, g): b.delete("transactions", i)
        else: _inv_unmirror(b, row); b.delete("transactions", tid)
        res = dict(deleted=tid, warnings=b.warnings, batch=b.name); b.commit(); return res
    except Exception: b.rollback(); raise


def set_cleared(tid, val):
    """Toggle c (cleared) on one row. Reconciled rows are changed only by undoing the reconcile."""
    if val not in ("", "c"): raise ApiError(400, "cleared must be 'c' or ''")
    b = Batch(note="clear toggle")
    try:
        row = b.row("transactions", tid)
        if not row: raise ApiError(404, "no such transaction")
        if row["cleared_status"] == "reconciled": raise ApiError(409, "row is reconciled; undo the reconcile to change it")
        b.update("transactions", tid, dict(cleared_raw="*" if val else None, cleared_status="cleared" if val else None))
        res = dict(txn_id=tid, cleared=val, batch=b.name); b.commit(); return res
    except Exception: b.rollback(); raise
