"""Per-account download settings: on/off + schedule (stored on the account, saved through Account Details), plus read-only info for the
Download tab and for the bot that fetches the csv files (`python3 app.py --download-list`). The csv files come from the bot or from the built-in Plaid download (plaid.py)."""
import datetime, glob, json, os, re
import core, edits, importer

SCHEDULES = ("monthly", "on demand")


def slugify(name): return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "account"


def slug_of(db, aid):
    r = db.execute("SELECT slug FROM import_map WHERE account_id=? ORDER BY confirmed DESC, slug LIMIT 1", (aid,)).fetchone()
    return r[0] if r else None


def ensure_slug(db, aid, name):
    """The account's import slug; makes (and confirms) one from the account name when it has none, like picking it on the Import page."""
    s = slug_of(db, aid)
    if s: return s
    base = s = slugify(name); n = 1
    while db.execute("SELECT 1 FROM import_map WHERE slug=?", (s,)).fetchone(): n += 1; s = f"{base}-{n}"
    db.execute("INSERT INTO import_map(slug, account_id, confirmed, updated) VALUES(?,?,1,?)", (s, aid, datetime.datetime.now().isoformat(timespec="seconds")))
    return s


def info(db, a):
    """a = accounts row -> settings and what is on disk / in the log (read-only)."""
    slug = slug_of(db, a["account_id"])
    d = dict(enabled=bool(a["dl_enabled"]), schedule=a["dl_schedule"], slug=slug, folder=os.path.join(importer.IMPORT_DIR, slug) if slug else None,
             latest_file=None, latest_month=None, latest_file_modified=None, last_import=None)
    files = sorted(glob.glob(os.path.join(importer.IMPORT_DIR, slug, f"{slug}_*.csv")), key=lambda f: (f[-11:], f)) if slug else []  # newest month last (also _plaid_ files)
    if files:
        f = files[-1]; m = re.search(r"_(\d{4}-\d{2})\.csv$", f)
        d.update(latest_file=os.path.basename(f), latest_month=m and m[1], latest_file_modified=datetime.datetime.fromtimestamp(os.path.getmtime(f)).isoformat(timespec="seconds"))
    for b, ts in db.execute("SELECT batch, MIN(ts) FROM edit_log WHERE note='csv import' AND account_id=? GROUP BY batch ORDER BY MIN(id) DESC", (a["account_id"],)):
        if not edits.is_reverted(db, b): d["last_import"] = dict(batch=b, ts=ts); break
    return d


def enabled(db):
    """What the bot reads: every account with downloads on."""
    rows = db.execute("SELECT * FROM accounts WHERE dl_enabled=1 ORDER BY name COLLATE NOCASE").fetchall()
    return [dict(account_id=a["account_id"], account=a["name"], investment=(a["register_type"] or a["list_type"]) == "Invst",
                 **{k: v for k, v in info(db, a).items() if k not in ("enabled", "folder", "latest_file_modified")}) for a in rows]


def main():
    print(json.dumps(enabled(core.connect()), indent=1))
