# Ledger

A local, single-user, Quicken-style personal finance app: accounts, registers, investments, reports, csv and Plaid imports. Python standard library + SQLite + plain HTML/JS. No packages to install. Binds `127.0.0.1` only. Your data never leaves the machine.

**Docs:** [install](docs/INSTALL.md) · [setup (first run, imports)](docs/SETUP.md) · [limitations](docs/LIMITATIONS.md)

## Quick start
    git clone <url-or-path-of-the-ledger-repo> ~/ledger && cd ~/ledger
    nohup python3 -u app.py --port 8765 > data/server.log 2>&1 &   # run `python3 app.py --init-only` first if data/ doesn't exist
    # open http://127.0.0.1:8765/ and add your first account

A new install starts **empty**. To look around first: `python3 testfix.py /tmp/sample.sqlite` writes a small made-up ledger, then `LEDGER_DB=/tmp/sample.sqlite python3 app.py --port 8766`. See [install](docs/INSTALL.md).

- The first start creates `data/ledger.sqlite`. Schema changes (`core.migrate`, tracked by `PRAGMA user_version`) first save `data/backups/ledger-<timestamp>-pre-vN.sqlite`.
- Settings are environment variables: `LEDGER_DB` (working database), `LEDGER_IMPORTS` (csv folder).
- `data/` and `imports/` are git-ignored (so are Plaid keys and tokens, which live in `data/`).
- Coming from Quicken? Optional, and a separate project: **ledger-quicken-import** turns an extracted Quicken database into a Ledger database (`python3 import_quicken.py --source <extracted-database> --out ledger.sqlite`). Ledger itself has no Quicken code.
- Tests: `for t in test_*.py; do python3 $t | tail -1; done`. They use made-up data (`testfix.py`), need no real data, and never touch your database. Details in [install](docs/INSTALL.md).

Balances: non-investment = sum of all amounts (opening-balance row included). Investment = inferred cash + securities at last price on or before today. "Excl. stale" drops rows flagged `STALE?`; "OB-adjusted" swaps the opening-balance row for `opening_balance_check.suggested_ob_replacement_end`.

## Editing
- Register has **+ Add transaction** and per-row **edit** (the dialog also deletes). Amount is signed (negative = payment).
- Splits must sum to the amount; each split line is a category *or* a transfer. A transfer creates/updates/deletes the mirror row in the other account (`transactions.link_id`, `splits.link_txn_id`). Editing either side of a simple transfer re-syncs the other. Mirrors of split transfers are locked (edit the original). Legacy imported transfers (unlinked) are synced only when exactly one obvious counterpart exists (same date, opposite amount); otherwise the response carries a warning.
- Every change is recorded in `edit_log` (before/after JSON per row, one batch per action). Undo a batch: `python3 app.py --revert <batch>`. A later edit of the same rows, an unknown batch, or an already-reverted batch gives a plain error message.
- Writes only touch the working DB. API writes require `Content-Type: application/json` and a 127.0.0.1/localhost Host header.
- Test: `test_e2e.py`.

## Hidden accounts
`accounts.hidden`. Hide/unhide in Account Details. The account list has a "show hidden accounts" toggle (`#/?showhidden` opens with it on), independent of "hide empty / zero-balance". Hidden accounts are excluded from the list and group subtotals unless revealed (then dimmed and included). They remain transfer targets and keep working registers. Test: `test_hidden.py`.

## CSV import (`#/import`)
Drop `imports/<slug>/<slug>_<YYYY-MM>.csv` files (columns and examples in [setup](docs/SETUP.md) section 6). `importer.py` + `transactions.ext_id` and `import_map`.
- **Mapping** slug → ledger account is stored in `import_map`. Pick the account on the Import page; picking maps and confirms. Nothing imports for an unmapped slug.
- **Preview** (`GET /api/import/preview`, read-only): each row is `new`, `matched` (same amount, date ±3 days, one-to-one against existing rows that have no external id), `duplicate` (txn_id already on a transaction or repeated in the files) or `deferred`. Also lists ledger rows in the files' months that no file row matched.
- **Import** (`POST /api/import/run`): ONE `edit_log` batch: inserts `new` rows (source `import`, `ext_id` = txn_id) and stamps `ext_id` on `matched` rows. No transfers are created. Category: last category used for the same payee in that account, else a short Plaid→ledger map, else none.
- **Revert** (`POST /api/import/revert {batch}` or `python3 app.py --revert BATCH`); refused if a later live edit touched the same rows.
- **Investment accounts** (extra columns `type,security,quantity,price`): paired rows are collapsed into ONE transaction: buy + cash row of the same security = `ReinvDiv`; sweep buy (price 1.00, qty = dollars) + cash row = `Div` on the cash row's security; sweep sell + blank cash row = `XOut`/`XIn`; lone buy/sell = `Buy`/`Sell`; cash row with a security = `Div`; fee = `MiscExp`. `ext_id` of a collapsed group is the comma-joined ids. Matching against existing rows: same action family + ticker + quantity (or amount for cash-type actions), date ±3 days. Unknown security or unsupported type = `deferred` (with reason).
- Test: `test_import.py` (writes its own csv files in a temp folder).

