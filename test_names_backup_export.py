#!/usr/bin/env python3
"""Payees (list/rename/merge/delete unused), tags (create/rename/delete + tag on forms), backup/restore, CSV export. Fresh empty ledger."""
import csv, io, os, sqlite3, subprocess, sys, urllib.request
from testempty import Empty, Checker, HERE
check = Checker(); E = Empty(8790); api = E.api; E.start()
try:
    chk = api("POST", "/api/accounts", dict(name="Checking", type="Checking", opening_balance=1000, opening_date="2026-01-01"))[1]["account_id"]
    sav = api("POST", "/api/accounts", dict(name="Savings", type="Savings"))[1]["account_id"]
    api("POST", "/api/categories", dict(name="Food")); api("POST", "/api/categories", dict(name="Fun"))
    T = lambda **k: api("POST", f"/api/accounts/{chk}/transactions", dict(dict(date="2026-02-01", amount=-10, category="Food"), **k))
    for p in ("Safeway", "SAFEWAY #12", "Safeway"): T(payee=p)
    db = sqlite3.connect(E.db); db.execute("INSERT INTO memorized_transactions(payee_text, amount, kind) VALUES ('SAFEWAY #12', -5, 'x')"); db.commit()
    api("POST", "/api/scheduled", dict(account_id=chk, payee="SAFEWAY #12", amount=-20, category="Food", freq="monthly", next_date="2027-01-01"))
    api("POST", "/api/payees/merge", dict(names=["unused"], into="Orphan"))
    P = lambda: {p["name"]: p for p in api("GET", "/api/payees")[1]["payees"]}
    check("payee list has usage counts", P()["Safeway"]["txns"] == 2 and P()["SAFEWAY #12"]["txns"] == 1 and P()["SAFEWAY #12"]["memorized"] == 1 and P()["SAFEWAY #12"]["scheduled"] == 1, P())
    s, r = api("POST", "/api/payees/merge", dict(names=["SAFEWAY #12"], into="Safeway"))
    check("merge cascades to transactions, memorized and scheduled", s == 200 and "SAFEWAY #12" not in P() and P()["Safeway"]["txns"] == 3 and P()["Safeway"]["memorized"] == 1 and P()["Safeway"]["scheduled"] == 1, (r, P()))
    ids = {x[0] for x in db.execute("SELECT payee_id FROM transactions WHERE payee_text='Safeway'")}
    check("all merged rows point at one payee row", len(ids) == 1 and None not in ids and db.execute("SELECT COUNT(*) FROM payees WHERE name LIKE 'safeway%'").fetchone()[0] == 1, ids)
    s, r = api("PUT", "/api/payees", dict(name="Safeway", new_name="Safeway Inc")); check("rename", s == 200 and "Safeway" not in P() and P()["Safeway Inc"]["txns"] == 3, (r, P()))
    check("delete refused when used (409), allowed when unused", api("DELETE", "/api/payees", dict(name="Safeway Inc"))[0] == 409 and api("DELETE", "/api/payees", dict(name="Orphan"))[0] == 200 and "Orphan" not in P())
    check("blank / empty merges refused", api("POST", "/api/payees/merge", dict(names=["Safeway Inc"], into=" "))[0] == 400 and api("POST", "/api/payees/merge", dict(names=[], into="x"))[0] == 400)
    rv = subprocess.run([sys.executable, "app.py", "--revert", r["batch"]], env=E.env, cwd=HERE, capture_output=True, text=True)
    check("rename is revertable", rv.returncode == 0 and "Safeway" in P() and "Safeway Inc" not in P(), rv.stdout + rv.stderr)
    # tags
    s, r = api("POST", "/api/tags", dict(name="Vacation")); check("create tag", s == 200, r); tid = r["tag_id"]
    check("duplicate tag (case-insensitive) refused", api("POST", "/api/tags", dict(name="vacation"))[0] == 409)
    check("unknown tag on a transaction refused", T(payee="x", tag="Nope")[0] == 400)
    s, r = T(payee="Hotel", tag="vacation", amount=-50); check("tag on the bank form", s == 200 and db.execute("SELECT tag FROM transactions WHERE txn_id=?", (r["txn_id"],)).fetchone()[0] == "Vacation", r)
    tx = r["txn_id"]
    s, r = api("POST", f"/api/accounts/{chk}/transactions", dict(date="2026-02-02", amount=-30, payee="Split", splits=[dict(amount=-20, category="Food"), dict(amount=-10, category="Fun")]))
    check("tag listed with usage; delete refused while used", {t["name"]: t for t in api("GET", "/api/tags")[1]["tags"]}["Vacation"]["used"] == 1 and api("DELETE", f"/api/tags/{tid}")[0] == 409)
    s, r = api("PUT", f"/api/tags/{tid}", dict(name="Trips")); check("rename tag cascades", s == 200 and db.execute("SELECT tag FROM transactions WHERE txn_id=?", (tx,)).fetchone()[0] == "Trips", r)
    s, r = api("PUT", f"/api/transactions/{tx}", dict(date="2026-02-01", amount=-50, payee="Hotel", category="Food", tag="")); check("clear tag from the form", s == 200 and db.execute("SELECT tag FROM transactions WHERE txn_id=?", (tx,)).fetchone()[0] is None, r)
    check("delete unused tag", api("DELETE", f"/api/tags/{tid}")[0] == 200)
    # backups
    check("no backups listed yet except none required", api("GET", "/api/backups")[0] == 200)
    s, r = api("POST", "/api/backups", dict(label="before trip")); check("back up now", s == 200 and r["name"].endswith("-manual-before-trip.sqlite") and r["size"] > 0, r); bk = r["name"]
    check("listed newest first", api("GET", "/api/backups")[1]["backups"][0]["name"] == bk)
    bal = lambda: {a["name"]: a["balance"] for a in api("GET", "/api/accounts")[1]}["Checking"]
    b0 = bal(); T(payee="After backup", amount=-100); check("balance changed after backup", bal() == b0 - 10000)
    check("restore needs confirm; unknown / traversal names refused", [api("POST", "/api/backups/restore", dict(name=bk))[0], api("POST", "/api/backups/restore", dict(name="x", confirm=True))[0],
          api("POST", "/api/backups/restore", dict(name="../ledger.sqlite", confirm=True))[0]] == [400, 404, 404])
    s, r = api("POST", "/api/backups/restore", dict(name=bk, confirm=True))
    check("restore brings the old data back", s == 200 and bal() == b0, r)
    check("restore first backed up the current database", any(x["kind"] == "pre-restore" for x in api("GET", "/api/backups")[1]["backups"]) and r["previous_saved_as"].endswith("pre-restore.sqlite"))
    pre = r["previous_saved_as"]; s, r = api("POST", "/api/backups/restore", dict(name=pre, confirm=True)); check("restoring the pre-restore backup undoes the restore", s == 200 and bal() == b0 - 10000, r)
    # restart-safe: a stale temp file from a crashed restore doesn't matter; server restarts onto the restored data
    open(E.db + ".restore-tmp", "wb").write(b"junk"); E.stop(); E.start()
    check("server restarts cleanly on the restored database", bal() == b0 - 10000)
    api("POST", "/api/backups/restore", dict(name=bk, confirm=True)); check("a restore with a leftover temp file works", bal() == b0 and not os.path.exists(E.db + ".restore-tmp"))
    # export
    def get(path):
        with urllib.request.urlopen(f"http://127.0.0.1:{E.port}{path}") as r: return r.status, r.headers, r.read().decode()
    s, h, body = get("/api/export/transactions.csv"); rows = list(csv.DictReader(io.StringIO(body)))
    check("export all: csv with download header", s == 200 and "text/csv" in h["Content-Type"] and "attachment" in h["Content-Disposition"] and len(rows) >= 5, h)
    sp = [r for r in rows if r["payee"] == "Split"]
    check("split transaction expands to one row per split, summing to the total", len(sp) == 2 and sum(float(r["amount"]) for r in sp) == -30 and sp[0]["txn_amount"] == "-30.0" and {r["category"] for r in sp} == {"Food", "Fun"}, sp)
    s, h, body = get(f"/api/export/transactions.csv?account={chk}&from=2026-02-02&to=2026-02-02"); rows = list(csv.DictReader(io.StringIO(body)))
    check("one account + date range", {r["date"] for r in rows} == {"2026-02-02"} and {r["account"] for r in rows} == {"Checking"} and len(rows) == 2, rows)
    for u in ("/api/export/transactions.csv?from=bad", "/api/export/transactions.csv?account=999"):
        try: get(u); check("refused " + u, False)
        except Exception as e: check("refused " + u, getattr(e, "code", 0) in (400, 404))
    s, h, body = get("/api/export/accounts.csv"); rows = {r["account"]: r for r in csv.DictReader(io.StringIO(body))}
    check("accounts export: both accounts, balance", set(rows) == {"Checking", "Savings"} and float(rows["Checking"]["balance_today"]) == (b0 / 100), rows)
    # investment rows
    br = api("POST", "/api/accounts", dict(name="Broker", type="Brokerage"))[1]["account_id"]; api("POST", "/api/securities", dict(name="Acme", symbol="ACME"))
    api("POST", f"/api/accounts/{br}/transactions", dict(date="2026-03-01", action="Buy", security="Acme", qty=2, price=5))
    rows = [r for r in csv.DictReader(io.StringIO(get(f"/api/export/transactions.csv?account={br}")[2]))]
    check("investment rows export action, security, qty, price", rows[0]["num_or_action"] == "Buy" and rows[0]["security"] == "Acme" and rows[0]["qty"] == "2.0" and rows[0]["price"] == "5.0" and rows[0]["amount"] == "10.0", rows)
finally: E.stop()
sys.exit(0 if check.ok else 1)
