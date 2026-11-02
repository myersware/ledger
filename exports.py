"""CSV export (read-only): transactions for one account or all accounts, optional date range, split lines expanded; and the accounts list."""
import csv, datetime, io, re
import core
from edits import ApiError, Raw, reading

TXN_COLS = ["date", "account", "num_or_action", "payee", "memo", "category", "transfer", "tag", "amount", "cleared", "security", "qty", "price", "txn_id", "split", "txn_amount"]


def _csv(header, rows):
    o = io.StringIO(); w = csv.writer(o); w.writerow(header); w.writerows(rows); return o.getvalue().encode()


def _date(v, what):
    if not v: return None
    try: return datetime.date.fromisoformat(v).isoformat()
    except ValueError: raise ApiError(400, f"{what} must be YYYY-MM-DD")


@reading
def txn_csv(db, m, qs):
    """?account=<id>|all &from= &to=. A transaction with splits becomes one row per split line (amount = the line; txn_amount = the whole); sum `amount` for totals."""
    acct, lo, hi = qs.get("account", "all"), _date(qs.get("from"), "from"), _date(qs.get("to"), "to")
    where, args = ["1=1"], []
    if acct != "all":
        try: where.append("t.account_id=?"); args.append(int(acct))
        except ValueError: raise ApiError(400, "account must be an id or 'all'")
        if not db.execute("SELECT 1 FROM accounts WHERE account_id=?", (args[0],)).fetchone(): raise ApiError(404, "no such account")
    if lo: where.append("t.date>=?"); args.append(lo)
    if hi: where.append("t.date<=?"); args.append(hi)
    sp = {}
    for s in db.execute(f"SELECT s.* FROM splits s JOIN transactions t USING(txn_id) WHERE {' AND '.join(where)} ORDER BY s.txn_id, s.split_seq", args): sp.setdefault(s["txn_id"], []).append(s)
    rows = []
    for t in db.execute(f"SELECT t.*, a.name AS acct FROM transactions t JOIN accounts a USING(account_id) WHERE {' AND '.join(where)} ORDER BY a.name COLLATE NOCASE, t.date, t.txn_id", args):
        import json
        j = json.loads(t["invst_json"] or "{}") if t["action"] else {}
        base = [t["date"], t["acct"], t["action"] or t["check_number"] or "", t["payee_text"] or "", t["memo"] or ""]
        cl = {"cleared": "c", "reconciled": "R"}.get(t["cleared_status"], "")
        tail = [t["security_name"] or "", j.get("Q") if j.get("Q") is not None else "", j.get("I") if j.get("I") is not None else "", t["txn_id"]]
        if t["txn_id"] in sp:
            n = len(sp[t["txn_id"]])
            for i, s in enumerate(sp[t["txn_id"]], 1):
                rows.append(base[:4] + [s["memo"] or t["memo"] or "", s["category"] or "", s["transfer_account"] or "", s["tag"] or "", s["amount"], cl, *tail, f"{i}/{n}", t["amount"]])
        else:
            rows.append(base + [t["category"] or "", t["transfer_account"] or "", t["tag"] or "", "" if t["amount"] is None else t["amount"], cl, *tail, "", ""])
    nm = "all" if acct == "all" else re.sub(r"[^A-Za-z0-9]+", "-", db.execute("SELECT name FROM accounts WHERE account_id=?", (int(acct),)).fetchone()[0]).strip("-")
    return Raw(_csv(TXN_COLS, rows), "text/csv", f"ledger-transactions-{nm}{'-from-' + lo if lo else ''}{'-to-' + hi if hi else ''}.csv")


@reading
def acct_csv(db, m, qs):
    bal = {a["account_id"]: a for a in core.account_balances(db)}
    rows = []
    for a in db.execute("SELECT * FROM accounts ORDER BY list_type, name COLLATE NOCASE"):
        b = bal[a["account_id"]]
        rows.append([a["name"], a["acct_type"] or a["register_type"] or a["list_type"], a["register_type"] or a["list_type"], a["institution"] or "", a["acct_number"] or "", a["description"] or "",
                     "yes" if a["hidden"] else "", b["txn_count"], b["first_date"] or "", b["last_date"] or "", b["balance"] / 100, b["cash"] / 100 if b["inv"] else "", b["securities"] / 100 if b["inv"] else ""])
    return Raw(_csv(["account", "type", "family", "institution", "account_number", "description", "hidden", "transactions", "first_date", "last_date", "balance_today", "cash", "securities"], rows),
               "text/csv", "ledger-accounts.csv")


ROUTES = [("GET", r"/api/export/transactions\.csv", txn_csv), ("GET", r"/api/export/accounts\.csv", acct_csv)]
