"""Account Details dialog backend: read, save (one revertable edit_log batch) and delete (only when empty)."""
import re
import datetime
import core, downloads, edits, scheduled
from edits import ApiError

NOTE = "account details"
TEXT = ("description", "institution", "acct_number", "customer_id", "contact_name", "phone", "url_home", "url_activity", "url_other", "comments")
LIMITS = {"name": 120, "comments": 5000}
FIELDS = ("name", *TEXT, "acct_type", "tax_deferred", "single_fund", "tracking", "credit_limit", "hidden")  # + download settings, see get/save
FAM = dict(core.ACCT_TYPES)


def family(a): return a["register_type"] or a["list_type"]


def label(a):
    fam = family(a)
    return a["acct_type"] if a["acct_type"] and FAM.get(a["acct_type"]) == fam else core.FAMILY_DEFAULT.get(fam)


def mask(s):
    """Quicken shows account numbers masked (XXXXX627): any run of 6+ digits keeps only its last 3."""
    return re.sub(r"\d{6,}", lambda m: "X" * (len(m[0]) - 3) + m[0][-3:], s)


def url(s):
    s = s.strip()
    if not s: return ""
    if not re.match(r"^[a-z][a-z0-9+.-]*:", s, re.I) and "." in s: s = "https://" + s
    if not re.match(r"^https?://[^\s/]+\.[^\s/]+", s, re.I): raise ApiError(400, f"not a web address (http/https only): {s}")
    return s


def usage(db, aid):
    q = lambda sql: db.execute(sql, (aid,)).fetchone()[0]
    u = dict(txns=q("SELECT COUNT(*) FROM transactions WHERE account_id=?"),
             transfers_in=q("SELECT COUNT(*) FROM transactions WHERE transfer_account_id=? AND account_id<>transfer_account_id")
             + q("SELECT COUNT(*) FROM splits WHERE transfer_account_id=?"),
             import_map=q("SELECT COUNT(*) FROM import_map WHERE account_id=?"), reconciles=q("SELECT COUNT(*) FROM reconcile WHERE account_id=?"),
             memorized=q("SELECT COUNT(*) FROM memorized_transactions WHERE transfer_account_id=?"),
             positions=q("SELECT COUNT(*) FROM security_positions WHERE account_id=?"), scheduled=scheduled.account_refs(db, aid))
    why = [f"{n} {w}" for k, w in (("txns", "transactions"), ("transfers_in", "transfers from other accounts"), ("import_map", "CSV import mapping(s)"),
                                   ("reconciles", "reconcile record(s)"), ("memorized", "memorized transaction(s)"), ("positions", "holding row(s)"), ("scheduled", "scheduled transaction(s)")) if (n := u[k])]
    u["can_delete"] = not why
    u["delete_blocked"] = ("Can't delete: this account has " + ", ".join(why) + ". Only an empty account can be deleted.") if why else ""
    return u


def get(db, aid):
    a = db.execute("SELECT * FROM accounts WHERE account_id=?", (aid,)).fetchone()
    if not a: raise ApiError(404, "no such account")
    fam = family(a); d = {k: a[k] for k in FIELDS}
    d.update(account_id=aid, hidden=bool(a["hidden"]), acct_type=label(a), family=fam, inv=fam == "Invst",
             type_locked=fam not in core.FAMILY_DEFAULT, guessed=(a["prefilled"] or "").split(",") if a["prefilled"] else [],
             types=[dict(label=l, family=f) for l, f in core.ACCT_TYPES], usage=usage(db, aid))
    if d["inv"] and not d["tracking"]: d["tracking"] = "complete"
    d["download"] = downloads.info(db, a)
    return d


def _b(v, k):
    if v is None or v == "": return None
    if isinstance(v, bool): return int(v)
    raise ApiError(400, f"{k} must be yes, no or blank")