- **Shortcuts in the preview**: a deferred row with an unknown security has *create security X* (ticker as name; rename it on the Securities page), a slug with no account has *create this account* (pick a type, it is mapped for you), and in the Plaid panel each unlinked Plaid account has *create ledger account for …* (`POST /api/plaid/create_account`; the opening balance is not set, do that in Account Details).
- **Generic bank csv** (any file in `imports/<slug>/` that has no Plaid `transaction_id`): the *csv columns…* link next to the slug maps the date, payee, amount (or debit + credit), memo, date format and a sign flip. The map is saved per slug (`import_map.csv_map`). Each row gets a stable id from slug + date + amount + payee + nth identical row, so re-importing a file or an overlapping statement shows duplicates, not new rows. Rows that fail to parse are skipped with a warning. Test: `test_import_extras.py`.

## Reconcile and cleared status
- Uses the status column: `transactions.cleared_status` (`cleared` = c, `reconciled` = R, NULL = uncleared). The `reconcile` table holds the last statement per account (date, balance, rows marked, batch).
- Register: the Clr cell toggles **c** on a row (logged as a `clear toggle` batch); R rows can't be toggled (undo the reconcile instead). Non-investment registers link to **Reconcile…** (`#/rec/<id>`).
- Reconcile page: enter statement end date + ending balance (register sign: money owed on a card is negative), tick rows (c rows are pre-ticked), see beginning (already-R rows up to that date) + ticked = cleared balance and the difference live. **Finish** is enabled only at difference 0.00 (checked again server-side in cents, 409 otherwise); it marks ticked rows R, sets unticked c rows back to uncleared and records the statement. Balances never change (only status). **Undo last reconcile** reverts that one batch (refused if a later edit touched those rows). Not offered for investment accounts.
- Test: `test_reconcile.py`.

## Investment transactions
- Investment registers have **+ Add investment transaction**, edit and delete for every action: Buy, Sell, Div, ReinvDiv, ReinvInt, ReinvLg, ReinvSh, CGLong, CGShort, IntInc, XIn/XOut, MiscExp, **ShrsIn**, **ShrsOut** and **StkSplit**. Amount is entered positive; the action gives the direction (`core.effect`). Qty × price fills the amount if blank. Security by name or ticker. ShrsIn takes a qty, a total **cost basis** (or price) and an **acquired date** (stored as `D` in `invst_json`): the lot keeps that date and cost, so the portfolio's lots, age and long/short term follow it. ShrsOut takes a qty and removes the oldest lots first (API: optional `lot` = txn id of the buy). StkSplit takes a **ratio new:old** (`2:1`, `1:10` reverse; API `ratio`); it is stored as `Q = 10 × ratio`, so editing an old export split shows `Q/10`.
- XIn/XOut can name a bank-type **or another investment** account: a linked mirror row is created/updated/deleted there (`link_id`). Plain XIn/XOut pairs (bank↔investment, investment↔investment) can be edited from either side. The bank form can also transfer to an investment account (becomes an XIn/XOut there). Editing an imported cash-transfer row with an unlinked counterpart keeps its transfer label and leaves the other side alone (warning if date/amount changed).
- **Transfer shares…** button on an investment register (`POST /api/investments/transfer-shares`, `invest.py`): moves a quantity of a security to another investment account. One ShrsOut at the source and one ShrsIn per consumed lot at the destination, each keeping the lot's original acquired date and cost; the oldest lots go first (or the one named by `lot`). One edit_log batch; the pair is edited as a group (edit refused, delete removes both sides, revert undoes it). Cash does not move: use XOut/XIn with a transfer account.
- Scheduled investment transactions: `scheduled.py` accepts `inv:{action, security, qty, price, amount, ratio, transfer}` for an investment account (API; the Scheduled page lists and enters/skips/deletes them, creating them is API-only for now).
- Selling more than held saves but warns. The register shows a Holdings line.
- Tests: `test_invest.py`, `test_invest_actions.py`.

