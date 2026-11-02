"""Quicken-style reconcile. Marks live in transactions.cleared_status ('cleared' = c, 'reconciled' = R) as imported data has them."""
import datetime, sqlite3
import core, edits


def _acct(db, aid):
    a = db.execute("SELECT * FROM accounts WHERE account_id=?", (aid,)).fetchone()
    if not a: raise edits.ApiError(404, "no such account")
    if a["register_type"] not in edits.OK_TYPES: raise edits.ApiError(400, "reconcile is for bank, card, cash and other asset/liability accounts (not investment accounts)")
    return a


def _date(s):
    try: return datetime.date.fromisoformat(str(s)).isoformat()
    except ValueError: raise edits.ApiError(400, "statement date must be YYYY-MM-DD")


def last(db, aid):
    r = db.execute("SELECT * FROM reconcile WHERE account_id=? ORDER BY id DESC LIMIT 1", (aid,)).fetchone()
    return r and dict(id=r["id"], date=r["statement_date"], balance=r["statement_balance"], rows=r["rows_marked"], batch=r["batch"], ts=r["ts"])


def data(db, aid, date):
    a = _acct(db, aid); date = _date(date)
    begin, rows, nr = 0, [], 0
    for r in db.execute("SELECT * FROM transactions WHERE account_id=? ORDER BY date, txn_id", (aid,)):
        if r["cleared_status"] == "reconciled":
            nr += 1
            if r["date"] <= date: begin += core.cents(r["amount"])
        elif r["date"] <= date:
            rows.append(dict(txn_id=r["txn_id"], date=r["date"], num=r["check_number"] or "", payee=r["payee_text"] or "", memo=r["memo"] or "",
                             amount=r["amount"], cleared=r["cleared_status"] == "cleared", stale=core.is_stale(r)))
    return dict(account=a["name"], account_id=aid, date=date, beginning=begin / 100, rows=rows, reconciled_rows=nr, last=last(db, aid))


def finish(aid, d):
    if not isinstance(d, dict): raise edits.ApiError(400, "JSON object expected")
    date = _date(d.get("statement_date"))
    try: target = core.cents(float(d.get("statement_balance")))
    except (TypeError, ValueError): raise edits.ApiError(400, "statement_balance must be a number")
    try: ids = {int(x) for x in d.get("cleared_ids") or []}
    except (TypeError, ValueError): raise edits.ApiError(400, "cleared_ids must be a list of ids")
    b = edits.Batch(edits.batch_id("rec"), "reconcile")
    try:
        _acct(b.db, aid)
        begin, pending = 0, {}
        for r in b.db.execute("SELECT * FROM transactions WHERE account_id=? AND date<=?", (aid, date)):
            if r["cleared_status"] == "reconciled": begin += core.cents(r["amount"])
            else: pending[r["txn_id"]] = r
        if ids - set(pending): raise edits.ApiError(400, f"rows not eligible (wrong account, after the statement date, or already reconciled): {sorted(ids - set(pending))[:5]}")
        bal = begin + sum(core.cents(pending[i]["amount"]) for i in ids)
        if bal != target: raise edits.ApiError(409, f"difference is {(target - bal) / 100:,.2f}, not 0.00 (cleared balance {bal / 100:,.2f})")
        for i, r in pending.items():
            if i in ids: b.update("transactions", i, dict(cleared_status="reconciled", cleared_raw="X"))
            elif r["cleared_status"] == "cleared": b.update("transactions", i, dict(cleared_status=None, cleared_raw=None))  # unticked c rows
        b.insert("reconcile", dict(account_id=aid, statement_date=date, statement_balance=target / 100, rows_marked=len(ids),
                                   ts=datetime.datetime.now().isoformat(timespec="seconds"), batch=b.name))
        res = dict(batch=b.name, marked=len(ids), statement_date=date, statement_balance=target / 100); b.commit(); return res
    except Exception: b.rollback(); raise


def undo(aid):
    db = core.connect()
    try:
        _acct(db, aid); l = last(db, aid)
    finally: db.close()
    if not l: raise edits.ApiError(404, "no reconcile to undo for this account")
    n = edits.revert_batch(l["batch"])
    return dict(undone=l["batch"], statement_date=l["date"], statement_balance=l["balance"], edits=n)
