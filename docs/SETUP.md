# Setup (first run)

Assumes you did [INSTALL.md](INSTALL.md) up to "first start".

## Starting empty
With no database yet the app creates an empty `data/ledger.sqlite` (current schema, no backup needed since there is nothing to lose) and everything is built from the screens. Suggested order:

1. **Accounts page > + new account.** Pick a type (checking, savings, credit card, brokerage, IRA, ...), a name, and optionally an opening balance and date. It becomes one "Opening Balance" transaction (for a credit card or loan, enter the amount you owe; for a brokerage, the opening cash). Names are unique, ignoring case.
2. **Categories** (header link). First run offers a small generic US household starter list (income, housing, food, transport, health, ...). Add your own as `Name` or `Parent:Child`, rename (this follows through every transaction, split, memorized and scheduled item that uses it) or delete (refused, with counts, while a category is in use or has subcategories).
3. **Securities** (header link), only if you track investments: name, ticker, type. A security with transactions can't be deleted. Click *prices* on a row to type in a price (date, price); a Buy or Sell that carries a price also records it, unless a newer price exists. There is no price download, so holdings are valued at the last price you entered.
4. **Transactions**: the `+ Add transaction` button in each register, as before.
5. **Scheduled** (header link): bills and deposits. Pick account, payee, category (or splits, or a transfer), amount, how often (once, weekly, every 2 weeks, monthly, quarterly, yearly), next date, optional end date or number of times, and *remind me* or *enter automatically*. Or press **repeat** on any bank-register row to schedule it. The page lists what is due (overdue in red) and the next 30 days; **enter** makes a normal transaction (you can change the amount) and moves to the next date, **skip** just moves on, **edit** / **delete** change the schedule (entered transactions stay). Month-end dates stick to the day you chose (a bill on the 31st goes to Feb 28/29, then back to the 31st).
   There is no background process. *Enter automatically* items are entered when the app starts and whenever the accounts or scheduled page loads; the page shows what was entered, and the accounts page shows a due/overdue count.

Every one of these changes is written to `edit_log` and can be undone: `python3 app.py --revert <batch>` (the batch name is returned by the API and listed on the Scheduled page for entries). A schedule's enter is one batch: undoing it removes the transaction and puts the schedule back.

## 1. The working database
Everything lives in one sqlite file, `data/ledger.sqlite` (or the path in `LEDGER_DB`); all edits go there.

