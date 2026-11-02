"""Helper for the from-scratch tests: a brand-new empty ledger and a scratch server on it."""
import json, os, subprocess, sys, tempfile, time, urllib.error, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
class Empty:
    def __init__(self, port):
        self.tmp = tempfile.mkdtemp(); self.port = port; self.db = os.path.join(self.tmp, "data", "ledger.sqlite")
        self.env = dict(os.environ, LEDGER_DB=self.db, LEDGER_IMPORTS=os.path.join(self.tmp, "imports")); self.p = None
        self.init = subprocess.run([sys.executable, "app.py", "--init-only"], env=self.env, cwd=HERE, capture_output=True, text=True)
    def start(self):
        self.p = subprocess.Popen([sys.executable, "app.py", "--port", str(self.port)], env=self.env, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(50):
            try: urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/meta"); return
            except Exception: time.sleep(.2)
        raise SystemExit("server did not start")
    def stop(self):
        if self.p: self.p.terminate(); self.p.wait()
    def api(self, method, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, data=json.dumps(body if body is not None else {}).encode() if method != "GET" else None, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r: return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")
class Checker:
    ok = True
    def __call__(self, name, cond, det=""): self.ok &= bool(cond); print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  {det}"))
