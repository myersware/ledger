#!/usr/bin/env python3
"""Household inventory: items (validation, totals, purchase-transaction link), attachments (store/serve/delete, path safety), csv export, undo,
the net worth 'Household items' line and its saved toggle (account balances never change), and the v9 -> v10 migration with a backup. Fresh empty ledger."""
import base64, datetime, glob, os, sqlite3, subprocess, sys, urllib.error, urllib.request
from testempty import Empty, Checker, HERE
import core
check = Checker(); E = Empty(8791); api = E.api; E.start()
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
b64 = lambda b: base64.b64encode(b).decode()
def raw(path, method="GET"):
    try:
        with urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{E.port}{path}", method=method)) as r: return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e: return e.code, dict(e.headers), e.read()
try:
    db = sqlite3.connect(E.db)
    check("fresh install is schema v10+ with the inventory and settings tables", db.execute("PRAGMA user_version").fetchone()[0] >= 10 and db.execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0] == 0 and db.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0)
    chk = api("POST", "/api/accounts", dict(name="Checking", type="Checking", opening_balance=5000, opening_date="2026-01-01"))[1]["account_id"]
    api("POST", "/api/categories", dict(name="Hobby"))
    tx = api("POST", f"/api/accounts/{chk}/transactions", dict(date="2026-03-15", payee="Guitar Center", memo="Martin D-28", amount=-2499.99, category="Hobby"))[1]["txn_id"]
    bal = lambda: {a["name"]: (a["balance"], a["cash"]) for a in api("GET", "/api/accounts")[1]}
    nw = lambda h: api("GET", f"/api/reports/networth?from=2026-01-01&to={datetime.date.today().isoformat()}&gran=month&household={h}")[1]
    b0 = bal(); nw0 = nw(0)
    # ---- validation
    check("name required", api("POST", "/api/inventory", dict(name=" "))[0] == 400)
    check("bad date / negative price / unknown txn refused", api("POST", "/api/inventory", dict(name="x", purchase_date="03/15/2026"))[0] == 400 and api("POST", "/api/inventory", dict(name="x", purchase_price="-5"))[0] == 400
          and api("POST", "/api/inventory", dict(name="x", current_value="abc"))[0] == 400 and api("POST", "/api/inventory", dict(name="x", txn_id=99999))[0] == 404)
    check("nothing was created by the refusals", api("GET", "/api/inventory")[1]["totals"]["count"] == 0)
    # ---- create / read
    s, r = api("POST", "/api/inventory", dict(name="Martin D-28", category="Musical instruments", make="Martin", model="D-28", serial="2345678", location="Living room",
                                              purchase_date="2026-03-15", purchase_price="$2,499.99", txn_id=tx, notes="bought new"))
    check("create item", s == 200 and r["item_id"] and r["batch"].startswith("ui-"), r); g = r["item_id"]
    s, r = api("POST", "/api/inventory", dict(name="Yamaha P-125", category="Musical instruments", purchase_date="2025-01-10", purchase_price=600, current_value=450, value_date="2026-06-01"))
    p = r["item_id"]
    s, r = api("POST", "/api/inventory", dict(name="Old watch", category="Jewelry", current_value=1000)); w = r["item_id"]
    L = api("GET", "/api/inventory")[1]; it = {i["name"]: i for i in L["items"]}
    check("purchase price kept as a number; value falls back to purchase price", it["Martin D-28"]["purchase_price"] == 2499.99 and it["Martin D-28"]["value"] == 2499.99 and it["Martin D-28"]["value_is_purchase"] is True, it["Martin D-28"])
    check("current value wins over purchase price", it["Yamaha P-125"]["value"] == 450 and it["Yamaha P-125"]["value_is_purchase"] is False)
    check("a current value with no date gets today's", it["Old watch"]["value_date"] == datetime.date.today().isoformat(), it["Old watch"])
    check("totals and category totals", L["totals"] == dict(count=3, purchase=3099.99, value=3949.99) and {c["category"]: (c["count"], c["purchase"], c["value"]) for c in L["categories"]} == {"Musical instruments": (2, 3099.99, 2949.99), "Jewelry": (1, 0.0, 1000.0)}, (L["totals"], L["categories"]))
    check("linked purchase transaction is shown", it["Martin D-28"]["txn"]["payee"] == "Guitar Center" and it["Martin D-28"]["txn"]["amount"] == -2499.99 and it["Martin D-28"]["txn"]["account"] == "Checking", it["Martin D-28"]["txn"])
    check("get one item", api("GET", f"/api/inventory/{g}")[1]["serial"] == "2345678" and api("GET", "/api/inventory/9999")[0] == 404)
    ts = api("GET", "/api/inventory/txn-search?q=guitar")[1]["transactions"]; ts2 = api("GET", "/api/inventory/txn-search?q=2499.99")[1]["transactions"]; ts3 = api("GET", "/api/inventory/txn-search?q=zzzz")[1]["transactions"]
    check("purchase picker finds by payee, memo text and amount", [t["txn_id"] for t in ts] == [tx] and [t["txn_id"] for t in ts2] == [tx] and ts3 == [] and api("GET", "/api/inventory/txn-search?q=d-28")[1]["transactions"][0]["txn_id"] == tx)
    check("inventory edits never mark the linked account as edited", db.execute("SELECT COUNT(*) FROM edit_log WHERE tbl='inventory_items' AND account_id IS NOT NULL").fetchone()[0] == 0)
    # ---- update
    s, r = api("PUT", f"/api/inventory/{g}", dict(current_value="2100", value_date="2026-10-01", location="Studio")); check("update", s == 200, r)
    x = api("GET", f"/api/inventory/{g}")[1]; check("only the sent fields changed", x["current_value"] == 2100 and x["location"] == "Studio" and x["make"] == "Martin" and x["txn_id"] == tx and x["purchase_price"] == 2499.99, x)
    s, r = api("PUT", f"/api/inventory/{g}", dict(txn_id="")); check("unlink the transaction", s == 200 and api("GET", f"/api/inventory/{g}")[1]["txn_id"] is None)
    ub = r["batch"]
    rv = subprocess.run([sys.executable, "app.py", "--revert", ub], env=E.env, cwd=HERE, capture_output=True, text=True)
    check("update is revertable (link back)", rv.returncode == 0 and api("GET", f"/api/inventory/{g}")[1]["txn_id"] == tx, rv.stdout + rv.stderr)
    check("update of a missing item is 404", api("PUT", "/api/inventory/9999", dict(name="x"))[0] == 404)
    # ---- files
    s, r = api("POST", f"/api/inventory/{g}/files", dict(name="front photo.png", data=b64(PNG))); check("upload a photo", s == 200 and r["name"] == "front photo.png", r)
    s, r2 = api("POST", f"/api/inventory/{g}/files", dict(name="front photo.png", data=b64(PNG))); check("same name gets a number, nothing overwritten", r2["name"] == "front photo-2.png")
    s, r3 = api("POST", f"/api/inventory/{g}/files", dict(name="../../evil.html", data=b64(b"<script>alert(1)</script>"))); check("path in the name is stripped", s == 200 and r3["name"] == "evil.html", r3)
    folder = os.path.join(E.tmp, "data", "inventory", str(g)); check("files live in <data dir>/inventory/<id>/", sorted(os.listdir(folder)) == ["evil.html", "front photo-2.png", "front photo.png"], os.listdir(folder))
    check("nothing escaped the item folder", not os.path.exists(os.path.join(E.tmp, "data", "evil.html")) and not os.path.exists(os.path.join(E.tmp, "evil.html")))
    check("bad uploads refused", api("POST", f"/api/inventory/{g}/files", dict(name="a", data="***"))[0] == 400 and api("POST", f"/api/inventory/{g}/files", dict(name="a", data=""))[0] == 400 and api("POST", "/api/inventory/9999/files", dict(name="a", data=b64(b"x")))[0] == 404)
    st, h, body = raw(f"/api/inventory/{g}/files/front%20photo.png"); check("photo is served inline as an image, byte for byte", st == 200 and h["Content-Type"].startswith("image/png") and h["Content-Disposition"].startswith("inline") and body == PNG and h.get("X-Content-Type-Options") == "nosniff", (st, h))
    st, h, body = raw(f"/api/inventory/{g}/files/evil.html"); check("an html upload is never shown as a page (download only)", st == 200 and h["Content-Type"].startswith("application/octet-stream") and h["Content-Disposition"].startswith("attachment"), h)
    st, h, body = raw(f"/api/inventory/{g}/files/front%20photo.png?download=1"); check("download=1 forces a download", h["Content-Disposition"].startswith("attachment"))
    check("path traversal on read is refused", all(raw(u)[0] in (404, 400) for u in (f"/api/inventory/{g}/files/..%2f..%2fledger.sqlite", f"/api/inventory/{g}/files/%2e%2e", f"/api/inventory/{p}/files/..%2f{g}%2fevil.html", f"/api/inventory/{g}/files/.hidden")))
    L = api("GET", f"/api/inventory/{g}")[1]; check("item lists its files with sizes", [f["name"] for f in L["files"]] == ["evil.html", "front photo-2.png", "front photo.png"] and L["files"][2]["image"] and L["files"][0]["viewable"] is False and L["files"][2]["size"] == len(PNG), L["files"])
    check("list shows the attachment count", {i["name"]: i["files"] for i in api("GET", "/api/inventory")[1]["items"]}["Martin D-28"] == 3)
    check("delete one file", api("DELETE", f"/api/inventory/{g}/files/evil.html")[0] == 200 and not os.path.exists(os.path.join(folder, "evil.html")) and api("DELETE", f"/api/inventory/{g}/files/evil.html")[0] == 404)
    check("path traversal on delete is refused", api("DELETE", f"/api/inventory/{g}/files/..%2f..%2fdata")[0] in (404, 400) and os.path.isdir(folder))
    s, r = api("POST", f"/api/inventory/{p}/files", dict(name="big.bin", data=b64(b"x" * (25 * 1024 * 1024 + 1)))); check("a file over 25 MB is refused", s == 413, (s, r))
    # ---- csv
    st, h, body = raw("/api/export/inventory.csv"); lines = body.decode().splitlines()
    check("csv export", st == 200 and h["Content-Disposition"].endswith('ledger-inventory.csv"') and lines[0].startswith("item_id,name,category,make,model,serial,location,purchase_date,purchase_price,current_value,value_date,value_used,notes,purchase_txn_id,attachments") and len(lines) == 4, body[:300])
    import csv, io
    rows = {r["name"]: r for r in csv.DictReader(io.StringIO(body.decode()))}
    check("csv values and attachment count", rows["Martin D-28"]["serial"] == "2345678" and rows["Martin D-28"]["value_used"] == "2100.0" and rows["Martin D-28"]["attachments"] == "2" and rows["Martin D-28"]["purchase_txn_id"] == str(tx) and rows["Old watch"]["purchase_price"] == "" and rows["Old watch"]["value_used"] == "1000.0", rows["Martin D-28"])
    # ---- net worth
    check("account balances are unchanged by adding items", bal() == b0, (bal(), b0))
    n_on, n_off = nw(1), nw(0); last = lambda R: R["points"][-1]
    hv = 2100 + 450 + 1000
    check("toggle off: net worth equals the no-items figure, no Household line", last(n_off)["net"] == last(nw0)["net"] and "Household items" not in n_off["types"] and n_off["household"]["included"] is False)
    check("toggle on: one 'Household items' line added to assets and net", last(n_on)["types"]["Household items"] == hv and round(last(n_on)["net"] - last(n_off)["net"], 2) == hv and round(last(n_on)["assets"] - last(n_off)["assets"], 2) == hv and last(n_on)["liabilities"] == last(n_off)["liabilities"], last(n_on))
    check("other lines are identical with it on or off", {k: v for k, v in last(n_on)["types"].items() if k != "Household items"} == last(n_off)["types"])
    pts = {p_["date"][:7]: p_ for p_ in n_on["points"]}
    check("items count from their purchase date (undated ones always)", pts["2026-01"]["types"]["Household items"] == 1000 + 450 and pts["2026-03"]["types"]["Household items"] == 1000 + 450 + 2100 and last(n_on)["types"]["Household items"] == hv, [(k, v["types"]["Household items"]) for k, v in pts.items()])
    # the toggle is saved in the database and used when the page does not say
    check("setting defaults to on", api("GET", "/api/settings")[1] == dict(household_networth=True) and api("GET", "/api/inventory")[1]["household_networth"] is True)
    dflt = api("GET", f"/api/reports/networth?from=2026-01-01&to={datetime.date.today().isoformat()}&gran=month")[1]; check("report with no parameter follows the setting (on)", last(dflt)["types"].get("Household items") == hv)
    check("setting must be true or false", api("PUT", "/api/settings", dict(household_networth="no"))[0] == 400)
    s, r = api("PUT", "/api/settings", dict(household_networth=False)); check("turn it off", s == 200 and r == dict(household_networth=False))
    off = api("GET", f"/api/reports/networth?from=2026-01-01&to={datetime.date.today().isoformat()}&gran=month")[1]
    check("report follows the saved setting (off) and matches the no-items net worth", "Household items" not in off["types"] and last(off)["net"] == last(nw0)["net"] and off["household"] == dict(included=False, items=3, enabled=False), off["household"])
    check("setting is stored in the database (survives a restart)", sqlite3.connect(E.db).execute("SELECT value FROM settings WHERE key='household_networth'").fetchone()[0] == "0")
    check("account balances unchanged after the toggle", bal() == b0)
    api("PUT", "/api/settings", dict(household_networth=True))
    # ---- delete / undo
    s, r = api("DELETE", f"/api/inventory/{g}", {}); check("delete item keeps its files by default", s == 200 and r["files_deleted"] == 0 and os.path.isdir(folder) and api("GET", f"/api/inventory/{g}")[0] == 404, r)
    rv = subprocess.run([sys.executable, "app.py", "--revert", r["batch"]], env=E.env, cwd=HERE, capture_output=True, text=True)
    x = api("GET", f"/api/inventory/{g}"); check("revert brings the item back with its id and files", rv.returncode == 0 and x[0] == 200 and x[1]["name"] == "Martin D-28" and len(x[1]["files"]) == 2 and x[1]["txn_id"] == tx, (rv.stdout + rv.stderr, x))
    s, r = api("DELETE", f"/api/inventory/{g}", dict(delete_files=True)); check("delete with delete_files removes the folder", s == 200 and r["files_deleted"] == 2 and not os.path.exists(folder), r)
    s, r = api("POST", "/api/inventory", dict(name="New one")); check("ids are never reused after a delete", r["item_id"] > w, r)
    s, r = api("POST", "/api/inventory", dict(name="Temp")); rv = subprocess.run([sys.executable, "app.py", "--revert", r["batch"]], env=E.env, cwd=HERE, capture_output=True, text=True)
    check("create is revertable", rv.returncode == 0 and api("GET", f"/api/inventory/{r['item_id']}")[0] == 404)
    check("account balances still identical at the end", bal() == b0)
