"""Scheduled transactions (bills & deposits). A schedule makes a normal transaction (edit-logged, undoable) when it is entered; no background process:
auto-enter schedules are entered when the app starts and when the pages load (POST /api/scheduled/run); 'remind' schedules wait for Enter / Skip."""
import calendar, datetime, json
import core, edits
from edits import ApiError, Batch, reading

NOTE = "scheduled setup"
FREQ = {"once": "Once", "weekly": "Weekly", "biweekly": "Every 2 weeks", "monthly": "Monthly", "quarterly": "Quarterly", "yearly": "Yearly"}
D = datetime.date


def add_months(d, n, anchor):
    """d + n months, on the anchor day of the month, or the month's last day when it is shorter (Jan 31 + 1 month = Feb 28/29)."""
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12); m += 1
    return D(y, m, min(anchor, calendar.monthrange(y, m)[1]))


def advance(freq, d, anchor=None):
    """The occurrence after d. anchor = the day of month the schedule is meant for (so Jan 31 -> Feb 28 -> Mar 31, Feb 29 -> Feb 28 -> Feb 29)."""
    a = anchor or d.day
    return {"weekly": lambda: d + datetime.timedelta(7), "biweekly": lambda: d + datetime.timedelta(14), "monthly": lambda: add_months(d, 1, a),
            "quarterly": lambda: add_months(d, 3, a), "yearly": lambda: add_months(d, 12, a)}[freq]()


def _date(v, what):
    try: return D.fromisoformat(str(v)).isoformat()
    except ValueError: raise ApiError(400, f"{what} must be YYYY-MM-DD")


def _names(db):
    return ({r[0]: r[1] for r in db.execute("SELECT category_id, name FROM categories")}, {r[0]: r[1] for r in db.execute("SELECT account_id, name FROM accounts")})


def _view(row, cats, accts):
    """A scheduled row as the API shows it (names instead of ids)."""
    sp = json.loads(row["splits_json"] or "[]")
    return dict(id=row["sched_id"], account_id=row["account_id"], account=accts.get(row["account_id"]), payee=row["payee"] or "", memo=row["memo"] or "", amount=row["amount"],
                category=cats.get(row["category_id"], "") if row["category_id"] else "", transfer=accts.get(row["transfer_account_id"], "") if row["transfer_account_id"] else "",
                splits=[dict(category=cats.get(s["category_id"], "") if s["category_id"] else "", transfer=accts.get(s["transfer_account_id"], "") if s["transfer_account_id"] else "",
                             memo=s["memo"] or "", amount=s["amount"]) for s in sp],
                freq=row["freq"], next_date=row["next_date"], end_date=row["end_date"], count=row["remaining"], auto=bool(row["auto"]), active=bool(row["active"]),
                last_entered=row["last_entered"], inv=json.loads(row["inv_json"]) if row["inv_json"] else None)


def category_refs(db):
    """category_id -> number of schedules using it (also in splits)."""
    out = {}
    for r in db.execute("SELECT category_id, splits_json FROM scheduled"):
        ids = {r[0]} | {s["category_id"] for s in json.loads(r[1] or "[]")}
        for i in ids - {None}: out[i] = out.get(i, 0) + 1
    return out


def account_refs(db, aid):
    n = 0
    for r in db.execute("SELECT account_id, transfer_account_id, splits_json FROM scheduled"):
        n += aid in ({r[0], r[1]} | {s["transfer_account_id"] for s in json.loads(r[2] or "[]")})
    return n