## Payees, tags, backups, export (`names.py`, `backups.py`, `exports.py`)
- **Payees** page (`#/payees`, `GET /api/payees`): every payee with its transaction, memorized and scheduled counts; **rename** (to an existing name = merge), **merge ticked…**, **delete** (unused only). A rename or merge rewrites transactions, memorized and scheduled items and the payee list in one edit_log batch.
- **Tags** page (`#/tags`): add, rename (cascades to transactions, splits and memorized items) and delete (refused while used). The transaction form has a **Tag** field (pick an existing tag; unknown tags are refused). Splits carry tags through the API.
- **Data** page (`#/data`): **Back up now** (`POST /api/backups`, a dated copy in `data/backups/`, optional label), the list of backups with schema version, **restore…** (`POST /api/backups/restore {name, confirm:true}`: integrity-checks the file, backs up the current database first as `pre-restore`, swaps the file atomically, migrates it if it is older; restart not needed and a crash mid-restore leaves the old database in place).
- **Export csv** (`GET /api/export/transactions.csv?account=<id|all>&from=&to=`, `GET /api/export/accounts.csv`): one row per transaction, or per split line; includes transfer, tag, security, qty, price, txn id and split position. Both are downloads, nothing is written.
- Tests: `test_names_backup_export.py`, `test_ui.py` (drives the real page in headless chromium; skipped if chromium or node is missing).

## Portfolio (read-only)
**Portfolio** (`#/portfolio`): per security (or per account) shares, latest price and its date, market value, cost basis, gain/loss and % of portfolio, with each account's cash and a totals row. Click ▸ to expand a security's open lots (account, acquired, shares, cost/share, basis, value, gain, short/long by date only). Sortable columns; hidden accounts excluded unless "show hidden". ⚠ marks a price older than 7 days, a holding with no price, a negative position, or lots with no cost in the data (counted as 0). `GET /api/portfolio?group=security|account&hidden=1`.
Shares and values come from the same code as the account balances. Lots are built oldest-first (FIFO) from the transactions: Buy/Reinv/ShrsIn add a lot, Sell/ShrsOut use the oldest first, splits rescale. Quicken's own lot picks are not carried over, and shares moved in from another account carry the transfer date. Test: `test_portfolio.py`.

## Reports (read-only)
Open **Spending** (`#/spending`) or **Net worth** (`#/networth`) from the header.

**Spending by category**: presets this month / last month / YTD / last year / custom. Category hierarchy (split on `:`) with subtotals, Income and Expense side by side. Split lines count at the split amount. Transfers, investment accounts and "Opening Balance" rows are excluded. Uncategorized lines appear as `(uncategorized)`. Refunds reduce expense. Options: include hidden accounts, exclude rows whose merge note has `STALE?`. Click a category to list its transactions (includes subcategories).

**Net worth**: month-end points (year-end for ranges > ~10.4 years, or choose), capped at today. Assets minus liabilities (account types CCard, Oth L are liabilities). Investment accounts = cash + holdings at the last price on or before the date. Inline SVG chart plus a table by account type. Today's value equals the sum of account-list balances, plus one **Household items** line when that option is on (see Household inventory).

Endpoints: `GET /api/reports/spending`, `/api/reports/spending/transactions`, `/api/reports/networth` (params `from`, `to`, `hidden=1`, `exclude_stale=1`, `category`, `section`, `gran`, `household=0|1` to override the saved setting). Test: `test_reports.py` (checks against direct SQL, account balances, register running balances, and that nothing is written).

