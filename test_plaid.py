#!/usr/bin/env python3
"""Built-in Plaid downloads against a fake Plaid HTTP server (stdlib): config + permissions, link token, exchange, mapping, sync -> csv -> preview, dedupe (ext id and amount/date),
pending/modified/removed, login-required error, no ledger write before Import, tokens/secrets never exposed or logged, Download tab renders. Scratch DB from the made-up fixture."""
import csv, glob, json, os, sqlite3, subprocess, sys, tempfile, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import testfix; T = testfix.make()
import core
tmp = tempfile.mkdtemp(); PD = os.path.join(tmp, "pd"); os.makedirs(PD); os.environ["LEDGER_PLAID_DIR"] = PD
import plaid, edits
scratch = os.path.join(tmp, "s.sqlite"); PORT = 8794; SEC = "secret-XYZ-123"; TOK = "access-token-ABC-999"
s = sqlite3.connect(core.DB_PATH); d = sqlite3.connect(scratch); s.backup(d); d.close(); s.close()
ok = True
def check(n, c, det=""):
    global ok; ok &= bool(c); print(("PASS " if c else "FAIL ") + n + ("" if c else f"  {det}"))
def q(sql, *a):
    x = sqlite3.connect(scratch); x.row_factory = sqlite3.Row; r = x.execute(sql, a).fetchall(); x.commit(); x.close(); return r
