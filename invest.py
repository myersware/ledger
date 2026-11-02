"""Investment account to investment account transfers. Cash (XIn/XOut) goes through edits.inv_*; shares move here, keeping each lot's acquired date and cost."""
import json
from decimal import Decimal as Dc
import edits, portfolio
from edits import ApiError, Batch

Z = Dc("0.000001")


def _inv_acct(b, v, what):
    try: a = b.db.execute("SELECT * FROM accounts WHERE account_id=?", (int(v),)).fetchone()
    except (TypeError, ValueError): a = None
    if not a or a["register_type"] != "Invst": raise ApiError(400, f"{what} must be an investment account")
    return a


def transfer_shares(m, d):
    """Move qty shares of a security from one investment account to another on a date. The source's lots are used oldest first (or the lot named by `lot`);
    the destination gets one ShrsIn per lot used, with that lot's acquired date and cost. All rows share a group id: deleting any one deletes the whole transfer."""
    b = Batch(None, "share transfer")
    try:
        fa, ta = _inv_acct(b, d.get("from_account"), "from_account"), _inv_acct(b, d.get("to_account"), "to_account")
        if fa["account_id"] == ta["account_id"]: raise ApiError(400, "pick two different accounts")
        date = str(d.get("date") or ""); sec = edits._sec(b, fa["account_id"], d.get("security"))
        if not sec: raise ApiError(400, "security is required")
        q = edits._num(d, "qty", True)
        if q <= 0: raise ApiError(400, "qty must be > 0")
        t0, _ = edits.inv_t(b, fa["account_id"], dict(date=date, action="ShrsOut", security=sec["name"], qty=q, lot=d.get("lot"), memo=d.get("memo")))   # validates date, qty, lot
        rows = b.db.execute("SELECT * FROM transactions WHERE account_id=? AND date<=? ORDER BY date, txn_id", (fa["account_id"], date)).fetchall()
        lots, _debt = portfolio.fifo_lots(rows).get(sec["name"], ([], Dc(0)))
        pick = json.loads(t0["invst_json"]).get("L"); rem = Dc(repr(q)); used = []
        for l in sorted(lots, key=lambda l: l[3] != pick):
            if rem <= Z: break
            t = min(rem, l[1]); used.append((l[0], t, None if l[2] is None else l[2] * t / l[1])); rem -= t
        if rem > Z: raise ApiError(400, f"{sec['name']}: {fa['name']} holds only {float(q) - float(rem):g} shares on {date}")
        base = dict(register_type="Invst", source="ledger", date=date, security_id=sec["security_id"], security_name=sec["name"], payee_text=None, split_count=0,
                    memo=t0["memo"], cleared_raw=None, cleared_status=None)
        out = b.insert("transactions", dict(base, account_id=fa["account_id"], action="ShrsOut", amount=None, amount_u=None, invst_json=json.dumps(json.loads(t0["invst_json"])),
                                            transfer_account_id=ta["account_id"], transfer_account=ta["name"]))
        ins = []
        for ad, t, cost in used:
            j = {"Q": float(t), "G": out}
            if cost is not None: j["I"] = round(float(cost / t), 6)
            if ad != date: j["D"] = ad
            amt = None if cost is None else round(float(cost), 4)
            ins.append(b.insert("transactions", dict(base, account_id=ta["account_id"], action="ShrsIn", amount=amt, amount_u=amt, invst_json=json.dumps(j), link_id=out,
                                                     transfer_account_id=fa["account_id"], transfer_account=fa["name"])))
        b.update("transactions", out, dict(invst_json=json.dumps(dict(json.loads(t0["invst_json"]), G=out)), link_id=ins[0]))
        res = dict(out_txn=out, in_txns=ins, lots=[dict(acquired=a, shares=float(t), cost=None if c is None else float(c)) for a, t, c in used], batch=b.name)
        b.commit(); return res
    except Exception: b.rollback(); raise


ROUTES = [("POST", r"/api/investments/transfer-shares", transfer_shares)]
