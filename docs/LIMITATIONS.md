# Limitations

What Ledger can't do. This comes from reading the code and running it. If a feature isn't described in the README or these docs, assume it doesn't exist.

## How it runs
- **Local only.** The server binds to `127.0.0.1`. Nothing is uploaded and nothing is reachable from other machines.
- **Single user, no login.** Anyone who can open a browser on the computer (or use its network stack) can open Ledger and edit the data. There is no user system, no password, no encryption of the database file. The only guard: write requests must be JSON and have a `127.0.0.1`/`localhost` Host header. Keep the folder on a disk you trust (and back it up).
- **One working database, one process.** No sync, no multi-device use, no locking beyond what sqlite does.
- **You start it yourself** (or set up the optional systemd unit in [INSTALL.md](INSTALL.md)). If the server isn't running, the page doesn't load.
- **Single currency**, no foreign-currency accounts.
- Only tested on linux with python 3.14 (and chromium for the tests).

## Getting data in
- **Starts empty.** You build your own accounts ([SETUP.md](SETUP.md) "Starting empty"). Ledger doesn't read Quicken (QIF/QDF) files; the optional, separate `ledger-quicken-import` tool loads a database extracted from Quicken. Any tool that writes the same tables can do the same.
- **No bank login. Downloads are optional and go through Plaid only.** Without Plaid keys the app never talks to anything: new transactions come only from csv files in `imports/` ([SETUP.md](SETUP.md) section 6), which you (or a script or assistant) put there. With your own Plaid keys ([SETUP.md](SETUP.md) section 8) the app can download csv files itself. Ledger never sees your bank password (you enter it in Plaid's window), nothing is imported without the preview and your confirmation, and the transactions travel only between your computer and Plaid. Plaid availability, cost, institution support (Fidelity may need Plaid's approval, Schwab can take days) and login expiry are outside Ledger's control. The Plaid download is untested against real banks until you try it.
- **Download settings are only settings.** The Download tab (Account Details) records, per account, whether downloads are on and whether they are monthly or on demand, and shows the latest file and last import. Accounts linked to Plaid get a "Download now" button; for the rest, whatever pulls the files has to read the settings itself (`python3 app.py --download-list`) and honour them.
- The import only handles csv files in `imports/<slug>/<slug>_<YYYY-MM>.csv` with the columns listed in SETUP. No QIF/QFX/OFX import, and no other csv layouts.
- Imports **never create transfers**, and only guess categories from the same payee's history plus a short built-in map of Plaid-style bank categories (`PLAID_MAP` in `importer.py`). Most new rows will be uncategorized.
- Investment imports collapse paired rows into one transaction. Rows with an unknown security or a type other than buy/sell/cash/fee are left `deferred`.
- No way to merge a newer Quicken extraction into a working database: the importer makes a new database, and edits made in the app don't carry over.
- When an account is first set up, its institution, type and account number are guessed from the account name using a list of institution names in `core.py` (`INSTITUTIONS`, a short list of common banks and brokers). Other institutions come out blank and you fill them in by hand.

## What it doesn't do (and Quicken does)
- **No online bill pay, no payments, no connection to anything that moves money.**
- **No budgets, no alerts or notifications.** Scheduled transactions exist (see below) but nothing nags you outside the app. Quicken's own scheduled items are not imported. Memorized transactions that an import carries along aren't used.
- **No tax schedule, no fees fields** (omitted from Account Details: no data behind them). No tax reports.
- **Accounts, categories, securities and prices can be created from screens; tags and payees can't** (payees are free text on each transaction, and there is no tag field when adding or editing a transaction). Accounts can be edited, hidden and deleted when empty. A new account's opening balance is an ordinary transaction.
- **Scheduled transactions are simple:** one occurrence is entered at a time (auto items catch up one per missed date, up to 400 per item per start), no "every N days/months" other than the six listed frequencies, no variable-amount estimates, no pay-bills-online, and no scheduled *investment* transactions. Entering or skipping happens only when you press the button or when the app starts or a page loads: a closed app enters nothing.
- **No printing, no export of the ledger** to csv/QIF.
- **Reports are two only:** spending by category and net worth over time. No income/expense by payee, no cash flow, no investment performance.