## Household inventory (schema v10)
**Inventory** (`#/inventory`): a list of valuables (musical instruments, electronics, jewelry...). Each item has name, category, make, model, serial number, location/room, purchase date and price, current value with its date, notes, an optional link to the purchase transaction and any number of photos or receipts. The list is searchable, sortable and filterable by category (click a category), with totals of purchase price and value. Value = current value, else purchase price (marked `*`).
- **Link to purchase…** in the item dialog searches transactions (payee, memo or amount; not investment accounts) and fills the purchase date and price from the one you pick. The link is only a reference; it never changes the transaction. If the transaction is later deleted the link just shows nothing.
- **Photos and receipts** are plain files in `data/inventory/<item id>/` (git-ignored, on this computer only; `GET /api/inventory/<id>/files/<name>` shows images and PDFs inline, anything else downloads, and an uploaded html file is never rendered). Max 25 MB per file, 40 per item. Uploading, viewing and deleting a file are done in the item dialog. Files are not in the edit log: deleting an item keeps its folder unless you answer "also delete the files"; ids are never reused (`AUTOINCREMENT`), so undoing the delete brings the files back with the item.
- **Net worth**: the setting `household_networth` (table `settings`, default **on**) adds one **Household items** line to assets, using each item's value from its purchase date (undated items always). It is an estimate, not an account: account balances, the account list and every other net worth line are unchanged, and unticking *include household items* on the Net worth page (or the Inventory page) removes it. The choice is saved in the database. `GET/PUT /api/settings {household_networth: true|false}`.
- **CSV export** for an insurer: `GET /api/export/inventory.csv` (Data page: *Download household inventory*): one row per item with `value_used`, the purchase transaction id and the number of attachments.
- API: `GET/POST /api/inventory`, `GET/PUT/DELETE /api/inventory/<id>` (`DELETE {delete_files:true}` also removes the files), `GET /api/inventory/txn-search?q=`, `GET/POST /api/inventory/<id>/files` (`{name, data: base64}`), `GET/DELETE /api/inventory/<id>/files/<name>`. Every item change is an `edit_log` batch (note `inventory`), so `app.py --revert <batch>` undoes it; the edit never marks the linked account as edited. Migration v10 adds the `inventory_items` and `settings` tables after the usual dated backup.
- Tests: `test_inventory.py` (backend, files, csv, net worth toggle, migration), plus the inventory steps in `test_ui.py`.

## Starting empty, catalog, scheduled
- **Starting from scratch** (`core.create_schema`): `POST /api/accounts` (+ new account, optional opening balance), categories (`catalog.py`: create, rename with cascade, delete guard, starter list), securities and manual prices, and scheduled transactions (`scheduled.py`, schema v8; enter/skip/auto-enter at start and page load). All writes are `edit_log` batches. Walkthrough: docs/SETUP.md "Starting empty". Tests: `test_fresh_install.py`, `test_scheduled.py` (helper `testempty.py`).

## Account Details (`account_details.py`)
- Open: ⚙ next to an account in the list (or double-click the row), "⚙ details" in the register header, or `#/a/<id>/details`. API: `GET/PUT /api/accounts/<id>/details`, `DELETE /api/accounts/<id>`.
- Fields (Quicken General tab): name, description, type, institution, account number, customer ID, contact, phone, home/activity/other page (Go opens http/https only), comments, hidden; investment accounts also tax deferred, single mutual fund, tracking method (a note only); credit limit for credit cards. Tax Schedule and Fees are omitted (no data behind them).
- Migration v6 adds the columns to `accounts` and fills what it can: description / credit limit are taken as they are; type, institution, account number (from `XX1234` in the name) and tax deferred are *guessed from the name* and listed in `accounts.prefilled` until the first save.
- Every save is one `edit_log` batch (note `account details`). Account numbers are masked on save (6+ digit runs keep the last 3). Rename also rewrites the `transfer_account` text on transfers/splits. A type change moves `accounts.register_type`/`list_type` and every row's `register_type` together; switching between investment and non-investment is refused when the account has transactions. Delete Account works only for an account with no transactions, transfers in, import mapping, reconcile records, memorized items or holdings; it is logged, so revert brings the row back.
- **Download tab** (schema v7): per account, downloads on/off and schedule `monthly` or `on demand`; read-only import slug, latest file in `imports/<slug>/` and last (non-reverted) import. Saved with the other details (`dl_enabled`, `dl_schedule`), one `edit_log` batch, revertable. Turning downloads on for an account with no import mapping creates one from the account name (`Main Checking` -> `main-checking`, confirmed, like picking it on the Import page). There is no download button: the app never fetches anything. Whatever does (a script, an assistant) reads `python3 app.py --download-list` (JSON of the enabled accounts: `account_id, account, investment, slug, schedule, latest_file, latest_month, last_import`) or `GET /api/downloads`. Migration v7 turns downloads on (monthly) for accounts that already have an import mapping. Test: `test_downloads.py`.
- Test: `test_account_details.py` (builds its own older database to migrate).

## Other files
- The data repairs, balance check and tests that belong to one person's data are not in this repo.
- `ui_cdp.js`: tiny headless-chromium driver used by `test_ui.py`.
- `testfix.py`: the made-up ledger used by the tests; `python3 testfix.py NEW.sqlite` writes it as a sample.