def save(aid, body):
    b = edits.Batch(None, NOTE)
    try:
        a = b.row("accounts", aid)
        if not a: raise ApiError(404, "no such account")
        fam = family(a); ch = {}
        def put(k, v):
            if a[k] != v: ch[k] = v
        if "name" in body:
            n = str(body["name"]).strip()
            if not n: raise ApiError(400, "Account name can't be blank")
            if len(n) > LIMITS["name"]: raise ApiError(400, "Account name is too long")
            if b.db.execute("SELECT 1 FROM accounts WHERE name=? COLLATE NOCASE AND account_id<>?", (n, aid)).fetchone():
                raise ApiError(409, f"There is already an account named '{n}'")
            put("name", n)
        for k in TEXT:
            if k in body:
                v = str(body[k] if body[k] is not None else "").strip()
                if len(v) > LIMITS.get(k, 300): raise ApiError(400, f"{k} is too long")
                v = mask(v) if k == "acct_number" else url(v) if k.startswith("url_") else v
                put(k, v or None)
        if "credit_limit" in body:
            v = body["credit_limit"]
            try: v = None if v in (None, "") else round(float(str(v).replace(",", "")), 2)
            except ValueError: raise ApiError(400, "Credit limit must be a number")
            if v is not None and v < 0: raise ApiError(400, "Credit limit can't be negative")
            put("credit_limit", v)
        if "hidden" in body:
            if not isinstance(body["hidden"], bool): raise ApiError(400, "hidden must be true or false")
            put("hidden", int(body["hidden"]))
        if "dl_enabled" in body:
            if not isinstance(body["dl_enabled"], bool): raise ApiError(400, "dl_enabled must be true or false")
            put("dl_enabled", int(body["dl_enabled"]))
            if body["dl_enabled"]: downloads.ensure_slug(b.db, aid, a["name"])
        if "dl_schedule" in body:
            if body["dl_schedule"] not in downloads.SCHEDULES: raise ApiError(400, "schedule must be 'monthly' or 'on demand'")
            put("dl_schedule", body["dl_schedule"])
        retype = None
        if "acct_type" in body and body["acct_type"] != label(dict(a)):
            lab = body["acct_type"]
            if lab not in FAM: raise ApiError(400, f"unknown account type '{lab}'")
            if fam not in core.FAMILY_DEFAULT: raise ApiError(400, "This account's type can't be changed here")
            if FAM[lab] != fam:
                n = b.db.execute("SELECT COUNT(*) FROM transactions WHERE account_id=?", (aid,)).fetchone()[0]
                if (FAM[lab] == "Invst" or fam == "Invst") and n:
                    raise ApiError(409, f"Can't change between an investment and a non-investment account type: this account has {n} transactions, "
                                        "which are kept differently (cash + shares vs plain amounts).")
                retype = FAM[lab]; ch["register_type"] = retype; ch["list_type"] = retype
            put("acct_type", lab)
        inv = (retype or fam) == "Invst"
        for k in ("tax_deferred", "single_fund"):
            if k in body and inv: put(k, _b(body[k], k))
        if "tracking" in body and inv:
            if body["tracking"] not in ("simple", "complete"): raise ApiError(400, "tracking must be simple or complete")
            put("tracking", body["tracking"])
        if not ch: b.rollback(); return dict(changed=[], batch=None)
        if set(ch) - {"dl_enabled", "dl_schedule"}: ch["prefilled"] = None  # saving real details means you have looked at the guesses; a download toggle does not
        b.update("accounts", aid, ch)
        if retype:  # keep each row's register_type equal to its account's; amounts untouched
            for (t,) in b.db.execute("SELECT txn_id FROM transactions WHERE account_id=? AND COALESCE(register_type,'')<>?", (aid, retype)).fetchall():
                b.update("transactions", t, dict(register_type=retype))
        out = dict(changed=[k for k in ch if k != "prefilled"], batch=b.name); b.commit(); return out
    except Exception:
        b.rollback(); raise


def delete(aid):
    b = edits.Batch(None, NOTE)
    try:
        a = b.row("accounts", aid)
        if not a: raise ApiError(404, "no such account")
        u = usage(b.db, aid)
        if not u["can_delete"]: raise ApiError(409, u["delete_blocked"])
        b.delete("accounts", aid); out = dict(deleted=a["name"], batch=b.name); b.commit(); return out
    except Exception:
        b.rollback(); raise


def create(m, body):
    """New account. type = a label from ACCT_TYPES. opening_balance (optional, dated opening_date, default today) is an 'Opening Balance' transaction
    (an XIn cash row for investment accounts). For credit cards and liabilities it is the amount owed (positive = you owe it)."""
    n = str(body.get("name") or "").strip(); lab = body.get("type")
    if not n: raise ApiError(400, "Account name can't be blank")
    if len(n) > LIMITS["name"]: raise ApiError(400, "Account name is too long")
    if lab not in FAM: raise ApiError(400, f"unknown account type '{lab}'")
    fam = FAM[lab]
    try: ob = None if body.get("opening_balance") in (None, "") else round(float(str(body["opening_balance"]).replace(",", "")), 2)
    except ValueError: raise ApiError(400, "Opening balance must be a number")
    date = str(body.get("opening_date") or datetime.date.today())
    try: datetime.date.fromisoformat(date)
    except ValueError: raise ApiError(400, "Opening date must be YYYY-MM-DD")
    if fam == "Invst" and ob is not None and ob < 0: raise ApiError(400, "An investment account's opening cash can't be negative")
    b = edits.Batch(None, "new account")
    try:
        if b.db.execute("SELECT 1 FROM accounts WHERE name=? COLLATE NOCASE", (n,)).fetchone(): raise ApiError(409, f"There is already an account named '{n}'")
        row = dict(name=n, list_type=fam, register_type=fam, acct_type=lab, source="ledger", hidden=0)
        for k in ("description", "institution", "customer_id", "comments"):
            if str(body.get(k) or "").strip(): row[k] = str(body[k]).strip()[:LIMITS.get(k, 300)]
        if str(body.get("acct_number") or "").strip(): row["acct_number"] = mask(str(body["acct_number"]).strip())
        if fam == "CCard" and body.get("credit_limit") not in (None, ""):
            try: row["credit_limit"] = round(float(str(body["credit_limit"]).replace(",", "")), 2)
            except ValueError: raise ApiError(400, "Credit limit must be a number")
        if fam == "Invst":
            row.update(tax_deferred=int(lab in ("Traditional IRA", "Roth IRA", "401(k)/403(b)")), single_fund=0, tracking="complete")
        aid = b.insert("accounts", row)
        if ob:
            if fam == "Invst":
                t, _ = edits.inv_t(b, aid, dict(date=date, action="XIn", amount=ob, payee="Opening Balance"))
                b.insert("transactions", dict(t, account_id=aid, register_type="Invst", source="ledger"))
            else:
                edits.create(aid, dict(date=date, amount=-ob if fam in ("CCard", "Oth L") else ob, payee="Opening Balance"), b)
        out = dict(account_id=aid, name=n, batch=b.name); b.commit(); return out
    except Exception:
        b.rollback(); raise


ROUTES = [("POST", r"/api/accounts", create)]
