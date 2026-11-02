#!/usr/bin/env python3
"""Hidden-account flag test on a SCRATCH copy: API + headless render + restart persistence."""
import json, os, re, sqlite3, subprocess, sys, tempfile, time, urllib.request
import testfix; T = testfix.make()  # made-up start database -> throwaway working DB (no real data needed)
import core

tmp = tempfile.mkdtemp(); scratch = os.path.join(tmp, "s.sqlite"); PORT = 8798
s = sqlite3.connect(core.DB_PATH); d = sqlite3.connect(scratch); s.backup(d); d.execute('UPDATE accounts SET hidden=0'); d.commit(); d.close(); s.close()
env = dict(os.environ, LEDGER_DB=scratch); procs = []
def start():
    p = subprocess.Popen([sys.executable, "app.py", "--port", str(PORT)], env=env, cwd=core.HERE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    procs.append(p); time.sleep(1.5); return p
def call(method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method, data=None if body is None else json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read())
def render(frag):
    out = subprocess.run(["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", "--virtual-time-budget=5000", "--dump-dom", f"http://127.0.0.1:{PORT}/{frag}"], capture_output=True, text=True, timeout=90).stdout
    return out
def group(dom, label):  # (subtotal, rows) for a group
    m = re.search(rf'<tr class="grp"><td>{label}</td><td class="n"><span class="[^"]*">([-\d,\.]+)</span>', dom)
    return float(m[1].replace(",", "")) if m else None
ok = True
def check(n, c, det=""):
    global ok; ok &= bool(c); print(("PASS " if c else "FAIL ") + n + ("" if c else f"  {det}"))
try:
    srv = start()
    A = call("GET", "/api/accounts")[1]
    check("all accounts start visible (a fresh database has no hidden flags)", not any(a["hidden"] for a in A))
    t = max((a for a in A if a["type"] == "Bank" and a["txn_count"]), key=lambda a: abs(a["balance"]))
    bal = t["balance"] / 100
    dom0 = render(""); g0 = group(dom0, "Bank"); n0 = dom0.count('>hide</a>')
    check("rendered list has the account", t["name"] in dom0, t["name"])
    st, r = call("POST", f"/api/accounts/{t['account_id']}/hidden", {"hidden": True})
    check("hide: 200", st == 200 and r["hidden"] is True, r)
    dom1 = render(""); g1 = group(dom1, "Bank")
    check("hidden account disappears from list", t["name"] not in dom1)
    check(f"Bank subtotal changes by exactly its balance ({bal:,.2f})", abs((g0 - g1) - bal) < 0.005, (g0, g1))
    dom2 = render("#/?showhidden")
    check("show-hidden reveals it, dimmed, marked, with no per-row hide/unhide link (that lives in Account Details)", t["name"] in dom2 and 'class="dim"' in dom2 and "unhide</a>" not in dom2 and ">hide</a>" not in dom2 and "adet(" in dom2 and "(hidden)" in dom2)
    check("revealed subtotal includes it again", abs(group(dom2, "Bank") - g0) < 0.005, (group(dom2, "Bank"), g0))
    check("register of hidden account still works", call("GET", f"/api/accounts/{t['account_id']}/register?limit=5")[0] == 200)
    other = next(a for a in A if a["type"] == "Bank" and a["account_id"] != t["account_id"] and a["txn_count"])
    st, r = call("POST", f"/api/accounts/{other['account_id']}/transactions", dict(date="2026-10-31", payee="hid-test", amount=-1, transfer=t["name"]))
    check("hidden account still a valid transfer target (mirror created)", st == 200 and r["link_id"], (st, r))
    check("and in /api/meta", any(a["name"] == t["name"] for a in call("GET", "/api/meta")[1]["accounts"]))
    # zero-balance toggle independent: hidden count in the checkbox label is separate
    check("hide-zero and show-hidden are separate controls", "hide empty / zero-balance" in dom1 and "show hidden accounts (1)" in dom1)
    srv.terminate(); srv.wait(); time.sleep(0.5); start()
    A2 = {a["account_id"]: a for a in call("GET", "/api/accounts")[1]}
    check("flag survives server restart", A2[t["account_id"]]["hidden"] is True)
    check("still hidden in render after restart", t["name"] not in render(""))
    st, r = call("POST", f"/api/accounts/{t['account_id']}/hidden", {"hidden": False})
    dom3 = render(""); check("unhide restores it with the original subtotal", st == 200 and t["name"] in dom3 and abs(group(dom3, "Bank") - g0) < 0.005, (st, group(dom3, "Bank"), g0))
    check("bad body -> 400, bad account -> 404", call("POST", f"/api/accounts/{t['account_id']}/hidden", {"hidden": "yes"})[0] == 400 and call("POST", "/api/accounts/99999/hidden", {"hidden": True})[0] == 404)
finally:
    for p in procs: p.terminate()
print("ALL PASS" if ok else "FAILURES"); sys.exit(0 if ok else 1)
