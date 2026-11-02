"""Portfolio: holdings per security (shares, price, value, basis, gain) with open lots. Read-only.
Shares and market value come from core.securities_value (same as the account balances); lots are built FIFO from the transactions
(Buy/Reinv*/ShrsIn add a lot (a ShrsIn can carry its own acquired date "D" and cost basis), cost 0 and flagged when the row has no amount or price, Sell/ShrsOut use the oldest first unless the row names a lot with \"L\": <txn_id of the buy> in its data, StkSplit rescales). Lot dates for shares moved in from another
account are the transfer date (no original date is kept), and Quicken's own lot choices are not carried over."""
import datetime, json
from decimal import Decimal as Dc
import core

STALE_DAYS = 7


def fifo_lots(rows):
    """rows: one account's transactions (date order) -> {security: (lots, debt)}; lot = [date, shares, cost|None, txn_id]; debt = shares sold beyond what was held."""
    out = {}
    for r in rows:
        name, act = r["security_name"], r["action"]
        if not name or act not in core.SH_PLUS | core.SH_MINUS | {"StkSplit"}: continue
        j = json.loads(r["invst_json"] or "{}"); q = Dc(repr(j.get("Q") or 0))
        lots, debt = out.get(name, ([], Dc(0)))
        if act == "StkSplit":
            f = q / 10; debt *= f
            for l in lots: l[1] *= f
        elif q and act in core.SH_PLUS:
            cost = Dc(repr(r["amount"])) if r["amount"] is not None else Dc(repr(j["I"])) * q if j.get("I") else None
            take = min(q, debt); debt -= take
            if q > take:
                lots.append([j.get("D") or r["date"], q - take, None if cost is None else cost * (q - take) / q, r["txn_id"]])   # "D": acquired date of shares moved in (ShrsIn)
                lots.sort(key=lambda l: (l[0], l[3]))
        elif q:
            pick = j.get("L")   # optional lot choice: the txn_id of the buy this sale (or transfer out) comes from; what it can't cover, and any unknown id, falls back to oldest first
            for l in sorted(lots, key=lambda l: l[3] != pick):   # stable: the chosen lot first, then date order
                if not q: break
                t = min(q, l[1])
                if l[2] is not None: l[2] -= l[2] * t / l[1]
                l[1] -= t; q -= t
            lots[:] = [l for l in lots if l[1]]
            debt += q
        out[name] = (lots, debt)
    return out


def term(acq, today):
    d = datetime.date.fromisoformat(acq); t = datetime.date.fromisoformat(today)
    try: yr = d.replace(year=d.year + 1)
    except ValueError: yr = d.replace(year=d.year + 1, day=28)
    return "long" if t > yr else "short"


