"""Rule engine: match transactions against user-defined rules and record
classifications.

Rule precedence:
  - Rules are ordered by (priority ASC, id ASC). Lower priority number wins.
  - First matching rule classifies the transaction.
  - Manual classifications are never overwritten by rules (see apply_rules).
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable

VALID_CLASSIFICATIONS = {
    "business_income",
    "business_expense",
    "shared_expense",
    "personal",
    "transfer",
    "excluded",
    "unclassified",
}


@dataclass
class Rule:
    id: int
    name: str
    priority: int
    enabled: bool
    match_account_numbers: list[str] | None
    match_name_regex: str | None
    match_category_regex: str | None
    match_amount_min: float | None
    match_amount_max: float | None
    match_date_from: str | None
    match_date_to: str | None
    match_sign: str | None
    classification: str
    split_user_pct: float | None
    split_partner_pct: float | None
    note: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Rule":
        acct = json.loads(row["match_account_numbers"]) if row["match_account_numbers"] else None
        return cls(
            id=row["id"],
            name=row["name"],
            priority=row["priority"],
            enabled=bool(row["enabled"]),
            match_account_numbers=acct,
            match_name_regex=row["match_name_regex"],
            match_category_regex=row["match_category_regex"],
            match_amount_min=row["match_amount_min"],
            match_amount_max=row["match_amount_max"],
            match_date_from=row["match_date_from"],
            match_date_to=row["match_date_to"],
            match_sign=row["match_sign"],
            classification=row["classification"],
            split_user_pct=row["split_user_pct"],
            split_partner_pct=row["split_partner_pct"],
            note=row["note"],
        )

    def matches(self, tx: sqlite3.Row) -> bool:
        if self.match_account_numbers and tx["account_number"] not in self.match_account_numbers:
            return False
        haystack = f"{tx['name'] or ''}\n{tx['description'] or ''}"
        if self.match_name_regex and not re.search(self.match_name_regex, haystack, re.IGNORECASE):
            return False
        if self.match_category_regex and not re.search(
            self.match_category_regex, tx["category"] or "", re.IGNORECASE
        ):
            return False
        amt = tx["amount"]
        if self.match_amount_min is not None and amt < self.match_amount_min:
            return False
        if self.match_amount_max is not None and amt > self.match_amount_max:
            return False
        if self.match_date_from and tx["date"] < self.match_date_from:
            return False
        if self.match_date_to and tx["date"] > self.match_date_to:
            return False
        if self.match_sign == "positive" and amt <= 0:
            return False
        if self.match_sign == "negative" and amt >= 0:
            return False
        return True


def list_rules(conn: sqlite3.Connection, enabled_only: bool = False) -> list[Rule]:
    q = "SELECT * FROM rules"
    if enabled_only:
        q += " WHERE enabled = 1"
    q += " ORDER BY priority ASC, id ASC"
    return [Rule.from_row(r) for r in conn.execute(q).fetchall()]


def get_rule(conn: sqlite3.Connection, rule_id: int) -> Rule | None:
    row = conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    return Rule.from_row(row) if row else None


def create_rule(conn: sqlite3.Connection, fields: dict) -> int:
    if fields["classification"] not in VALID_CLASSIFICATIONS:
        raise ValueError(f"Invalid classification: {fields['classification']}")
    accounts = fields.get("match_account_numbers")
    if isinstance(accounts, list):
        accounts = json.dumps(accounts)
    cur = conn.execute(
        """
        INSERT INTO rules (
            name, priority, enabled, match_account_numbers, match_name_regex,
            match_category_regex, match_amount_min, match_amount_max,
            match_date_from, match_date_to, match_sign,
            classification, split_user_pct, split_partner_pct, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            fields["name"],
            int(fields.get("priority", 100)),
            1 if fields.get("enabled", True) else 0,
            accounts,
            fields.get("match_name_regex") or None,
            fields.get("match_category_regex") or None,
            fields.get("match_amount_min"),
            fields.get("match_amount_max"),
            fields.get("match_date_from") or None,
            fields.get("match_date_to") or None,
            fields.get("match_sign") or None,
            fields["classification"],
            fields.get("split_user_pct"),
            fields.get("split_partner_pct"),
            fields.get("note") or None,
        ),
    )
    rule_id = cur.lastrowid
    conn.execute(
        "INSERT INTO audit_log(event_type, entity_type, entity_id, details) VALUES (?, ?, ?, ?)",
        ("rule_create", "rule", rule_id, json.dumps({"name": fields["name"]})),
    )
    return rule_id


