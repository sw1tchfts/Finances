# Finances

Local web app that ingests Rocket Money CSV exports and reconciles a
partnership's shared expenses, business income, and business expenses
within a configurable partnership window.

Built for a specific use case: one partner needs to demonstrate to a court
that the partnership did not produce net profit, so no profit distribution
was owed, and the other partner actually owes money back.

## Design principles

- **Nothing is ever destructively overwritten.** Classifications are versioned
  (supersede rather than update). Rules are disabled rather than deleted.
  Every mutation goes to `audit_log`.
- **Re-imports are idempotent.** Every row gets a deterministic fingerprint
  including an occurrence counter, so overlapping exports never duplicate or
  clobber existing data.
- **Source preservation.** Original CSVs live under `data/raw/` unchanged.
  Every `imports` row stores the source file's sha256.
- **Local only.** SQLite in `data/finances.db`. No network dependencies.

## Install & run

```bash
python3 -m venv .venv
.venv/bin/pip install -e .

# First-time import + seed starter rules
.venv/bin/python scripts/import_csv.py data/raw/<your-export>.csv
.venv/bin/python scripts/seed_rules.py

# Launch the dashboard
.venv/bin/python -m app.main
# → http://127.0.0.1:5050
```

The DB file at `data/finances.db` and anything in `data/receipts/` are
gitignored. Back up the DB file if you want to preserve state across
machines.

## Data model

| Table             | What it holds                                                 |
|-------------------|---------------------------------------------------------------|
| `imports`         | One row per CSV file ingested; sha256 of file bytes           |
| `transactions`    | Canonical rows from the CSV; immutable after insert           |
| `rules`           | Match/classify rules (regex, date range, amount, account)     |
| `classifications` | Versioned per-transaction classifications (supersede, not update) |
| `receipts`        | Uploaded files keyed to transactions; sha256 of each file     |
| `settings`        | Partnership window, partner names, default split              |
| `audit_log`       | Every mutation for evidentiary replay                         |

### Classifications

| Key                | Counted in reconciliation?                           |
|--------------------|------------------------------------------------------|
| `business_income`  | Yes — adds to business revenue                       |
| `business_expense` | Yes — reduces net profit                             |
| `shared_expense`   | Yes — reduces net profit                             |
| `personal`         | No — one partner's private expense                   |
| `transfer`         | No — credit-card payment or internal transfer        |
| `excluded`         | No — explicitly ignored                              |
| `unclassified`     | No — default; surface these and review them          |

## Rules

Rules auto-classify transactions. Lower priority wins. A rule can match on
any combination of: account number(s), name/description regex, category
regex, amount range, date range, sign.

Manual classifications (`source = 'manual'`) are never overwritten by rules.
To force re-classification by rules, clear the manual override on the
transaction first.

Starter rules (installed by `scripts/seed_rules.py`):

- Credit-card payments, internal transfers, loan payments → `transfer`
- FENIX deposits on account …2543 (negative sign) → `business_income`
- `FREEDOM MTG`, `JK ROCK HOMES`, Mortgage/Rent/HOA categories → `shared_expense` 50/50
- Energy/Internet bills → `shared_expense` 50/50
- Groceries, Dining & Drinks → `shared_expense` 50/50

Edit or add rules via the Rules page.

## Typical workflow

1. Import the latest export to `data/raw/` and use `/import` (or the CLI).
2. Visit `/` — check the summary totals.
3. Click into "Review unclassified transactions" — for each row, either:
   - Create a new rule that classifies that pattern, then re-apply rules
   - Or set a manual classification via the transaction detail page
4. Upload receipts on the transaction detail page for anything that needs
   an evidentiary trail.
5. Repeat until the unclassified bucket is empty (or intentionally left
   as `excluded` / `personal`).
