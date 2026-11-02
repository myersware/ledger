# Install

Ledger is a small local app: python standard library + sqlite + one html file. There is nothing to `pip install`.

## Requirements
- python 3 (written and tested with 3.14; no third-party packages). A recent 3.x should do, but only 3.14 was tested.
- nothing else to start from scratch: Ledger starts **empty** and you create your own accounts (below). Optional: the separate `ledger-quicken-import` tool, if you want to bring in an extracted Quicken database (below). **To just try Ledger first, make a sample database** (below).
- a web browser.
- for the tests only: `chromium` on the PATH (some tests load pages headless).
- the `sqlite3` command line tool is optional (handy for looking at the database; the app doesn't use it).
- linux and macOS should both work; only linux was tested.

## Get the code
Clone the repo to wherever you want it. The rest of these docs call that folder `~/ledger`.

    git clone <url-or-path-of-the-ledger-repo> ~/ledger
    cd ~/ledger

A fresh clone has code only. `data/` (the working database and backups), and `imports/` (csv files) are git-ignored, so they are not in the repo. To move an existing install, copy `data/` and `imports/` as well.

## Starting from scratch
Do nothing. If there is no `data/ledger.sqlite`, the first start creates a new empty ledger and prints:

    starting empty: created a new empty ledger at <repo>/data/ledger.sqlite

Then open the app and follow [SETUP.md](SETUP.md) "Starting empty": add accounts, load the starter categories, add securities and prices, and set up scheduled bills.

**Just want to look around?** Make a small made-up ledger (about 1,700 transactions, 7 accounts, 2 investment accounts) and point a second copy of the app at it:

    python3 testfix.py /tmp/sample.sqlite
    LEDGER_DB=/tmp/sample.sqlite python3 app.py --port 8766

It refuses to overwrite an existing file.

## Bringing in Quicken data (optional)
Ledger does not read Quicken files. A separate project, **ledger-quicken-import**, takes a database extracted from Quicken and writes a ready Ledger database:

    cd ledger-quicken-import
    python3 import_quicken.py --source <path-to-extracted-database> --out ~/ledger/data/ledger.sqlite --ledger ~/ledger

It only reads the source and refuses to overwrite an existing output. Start Ledger afterwards as usual. Details are in that project's README.

## First start
From the repo folder:

    python3 app.py --init-only

This creates `data/ledger.sqlite` (an existing one is only migrated, with a backup in `data/backups/` first). `--init-only` is optional: plain `python3 app.py` does the same thing before it starts the server. More in [SETUP.md](SETUP.md).

## Start, stop, open
Start in the background (the log goes to `data/server.log`):

    cd ~/ledger
    nohup python3 -u app.py --port 8765 > data/server.log 2>&1 &

- `-u` makes python write to the log as it happens (the server also flushes by line itself). The log gets the startup line (with the database path) and, if a request crashes, the python traceback. Individual requests are not logged.
- the server binds to `127.0.0.1` only, so nothing else on the network can reach it. The default port is 8765 (`--port` changes it).
- if the port is taken you get `OSError: [Errno 98] Address already in use` in the log. Stop the old one, or use another port.

Open <http://127.0.0.1:8765/>

Stop it:

    pkill -f "app.py --port 8765"

(in a terminal without nohup, ctrl-c stops it; python prints a KeyboardInterrupt traceback, that is normal.)

Check it is running: `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8765/` prints `200`.

Pages: `#/` accounts, `#/import`, `#/spending`, `#/networth`, `#/inventory`. `#/?showhidden` opens the account list with hidden accounts shown.

## Start automatically (optional, linux with systemd)
Save as `~/.config/systemd/user/ledger.service` (`%h` is your home folder; change `WorkingDirectory` if the repo is elsewhere):

    [Unit]
    Description=Ledger

    [Service]
    WorkingDirectory=%h/ledger
    ExecStart=/usr/bin/python3 -u app.py --port 8765
    Restart=on-failure

    [Install]
    WantedBy=default.target

Then:

    systemctl --user daemon-reload
    systemctl --user enable --now ledger.service
    journalctl --user -u ledger -n 5      # the log is in the journal, not data/server.log

Remove with `systemctl --user disable --now ledger.service`. Don't run it together with the nohup copy (same port).

## Environment variables
| name | default | what it does |
|---|---|---|
| `LEDGER_DB` | `<repo>/data/ledger.sqlite` | the working database. Backups go in `backups/` next to it |
| `LEDGER_IMPORTS` | `<repo>/imports` | where the import page looks for csv files |

## Run the tests
The tests need no real data. Each one builds its own small made-up database (`testfix.py`) in a temp folder, makes a scratch working database from it, and starts its own server on its own port (8789-8799, plus 9333 for the chromium debugger in `test_ui.py`) when it needs one. They never touch your database, your `imports/` or the server on 8765. Run from the repo folder:

    for t in test_*.py; do echo "== $t"; python3 $t | tail -1; done

Each ends with `ALL PASS` (`test_e2e.py`: `N passed, 0 failed`) and exit code 0, about 25 seconds in all. Chromium (and node, for `test_ui.py`) has to be installed for 6 of them.
A test that crashes can leave its scratch server running. Look with `pgrep -af "app.py --port"` and `kill <pid>`.
