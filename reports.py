"""Read-only reports: spending by category and net worth over time. Never writes."""
import bisect, datetime, json, sqlite3
from decimal import Decimal as Dc
import core

UNCAT = "(uncategorized)"
LIAB = {"CCard", "Oth L"}


class ReportError(Exception):
    pass


def _d(s, what):
    try: return datetime.date.fromisoformat(str(s)).isoformat()
    except ValueError: raise ReportError(f"{what} must be YYYY-MM-DD")


def _lines_sql(hidden, stale):
    """Spending lines: plain transactions, or the split lines of split transactions, at the split amount.
    Excluded: transfers (transaction or split), investment accounts, 'Opening Balance*' rows, optionally hidden accounts / STALE? rows."""
    flt = ("AND a.register_type IS NOT 'Invst' AND COALESCE(t.payee_text,'') NOT LIKE 'Opening Balance%' AND t.date BETWEEN :d1 AND :d2 "
           + ("" if hidden else "AND a.hidden=0 ") + ("AND instr(COALESCE(t.merge_note,''),'STALE?')=0 " if stale else ""))
    return ("SELECT t.txn_id, t.date, a.name AS account, t.payee_text AS payee, t.memo AS memo, t.category AS cat, t.amount AS amt, 0 AS is_split "
            "FROM transactions t JOIN accounts a USING(account_id) WHERE t.split_count=0 AND t.transfer_account_id IS NULL AND t.amount IS NOT NULL " + flt +
            "UNION ALL "
            "SELECT t.txn_id, t.date, a.name, t.payee_text, COALESCE(NULLIF(s.memo,''), t.memo), s.category, s.amount, 1 "
            "FROM splits s JOIN transactions t USING(txn_id) JOIN accounts a USING(account_id) "
            "WHERE t.split_count>0 AND s.transfer_account_id IS NULL AND s.amount IS NOT NULL " + flt)


def _lines(db, d1, d2, hidden, stale):
    inc = {r["name"]: bool(r["is_income"]) for r in db.execute("SELECT name, is_income FROM categories")}
    for r in db.execute(_lines_sql(hidden, stale) + " ORDER BY 2, 1", dict(d1=d1, d2=d2)):
        c = core.cents(r["amt"]); cat = r["cat"] or None
        section = ("income" if inc.get(cat) else "expense") if cat else ("income" if c > 0 else "expense")
        yield dict(txn_id=r["txn_id"], date=r["date"], account=r["account"], payee=r["payee"] or "", memo=r["memo"] or "",
                   category=cat or UNCAT, cents=c, section=section, split=bool(r["is_split"]))


def spending(db, d1, d2, hidden=False, stale=False):
    d1, d2 = _d(d1, "from"), _d(d2, "to")
    if d1 > d2: raise ReportError("from is after to")
    tot = {"income": {}, "expense": {}}; cnt = {"income": {}, "expense": {}}
    for l in _lines(db, d1, d2, hidden, stale):
        tot[l["section"]][l["category"]] = tot[l["section"]].get(l["category"], 0) + l["cents"]
        cnt[l["section"]][l["category"]] = cnt[l["section"]].get(l["category"], 0) + 1
    out = dict(date_from=d1, date_to=d2, hidden=hidden, exclude_stale=stale)
    for sec in ("income", "expense"):
        sign = 1 if sec == "income" else -1  # expense shown as positive spending (refunds reduce it)
        nodes = {}
        for cat, c in tot[sec].items():
            parts = cat.split(":") if cat != UNCAT else [cat]
            for i in range(len(parts)):
                name = ":".join(parts[:i + 1]); n = nodes.setdefault(name, dict(name=name, label=parts[i], own=0, total=0, count=0, children=[]))
                n["total"] += sign * c
                if i == len(parts) - 1: n["own"] += sign * c; n["count"] += cnt[sec][cat]
        for name, n in nodes.items():
            if ":" in name: nodes[name.rsplit(":", 1)[0]]["children"].append(n)
        roots = sorted((n for k, n in nodes.items() if ":" not in k), key=lambda n: -n["total"])
        def fin(n):
            n["children"].sort(key=lambda x: -x["total"]); [fin(c) for c in n["children"]]
            n["own"] /= 100; n["total"] /= 100
        for r in roots: fin(r)
        out[sec] = dict(total=sum(sign * c for c in tot[sec].values()) / 100, categories=roots, rows=sum(cnt[sec].values()))
    out["net"] = round(out["income"]["total"] - out["expense"]["total"], 2)
    return out


def category_transactions(db, cat, section, d1, d2, hidden=False, stale=False, limit=2000):
    d1, d2 = _d(d1, "from"), _d(d2, "to")
    rows, total = [], 0
    for l in _lines(db, d1, d2, hidden, stale):
        if l["section"] != section: continue
        if l["category"] == cat or l["category"].startswith(cat + ":"):
            total += l["cents"]; rows.append(dict(l, amount=l["cents"] / 100))
    for r in rows: r.pop("cents"); r.pop("section")
    return dict(category=cat, section=section, count=len(rows), total=total / 100, rows=rows[:limit], truncated=len(rows) > limit)