## Household inventory
- **Values are your own estimates**: no depreciation, no price lookup, no history of past values (only the latest current value and its date). The Net worth line uses that one value for every date from the purchase date on.
- **No sold / disposed status** and no multiple quantities per item: delete a sold item (or enter each one separately).
- **Photos and receipts are files, not database rows.** They are not in the Data page backups or `edit_log`: copy `data/inventory/` yourself when you back up or move. Restoring an old database backup can leave files of items that no longer exist (and a re-used item number may show them); delete them by hand.
- Uploads are whole files in memory (max 25 MB each), no thumbnails are generated, and only images and PDFs open in the browser.

## Investments
- The **tracking method is a note only** (Simple/Complete in Account Details). The app always keeps positions and transactions.
- Holdings are valued at the **last price in the database** (the last price you entered). There is no price download; prices are typed in on the Securities page (or recorded from a Buy/Sell price), so investment balances and net worth go stale after that date.
- Investment accounts can be edited with every action (Buy, Sell, Div, Reinv*, CG*, IntInc, XIn, XOut, MiscExp, ShrsIn, ShrsOut, StkSplit). Short sales and options (CvrShrt, Short, Opt*) and anything else imported stay locked. A share transfer between accounts is one group: delete it, don't edit it. The scheduled Investment form is API-only. A ShrsOut lot can be chosen through the API (`lot`) but the form always takes oldest-first.
- **Lots and cost basis exist but there is no realized-gain report.** The Portfolio page builds FIFO lots (dates and cost) from the transactions; Quicken's own lot choices are not carried over, and imported shares moved between accounts have the transfer date unless you enter the acquired date. This is not tax or investment advice.
- Investment balances are cash inferred from the actions plus securities at the last price. The cash rules are a best reading of QIF semantics, not Quicken's own number.
- Reconcile is not offered for investment accounts.
- Selling more than held saves, with a warning.

## Payees, tags, backups, csv
- Payee merge is by exact name (case-sensitive, spaces matter); there are no payee rules or auto-categorise-by-payee beyond memorized transactions.
- Tags are one per transaction or split; there is no tag report yet.
- Restoring a backup replaces the whole database: anything entered since is only in the `pre-restore` backup. Backups are never pruned, delete old ones from `data/backups/` yourself.
- The csv export is a download for other tools; there is no csv *re-import* of it. Generic bank csv import needs the column map set once per slug; bank csv files with a split/multiple-currency layout are not handled.
- Plaid create-account does not set an opening balance.

## Reconciled history
- Transactions that were reconciled (`R`) in Quicken come in as reconciled. Ledger has **no record of the statements** behind them (its `reconcile` table starts empty), so your first reconcile in Ledger counts all older `R` rows as the beginning balance. That only works if the old Quicken history was right.
- `R` rows can't be cleared with the Clr toggle. The only way is "Undo last reconcile", and that only reaches Ledger's own most recent reconcile.
- Nothing stops you editing or deleting a reconciled row (only the Clr toggle is blocked), and that can quietly break an old reconcile.

## Data quality
- Ledger shows what is in the database. Opening-balance rows, duplicates from merging several files, and missing history all show up as wrong-looking balances. Compare a few accounts with a statement before you trust them.
- The only repair tools are your own edits in the app (all undoable).

## Verify and tests
- The tests need no real data and no backups (they use a made-up database from `testfix.py`).
- A test that crashes can leave a scratch server running on port 8793-8799 (`pgrep -af "app.py --port"`, then `kill`).
- The tests prove the app on made-up data. They don't prove your own data is right: compare with statements.
