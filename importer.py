"""CSV import (bank csv files / Plaid files). preview() is read-only; run_import() writes one edit_log batch."""
import csv, datetime, glob, hashlib, json, os, re, sqlite3
from decimal import Decimal, InvalidOperation
import core, edits

IMPORT_DIR = os.environ.get("LEDGER_IMPORTS", os.path.join(core.HERE, "imports"))
WINDOW = 3  # days, fuzzy match
DIRECT = "plaid-direct"  # csv source written by the built-in Plaid download; its rows may also match rows that already carry another bank id (same amount, date within WINDOW days)
# Only unambiguous Plaid detailed categories -> ledger categories (used after payee history).
PLAID_MAP = {
    "FOOD_AND_DRINK_GROCERIES": "Groceries", "FOOD_AND_DRINK_RESTAURANT": "Dining", "FOOD_AND_DRINK_FAST_FOOD": "Dining",
    "FOOD_AND_DRINK_COFFEE": "Dining", "MEDICAL_PHARMACIES_AND_SUPPLEMENTS": "Medical:Medicine",
    "ENTERTAINMENT_MUSIC_AND_AUDIO": "Music", "TRANSPORTATION_GAS": "Auto:Fuel",
}  # deliberately short: Plaid's labels are often wrong (a payment app is not always a transfer, a cash withdrawal is not rent)


def cents(s):
    return int((Decimal(s) * 100).to_integral_value())


def _money(v):
    v = str(v or "").strip().replace("$", "").replace(",", "").replace(" ", "")
    neg = v.startswith("(") and v.endswith(")") or v.endswith("-")
    v = v.strip("()").rstrip("-")
    if v in ("", "+", "-"): raise ValueError("no amount")
    d = Decimal(v); return -abs(d) if neg else d


def _gdate(v, fmt):
    v = str(v or "").strip()[:10] if fmt == "iso" else str(v or "").strip().split(" ")[0]
    for f in {"iso": ["%Y-%m-%d"], "mdy": ["%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y"], "dmy": ["%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y"], "ymd": ["%Y/%m/%d"]}.get(fmt) or ["%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"]:
        try: return datetime.datetime.strptime(v, f).date().isoformat()
        except ValueError: pass
    raise ValueError(f"bad date {v!r}")


def _generic(r, cm, slug, seen):
    """A row of a bank's own csv -> the importer's row shape, using the saved column map. The txn_id is a hash of slug/date/amount/payee/n-th identical row,
    so importing the same file (or overlapping statements) twice is caught as duplicates."""
    g = lambda k: (r.get(cm[k]) or "").strip() if cm.get(k) else ""
    date = _gdate(g("date"), cm.get("date_format") or "auto")
    amt = _money(g("amount")) if cm.get("amount") else abs(_money(g("credit") or "0")) - abs(_money(g("debit") or "0"))
    if cm.get("negate"): amt = -amt
    k = (date, g("payee"), amt); seen[k] = seen.get(k, 0) + 1
    h = hashlib.sha1(f"{slug}|{date}|{amt}|{g('payee')}|{seen[k]}".encode()).hexdigest()[:16]
    return dict(date=date, amount=str(amt), payee=g("payee"), memo=g("memo"), txn_id="csv-" + h, source="csv")


def csv_maps(db):
    return {r[0]: json.loads(r[1]) for r in db.execute("SELECT slug, csv_map FROM import_map WHERE csv_map IS NOT NULL")}


