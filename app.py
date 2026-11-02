#!/usr/bin/env python3
"""Ledger: local-only Quicken-style viewer. Usage: python3 app.py [--port 8765]. Binds 127.0.0.1 only."""
import argparse, datetime, json, os, re, sqlite3, sys, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import account_details, backups, catalog, core, downloads, edits, exports, importer, inventory, invest, names, plaid, portfolio, reconcile, reports, scheduled

STATIC = os.path.join(core.HERE, "static")


def mapping(db):
    return [dict(slug=r["slug"], account_id=r["account_id"], account=r["name"], confirmed=bool(r["confirmed"]), csv=bool(r["csv_map"]))
            for r in db.execute("SELECT m.*, a.name FROM import_map m LEFT JOIN accounts a USING(account_id) ORDER BY slug")]


class H(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="application/json", headers=None):
        b = body if isinstance(body, bytes) else body.encode()
        self.send_response(code); self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b))); self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items(): self.send_header(k, v)
        self.end_headers(); self.wfile.write(b)

    def body(self):
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            raise edits.ApiError(415, "Content-Type must be application/json")
        if (self.headers.get("Host") or "").split(":")[0] not in ("127.0.0.1", "localhost"):
            raise edits.ApiError(403, "bad Host")
        n = int(self.headers.get("Content-Length") or 0)
        try: return json.loads(self.rfile.read(n) or b"{}")
        except ValueError: raise edits.ApiError(400, "invalid JSON")

    def write(self, fn):
        try:
            r = fn()
            if isinstance(r, edits.Raw): self.send(200, r.body, r.ctype, {"Content-Disposition": f'{"inline" if r.inline else "attachment"}; filename="{r.filename}"', "X-Content-Type-Options": "nosniff"})
            else: self.send(200, json.dumps(r))
        except edits.ApiError as e:
            self.send(e.code, json.dumps({"error": str(e)}))
        except Exception as e:
            traceback.print_exc(); self.send(500, json.dumps({"error": str(e)}))

    def extra(self, method):
        """The newer endpoints (accounts, categories, securities, prices, scheduled) live in module ROUTES tables."""
        p = urlparse(self.path).path
        for mod in (account_details, catalog, scheduled, invest, inventory, names, backups, exports, importer):
            fn = edits.route(mod.ROUTES, method, p)
            if fn:
                arg = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()} if method == "GET" else None
                return self.write(lambda: fn(arg if method == "GET" else self.body())) or True

    def do_POST(self):
        if self.extra("POST"): return
        p = urlparse(self.path).path
        if p == "/api/import/mapping":
            def f():
                importer.set_mapping(self.body().get("mappings") or []); db = core.connect()
                try: return mapping(db)
                finally: db.close()
            return self.write(f)
        if p == "/api/import/run":
            return self.write(lambda: importer.run_import((self.body().get("slugs") or None)))
        if p == "/api/import/revert":
            def f():
                n = edits.revert_batch(str(self.body().get("batch", ""))); return {"reverted_edits": n}
            return self.write(f)
        if p.startswith("/api/plaid/"):
            def f():
                b = self.body(); db = sqlite3.connect(core.DB_PATH); db.row_factory = sqlite3.Row  # writable only for the import slug a download may create
                try:
                    if p == "/api/plaid/link_token": return plaid.link_token(b.get("kind", "bank"), b.get("item_id"))
                    if p == "/api/plaid/exchange": return plaid.exchange(b.get("public_token"), b.get("institution"))
                    if p == "/api/plaid/map": return plaid.set_map(db, b.get("item_id"), b.get("plaid_account_id"), b.get("account_id"))
                    if p == "/api/plaid/create_account": return plaid.create_account(db, b.get("item_id"), b.get("plaid_account_id"), b.get("name"))
                    if p == "/api/plaid/remove": return plaid.remove(b.get("item_id"))
                    if p == "/api/plaid/sync": return plaid.sync(db, b.get("account_id"), b.get("all") is True)
                    raise edits.ApiError(404, "not found")
                finally: db.close()
            return self.write(f)
        m = re.fullmatch(r"/api/accounts/(\d+)/reconcile(/undo)?", p)
        if m:
            return self.write((lambda: (self.body(), reconcile.undo(int(m[1])))[1]) if m[2] else (lambda: reconcile.finish(int(m[1]), self.body())))
        m = re.fullmatch(r"/api/transactions/(\d+)/cleared", p)
        if m: return self.write(lambda: edits.set_cleared(int(m[1]), self.body().get("cleared", "")))
        m = re.fullmatch(r"/api/accounts/(\d+)/hidden", urlparse(self.path).path)
        if m:
            def f():
                h = self.body().get("hidden")
                if not isinstance(h, bool): raise edits.ApiError(400, "hidden must be true or false")
                if not core.set_hidden(int(m[1]), h): raise edits.ApiError(404, "no such account")
                return {"account_id": int(m[1]), "hidden": h}
            return self.write(f)
        m = re.fullmatch(r"/api/accounts/(\d+)/transactions", urlparse(self.path).path)
        if not m: return self.send(404, json.dumps({"error": "not found"}))
        self.write(lambda: edits.create(int(m[1]), self.body()))

    def do_PUT(self):
        if self.extra("PUT"): return
        m = re.fullmatch(r"/api/accounts/(\d+)/details", urlparse(self.path).path)
        if m: return self.write(lambda: account_details.save(int(m[1]), self.body()))
        m = re.fullmatch(r"/api/transactions/(\d+)", urlparse(self.path).path)
        if not m: return self.send(404, json.dumps({"error": "not found"}))
        self.write(lambda: edits.update(int(m[1]), self.body()))

    def do_DELETE(self):
        if self.extra("DELETE"): return
        m = re.fullmatch(r"/api/accounts/(\d+)", urlparse(self.path).path)
        if m: return self.write(lambda: (self.body(), account_details.delete(int(m[1])))[1])
        m = re.fullmatch(r"/api/transactions/(\d+)", urlparse(self.path).path)
        if not m: return self.send(404, json.dumps({"error": "not found"}))
        self.write(lambda: (self.body(), edits.delete(int(m[1])))[1])

    def do_GET(self):
        if self.extra("GET"): return
        u = urlparse(self.path); p = u.path; qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if p in ("/", "/index.html"):
                return self.send(200, open(os.path.join(STATIC, "index.html"), "rb").read(), "text/html")
            db = core.connect()
            try:
                if p.startswith("/api/reports/"):
                    h, st = qs.get("hidden") == "1", qs.get("exclude_stale") == "1"; today = datetime.date.today().isoformat()
                    try:
                        if p == "/api/reports/spending": r = reports.spending(db, qs.get("from", today[:4] + "-01-01"), qs.get("to", today), h, st)
                        elif p == "/api/reports/spending/transactions": r = reports.category_transactions(db, qs.get("category", ""), qs.get("section", "expense"), qs.get("from", today[:4] + "-01-01"), qs.get("to", today), h, st)
                        elif p == "/api/reports/networth": r = reports.networth(db, qs.get("from", str(int(today[:4]) - 10) + today[4:]), qs.get("to", today), qs.get("gran", "auto"), h, household=(qs["household"] == "1") if "household" in qs else None)
                        else: return self.send(404, json.dumps({"error": "not found"}))
                        return self.send(200, json.dumps(r))
                    except reports.ReportError as e: return self.send(400, json.dumps({"error": str(e)}))
                if p == "/api/import/preview": return self.send(200, json.dumps(importer.preview(db)))
                if p == "/api/import/mapping": return self.send(200, json.dumps(mapping(db)))
                if p == "/api/import/batches": return self.send(200, json.dumps(importer.batches(db)))
                m = re.fullmatch(r"/api/accounts/(\d+)/reconcile", p)
                if m:
                    try: return self.send(200, json.dumps(reconcile.data(db, int(m[1]), qs.get("date") or datetime.date.today().isoformat())))
                    except edits.ApiError as e: return self.send(e.code, json.dumps({"error": str(e)}))
                if p == "/api/meta":
                    return self.send(200, json.dumps(dict(
                        accounts=[dict(id=r["account_id"], name=r["name"], editable=r["register_type"] in edits.OK_TYPES, inv=r["register_type"] == "Invst")
                                  for r in db.execute("SELECT * FROM accounts ORDER BY name COLLATE NOCASE")],
                        categories=[r[0] for r in db.execute("SELECT name FROM categories ORDER BY name")],
                        tags=[r[0] for r in db.execute("SELECT name FROM tags ORDER BY name COLLATE NOCASE")],
                        securities=[dict(name=r["name"], symbol=r["symbol"]) for r in db.execute("SELECT name, symbol FROM securities ORDER BY name COLLATE NOCASE")],
                        inv_actions=sorted(edits.INV_ACTIONS), types=[dict(label=l, family=f) for l, f in core.ACCT_TYPES], freqs=scheduled.FREQ, bank_accounts=[r["name"] for r in db.execute("SELECT name FROM accounts WHERE register_type IN ('Bank','CCard','Cash','Oth A','Oth L','Invst') ORDER BY name COLLATE NOCASE")])))
                m = re.fullmatch(r"/api/accounts/(\d+)/details", p)
                if m:
                    try: return self.send(200, json.dumps(account_details.get(db, int(m[1]))))
                    except edits.ApiError as e: return self.send(e.code, json.dumps({"error": str(e)}))
                if p == "/api/plaid": return self.send(200, json.dumps(plaid.overview(db)))
                if p == "/api/portfolio":
                    return self.send(200, json.dumps(portfolio.build(db, show_hidden=qs.get("hidden") == "1", group="account" if qs.get("group") == "account" else "security")))
                if p == "/api/downloads": return self.send(200, json.dumps(downloads.enabled(db)))
                if p == "/api/accounts":
                    return self.send(200, json.dumps(core.account_balances(db)))
                m = re.fullmatch(r"/api/accounts/(\d+)/register", p)
                if m:
                    r = core.register(db, int(m[1]), qs.get("q", ""), qs.get("sort", "date"), qs.get("dir", "desc"),
                                      int(qs.get("offset", 0)), min(int(qs.get("limit", 200)), 2000), qs.get("hide_stale") == "1")
                    return self.send(200 if r else 404, json.dumps(r or {"error": "no such account"}))
            finally:
                db.close()
            self.send(404, json.dumps({"error": "not found"}))
        except Exception as e:
            traceback.print_exc(); self.send(500, json.dumps({"error": str(e)}))

    def log_message(self, *a): pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--init-only", action="store_true"); ap.add_argument("--revert", metavar="BATCH")
    ap.add_argument("--download-list", action="store_true", help="print JSON: accounts with downloads on (slug, schedule, latest file, last import)"); a = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # so `python3 app.py > data/server.log` fills as it runs
    core.init_db()
    if a.revert:
        try: print("reverted", edits.revert_batch(a.revert), "edits of", a.revert)
        except edits.ApiError as e: raise SystemExit(f"revert failed: {e}")
        raise SystemExit
    if a.download_list: downloads.main(); raise SystemExit
    if a.init_only: raise SystemExit
    try: auto = scheduled.run_auto()
    except Exception as e: auto = dict(entered=[], errors=[str(e)])
    if auto["entered"] or auto["errors"]: print(f"scheduled: auto-entered {len(auto['entered'])}, errors: {auto['errors']}")
    print(f"Ledger on http://127.0.0.1:{a.port}/  (db: {core.DB_PATH})")
    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