def update_rule(conn: sqlite3.Connection, rule_id: int, fields: dict) -> None:
    if "classification" in fields and fields["classification"] not in VALID_CLASSIFICATIONS:
        raise ValueError(f"Invalid classification: {fields['classification']}")
    columns = [
        "name", "priority", "enabled", "match_account_numbers", "match_name_regex",
        "match_category_regex", "match_amount_min", "match_amount_max",
        "match_date_from", "match_date_to", "match_sign",
        "classification", "split_user_pct", "split_partner_pct", "note",
    ]
    sets = []
    vals: list = []
    for col in columns:
        if col in fields:
            v = fields[col]
            if col == "match_account_numbers" and isinstance(v, list):
                v = json.dumps(v)
            if col == "enabled":
                v = 1 if v else 0
            sets.append(f"{col} = ?")
            vals.append(v)
    if not sets:
        return
    sets.append("updated_at = datetime('now')")
    vals.append(rule_id)
    conn.execute(f"UPDATE rules SET {', '.join(sets)} WHERE id = ?", vals)
    conn.execute(
        "INSERT INTO audit_log(event_type, entity_type, entity_id, details) VALUES (?, ?, ?, ?)",
        ("rule_update", "rule", rule_id, json.dumps(fields, default=str)),
    )


def delete_rule(conn: sqlite3.Connection, rule_id: int) -> None:
    """Soft-delete by disabling. We never truly remove rules so history is
    preserved."""
    conn.execute("UPDATE rules SET enabled = 0, updated_at = datetime('now') WHERE id = ?", (rule_id,))
    conn.execute(
        "INSERT INTO audit_log(event_type, entity_type, entity_id, details) VALUES (?, ?, ?, ?)",
        ("rule_update", "rule", rule_id, '{"disabled": true}'),
    )


def current_classification(conn: sqlite3.Connection, tx_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM classifications WHERE transaction_id = ? AND superseded_at IS NULL",
        (tx_id,),
    ).fetchone()


def set_classification(
    conn: sqlite3.Connection,
    tx_id: int,
    classification: str,
    source: str,
    rule_id: int | None = None,
    split_user_pct: float | None = None,
    split_partner_pct: float | None = None,
    note: str | None = None,
) -> int:
    if classification not in VALID_CLASSIFICATIONS:
        raise ValueError(f"Invalid classification: {classification}")
    current = current_classification(conn, tx_id)
    now = conn.execute("SELECT datetime('now') AS now").fetchone()["now"]
    cur = conn.execute(
        """
        INSERT INTO classifications (
            transaction_id, classification, split_user_pct, split_partner_pct,
            source, rule_id, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (tx_id, classification, split_user_pct, split_partner_pct, source, rule_id, note),
    )
    new_id = cur.lastrowid
    if current is not None:
        conn.execute(
            "UPDATE classifications SET superseded_at = ?, superseded_by = ? WHERE id = ?",
            (now, new_id, current["id"]),
        )
    conn.execute(
        "INSERT INTO audit_log(event_type, entity_type, entity_id, details) VALUES (?, ?, ?, ?)",
        (
            "classify",
            "transaction",
            tx_id,
            json.dumps({"classification": classification, "source": source, "rule_id": rule_id}),
        ),
    )
    return new_id


def apply_rules(
    conn: sqlite3.Connection,
    tx_ids: Iterable[int] | None = None,
    overwrite_manual: bool = False,
) -> dict:
    """Apply currently-enabled rules to the given transactions (or all, if None).

    Skips transactions whose current classification has source='manual' unless
    overwrite_manual=True. Only writes a new classification when the rule result
    would differ from what's currently recorded, so re-running is cheap.
    """
    rules = list_rules(conn, enabled_only=True)

    if tx_ids is None:
        txs = conn.execute("SELECT * FROM transactions").fetchall()
    else:
        ids = list(tx_ids)
        if not ids:
            return {"matched": 0, "changed": 0, "unchanged": 0, "manual_skipped": 0}
        placeholders = ",".join("?" * len(ids))
        txs = conn.execute(
            f"SELECT * FROM transactions WHERE id IN ({placeholders})", ids
        ).fetchall()

    matched = 0
    changed = 0
    unchanged = 0
    manual_skipped = 0

    for tx in txs:
        current = current_classification(conn, tx["id"])
        if current and current["source"] == "manual" and not overwrite_manual:
            manual_skipped += 1
            continue

        hit = None
        for rule in rules:
            if rule.matches(tx):
                hit = rule
                break

        if hit is None:
            if current is None:
                set_classification(conn, tx["id"], "unclassified", "default")
            continue

        matched += 1
        same = (
            current is not None
            and current["classification"] == hit.classification
            and current["rule_id"] == hit.id
            and current["split_user_pct"] == hit.split_user_pct
            and current["split_partner_pct"] == hit.split_partner_pct
        )
        if same:
            unchanged += 1
            continue

        set_classification(
            conn,
            tx["id"],
            hit.classification,
            source=f"rule:{hit.id}",
            rule_id=hit.id,
            split_user_pct=hit.split_user_pct,
            split_partner_pct=hit.split_partner_pct,
        )
        changed += 1

    return {
        "matched": matched,
        "changed": changed,
        "unchanged": unchanged,
        "manual_skipped": manual_skipped,
    }