finally:
    E.stop()
# ---- migration from v9: backup first, then the new tables
E2 = Empty(8792); d = sqlite3.connect(E2.db); d.execute("DROP TABLE inventory_items"); d.execute("DROP TABLE settings"); d.execute("PRAGMA user_version=9")
d.execute("INSERT INTO accounts(name, list_type, register_type) VALUES('Keep me','Bank','Bank')"); d.commit(); d.close()
r = subprocess.run([sys.executable, "app.py", "--init-only"], env=E2.env, cwd=HERE, capture_output=True, text=True)
bk = glob.glob(os.path.join(E2.tmp, "data", "backups", f"*-pre-v{core.SCHEMA_VERSION}.sqlite")); d = sqlite3.connect(E2.db)
check("v9 -> v10 makes a dated backup first", r.returncode == 0 and len(bk) == 1 and sqlite3.connect(bk[0]).execute("PRAGMA user_version").fetchone()[0] == 9, r.stderr)
check("migrated: current version, tables there, data kept", d.execute("PRAGMA user_version").fetchone()[0] == core.SCHEMA_VERSION and d.execute("SELECT COUNT(*) FROM inventory_items").fetchone()[0] == 0 and d.execute("SELECT name FROM accounts").fetchone()[0] == "Keep me")
sys.exit(0 if check.ok else 1)