def read_files(cmaps=None):
    """-> {slug: dict(files, rows, inv_header, warnings)} from imports/<slug>/<slug>_<YYYY-MM>.csv. cmaps: slug -> column map for a bank's own csv layout (see _generic)."""
    out = {}; cmaps = cmaps or {}
    for d in sorted(glob.glob(os.path.join(IMPORT_DIR, "*", ""))):
        slug = os.path.basename(os.path.dirname(d)); e = out[slug] = dict(files=[], rows=[], inv_header=False, warnings=[])
        for f in sorted(glob.glob(os.path.join(d, "*.csv"))):
            fn = os.path.basename(f); e["files"].append(fn)
            if not fn.startswith(slug + "_"): e["warnings"].append(f"{fn}: file name does not start with '{slug}_'")
            cm = cmaps.get(slug); gseen = {}
            with open(f, newline="", encoding="utf-8-sig") as fh:
                rd = csv.DictReader(fh)
                if cm and (missing := [c for k, c in cm.items() if k in COLS and c and c not in (rd.fieldnames or [])]):
                    e["warnings"].append(f"{fn}: no column {', '.join(map(repr, missing))} (check the column mapping), file skipped"); continue
                e["inv_header"] |= "type" in (rd.fieldnames or []) and not cm
                for n, r in enumerate(rd, 2):
                    try:
                        if cm: r = _generic(r, cm, slug, gseen)
                        datetime.date.fromisoformat(r["date"]); r["_c"] = cents(r["amount"])
                    except (ValueError, InvalidOperation, KeyError, TypeError, ArithmeticError):
                        e["warnings"].append(f"{fn}:{n}: bad date/amount, skipped"); continue
                    if r.get("account") and r["account"] != slug: e["warnings"].append(f"{fn}:{n}: account column '{r['account']}' != folder '{slug}'")
                    if not r.get("txn_id"): e["warnings"].append(f"{fn}:{n}: no txn_id, skipped"); continue
                    r["_file"], r["_line"] = fn, n; e["rows"].append(r)
    return out


def _f(x):
    try: return float(x)
    except (TypeError, ValueError): return 0.0


def _sweep(r):  # money-market sweep leg: price 1.00 and quantity == dollars
    return r.get("type") in ("buy", "sell") and abs(_f(r.get("price")) - 1) < 1e-9 and abs(abs(_f(r.get("quantity"))) * 100 - abs(r["_c"])) <= 1


def inv_groups(rows):
    """Collapse Plaid's paired rows into one ledger transaction each:
    buy + cash row of the same security (price != 1) = ReinvDiv; sweep buy + cash row = Div on the cash row's security;
    sweep sell + cash row without security = XIn/XOut. Anything else is one transaction per row."""
    used, groups = set(), []
    def pair(i, pred):
        return next((j for j, x in enumerate(rows) if j not in used and j != i and x["date"] == rows[i]["date"] and pred(x)), None)
    def mk(parts, action, security, amount_c, qty=None, price=None):
        g = dict(parts=parts, action=action, security=security or None, amount_c=amount_c, qty=qty, price=price, date=parts[0]["date"],
                 ids=[p["txn_id"] for p in parts], payee=next((p.get("payee") for p in parts if p.get("payee")), "") or "", memo=next((p.get("memo") for p in parts if p.get("memo")), "") or "")
        groups.append(g); used.update(rows.index(p) for p in parts)
    for i, r in enumerate(rows):
        if i in used: continue
        t = r.get("type")
        if t == "buy":
            j = pair(i, lambda x: x.get("type") == "cash" and x.get("security") and x["_c"] == -r["_c"] and x["_c"] > 0)
            if j is not None:
                x = rows[j]
                if _sweep(r): mk([r, x], "Div", x["security"], x["_c"]); continue
                if x["security"] == r["security"]: mk([r, x], "ReinvDiv", r["security"], abs(r["_c"]), abs(_f(r["quantity"])), _f(r["price"])); continue
        elif t == "sell" and _sweep(r):
            j = pair(i, lambda x: x.get("type") == "cash" and not x.get("security") and x["_c"] == -r["_c"])
            if j is not None: mk([r, rows[j]], "XOut" if rows[j]["_c"] < 0 else "XIn", None, abs(r["_c"])); continue
    for i, r in enumerate(rows):
        if i in used: continue
        t, c = r.get("type"), r["_c"]
        if t == "buy": mk([r], "Buy", r.get("security"), abs(c), abs(_f(r["quantity"])), _f(r["price"]))
        elif t == "sell": mk([r], "Sell", r.get("security"), abs(c), abs(_f(r["quantity"])), _f(r["price"]))
        elif t == "cash" and r.get("security") and c > 0: mk([r], "Div", r["security"], c)
        elif t == "cash" and not r.get("security") and c: mk([r], "XIn" if c > 0 else "XOut", None, abs(c))
        elif t == "fee" and c < 0: mk([r], "MiscExp", r.get("security"), abs(c))
        else: mk([r], "?", r.get("security"), abs(c))  # unsupported type
    return sorted(groups, key=lambda g: (g["date"], g["parts"][0]["_line"]))