- it is created on first start. If it exists, Ledger uses it as it is.
- to start over, stop the server and move `data/ledger.sqlite` out of the way (don't delete it: it holds your edits). The next start makes a new empty one.
- to try a second copy without touching the first: `LEDGER_DB=/tmp/test/ledger.sqlite python3 app.py --init-only`.
- data from Quicken can be loaded by the optional, separate `ledger-quicken-import` tool, which writes a new Ledger database for you to start from.

## 2. Migrations and backups
The schema version is stored in sqlite as `PRAGMA user_version`. The current version is 9.
- when the version is older than the code, the server (or `--init-only`) first copies the database to `data/backups/ledger-<timestamp>-pre-vN.sqlite`, then migrates. One backup per start that needs a migration.
- a fresh copy jumps from 0 to 9 in one go, so it gets one `pre-v9` backup (an empty start gets none: nothing to lose). After you pull newer code that adds a schema version, the next start makes another backup first.
- imports and the one-off fix scripts also take a backup first (`pre-import-...`, `pre-fix-...`).
- check the version: `sqlite3 data/ledger.sqlite "pragma user_version"`.
- to go back to an older state: stop the server and put a backup file back as `data/ledger.sqlite`. Everything done after that backup is lost.

What the migrations add (see `core.py`): v1 indexes, v2 `edit_log` + transfer link columns, v3 `accounts.hidden`, v4 `transactions.ext_id` + `import_map`, v5 `reconcile`, v6 the account-details columns, v7 the download settings, v8 the `scheduled` table (scheduled transactions), v9 `scheduled.inv_json` (scheduled investment transactions) and `import_map.csv_map` (saved csv column mapping).

You can also make a backup yourself any time: **Data** page > **Back up now** (or `POST /api/backups`). **restore…** on the same page backs up the current database first, then swaps in the chosen one, without a restart.

## 3. Every edit can be undone
Add/edit/delete, clear toggles, reconcile, imports and account-details saves each write one batch to the `edit_log` table (before and after copies of each row). Undo a batch:

    python3 app.py --revert <batch-id>

It prints `reverted N edits of <batch>`. It refuses with a plain message (`revert failed: ...`) when the batch doesn't exist, was already reverted, or when a later edit touched the same rows (undo the newest first). The batch id is shown by the app (import batches, reconcile) and is in `edit_log.batch`:

    sqlite3 data/ledger.sqlite "select batch, note, min(ts) from edit_log group by batch order by min(id) desc limit 10"

## 4. Hiding accounts
- hide or unhide one account: open its account details (below) and tick "Hide this account".
- the account list has a "show hidden accounts" checkbox. `#/?showhidden` opens the list with it on.
- hidden accounts are left out of the list, group totals and reports (Spending and Net worth have an "include hidden" option). They still work as transfer targets and their registers still open.
- a fresh copy has no hidden accounts (a new database has no hidden flags).

## 5. Account details
Open it from the ⚙ next to an account in the list, by double-clicking the row, from "⚙ details" in the register header, or with `#/a/<id>/details`.
- fields: name, description, type, institution, account number, customer id, contact, phone, three web addresses, comments, hidden. Investment accounts also have tax deferred, single mutual fund and tracking. Credit cards have a credit limit.
- the **Download** tab has the download settings: see section 7.
- an imported database may only have name, description and credit limit. On migration to v6, type, institution, account number (from `XX1234` in the name) and tax deferred are *guessed from the account name* and flagged "please check" until the first save. The guesses use a list of institution names in `core.py` (`INSTITUTIONS`); edit it if yours aren't recognised.
- account numbers are masked on save (any run of 6+ digits keeps the last 3).
- rename also rewrites the account name on transfers. Changing type is refused when it would turn an investment account into a non-investment one (or the reverse) and the account has transactions.
- delete account works only for an account with no transactions, transfers, import mapping, reconcile records or holdings.
- every save is one undoable batch (note `account details`).
- "tracking" is only a note. The app always keeps positions and transactions.

## 6. Importing new transactions from csv
Ledger can add new transactions from csv files you drop into `imports/`. This is optional. Where the files come from is up to you: download them from your bank and reshape them, let a script or assistant write them (section 7), or let Ledger fetch them through Plaid (section 8).

**Layout:** one folder per account, one file per month. The folder name is a "slug" you choose (letters, digits, dashes):

    imports/<slug>/<slug>_<YYYY-MM>.csv        e.g. imports/my-checking/my-checking_2026-09.csv

**Columns** (header row required, utf-8):

    date,payee,amount,memo,category,txn_id,account,source

- `date` `YYYY-MM-DD`. `amount` signed as in the register: money out is negative, money in is positive (a card purchase is negative).
- `txn_id` must be present and **unique and stable** for each transaction (the bank's own id is ideal). It is how Ledger avoids importing the same row twice. Rows without it, or with a bad date or amount, are skipped with a warning.
- `account` should equal the slug (a different value only gives a warning). `source` is free text. `memo` and `category` can be empty. `category` is the *bank's* category name (Plaid style, e.g. `FOOD_AND_DRINK_RESTAURANT`); it is only looked up in the built-in map described below, never used as a ledger category directly.
- an example:

      date,payee,amount,memo,category,txn_id,account,source
      2026-09-03,Corner Cafe,-4.50,,,tx-0001,my-checking,manual
      2026-09-05,Payroll,2500.00,,,tx-0002,my-checking,manual

- **Investment accounts** add four columns: `type,security,quantity,price`. `type` is `buy`, `sell`, `cash` (dividend/interest/cash move) or `fee`; anything else is left `deferred` with a reason. `security` is a name or ticker that already exists in your ledger. Paired rows are collapsed into one transaction: a `buy` + a `cash` row of the same security = reinvested dividend; a sweep buy (price 1.00, quantity = dollars) + a `cash` row = dividend; a sweep `sell` + a `cash` row with no security = cash in/out. A lone buy/sell/cash/fee row is one transaction.

**On the Import page (`#/import`)** every folder under `imports/` is listed with an account dropdown. Pick the ledger account for each slug; picking one saves the mapping and confirms it (nothing is imported for a slug with no confirmed account).

How an import works:
1. **Preview** (read-only). Each row is `new`, `matched` (same amount, date within 3 days, to an existing row that has no bank id yet), `duplicate` (that `txn_id` is already in the ledger) or `deferred` (investment rows that don't fit, with the reason). Also listed: ledger rows in those months that no file row matched.
2. **Import** button: one batch. New rows are added; matched rows only get the id attached (so they won't be matched twice); duplicates are skipped. The batch appears under "Import batches".
3. **Revert** next to the batch undoes it (or `python3 app.py --revert <batch>`). It is refused if a later edit touched the same rows.

The category of a new row is the last one used for the same payee in that account, else a short built-in bank-category map (`PLAID_MAP` in `importer.py`, for Plaid-style category names), else blank. An import never creates transfers: a transfer between two of your accounts arrives as one row in each, uncategorized. Check them by hand.

## 7. Automating the downloads (optional)
**Unless you set up Plaid (section 8), the Ledger app never connects to a bank or downloads anything.** It only reads csv files already in `imports/`. If you use an assistant (or any script) that can pull bank csv files, point it at `~/ledger/imports/<slug>/` with the layout above. Nothing is added to the ledger until you open `#/import`, look at the preview and press Import.

**Download settings (Account Details > Download tab).** Per account you can record:
- *Download new transactions for this account*: on or off.
- *Schedule*: `monthly` (included in the scheduled monthly pull) or `on demand` (only when you ask for it).
- read-only: the account's **import slug**, its folder, the **latest file** in that folder (newest `<slug>_<YYYY-MM>.csv`, with its modified time) and the **last import** (the newest import batch that touched the account and has not been reverted).

Turning downloads on for an account that has no import slug creates one from the account name (`Brokerage XX1` -> `brokerage-xx1`, with `-2` etc. if taken) and confirms it, the same as picking the account on the Import page. Turning it off later keeps the slug. Saves are one undoable batch like any other detail (`python3 app.py --revert <batch>`; a revert does not remove a slug that was created). When you upgrade from an older version, accounts that already have an import mapping start with downloads on, monthly; everything else starts off.

Without Plaid there is no "download now" button, because the app can't reach your bank or any connector; the settings are then only there to be read by whatever does the pulling:

    python3 app.py --download-list        # run from the repo folder (or GET /api/downloads while the server runs)

prints JSON for the accounts with downloads on, for example:

    [{"account_id": 14, "account": "Main Checking", "investment": false, "slug": "main-checking", "schedule": "monthly",
      "latest_file": "main-checking_2026-09.csv", "latest_month": "2026-09", "last_import": {"batch": "import-20261031-212520", "ts": "2026-10-31T21:25:21"}}]

`latest_file`, `latest_month` and `last_import` are `null` when there is none yet. `investment: true` means the files need the investment columns. A script that does the monthly pull takes the entries with `"schedule": "monthly"`; one asked to fetch a particular account can use any entry. Messages like `copied ...` and `backup: ...` go to stderr, so stdout is always just the JSON.

## 8. Built-in downloads with Plaid (optional)
Ledger can fetch transactions itself through [Plaid](https://plaid.com) with **your own** developer keys. You log in to your bank inside Plaid's window (Ledger never sees the password); transactions go only between your computer and Plaid; nothing enters the ledger until you press Import on the preview.

1. Sign up at <https://dashboard.plaid.com/signup> and choose the free **Trial** plan (real bank data, a small number of logins; it is only offered to new Plaid accounts). Copy your `client_id` and the **production** `secret` from Developers > Keys.
2. Put the keys where the app can read them, either as environment variables `PLAID_CLIENT_ID`, `PLAID_SECRET`, `PLAID_ENV` (default `production`; `sandbox` for fake test banks), or in `data/plaid.json` (git-ignored), then restart Ledger:

       echo '{"PLAID_CLIENT_ID": "...", "PLAID_SECRET": "..."}' > data/plaid.json && chmod 600 data/plaid.json

   Optional key `"days_requested": 90` (history Plaid fetches at first link, up to 730). The app refuses a keys file that others can read. Linked banks are kept, with their access tokens, in `data/plaid_items.json` (chmod 600, git-ignored). Treat both files like passwords; keys are never logged.
3. Open an account > Account Details > **Download** tab (or the Import page), press **Link bank or card** (or **Link brokerage**) and log in in the Plaid window. Some institutions (Fidelity in particular) may first need you to request access in the Plaid dashboard (Production access / "Request access"), and Schwab can take up to 5 business days to start working. A bank that fails to link simply can't be used this way; use csv files instead.
4. Pick, per Plaid account, which ledger account it feeds (**Plaid account** dropdown on that account's Download tab). An investment account can only be paired with an investment account.
5. **Download now** (Download tab) fetches one account; **Download all** (Import page) fetches every linked account that has downloads turned on. A download only writes `imports/<slug>/<slug>_plaid_<YYYY-MM>.csv` (same layout as section 6, `source` = `plaid-direct`) and shows the preview counts. Review and import on the Import page as usual.

How it behaves: pending transactions are skipped until they post; changed or removed ones are updated in the csv files before you import them; rows already in the ledger are `duplicate` by id, and rows that came from another source (csv files from a script, or Quicken) are matched by amount and date (same day +-1 for rows that already carry another bank id, +-3 days for rows with none). If a bank asks you to log in again, the Import page shows "Log in again" next to it. Linking a new account of an already-linked bank re-reads history once. If `imports/<slug>/` also holds csv files from another source with the same transactions, import or remove those first, or they can show up twice. Unlinking a bank (Import page) removes it at Plaid; your ledger and csv files are untouched.