def _points(d1, d2, gran):
    d1 = datetime.date.fromisoformat(d1); d2 = datetime.date.fromisoformat(d2)
    if gran == "auto": gran = "year" if (d2 - d1).days > 3800 else "month"
    pts = []
    y, m = d1.year, d1.month
    while True:
        if gran == "year": e = datetime.date(y, 12, 31); y += 1
        else:
            e = (datetime.date(y + m // 12, m % 12 + 1, 1) - datetime.timedelta(days=1)); y, m = (y + m // 12, m % 12 + 1)
        if e > d2: break
        if e >= d1: pts.append(e.isoformat())
    if not pts or pts[-1] != d2.isoformat(): pts.append(d2.isoformat())
    return pts, gran


HOUSEHOLD = "Household items"


def household_cents(db, on):
    """Inventory value by date: [(purchase_date or None, cents)] using current_value, else purchase_price. (not an account; never part of an account balance)"""
    if not on: return []
    try: rows = db.execute("SELECT purchase_date, COALESCE(current_value, purchase_price, 0) AS v FROM inventory_items").fetchall()
    except sqlite3.OperationalError: return []
    return [(r["purchase_date"] or None, core.cents(r["v"])) for r in rows]


def networth(db, d1, d2, gran="auto", hidden=False, detail=False, household=None):
    """household=None follows the saved setting (default on); True/False overrides it. It adds one 'Household items' line (in assets) and nothing else."""
    today = datetime.date.today().isoformat()
    d1, d2 = _d(d1, "from"), min(_d(d2, "to"), today)
    if d1 > d2: raise ReportError("from is after to (to is capped at today)")
    if gran not in ("auto", "month", "year"): raise ReportError("gran must be auto, month or year")
    pts, gran = _points(d1, d2, gran)
    secsym = {r["name"]: r["symbol"] for r in db.execute("SELECT name, symbol FROM securities")}
    pr = {}
    for r in db.execute("SELECT symbol, date, price FROM prices WHERE price IS NOT NULL ORDER BY date, price_id"):
        e = pr.setdefault(r["symbol"], ([], [])); e[0].append(r["date"]); e[1].append(Dc(repr(r["price"])))
    rows_by = {}
    for r in db.execute("SELECT account_id, date, action, security_name, invst_json, amount FROM transactions WHERE date<=? ORDER BY account_id, date, txn_id", (d2,)):
        rows_by.setdefault(r["account_id"], []).append(r)
    series = []  # per account: (name, type, [cents at each point])
    for a in db.execute("SELECT * FROM accounts ORDER BY name COLLATE NOCASE"):
        if a["hidden"] and not hidden: continue
        inv = a["register_type"] == "Invst"; typ = a["register_type"] or a["list_type"]
        rows = rows_by.get(a["account_id"], []); i = 0; cash = 0; sh = {}; vals = []
        for D in pts:
            while i < len(rows) and rows[i]["date"] <= D:
                r = rows[i]; i += 1; cash += core.effect(r, inv)
                if inv and r["security_name"]:
                    q = Dc(repr((json.loads(r["invst_json"] or "{}")).get("Q") or 0)); n = r["security_name"]
                    if r["action"] in core.SH_PLUS: sh[n] = sh.get(n, Dc(0)) + q
                    elif r["action"] in core.SH_MINUS: sh[n] = sh.get(n, Dc(0)) - q
                    elif r["action"] == "StkSplit": sh[n] = sh.get(n, Dc(0)) * q / 10
            mv = Dc(0)
            for n, s in sh.items():
                e = pr.get(secsym.get(n))
                if e:
                    k = bisect.bisect_right(e[0], D) - 1
                    if k >= 0: mv += s * e[1][k]
            vals.append(cash + int((mv * 100).to_integral_value()))
        series.append((a["account_id"], a["name"], typ, vals))
    types = sorted({s[2] for s in series if s[2]} | {s[2] for s in series}, key=lambda t: (t is None, str(t)))
    on = core.household_on(db) if household is None else bool(household)
    items = household_cents(db, on)
    if items: types.append(HOUSEHOLD)
    out = []
    for k, D in enumerate(pts):
        bt = {}
        for _, _, typ, v in series: bt[typ] = bt.get(typ, 0) + v[k]
        if items: bt[HOUSEHOLD] = sum(c for d, c in items if d is None or d <= D)
        assets = sum(c for t, c in bt.items() if t not in LIAB); liab = sum(c for t, c in bt.items() if t in LIAB)
        p = dict(date=D, types={str(t): c / 100 for t, c in bt.items()}, assets=assets / 100, liabilities=liab / 100, net=(assets + liab) / 100)
        if detail: p["accounts"] = {s[0]: s[3][k] for s in series}
        out.append(p)
    n = db.execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0] if db.execute("SELECT 1 FROM sqlite_master WHERE name='inventory_items'").fetchone() else 0
    return dict(date_from=d1, date_to=d2, granularity=gran, hidden=hidden, types=[str(t) for t in types], points=out,
                household=dict(included=bool(items), items=n, enabled=on))