def row(where): return q(f"SELECT * FROM transactions WHERE {where} ORDER BY date, txn_id LIMIT 1")[0]
# ---- fixture rows the fake bank echoes back
G = row("account_id=1 AND date LIKE '2026-09-%' AND category='Groceries' AND split_count=0")          # no ext id -> matched on amount/date
R = row("account_id=1 AND date='2026-09-03' AND category='Rent'"); P = row("account_id=1 AND date='2026-09-10' AND category='Utilities:Electric'")
q("UPDATE transactions SET ext_id='conn-rent' WHERE txn_id=?", R["txn_id"]); q("UPDATE transactions SET ext_id='conn-power' WHERE txn_id=?", P["txn_id"])  # imported earlier by the connector
AB = row("account_id=5 AND action='Buy' AND security_name='Alpha Fund' AND date LIKE '2026-09-%'"); BB = row("account_id=5 AND action='Buy' AND security_name='Beta Fund' AND date='2026-09-11'")
q("UPDATE transactions SET ext_id='conn-beta' WHERE txn_id=?", BB["txn_id"]); IQ = lambda r: json.loads(r["invst_json"])
def pl(i, acct, date, amt, name, **k): return dict(transaction_id=i, account_id=acct, date=date, amount=amt, name=name, pending=k.pop("pending", False), merchant_name=k.pop("m", None), personal_finance_category=k.pop("cat", None))
# ---- fake Plaid
SEEN = []; MODE = {"login": False, "step": 0, "notready": False}
class F(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        b = json.loads(self.rfile.read(int(self.headers["Content-Length"]))); SEEN.append((self.path, b)); out, code = {}, 200
        if b.get("client_id") != "cid-1" or b.get("secret") != SEC: out, code = {"error_code": "INVALID_API_KEYS", "error_message": "bad keys"}, 400
        elif self.path == "/link/token/create": out = {"link_token": "link-test"}
        elif self.path == "/item/public_token/exchange": out = {"access_token": TOK, "item_id": "item-1"}
        elif self.path == "/accounts/get":
            out = {"accounts": [dict(account_id="a-chk", name="Checking", official_name="Total Checking", mask="1111", type="depository", subtype="checking"),
                                dict(account_id="a-sav", name="Savings", mask="2222", type="depository", subtype="savings"),
                                dict(account_id="a-inv", name="Brokerage", mask="3333", type="investment", subtype="brokerage")]}
        elif self.path == "/item/remove": out = {"request_id": "x"}
        elif b.get("access_token") != TOK: out, code = {"error_code": "INVALID_ACCESS_TOKEN", "error_message": "bad token"}, 400
        elif MODE["login"]: out, code = {"error_code": "ITEM_LOGIN_REQUIRED", "error_message": "login again"}, 400
        elif self.path == "/transactions/sync":
            c = b.get("cursor")
            if MODE["notready"]: out = dict(added=[], modified=[], removed=[], next_cursor="", has_more=False, transactions_update_status="NOT_READY")
            elif not c: out = dict(added=[pl("t-match", "a-chk", G["date"], -float(G["amount"]), "Market", m="Market", cat={"detailed": "FOOD_AND_DRINK_GROCERIES"}),
                                        pl("t-new", "a-chk", "2026-09-12", 9.99, "AMZN MKTP 123", m="Amazon", cat={"detailed": "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES"}),
                                        pl("t-pend", "a-chk", "2026-09-13", 5.0, "Pending Cafe", pending=True),
                                        pl("t-sav", "a-sav", "2026-09-12", 1.0, "Unlinked account row")], modified=[], removed=[], next_cursor="c1", has_more=True)
            elif c == "c1": out = dict(added=[pl("t-rent", "a-chk", "2026-09-05", 1200.0, "Landlord"), pl("t-power", "a-chk", "2026-09-14", -float(P["amount"]), "Power Co"),
                                              pl("t-gone", "a-chk", "2026-09-20", 3.0, "Will be removed")], modified=[], removed=[], next_cursor="c2", has_more=False)
            else: out = dict(added=[pl("t-late", "a-chk", "2026-10-02", 20.0, "October Shop")], modified=[pl("t-new", "a-chk", "2026-09-12", 9.99, "AMZN MKTP 123", m="Amazon Inc")], removed=[{"transaction_id": "t-gone"}], next_cursor="c3", has_more=False)
        elif self.path == "/investments/transactions/get":
            it = [dict(investment_transaction_id="i-match", account_id="a-inv", security_id="s-a", date=AB["date"], name="Buy Alpha", type="buy", amount=float(AB["amount"]), quantity=IQ(AB)["Q"], price=IQ(AB)["I"]),
                  dict(investment_transaction_id="i-beta", account_id="a-inv", security_id="s-b", date=BB["date"], name="Buy Beta", type="buy", amount=float(BB["amount"]), quantity=IQ(BB)["Q"], price=IQ(BB)["I"]),
                  dict(investment_transaction_id="i-new", account_id="a-inv", security_id="s-a", date="2026-09-15", name="Buy Alpha", type="buy", amount=140.0, quantity=7, price=20),
                  dict(investment_transaction_id="i-fee", account_id="a-inv", security_id=None, date="2026-09-19", name="Fee", type="fee", amount=15.0, quantity=0, price=0)]
            out = dict(investment_transactions=it, securities=[dict(security_id="s-a", ticker_symbol="ALPHX", name="Alpha Fund"), dict(security_id="s-b", ticker_symbol="BETAX", name="Beta Fund")], total_investment_transactions=len(it))
        else: out, code = {"error_code": "NOPE", "error_message": self.path}, 404
        body = json.dumps(out).encode(); self.send_response(code); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
fs = ThreadingHTTPServer(("127.0.0.1", 0), F); threading.Thread(target=fs.serve_forever, daemon=True).start()
env = dict(os.environ, LEDGER_DB=scratch, LEDGER_IMPORTS=T.imports, LEDGER_PLAID_DIR=PD, PLAID_CLIENT_ID="cid-1", PLAID_SECRET=SEC, PLAID_BASE_URL=f"http://127.0.0.1:{fs.server_port}")
for k in ("PLAID_ENV",): env.pop(k, None)
# ---- config
for k in ("PLAID_CLIENT_ID", "PLAID_SECRET", "PLAID_ENV", "PLAID_BASE_URL"): os.environ.pop(k, None)
try: plaid.config(); check("no keys -> clear error", False)
except edits.ApiError as e: check("no keys -> clear error", "PLAID_CLIENT_ID" in str(e) and "SETUP.md" in str(e), e)
cf = os.path.join(PD, "plaid.json"); open(cf, "w").write(json.dumps({"PLAID_CLIENT_ID": "c", "PLAID_SECRET": "s"})); os.chmod(cf, 0o644)
try: plaid.config(); check("keys file readable by others is refused", False)
except edits.ApiError as e: check("keys file readable by others is refused", "chmod 600" in str(e), e)
os.chmod(cf, 0o600); c = plaid.config(); check("chmod-600 keys file works; default env is production", c[:3] == ("c", "s", "https://production.plaid.com"), c)
os.environ.update(PLAID_CLIENT_ID="e", PLAID_SECRET="e2", PLAID_ENV="sandbox"); c = plaid.config(); check("environment wins; sandbox url", c[:3] == ("e", "e2", "https://sandbox.plaid.com"), c); os.remove(cf)
for k in ("PLAID_CLIENT_ID", "PLAID_SECRET", "PLAID_ENV"): os.environ.pop(k)
log = open(os.path.join(tmp, "server.log"), "w"); p = subprocess.Popen([sys.executable, "-u", "app.py", "--port", str(PORT)], env=env, cwd=core.HERE, stdout=log, stderr=subprocess.STDOUT)
def call(method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())
def counts(): return (q("SELECT COUNT(*) c FROM transactions")[0]["c"], q("SELECT COUNT(*) c FROM edit_log")[0]["c"])
def files(slug): return sorted(glob.glob(os.path.join(T.imports, slug, "*.csv")))
def rd(f): return list(csv.DictReader(open(f, newline="")))
try:
    time.sleep(1.5)
    s_, o = call("GET", "/api/plaid"); check("overview: configured, no items", s_ == 200 and o["configured"] and o["items"] == [], o)
    s_, o = call("POST", "/api/plaid/link_token", {"kind": "bank"}); b = [b for u, b in SEEN if u == "/link/token/create"][-1]
    check("link token (bank): transactions required, investments optional, 90 days, keys sent", o == {"link_token": "link-test"} and b["products"] == ["transactions"] and b["optional_products"] == ["investments"] and b["transactions"]["days_requested"] == 90 and "redirect_uri" not in b and b["client_id"] == "cid-1", b)
    call("POST", "/api/plaid/link_token", {"kind": "investments"}); b = [b for u, b in SEEN if u == "/link/token/create"][-1]
    check("link token (brokerage): investments required", b["products"] == ["investments"] and b["optional_products"] == ["transactions"], b)
    check("link token with bad kind is refused", call("POST", "/api/plaid/link_token", {"kind": "x"})[0] == 400)
    s_, o = call("POST", "/api/plaid/exchange", {"public_token": "public-ok", "institution": "Test Bank"}); check("exchange stores the item", s_ == 200 and o["accounts"] == 3, o)
    f = os.path.join(PD, "plaid_items.json"); check("items file is chmod 600 and holds the token", oct(os.stat(f).st_mode & 0o777) == "0o600" and TOK in open(f).read())
    s_, o = call("GET", "/api/plaid"); check("overview never shows the token or secret", TOK not in json.dumps(o) and SEC not in json.dumps(o) and len(o["items"][0]["accounts"]) == 3)
    check("sync of an unlinked account is refused with a plain message", call("POST", "/api/plaid/sync", {"account_id": 1})[0] == 400)
    chk = lambda pid, aid: call("POST", "/api/plaid/map", {"item_id": "item-1", "plaid_account_id": pid, "account_id": aid})
    check("map: investment <-> bank mismatch refused", chk("a-chk", 5)[0] == 400 and chk("a-inv", 1)[0] == 400)
    check("map checking, brokerage", chk("a-chk", 1)[0] == 200 and chk("a-inv", 5)[0] == 200)
    check("map: one ledger account cannot take two Plaid accounts", chk("a-sav", 1)[0] == 409)
    before = counts(); bal = q("SELECT account_id, ROUND(SUM(amount),2) s FROM transactions GROUP BY account_id")
    # ---- bank sync
    s_, r = call("POST", "/api/plaid/sync", {"account_id": 1}); o = r["items"][0] if s_ == 200 else r
    fl = files("main-checking"); check("sync writes one csv per month with plaid_ in the name; unlinked account's row is not written", s_ == 200 and [os.path.basename(x) for x in fl] == ["main-checking_plaid_2026-09.csv"], (s_, r, fl))
    R1 = rd(fl[0]); ids = [x["txn_id"] for x in R1]
    check("rows: posted only (pending skipped), sign flipped (out = negative), plaid category, source", sorted(ids) == sorted(["plaid:t-match", "plaid:t-new", "plaid:t-rent", "plaid:t-power", "plaid:t-gone"]) and
          {x["txn_id"]: x["amount"] for x in R1}["plaid:t-new"] == "-9.9900" and {x["txn_id"]: x["amount"] for x in R1}["plaid:t-rent"] == "-1200.0000" and R1[0]["source"] == "plaid-direct" and R1[0]["account"] == "main-checking" and
          [x for x in R1 if x["txn_id"] == "plaid:t-new"][0]["payee"] == "Amazon" and [x for x in R1 if x["txn_id"] == "plaid:t-new"][0]["memo"] == "AMZN MKTP 123" and [x for x in R1 if x["txn_id"] == "plaid:t-new"][0]["category"] == "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES", R1)
    check("pagination followed (2 calls) and the cursor was sent", [b.get("cursor") for u, b in SEEN if u == "/transactions/sync"] == [None, "c1"], [b.get("cursor") for u, b in SEEN if u == "/transactions/sync"])
    c = o["preview"]["main-checking"]; check("preview in the reply: groceries + rent matched, rent (2 days off an id'd row) matched, power (4 days off) new", (c["new"], c["matched"], c["duplicate"]) == (3, 2, 0), c)
    check("no ledger write and no balance change by a download (only csv files + import slug)", counts() == before and q("SELECT account_id, ROUND(SUM(amount),2) s FROM transactions GROUP BY account_id") == bal)
    pv = {x["slug"]: x for x in call("GET", "/api/import/preview")[1]}["main-checking"]; st = {x["txn_id"]: x for x in pv["rows"]}
    check("dedupe: no-id row matched by amount/date; id'd row matched within 3 days (rent 2 days off), not 4 (power)", st["plaid:t-match"]["status"] == "matched" and st["plaid:t-match"]["match"]["txn_id"] == G["txn_id"] and st["plaid:t-rent"]["status"] == "matched" and st["plaid:t-rent"]["match"]["txn_id"] == R["txn_id"] and st["plaid:t-power"]["status"] == "new", {k: v["status"] for k, v in st.items()})
    # a csv from another source whose ids are already in the ledger must not stop Plaid rows matching those same ledger rows (bot csv + Plaid csv in one folder)
    bd = os.path.join(T.imports, "main-checking"); bot = os.path.join(bd, "main-checking_2026-09.csv")
    with open(bot, "w", newline="") as fh: w = csv.writer(fh); w.writerow(plaid.HEAD); w.writerow([R["date"], "Landlord", R["amount"], "", "", "conn-rent", "main-checking", "bot"]); w.writerow([P["date"], "Power Co", P["amount"], "", "", "conn-power", "main-checking", "bot"])
    pv2 = {x["slug"]: x for x in call("GET", "/api/import/preview")[1]}["main-checking"]; st2 = {x["txn_id"]: x for x in pv2["rows"]}
    check("folder also holds a bot csv whose ids are in the ledger: those rows are duplicates, Plaid rent still matches its ledger row", st2["conn-rent"]["status"] == "duplicate" and st2["plaid:t-rent"]["status"] == "matched" and st2["plaid:t-rent"]["match"]["txn_id"] == R["txn_id"] and st2["plaid:t-power"]["status"] == "new", {k: v["status"] for k, v in st2.items()})
    os.makedirs(os.path.join(bd, "imported")); os.replace(bot, os.path.join(bd, "imported", "main-checking_2026-09.csv"))
    pv3 = {x["slug"]: x for x in call("GET", "/api/import/preview")[1]}["main-checking"]
    check("csv files moved into imported/ are ignored by preview and by the Download tab's latest file", "main-checking_2026-09.csv" not in pv3["files"] and "conn-rent" not in {x["txn_id"] for x in pv3["rows"]} and pv3["counts"]["total"] == pv["counts"]["total"], pv3["files"])
    check("download list latest_file ignores imported/", all((x["latest_file"] or "").startswith("main-checking_plaid_") for x in call("GET", "/api/downloads")[1] if x["slug"] == "main-checking"), call("GET", "/api/downloads")[1])
    # ---- import needs the user's button
    s_, ib = call("POST", "/api/import/run", {"slugs": ["main-checking"]}); n0 = before[0]
    check("import (the confirm step) adds new rows and links matched rows, appending to an existing id", s_ == 200 and q("SELECT COUNT(*) c FROM transactions")[0]["c"] == n0 + 3 and q("SELECT ext_id FROM transactions WHERE txn_id=?", R["txn_id"])[0][0] == "conn-rent,plaid:t-rent" and q("SELECT ext_id FROM transactions WHERE txn_id=?", G["txn_id"])[0][0] == "plaid:t-match", (s_, ib))
    # ---- second download: modified / removed / new month; nothing doubles
    s_, r = call("POST", "/api/plaid/sync", {"account_id": 1}); fl = files("main-checking"); R9 = rd(fl[0]); R10 = rd(fl[1]) if len(fl) > 1 else []
    check("second sync: cursor continues; new month file; removed row gone; modified row updated", [b.get("cursor") for u, b in SEEN if u == "/transactions/sync"][-1] == "c2" and len(fl) == 2 and "plaid:t-gone" not in [x["txn_id"] for x in R9] and [x for x in R9 if x["txn_id"] == "plaid:t-new"][0]["payee"] == "Amazon Inc" and [x["txn_id"] for x in R10] == ["plaid:t-late"], (fl, R9, R10))
    pv = {x["slug"]: x for x in call("GET", "/api/import/preview")[1]}["main-checking"]; c = pv["counts"]
    check("after import the earlier rows are duplicates; only the late row (and the changed one's id, already known) differ", sum(1 for x in pv["rows"] if x["status"] == "new") == 1 and [x["txn_id"] for x in pv["rows"] if x["status"] == "new"] == ["plaid:t-late"], c)
    check("importing again adds exactly one row", call("POST", "/api/import/run", {"slugs": ["main-checking"]})[0] == 200 and q("SELECT COUNT(*) c FROM transactions")[0]["c"] == n0 + 4)
    call("POST", "/api/plaid/sync", {"account_id": 1}); pv = {x["slug"]: x for x in call("GET", "/api/import/preview")[1]}["main-checking"]["counts"]
    check("a further download with nothing new leaves nothing to import", pv["new"] == 0 and pv["matched"] == 0, pv)
    # ---- investments
    s_, r = call("POST", "/api/plaid/sync", {"account_id": 5}); fl = files("fidelity-test-xx1234"); I = rd(fl[0]) if fl else []
    check("investment sync: csv with type/security/quantity/price, ticker as security, sign flipped, cash fee without security", s_ == 200 and [os.path.basename(x) for x in fl] == ["fidelity-test-xx1234_plaid_2026-09.csv"] and list(I[0]) == plaid.IHEAD and
          [x for x in I if x["txn_id"] == "plaid:i-new"][0]["security"] == "ALPHX" and [x for x in I if x["txn_id"] == "plaid:i-new"][0]["amount"] == "-140.0000" and [x for x in I if x["txn_id"] == "plaid:i-new"][0]["type"] == "buy" and
          [x for x in I if x["txn_id"] == "plaid:i-fee"][0]["security"] == "" and [x for x in I if x["txn_id"] == "plaid:i-fee"][0]["amount"] == "-15.0000", (s_, r, I))
    st = {x["txn_id"]: x["status"] for x in {x["slug"]: x for x in call("GET", "/api/import/preview")[1]}["fidelity-test-xx1234"]["rows"]}
    check("investment dedupe: no-id Buy matched, id'd Buy matched (same day), new Buy and fee new", st.get("plaid:i-match") == "matched" and st.get("plaid:i-beta") == "matched" and st.get("plaid:i-new") == "new" and st.get("plaid:i-fee") == "new", st)
    ib = [b for u, b in SEEN if u == "/investments/transactions/get"][-1]; check("investments request: account filter and date window", ib["options"]["account_ids"] == ["a-inv"] and ib["start_date"] < ib["end_date"], ib)
    bf = os.path.join(T.imports, "fidelity-test-xx1234", "fidelity-test-xx1234_2026-09.csv")  # bot csv: Beta buy already in the ledger under conn-beta
    with open(bf, "w", newline="") as fh: w = csv.writer(fh); w.writerow(plaid.IHEAD); w.writerow([BB["date"], "Buy Beta", BB["amount"], "", "", "conn-beta", "fidelity-test-xx1234", "bot", "buy", "BETAX", IQ(BB)["Q"], IQ(BB)["I"]])
    st = {x["txn_id"]: x["status"] for x in {x["slug"]: x for x in call("GET", "/api/import/preview")[1]}["fidelity-test-xx1234"]["rows"]}; os.remove(bf)
    check("investment: a bot csv row already in the ledger doesn't stop the Plaid row matching it", st.get("conn-beta") == "duplicate" and st.get("plaid:i-beta") == "matched" and st.get("plaid:i-new") == "new", st)
    # ---- Plaid still loading history: nothing written, cursor not kept
    cur = json.load(open(f))["items"]["item-1"]["cursor"]; MODE["notready"] = True; s_, r = call("POST", "/api/plaid/sync", {"account_id": 1}); MODE["notready"] = False
    check("NOT_READY reply: warning shown, saved cursor unchanged", s_ == 200 and any("still loading" in w for w in r["items"][0]["warnings"]) and json.load(open(f))["items"]["item-1"]["cursor"] == cur, (s_, r))
    # ---- download all + errors
    call("PUT", "/api/accounts/1/details", {"dl_enabled": True}); call("PUT", "/api/accounts/5/details", {"dl_enabled": True}); call("PUT", "/api/accounts/2/details", {"dl_enabled": True})
    s_, r = call("POST", "/api/plaid/sync", {"all": True}); check("download all: linked + enabled accounts only; unlinked enabled ones are reported", s_ == 200 and set(r["items"][0]["slugs"]) == {"main-checking", "fidelity-test-xx1234"} and r["skipped_not_linked"] == ["Savings"], r)
    MODE["login"] = True; nb = counts(); s_, r = call("POST", "/api/plaid/sync", {"account_id": 1}); items = call("GET", "/api/plaid")[1]["items"]
    check("login-required: reported on the item, nothing written", s_ == 200 and "ITEM_LOGIN_REQUIRED" in r["items"][0]["error"] and "ITEM_LOGIN_REQUIRED" in items[0]["error"] and counts() == nb)
    MODE["login"] = False; call("POST", "/api/plaid/sync", {"account_id": 1}); check("error clears after a good download", call("GET", "/api/plaid")[1]["items"][0]["error"] is None)
    s_, o = call("POST", "/api/plaid/link_token", {"item_id": "item-1"}); b = [b for u, b in SEEN if u == "/link/token/create"][-1]
    check("update-mode link token uses the stored token and no products", b.get("access_token") == TOK and "products" not in b)
    # ---- remapping resets history; unlink
    call("POST", "/api/plaid/map", {"item_id": "item-1", "plaid_account_id": "a-chk", "account_id": None}); call("POST", "/api/plaid/map", {"item_id": "item-1", "plaid_account_id": "a-sav", "account_id": 2})
    check("linking a new account re-reads history (cursor reset)", json.load(open(f))["items"]["item-1"]["cursor"] is None)
    check("unlink removes the item at Plaid and locally", call("POST", "/api/plaid/remove", {"item_id": "item-1"})[0] == 200 and any(u == "/item/remove" for u, _ in SEEN) and call("GET", "/api/plaid")[1]["items"] == [])
    # ---- UI
    call("POST", "/api/plaid/exchange", {"public_token": "public-ok", "institution": "Test Bank"}); call("POST", "/api/plaid/map", {"item_id": "item-1", "plaid_account_id": "a-chk", "account_id": 1})
    dom = subprocess.run(["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--virtual-time-budget=6000", "--dump-dom", f"http://127.0.0.1:{PORT}/#/a/1/details"], capture_output=True, text=True, timeout=90).stdout
    check("Download tab shows Plaid account dropdown, link buttons, Download now (enabled)", all(t in dom for t in ("Plaid account", "Link bank or card", "Link brokerage", "Download now", "Test Bank: Total Checking")) and 'id="pl_go" disabled' not in dom, dom[-400:])
    dom = subprocess.run(["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--virtual-time-budget=6000", "--dump-dom", f"http://127.0.0.1:{PORT}/#/import"], capture_output=True, text=True, timeout=90).stdout
    check("Import page has the Plaid panel with Download all", all(t in dom for t in ("Plaid downloads", "Download all", "Test Bank")))
finally:
    p.terminate(); log.close()
lg = open(os.path.join(tmp, "server.log")).read()
check("server log has no secret, token or public token", SEC not in lg and TOK not in lg and "public-ok" not in lg and "Traceback" not in lg, lg[-300:])
print("ALL PASS" if ok else "FAILURES"); sys.exit(0 if ok else 1)
