"""Idempotent starter rules.

Each rule is keyed by `name`; re-running only inserts rules that don't exist
yet. Safe to run after every schema change or fresh DB.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as db_module  # noqa: E402
from app import rules as rules_module  # noqa: E402


STARTER_RULES = [
    # --- Transfers (must be highest priority so they're excluded from math) ---
    {
        "name": "Transfer: credit card payments",
        "priority": 10,
        "match_category_regex": r"^Credit Card Payment$",
        "classification": "transfer",
        "note": "Moving money between accounts you own is not spending.",
    },
    {
        "name": "Transfer: internal transfers",
        "priority": 10,
        "match_category_regex": r"^Internal Transfers$",
        "classification": "transfer",
    },
    {
        "name": "Transfer: loan principal/interest payments",
        "priority": 11,
        "match_category_regex": r"^Loan Payment$",
        "classification": "transfer",
        "note": "Loan payments are principal/interest transfers. Reclassify if you need the interest as an expense.",
    },

    # --- Business income (FENIX deposits into the shared account) ---
    {
        "name": "Business income: FENIX deposits (…2543)",
        "priority": 20,
        "match_account_numbers": ["2543"],
        "match_name_regex": r"FENIX",
        "match_sign": "negative",
        "classification": "business_income",
        "note": "Income from business operations. Partnership window required to count.",
    },

    # --- Shared housing ---
    {
        "name": "Shared: mortgage (FREEDOM MTG)",
        "priority": 30,
        "match_name_regex": r"FREEDOM MTG",
        "classification": "shared_expense",
        "split_user_pct": 50,
        "split_partner_pct": 50,
    },
    {
        "name": "Shared: rent (JK ROCK HOMES)",
        "priority": 30,
        "match_name_regex": r"JK ROCK HOMES",
        "classification": "shared_expense",
        "split_user_pct": 50,
        "split_partner_pct": 50,
    },
    {
        "name": "Shared: mortgage/rent/HOA category",
        "priority": 31,
        "match_category_regex": r"Mortgage, Rent, HOA|Rent|Mortgage",
        "classification": "shared_expense",
        "split_user_pct": 50,
        "split_partner_pct": 50,
    },
    {
        "name": "Shared: utilities (energy/internet)",
        "priority": 32,
        "match_category_regex": r"^1\. Bills - (Energy|Internet)$",
        "classification": "shared_expense",
        "split_user_pct": 50,
        "split_partner_pct": 50,
    },

    # --- Shared food ---
    {
        "name": "Shared: groceries",
        "priority": 40,
        "match_category_regex": r"^Groceries$",
        "classification": "shared_expense",
        "split_user_pct": 50,
        "split_partner_pct": 50,
    },
    {
        "name": "Shared: dining & drinks",
        "priority": 41,
        "match_category_regex": r"^Dining & Drinks$",
        "classification": "shared_expense",
        "split_user_pct": 50,
        "split_partner_pct": 50,
    },
]


def main() -> None:
    conn = db_module.connect()
    db_module.init_schema(conn)
    existing = {r["name"] for r in conn.execute("SELECT name FROM rules").fetchall()}
    created = 0
    for spec in STARTER_RULES:
        if spec["name"] in existing:
            continue
        rules_module.create_rule(conn, spec)
        created += 1
    print(f"Seeded {created} new rule(s); {len(existing)} were already present.")

    if created > 0:
        result = rules_module.apply_rules(conn)
        print(
            f"Applied rules: {result['changed']} changed, "
            f"{result['unchanged']} unchanged, "
            f"{result['manual_skipped']} manual preserved."
        )


if __name__ == "__main__":
    main()