def _clean(b, d):
    """Validate a full schedule body -> scheduled column dict. The transaction part goes through the same checks as a normal transaction."""
    try: aid = int(d.get("account_id"))
    except (TypeError, ValueError): raise ApiError(400, "account is required")
    nxt = _date(d.get("next_date"), "next date")
    ar = b.db.execute("SELECT register_type FROM accounts WHERE account_id=?", (aid,)).fetchone(); inv = bool(ar) and ar[0] == "Invst"
    if not inv: edits._acct(b, aid)
    if inv:  # an investment schedule: the body (action, security, qty, price, amount, transfer, ratio...) is checked like a new investment transaction and entered through inv_create
        if not isinstance(d.get("inv"), dict): raise ApiError(400, "an investment schedule needs an inv object: {action, security, qty, price, amount, ...}")
        body = {k: v for k, v in d["inv"].items() if k != "date"}; t, _ = edits.inv_t(b, aid, dict(body, date=nxt)); splits = []
        body.pop("lot", None)  # a lot choice (a transaction id) wouldn't mean anything for later occurrences
    else: t, splits, _ = edits._lookup(b, dict(d, date=nxt), aid)
    if d.get("freq") not in FREQ: raise ApiError(400, f"frequency must be one of {', '.join(FREQ)}")
    end = _date(d["end_date"], "end date") if d.get("end_date") else None
    if end and end < nxt: raise ApiError(400, "end date is before the next date")
    cnt = d.get("count")
    try: cnt = None if cnt in (None, "") else int(cnt)
    except (TypeError, ValueError): raise ApiError(400, "count must be a whole number")
    if cnt is not None and cnt < 1: raise ApiError(400, "count must be 1 or more")
    once = d["freq"] == "once"
    return dict(inv_json=json.dumps(body) if inv else None, account_id=aid, payee=t["payee_text"], memo=t["memo"], amount=t["amount"], category_id=t["category_id"], transfer_account_id=t["transfer_account_id"],
                splits_json=json.dumps([dict(category_id=s["category_id"], transfer_account_id=s["transfer_account_id"], memo=s["memo"], amount=s["amount"]) for s in splits]) if splits else None,
                freq=d["freq"], next_date=nxt, anchor_day=D.fromisoformat(nxt).day, end_date=None if once else end, remaining=None if once else cnt,
                auto=int(bool(d.get("auto"))), active=int(d.get("active", True) is not False))


@reading
def sched_list(db, m, qs):
    today = D.today(); days = int(qs.get("days", 30)); cats, accts = _names(db); lim = (today + datetime.timedelta(days)).isoformat(); t = today.isoformat(); items = []
    for r in db.execute("SELECT * FROM scheduled ORDER BY active DESC, next_date, sched_id"):
        v = _view(r, cats, accts)
        v["status"] = "ended" if not v["active"] else "overdue" if v["next_date"] < t else "due" if v["next_date"] == t else "soon" if v["next_date"] <= lim else "later"
        v["days"] = (D.fromisoformat(v["next_date"]) - today).days
        if qs.get("all") == "1" or v["status"] in ("overdue", "due", "soon"): items.append(v)
    recent = []
    for r in db.execute("SELECT batch, ts, note, after_json FROM edit_log WHERE id IN (SELECT MIN(id) FROM edit_log WHERE note IN ('scheduled enter','scheduled auto-enter') "
                        "AND tbl='transactions' AND op='insert' GROUP BY batch) ORDER BY id DESC LIMIT 15"):
        if edits.is_reverted(db, r["batch"]): continue
        a = json.loads(r["after_json"]); recent.append(dict(batch=r["batch"], ts=r["ts"], auto=r["note"] == "scheduled auto-enter", date=a["date"], payee=a["payee_text"], amount=a["amount"], account=accts.get(a["account_id"])))
    due, overdue = counts(db, t)
    return dict(items=items, due=due, overdue=overdue, today=t, days=days, recent=recent)


def counts(db, today):
    return db.execute("SELECT COUNT(*), COALESCE(SUM(next_date<?),0) FROM scheduled WHERE active=1 AND next_date<=?", (today, today)).fetchone()


def create(m, d):
    b = Batch(None, NOTE)
    try:
        sid = b.insert("scheduled", _clean(b, d)); out = dict(id=sid, batch=b.name); b.commit(); return out
    except Exception: b.rollback(); raise


def update(m, d):
    sid = int(m[1]); b = Batch(None, NOTE)
    try:
        s = b.row("scheduled", sid)
        if not s: raise ApiError(404, "no such schedule")
        cats, accts = _names(b.db); v = _view(s, cats, accts)
        new = _clean(b, dict(v, **{k: d[k] for k in d if k in v and k not in ("id",)}))
        if new["next_date"] == s["next_date"]: new["anchor_day"] = s["anchor_day"]  # an unchanged date keeps its month-end intent
        if new["active"] and new["end_date"] and new["next_date"] > new["end_date"]: raise ApiError(400, "the next date is after the end date")
        ch = {k: x for k, x in new.items() if s[k] != x}
        if ch: b.update("scheduled", sid, ch)
        out = dict(id=sid, batch=b.name if ch else None); b.commit(); return out
    except Exception: b.rollback(); raise