def build(db, today=None, show_hidden=False, group="security"):
    today = today or datetime.date.today().isoformat()
    lp = core.last_prices(db, today, dated=True)
    secs = {r["name"]: r["symbol"] for r in db.execute("SELECT name, symbol FROM securities")}
    cash = {a["account_id"]: a["cash"] for a in core.account_balances(db, today)}
    rows_by = {}
    for r in db.execute("SELECT t.* FROM transactions t JOIN accounts a USING(account_id) WHERE a.register_type='Invst' AND t.date<=? ORDER BY t.date, t.txn_id", (today,)):
        rows_by.setdefault(r["account_id"], []).append(r)
    pos = []   # one per account x security
    for a in db.execute("SELECT * FROM accounts WHERE register_type='Invst' ORDER BY name COLLATE NOCASE"):
        if a["hidden"] and not show_hidden: continue
        rows = rows_by.get(a["account_id"], [])
        _, sh = core.securities_value(rows, secs, {})
        fl = fifo_lots(rows)
        for name, s in sh.items():
            if abs(s) <= Dc("0.000001"): continue
            sym = secs.get(name); p = lp.get(sym)
            lots = [dict(date=d, shares=float(q), cost=0.0 if c is None else float(c), nocost=c is None, txn_id=t) for d, q, c, t in fl.get(name, ([], 0))[0]]
            pos.append(dict(account_id=a["account_id"], account=a["name"], hidden=bool(a["hidden"]), name=name, symbol=sym, shares=s,
                            price=p and p[0], price_date=p and p[1], lots=lots, short=s < 0))
    def row(ps, key):
        sh = sum((x["shares"] for x in ps), Dc(0)); p = ps[0]["price"]; pd = ps[0]["price_date"]
        lots = [dict(l, account=x["account"], account_id=x["account_id"]) for x in ps for l in x["lots"]]
        lots.sort(key=lambda l: (l["date"], l["txn_id"]))
        mv = sh * p if p is not None else None
        cost = sum(l["cost"] for l in lots) if lots and not any(x["short"] for x in ps) else None
        for l in lots:
            l["cost_per_share"] = None if l["nocost"] else l["cost"] / l["shares"]
            l["value"] = l["shares"] * float(p) if p is not None else None
            l["gain"] = None if l["value"] is None else l["value"] - l["cost"]
            l["term"] = term(l["date"], today)
        g = float(mv) - cost if mv is not None and cost is not None else None
        stale = pd is not None and (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(pd)).days > STALE_DAYS
        return dict(key=key, name=ps[0]["name"], symbol=ps[0]["symbol"], shares=float(sh), price=p and float(p), price_date=pd, stale=stale,
                    no_price=p is None, nocost_lots=sum(l["nocost"] for l in lots), short=any(x["short"] for x in ps), value=mv and float(mv), cost=cost, gain=g,
                    gain_pct=g / cost * 100 if g is not None and cost else None, accounts=sorted({x["account"] for x in ps}), lots=lots)
    if group == "account":
        groups = []
        for aid in dict.fromkeys(x["account_id"] for x in pos):
            ps = [x for x in pos if x["account_id"] == aid]
            groups.append(dict(account_id=aid, account=ps[0]["account"], hidden=ps[0]["hidden"], cash=cash[aid] / 100,
                               rows=[row([x], f"{aid}|{x['name']}") for x in ps]))
        for a in db.execute("SELECT * FROM accounts WHERE register_type='Invst' ORDER BY name COLLATE NOCASE"):  # accounts with cash only
            if (show_hidden or not a["hidden"]) and a["account_id"] not in {g["account_id"] for g in groups} and cash[a["account_id"]]:
                groups.append(dict(account_id=a["account_id"], account=a["name"], hidden=bool(a["hidden"]), cash=cash[a["account_id"]] / 100, rows=[]))
        groups.sort(key=lambda g: g["account"].lower()); rows = [r for g in groups for r in g["rows"]]
    else:
        rows = [row(ps, n) for n in sorted({x["name"] for x in pos}, key=str.lower) for ps in [[x for x in pos if x["name"] == n]]]
        groups = None
    cash_rows = [dict(account_id=g["account_id"], account=g["account"], amount=g["cash"]) for g in groups] if groups else \
        [dict(account_id=a["account_id"], account=a["name"], amount=cash[a["account_id"]] / 100)
         for a in db.execute("SELECT * FROM accounts WHERE register_type='Invst' ORDER BY name COLLATE NOCASE") if (show_hidden or not a["hidden"]) and cash[a["account_id"]]]
    secv = sum(r["value"] or 0 for r in rows); cashv = sum(c["amount"] for c in cash_rows); total = secv + cashv
    priced = [r for r in rows if r["cost"] is not None and r["value"] is not None]
    cost = sum(r["cost"] for r in priced); gain = sum(r["value"] - r["cost"] for r in priced)
    for r in rows: r["pct"] = (r["value"] or 0) / total * 100 if total else None
    for c in cash_rows: c["pct"] = c["amount"] / total * 100 if total else None
    return dict(as_of=today, group=group, rows=rows, groups=groups, cash=cash_rows,
                totals=dict(securities=secv, cash=cashv, value=total, cost=cost, gain=gain, gain_pct=gain / cost * 100 if cost else None,
                            unpriced=sum(r["no_price"] for r in rows), stale=sum(r["stale"] for r in rows),
                            no_basis=sum(r["cost"] is None for r in rows)))