FAM = {"Div": {"Div", "DivX"}, "IntInc": {"IntInc", "IntIncX"}, "MiscExp": {"MiscExp", "MiscExpX"}, "XIn": {"XIn", "Cash", "ContribX"}, "XOut": {"XOut", "Cash"},
       "ReinvDiv": {"ReinvDiv", "ReinvLg", "ReinvSh", "ReinvInt", "Buy", "BuyX"}, "Buy": {"Buy", "BuyX", "ReinvDiv", "ReinvLg", "ReinvSh", "ReinvInt"},
       "Sell": {"Sell", "SellX", "ShrsOut"}}


def _inv_match(g, x, sym):
    """x: existing row (Row) + parsed (symbol, qty). Match on ticker + quantity (or amount for cash-type actions)."""
    if x["action"] not in FAM.get(g["action"], ()): return False
    a = g["action"]
    if a in ("Buy", "Sell", "ReinvDiv"):
        return sym == g["security"] and abs(x["_q"] - g["qty"]) <= max(0.0005, 0.001 * g["qty"])
    if a in ("XIn", "XOut"):
        return not x["security_name"] and core.effect(x, True) == (g["amount_c"] if a == "XIn" else -g["amount_c"])
    if a == "Div" and sym != g["security"]: return False
    return abs(core.cents(x["amount"])) == g["amount_c"]


def _inv_preview(db, aid, rows, ext, seen, res, counts):
    import json as _json
    class _B: pass
    shim = _B(); shim.db = db
    groups = inv_groups(rows)
    symof = {r["name"]: r["symbol"] for r in db.execute("SELECT name, symbol FROM securities")}
    lo = (datetime.date.fromisoformat(min(r["date"] for r in rows)) - datetime.timedelta(days=WINDOW)).isoformat()
    hi = (datetime.date.fromisoformat(max(r["date"] for r in rows)) + datetime.timedelta(days=WINDOW)).isoformat()
    existing = []
    fids = {i for g in groups for i in g["ids"] if any(p.get("source") == DIRECT for p in g["parts"])}; direct = bool(fids)  # only ids of direct (Plaid) rows are claimed; other csv ids already in the DB don't block matching
    for x in db.execute("SELECT * FROM transactions WHERE account_id=? AND date BETWEEN ? AND ?" + ("" if direct else " AND ext_id IS NULL"), (aid, lo, hi)):
        if x["ext_id"] and fids & set(x["ext_id"].split(",")): continue
        x = dict(x); x["_q"] = _f((_json.loads(x["invst_json"] or "{}")).get("Q")); x["_sym"] = symof.get(x["security_name"]); existing.append(x)
    for g in groups:
        g["status"] = None; g["match"] = None
        if g["action"] == "?": g["status"] = "deferred"; g["reason"] = "unsupported Plaid type: " + (g["parts"][0].get("type") or "")
        elif any(i in ext or i in seen for i in g["ids"]): g["status"] = "duplicate"; g["match"] = next((ext[i] for i in g["ids"] if i in ext), None)
        seen.update(g["ids"])
    pairs = []
    for i, g in enumerate(groups):
        if g["status"]: continue
        for x in existing:
            dd = abs((datetime.date.fromisoformat(g["date"]) - datetime.date.fromisoformat(x["date"])).days)
            if dd <= WINDOW and _inv_match(g, x, x["_sym"]): pairs.append((dd, i, x["txn_id"], x))
    pairs.sort(key=lambda p: p[:3]); ur, ux = set(), set()
    for dd, i, xid, x in pairs:
        if i in ur or xid in ux: continue
        ur.add(i); ux.add(xid); groups[i]["status"] = "matched"; groups[i]["match"] = dict(txn_id=xid, date=x["date"], payee=f"{x['action']} {x['security_name'] or ''}".strip(), days=dd)
    counts["proposed"] = len(groups)
    for g in groups:
        if not g["status"]:
            try:
                if g["security"]: edits._sec(shim, aid, g["security"])
                g["status"] = "new"
            except edits.ApiError as e: g["status"] = "deferred"; g["reason"] = str(e)
        counts[g["status"]] += 1
        a, ac = g["action"], g["amount_c"] / 100
        eff = ac if a in ("Div", "IntInc", "Sell", "XIn") else -ac if a in ("Buy", "XOut", "MiscExp") else 0
        res["rows"].append(dict(file=g["parts"][0]["_file"], line=g["parts"][0]["_line"], date=g["date"], payee=g["payee"], amount=ac, cash_effect=eff, memo=g["memo"],
                                plaid_category="", txn_id=",".join(g["ids"]), status=g["status"], match=g["match"], possible_match=None, category=None, category_source=None,
                                type=a, security=g["security"], quantity=g["qty"], price=g["price"], reason=g.get("reason"),
                                parts=[dict(file=p["_file"], line=p["_line"], type=p.get("type"), security=p.get("security"), amount=p["_c"] / 100) for p in g["parts"]]))