def delete(m, d):
    sid = int(m[1]); b = Batch(None, NOTE)
    try:
        if not b.row("scheduled", sid): raise ApiError(404, "no such schedule")
        b.delete("scheduled", sid); out = dict(deleted=sid, batch=b.name); b.commit(); return out
    except Exception: b.rollback(); raise


def _after(s, entered=None):
    """Column changes that move a schedule past its current occurrence."""
    once = s["freq"] == "once"; nxt = s["next_date"] if once else advance(s["freq"], D.fromisoformat(s["next_date"]), s["anchor_day"]).isoformat()
    rem = None if s["remaining"] is None else s["remaining"] - 1
    act = not once and (rem is None or rem > 0) and (not s["end_date"] or nxt <= s["end_date"])
    ch = dict(next_date=nxt, remaining=rem, active=int(act))
    if entered: ch["last_entered"] = entered
    return ch


def _enter(sid, d, auto=False):
    b = Batch(None, "scheduled auto-enter" if auto else "scheduled enter")
    try:
        s = b.row("scheduled", sid)
        if not s: raise ApiError(404, "no such schedule")
        if not s["active"]: raise ApiError(409, "this schedule has ended")
        cats, accts = _names(b.db); v = _view(s, cats, accts); date = _date(d.get("date") or s["next_date"], "date")
        amt = v["amount"]
        if v["inv"] and d.get("amount") not in (None, ""): raise ApiError(400, "an investment schedule can't be entered with a different amount; edit the schedule")
        if d.get("amount") not in (None, ""):
            if v["splits"]: raise ApiError(400, "a split schedule can't be entered with a different amount; edit the schedule")
            amt = d["amount"]
        res = edits.inv_create(s["account_id"], dict(v["inv"], date=date), b) if v["inv"] else edits.create(s["account_id"], dict(date=date, amount=amt, payee=v["payee"], memo=v["memo"], category=v["category"], transfer=v["transfer"], splits=v["splits"]), b)
        ch = _after(s, date); b.update("scheduled", sid, ch)
        out = dict(res, id=sid, date=date, next_date=ch["next_date"], active=bool(ch["active"]), payee=v["payee"], account=v["account"], amount=amt); b.commit(); return out
    except Exception: b.rollback(); raise


def enter(m, d): return _enter(int(m[1]), d)


def skip(m, d):
    sid = int(m[1]); b = Batch(None, "scheduled skip")
    try:
        s = b.row("scheduled", sid)
        if not s: raise ApiError(404, "no such schedule")
        if not s["active"]: raise ApiError(409, "this schedule has ended")
        ch = _after(s); b.update("scheduled", sid, ch); out = dict(id=sid, next_date=ch["next_date"], active=bool(ch["active"]), batch=b.name); b.commit(); return out
    except Exception: b.rollback(); raise


def run_auto(m=None, d=None, today=None):
    """Enter every due occurrence of the auto-enter schedules (oldest first; one undoable batch each). -> dict(entered, errors, due)."""
    today = today or D.today().isoformat(); entered, errors = [], []
    db = core.connect()
    try: ids = [r[0] for r in db.execute("SELECT sched_id FROM scheduled WHERE active=1 AND auto=1 AND next_date<=? ORDER BY next_date, sched_id", (today,))]
    finally: db.close()
    for sid in ids:
        for _ in range(400):  # a schedule left for years still stops
            db = core.connect()
            try: s = db.execute("SELECT active, next_date FROM scheduled WHERE sched_id=?", (sid,)).fetchone()
            finally: db.close()
            if not s["active"] or s["next_date"] > today: break
            try: entered.append(_enter(sid, {}, auto=True))
            except Exception as e: errors.append(dict(id=sid, error=str(e))); break
    db = core.connect()
    try: due = counts(db, today)
    finally: db.close()
    return dict(entered=entered, errors=errors, due=due[0], overdue=due[1])


ROUTES = [("GET", r"/api/scheduled", sched_list), ("POST", r"/api/scheduled", create), ("POST", r"/api/scheduled/run", run_auto),
          ("PUT", r"/api/scheduled/(\d+)", update), ("DELETE", r"/api/scheduled/(\d+)", delete),
          ("POST", r"/api/scheduled/(\d+)/enter", enter), ("POST", r"/api/scheduled/(\d+)/skip", skip)]
