#!/usr/bin/env python3
"""Drives the real page in headless chromium (CDP): investment form (new actions), share transfer dialog, tag field, payees/tags/data pages, import shortcuts. Skipped if chromium or node is missing. Fresh empty ledger."""
import json, os, shutil, subprocess, sys, tempfile, time
from testempty import Empty, Checker
check = Checker()
if not (shutil.which("chromium") and shutil.which("node")): print("SKIP no chromium/node"); sys.exit(0)
E = Empty(8790); api = E.api; E.start(); DBG = 9333; prof = tempfile.mkdtemp()
ch = subprocess.Popen(["chromium", "--headless=new", "--no-sandbox", "--disable-gpu", f"--remote-debugging-port={DBG}", f"--user-data-dir={prof}", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(50):
        try: __import__("urllib.request").request.urlopen(f"http://127.0.0.1:{DBG}/json/version"); break
        except Exception: time.sleep(.2)
    imp = os.path.join(E.tmp, "imports"); os.makedirs(os.path.join(imp, "newbank"))
    open(os.path.join(imp, "newbank", "a.csv"), "w").write("Posted,Description,Amount\n09/03/2026,Coffee,-4.50\n")
    chk = api("POST", "/api/accounts", dict(name="Checking", type="Checking", opening_balance="1000", opening_date="2026-01-01"))[1]["account_id"]
    b1 = api("POST", "/api/accounts", dict(name="Broker A", type="Brokerage"))[1]["account_id"]; b2 = api("POST", "/api/accounts", dict(name="Broker B", type="Brokerage"))[1]["account_id"]
    api("POST", "/api/securities", dict(name="Acme", symbol="ACME"))
    api("POST", "/api/tags", dict(name="trip"))
    api("POST", f"/api/accounts/{chk}/transactions", dict(date="2026-02-01", payee="Cafe  Uno", amount="-5"))
    api("POST", f"/api/accounts/{chk}/transactions", dict(date="2026-02-02", payee="Cafe Uno", amount="-6"))
    api("POST", f"/api/accounts/{b1}/transactions", dict(date="2026-01-05", action="Buy", security="ACME", qty="10", price="5"))
    fill = lambda **k: ";".join(f"document.getElementById('{i}').value={json.dumps(v)}" for i, v in k.items())
    steps = [
      ["payees page", "#/payees", "return document.getElementById('app').innerText"],
      ["tags page", "#/tags", "return document.getElementById('app').innerText"],
      ["data page", "#/data", "return document.getElementById('app').innerText"],
      ["backup now", None, "bkNow.call?0:0; window.prompt=()=>'ui'; await bkNow(); await new Promise(r=>setTimeout(r,500)); return document.getElementById('app').innerText"],
      ["bank form tag", f"#/a/{chk}", f"await form({chk}); {fill(f_date='2026-03-01', f_payee='Hotel', f_amt='-80', f_tag='trip')}; document.getElementById('save').click(); await new Promise(r=>setTimeout(r,800)); return [!!document.getElementById('f_tag'), document.getElementById('err').textContent]"],
      ["inv form actions", f"#/a/{b1}", f"await iform({b1}); const o=[...i_act.options].map(x=>x.value); const vis=a=>{{i_act.value=a;iAct();return ['i_qpl','i_prl','i_ratl','i_aml','i_acql'].map(k=>document.getElementById(k).style.display==='none'?0:1).join('')}}; return {{o, shrsin:vis('ShrsIn'), shrsout:vis('ShrsOut'), split:vis('StkSplit'), buy:vis('Buy'), xin:vis('XIn')}}"],
      ["inv form save ShrsIn", None, f"i_act.value='ShrsIn';iAct();{fill(i_date='2026-04-01', i_sec='ACME', i_qty='5', i_amt='20', i_acq='2025-01-02')}; isave.click(); await new Promise(r=>setTimeout(r,800)); return ierr2.textContent"],
      ["inv form save split", None, f"await iform({b1}); i_act.value='StkSplit';iAct();{fill(i_date='2026-05-01', i_sec='ACME', i_ratio='2:1')}; isave.click(); await new Promise(r=>setTimeout(r,800)); return ierr2.textContent"],
      ["transfer dialog", f"#/a/{b1}", f"await xform({b1},'Broker A'); {fill(x_sec='ACME', x_qty='6', x_date='2026-06-01')}; x_go.click(); await new Promise(r=>setTimeout(r,800)); return x_err.textContent"],
      ["group row edit view", f"#/a/{b2}", f"const g=Object.values(ROWS).find(r=>r.group); await iform({b2}, g.txn_id); return [isave.style.display, ierr2.textContent.slice(0,45)]"],
      ["split edit prefill", f"#/a/{b1}", f"const g=Object.values(ROWS).find(r=>r.num==='StkSplit'); await iform({b1}, g.txn_id); return [i_ratio.value, i_act.value]"],
      ["import page", "#/import", "return document.getElementById('app').innerText.slice(0,600)+' || '+[...document.querySelectorAll('a.tg')].map(a=>a.textContent).join('|')"],
      ["csv map dialog", None, "await csvMap('newbank'); const t=cd_body.innerText; cm_date.value='Posted';cm_payee.value='Description';cm_amount.value='Amount';cm_fmt.value='mdy'; cd_save.click(); await new Promise(r=>setTimeout(r,800)); return [t.slice(0,80), cdlg.open]"],
      ["create account from slug", None, "await impNew('newbank'); const nm=n_name.value; n_type.value='Checking'; await nSave(); await new Promise(r=>setTimeout(r,800)); return [nm, location.hash]"],
      ["create security shortcut", "#/import", "await impSec('NEWSEC'); return 1"],
      ["payee merge", "#/payees", "PYS=new Set(PYL.filter(p=>/cafe/i.test(p.name)).map(p=>p.name)); window.prompt=()=>'Cafe Uno'; await pyMerge(); await new Promise(r=>setTimeout(r,500)); return document.getElementById('app').innerText.slice(0,300)"],
    ]
    sf = os.path.join(E.tmp, "steps.json"); json.dump(steps, open(sf, "w"))
    r = subprocess.run(["node", "ui_cdp.js", str(DBG), f"http://127.0.0.1:{E.port}", sf], capture_output=True, text=True, timeout=120)
    try: R = json.loads(r.stdout)
    except Exception: print(r.stdout, r.stderr); raise
    O = {k: v for k, v in R["out"]}
    for k, v in R["out"]: print("  ", k, "->", str(v)[:200].replace("\n", " "))
    check("no page errors", not R["errors"], R["errors"])
    check("payees page lists names with counts", "Cafe Uno" in O["payees page"] and "3 payee(s)" in O["payees page"])
    check("tags page", "trip" in O["tags page"]); check("data page has backups + export", "Back up now" in O["data page"] and "Download transactions" in O["data page"])
    check("back up now from the page", "ui" in O["backup now"] and "manual" in O["backup now"], O["backup now"])
    check("tag field on bank form saves", O["bank form tag"] == [True, ""] and any(r["payee"] == "Hotel" and r["tag"] == "trip" for r in api("GET", f"/api/accounts/{chk}/register?limit=50")[1]["rows"]), O["bank form tag"])
    v = O["inv form actions"]; check("investment form offers the new actions", {"ShrsIn", "ShrsOut", "StkSplit", "ReinvInt", "CGLong", "CGShort", "ReinvLg", "ReinvSh"} <= set(v["o"]), v["o"])
    print("   visibility (qpl,prl,ratl,aml,acql):", {k: v[k] for k in ("shrsin", "shrsout", "split", "buy", "xin")})
    check("ShrsIn shows qty, price, amount, acquired", v["shrsin"] == "11011"); check("ShrsOut shows only qty", v["shrsout"] == "10000"); check("split shows only ratio", v["split"] == "01100"); check("buy", v["buy"] == "11010")
    check("ShrsIn saved from the form", O["inv form save ShrsIn"] == "", O["inv form save ShrsIn"]); check("split saved from the form", O["inv form save split"] == "", O["inv form save split"])
    rows = api("GET", f"/api/accounts/{b1}/register?limit=50")[1]["rows"]; byact = {r["num"]: r for r in rows}
    check("ShrsIn acquired date + qty stored", byact["ShrsIn"]["acquired"] == "2025-01-02" and byact["ShrsIn"]["qty"] == 5, byact["ShrsIn"]); check("split stored as ratio 2", byact["StkSplit"]["qty"] == 20, byact["StkSplit"])
    check("share transfer from the dialog", O["transfer dialog"] == "", O["transfer dialog"])
    b2rows = api("GET", f"/api/accounts/{b2}/register?limit=50")[1]["rows"]; check("destination got the lots", sum(r["qty"] or 0 for r in b2rows if r["num"] == "ShrsIn") == 6 and all(r["group"] for r in b2rows), b2rows)
    check("group row form: save hidden + note", O["group row edit view"][0] == "none" and "share transfer" in O["group row edit view"][1], O["group row edit view"])
    check("split edit prefill ratio", O["split edit prefill"] == ["2", "StkSplit"], O["split edit prefill"])
    check("import page shows create-account + csv-columns links", "create this account" in O["import page"] and "csv columns" in O["import page"], O["import page"][-200:])
    check("csv mapping saved from dialog", api("GET", "/api/import/csv-columns?slug=newbank")[1]["map"]["amount"] == "Amount" and O["csv map dialog"][1] is False, O["csv map dialog"])
    check("create account from slug maps it", O["create account from slug"][0] == "Newbank" and O["create account from slug"][1].startswith("#/import"), O["create account from slug"])
    m = {x["slug"]: x for x in api("GET", "/api/import/mapping")[1]}; check("slug mapped to the new account", m.get("newbank", {}).get("account_id"), m)
    check("create security shortcut", any(x["symbol"] == "NEWSEC" for x in api("GET", "/api/securities")[1]["securities"]), api("GET", "/api/securities")[1])
    check("payee merge from the page", [p["name"] for p in api("GET", "/api/payees")[1]["payees"] if "afe" in p["name"]] == ["Cafe Uno"], api("GET", "/api/payees")[1])
finally:
    ch.terminate(); E.stop(); shutil.rmtree(prof, ignore_errors=True)
sys.exit(0 if check.ok else 1)
