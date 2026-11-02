"""Back up now, list data/backups, restore. A restore first backs up the current database ('pre-restore'), then swaps the chosen backup in
atomically (copy to a temp file next to the database, then os.replace), so a crash part-way leaves the old database in place."""
import datetime, os, re, sqlite3
import core
from edits import ApiError

NAME = re.compile(r"ledger-\d{8}-\d{6}-[A-Za-z0-9_.-]+\.sqlite")


def _dir(): return os.path.join(os.path.dirname(core.DB_PATH), "backups")


def _info(f):
    p = os.path.join(_dir(), f); st = os.stat(p); ver = None
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True); ver = c.execute("PRAGMA user_version").fetchone()[0]; c.close()
    except sqlite3.Error: pass
    return dict(name=f, size=st.st_size, when=datetime.datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"), schema=ver, kind=f[:-7].split("-", 3)[3])


def bk_list(m, qs):
    os.makedirs(_dir(), exist_ok=True)
    return dict(dir=_dir(), backups=sorted((_info(f) for f in os.listdir(_dir()) if NAME.fullmatch(f)), key=lambda x: x["name"], reverse=True))


def bk_create(m, d):
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", str(d.get("label") or "").strip())[:30].strip("-")
    p = core.backup("manual" + (f"-{label}" if label else ""))
    return dict(_info(os.path.basename(p)), path=p)


def bk_restore(m, d):
    name = str(d.get("name") or "")
    if d.get("confirm") is not True: raise ApiError(400, "restore needs confirm: true")
    if not NAME.fullmatch(name) or not os.path.exists(os.path.join(_dir(), name)): raise ApiError(404, "no such backup")
    src_path = os.path.join(_dir(), name)
    try:  # check it before touching anything
        c = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
        ok = c.execute("PRAGMA quick_check").fetchone()[0] == "ok" and c.execute("SELECT 1 FROM sqlite_master WHERE name='transactions'").fetchone()
        c.close()
    except sqlite3.Error: ok = False
    if not ok: raise ApiError(400, "that backup file is not a usable Ledger database; nothing was changed")
    pre = core.backup("pre-restore")
    tmp = core.DB_PATH + ".restore-tmp"
    if os.path.exists(tmp): os.remove(tmp)
    s = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True); t = sqlite3.connect(tmp); s.backup(t); t.close(); s.close()
    os.replace(tmp, core.DB_PATH)
    core.migrate(backup=False)   # an older backup is brought up to the current schema (the pre-restore backup above is the safety copy)
    return dict(restored=name, previous_saved_as=os.path.basename(pre))


ROUTES = [("GET", r"/api/backups", bk_list), ("POST", r"/api/backups", bk_create), ("POST", r"/api/backups/restore", bk_restore)]