def preview(db):
    maps = {r["slug"]: r for r in db.execute("SELECT m.*, a.name, a.register_type FROM import_map m LEFT JOIN accounts a USING(account_id)")}
    data = read_files(csv_maps(db))
    ext = {k: r[1] for r in db.execute("SELECT ext_id, txn_id FROM transactions WHERE ext_id IS NOT NULL") for k in r[0].split(",")}  # ext_id may hold 2 ids (investment pairs)
    balances = {a["account_id"]: a for a in core.account_balances(db)} if maps else {}
    cats = {r[0] for r in db.execute("SELECT name FROM categories")}
    seen, result = set(), []
    for slug in sorted(set(maps) | set(data)):
        m, dta = maps.get(slug), data.get(slug, dict(files=[], rows=[], inv_header=False, warnings=[]))
        res = dict(slug=slug, files=dta["files"], warnings=list(dta["warnings"]), rows=[], account_id=None, account=None, confirmed=False)
        counts = dict(new=0, matched=0, duplicate=0, deferred=0, total=len(dta["rows"]))
        res["counts"] = counts; res["ledger_only"] = []; result.append(res)
        if not m or m["account_id"] is None:
            res["warnings"].append("no account mapped for this slug"); continue
        aid = m["account_id"]; inv = m["register_type"] == "Invst"; b = balances.get(aid)
        res.update(account_id=aid, account=m["name"], confirmed=bool(m["confirmed"]), inv=inv,
                   account_info=dict(type=b["type"], balance=b["balance"] / 100, last_date=b["last_date"], txn_count=b["txn_count"]) if b else None)
        if inv:
            syms = {r["security"] for r in dta["rows"] if r.get("security")}
            held = {r[0] for r in db.execute("SELECT symbol FROM security_positions WHERE account_id=? AND ABS(shares)>1e-6", (aid,))}
            res["evidence"] = f"{len(syms & held)}/{len(syms)} file securities currently held in this account ({', '.join(sorted(syms & held)) or '-'})"
        rows = dta["rows"]
        if not rows: continue
        if inv:
            _inv_preview(db, aid, rows, ext, seen, res, counts); continue
        lo = (datetime.date.fromisoformat(min(r["date"] for r in rows)) - datetime.timedelta(days=WINDOW)).isoformat()
        hi = (datetime.date.fromisoformat(max(r["date"] for r in rows)) + datetime.timedelta(days=WINDOW)).isoformat()
        existing = db.execute("SELECT txn_id, date, amount, payee_text, ext_id FROM transactions WHERE account_id=? AND date BETWEEN ? AND ? AND ext_id IS NULL",
                              (aid, lo, hi)).fetchall()
        fids = {r["txn_id"] for r in rows if r.get("source") == DIRECT}  # direct rows may also claim rows that already hold another bank id, unless that row already carries one of the direct rows' own ids
        existing_x = [x for x in db.execute("SELECT txn_id, date, amount, payee_text, ext_id FROM transactions WHERE account_id=? AND date BETWEEN ? AND ? AND ext_id IS NOT NULL",
                                            (aid, lo, hi)) if not fids & set(x["ext_id"].split(","))] if any(r.get("source") == DIRECT for r in rows) else []
        # classify: duplicate (txn_id known) first, then fuzzy matching one-to-one, closest date first
        for r in rows:
            r["_status"] = None
            if r["txn_id"] in ext or r["txn_id"] in seen: r["_status"] = "duplicate"; r["_match"] = ext.get(r["txn_id"])
            seen.add(r["txn_id"])
        pairs = []
        for i, r in enumerate(rows):
            if r["_status"]: continue
            for x in existing + (existing_x if r.get("source") == DIRECT else []):
                dd = abs((datetime.date.fromisoformat(r["date"]) - datetime.date.fromisoformat(x["date"])).days)
                amt_ok = (abs(cents(x["amount"] or 0)) == abs(r["_c"])) if inv else (cents(x["amount"] or 0) == r["_c"])
                if dd <= WINDOW and amt_ok:
                    same = (x["payee_text"] or "").strip().lower() == (r.get("payee") or "").strip().lower()
                    pairs.append((dd, (0 if same else 1, x["ext_id"] is not None), i, x["txn_id"], x))
        pairs.sort(key=lambda p: p[:4]); used_r, used_x = set(), set()
        for dd, _, i, xid, x in pairs:
            if i in used_r or xid in used_x: continue
            used_r.add(i); used_x.add(xid); rows[i]["_status"] = "possible-match" if inv else "matched"; rows[i]["_match"] = dict(txn_id=xid, date=x["date"], payee=x["payee_text"], days=dd)
        if not inv:  # ledger rows inside the months the files cover that no file row claimed (info only)
            mo = sorted(re.search(r"_(\d{4}-\d{2})\.csv$", f).group(1) for f in dta["files"] if re.search(r"_(\d{4}-\d{2})\.csv$", f))
            if mo:
                y, mm = map(int, mo[-1].split("-")); end = (datetime.date(y + mm // 12, mm % 12 + 1, 1) - datetime.timedelta(days=1)).isoformat()
                for x in db.execute("SELECT txn_id, date, amount, payee_text, source FROM transactions WHERE account_id=? AND date BETWEEN ? AND ? AND ext_id IS NULL ORDER BY date, txn_id", (aid, mo[0] + "-01", end)):
                    if x["txn_id"] not in used_x: res["ledger_only"].append(dict(txn_id=x["txn_id"], date=x["date"], amount=x["amount"], payee=x["payee_text"], source=x["source"]))
        hist, dflt = {}, {}
        if not inv:
            try: dflt = {" ".join(n.split()).lower(): c for n, c in db.execute("SELECT name, default_category FROM payees WHERE default_category IS NOT NULL AND name IS NOT NULL") if c in cats}
            except Exception: dflt = {}
            for p in db.execute("SELECT payee_text, category FROM transactions WHERE account_id=? AND category IS NOT NULL AND payee_text IS NOT NULL ORDER BY date, txn_id", (aid,)):
                hist[p[0].strip().lower()] = p[1]
        for r in rows:
            st = r["_status"] or "new"
            counts[st] += 1
            cat = src = None
            if not inv and st == "new":
                h = hist.get((r.get("payee") or "").strip().lower()); dc = dflt.get(" ".join((r.get("payee") or "").split()).lower())
                pm = PLAID_MAP.get(r.get("category")) if PLAID_MAP.get(r.get("category")) in cats else None
                if dc and not pm: cat, src = dc, "from payee default"   # a source category always wins over the payee default
                elif h: cat, src = h, "payee-history"
                elif pm: cat, src = pm, "plaid-map"
            res["rows"].append(dict(file=r["_file"], line=r["_line"], date=r["date"], payee=r.get("payee") or "", amount=r["_c"] / 100,
                                    memo=r.get("memo") or "", plaid_category=r.get("category") or "", txn_id=r["txn_id"], status=st,
                                    possible_match=(r.get("_match") if st == "deferred" else None), match=(r.get("_match") if st in ("matched", "duplicate") else None),
                                    category=cat, category_source=src, type=r.get("type"), security=r.get("security"),
                                    quantity=r.get("quantity"), price=r.get("price")))
    return result


def run_import(slugs=None):
    b = edits.Batch(edits.batch_id("import"), "csv import")
    try:
        pv = [p for p in preview(b.db) if (slugs is None or p["slug"] in slugs)]
        todo = [p for p in pv if p["counts"]["new"] or p["counts"]["matched"]]
        bad = [p["slug"] for p in todo if not p["confirmed"]]
        if bad: raise edits.ApiError(409, "mapping not confirmed for: " + ", ".join(bad))
        out = []
        for p in todo:
            rt = b.db.execute("SELECT register_type FROM accounts WHERE account_id=?", (p["account_id"],)).fetchone()[0]
            ins = lnk = 0
            for r in p["rows"]:
                if r["status"] == "new" and rt == "Invst":
                    t, _ = edits.inv_t(b, p["account_id"], dict(date=r["date"], action=r["type"], security=r["security"], qty=r["quantity"], price=r["price"],
                                                                  amount=r["amount"], memo=r["memo"], payee=r["payee"]))
                    b.insert("transactions", dict(t, account_id=p["account_id"], register_type="Invst", source="import", ext_id=r["txn_id"])); ins += 1
                elif r["status"] == "new":
                    cid = b.db.execute("SELECT category_id FROM categories WHERE name=?", (r["category"],)).fetchone() if r["category"] else None
                    pid = b.db.execute("SELECT payee_id FROM payees WHERE name=?", (r["payee"],)).fetchone() if r["payee"] else None
                    b.insert("transactions", dict(account_id=p["account_id"], register_type=rt, date=r["date"], amount=r["amount"], amount_u=r["amount"],
                             payee_text=r["payee"] or None, payee_id=pid and pid[0], memo=r["memo"] or None, category=r["category"], category_id=cid and cid[0],
                             split_count=0, source="import", ext_id=r["txn_id"]))
                    ins += 1
                elif r["status"] == "matched":
                    cur = b.db.execute("SELECT ext_id FROM transactions WHERE txn_id=?", (r["match"]["txn_id"],)).fetchone()[0]
                    b.update("transactions", r["match"]["txn_id"], dict(ext_id=f"{cur},{r['txn_id']}" if cur else r["txn_id"])); lnk += 1
            out.append(dict(slug=p["slug"], account=p["account"], inserted=ins, linked=lnk, new_total=round(sum(r.get("cash_effect", r["amount"]) for r in p["rows"] if r["status"] == "new"), 2)))
        if not out:
            b.rollback(); return dict(batch=None, accounts=[], note="nothing to import")
        b.commit(); return dict(batch=b.name, accounts=out)
    except Exception:
        b.rollback(); raise


def set_mapping(items):
    db = sqlite3.connect(core.DB_PATH)
    try:
        for it in items:
            a = db.execute("SELECT account_id FROM accounts WHERE account_id=?", (it.get("account_id"),)).fetchone()
            if not a or not isinstance(it.get("slug"), str) or not it["slug"]: raise edits.ApiError(400, f"bad mapping entry: {it}")
            db.execute("INSERT INTO import_map(slug, account_id, confirmed, updated) VALUES(?,?,1,?) ON CONFLICT(slug) DO UPDATE SET account_id=excluded.account_id, confirmed=1, updated=excluded.updated",
                       (it["slug"], a[0], datetime.datetime.now().isoformat(timespec="seconds")))
        db.commit()
    finally:
        db.close()


COLS = ("date", "payee", "amount", "debit", "credit", "memo")


def _slug(v):
    v = str(v or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", v): raise edits.ApiError(400, "bad slug")
    return v


@edits.reading
def csv_columns(db, m, qs):
    """The columns and first rows of the csv files in imports/<slug>/, and the saved map, for the column-mapping dialog."""
    slug = _slug(qs.get("slug")); files = []
    for f in sorted(glob.glob(os.path.join(IMPORT_DIR, slug, "*.csv"))):
        with open(f, newline="", encoding="utf-8-sig") as fh:
            rd = csv.DictReader(fh); files.append(dict(file=os.path.basename(f), columns=rd.fieldnames or [], sample=[r for _, r in zip(range(5), rd)]))
    return dict(slug=slug, files=files, map=csv_maps(db).get(slug))


def set_csv_map(m, d):
    """Save (or with map=null clear) the column map for a slug: {date, payee, amount | debit/credit, memo, date_format: auto|iso|mdy|dmy, negate}. Applies from the next preview."""
    slug = _slug(d.get("slug")); cm = d.get("map")
    if cm is not None:
        if not isinstance(cm, dict) or not cm.get("date"): raise edits.ApiError(400, "pick the date column")
        if not cm.get("amount") and not (cm.get("debit") or cm.get("credit")): raise edits.ApiError(400, "pick the amount column, or the debit and credit columns")
        if cm.get("date_format") not in (None, "", "auto", "iso", "mdy", "dmy", "ymd"): raise edits.ApiError(400, "date_format must be auto, iso, mdy, dmy or ymd")
        cm = {k: str(cm[k]) for k in COLS if cm.get(k)} | {k: cm[k] for k in ("date_format", "negate") if cm.get(k)}
        cols = {c for f in csv_columns_raw(slug) for c in f}
        if cols and (bad := [c for k, c in cm.items() if k in COLS and c not in cols]): raise edits.ApiError(400, f"the files have no column {', '.join(map(repr, bad))}")
    db = sqlite3.connect(core.DB_PATH)
    try:
        db.execute("INSERT INTO import_map(slug, csv_map, updated) VALUES(?,?,?) ON CONFLICT(slug) DO UPDATE SET csv_map=excluded.csv_map, updated=excluded.updated",
                   (slug, None if cm is None else json.dumps(cm), datetime.datetime.now().isoformat(timespec="seconds")))
        db.commit()
    finally: db.close()
    return dict(slug=slug, map=cm)


def csv_columns_raw(slug):
    out = []
    for f in glob.glob(os.path.join(IMPORT_DIR, slug, "*.csv")):
        with open(f, newline="", encoding="utf-8-sig") as fh: out.append(csv.DictReader(fh).fieldnames or [])
    return out


ROUTES = [("GET", r"/api/import/csv-columns", csv_columns), ("POST", r"/api/import/csv-map", set_csv_map)]


def batches(db):
    out = []
    for r in db.execute("SELECT batch, MIN(ts) ts, SUM(op='insert') ins, SUM(op='update') upd FROM edit_log WHERE note='csv import' GROUP BY batch ORDER BY MIN(id) DESC"):
        out.append(dict(batch=r["batch"], ts=r["ts"], inserted=r["ins"], linked=r["upd"],
                        reverted=bool(db.execute("SELECT 1 FROM edit_log WHERE batch=?", ("revert:" + r["batch"],)).fetchone())))
    return out
